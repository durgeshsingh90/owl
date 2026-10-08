"""Durable daily refresh of saved and downloaded Confluence pages."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import httpx
from app.bookmarks import confluence
from app.core.database import connection
from app.core.logging import error_details, event
from fastapi import HTTPException

DAY = 24 * 60 * 60
RETRY = 2 * 60 * 60
LEASE = 10 * 60


def status():
    with connection() as db:
        row = dict(
            db.execute("SELECT * FROM bookmark_refresh_schedule WHERE id=1").fetchone()
        )
        row["workspace_revision"] = db.execute(
            "SELECT revision FROM bookmark_workspace WHERE id=1"
        ).fetchone()[0]
    owner, lease = row.pop("owner"), row.pop("lease_until")
    row["manual"] = bool(owner and owner.startswith(MANUAL))
    if row["status"] == "running" and lease <= time.time():
        # The update stopped without finishing (OWL or the browser closed).
        row.update(status="retrying", message="The last update stopped before finishing.")
    row["interval_hours"] = DAY / 3600
    row["retry_hours"] = RETRY / 3600
    return row


def claim():
    now, owner = time.time(), uuid.uuid4().hex
    with connection() as db:
        changed = db.execute(
            "UPDATE bookmark_refresh_schedule SET owner=?,lease_until=?,status='running',"
            "last_attempt=?,completed=0,total=0,failed=0,message='' "
            "WHERE id=1 AND next_run<=? AND lease_until<=?",
            (owner, now + LEASE, now, now, now),
        ).rowcount
    return owner if changed else None


def finish(owner, *, success, message=""):
    now = time.time()
    with connection() as db:
        db.execute(
            "UPDATE bookmark_refresh_schedule SET next_run=?,status=?,message=?,"
            "last_success=CASE WHEN ? THEN ? ELSE last_success END,owner=NULL,lease_until=0 "
            "WHERE id=1 AND owner=?",
            (
                now + (DAY if success else RETRY),
                "scheduled" if success else "retrying",
                message,
                success,
                now,
                owner,
            ),
        )


# Update all runs in the browser; it reports here so the schedule shows it running and
# counts it as the day's refresh once it has gone through every bookmark.
MANUAL = "manual-"


def manual_start(total):
    """Hold the schedule for a manual Update all; None while an update is running."""
    now, owner = time.time(), MANUAL + uuid.uuid4().hex
    with connection() as db:
        changed = db.execute(
            "UPDATE bookmark_refresh_schedule SET owner=?,lease_until=?,status='running',"
            "last_attempt=?,completed=0,total=?,failed=0,message='' WHERE id=1 AND lease_until<=?",
            (owner, now + LEASE, now, total, now),
        ).rowcount
    return owner if changed else None


def manual_progress(owner, completed, failed):
    with connection() as db:
        return bool(
            db.execute(
                "UPDATE bookmark_refresh_schedule SET completed=?,failed=?,lease_until=? WHERE id=1 AND owner=?",
                (completed, failed, time.time() + LEASE, owner),
            ).rowcount
        )


def manual_finish(owner, completed, failed):
    """Done for the day once every bookmark was tried; pages that failed show their error."""
    with connection() as db:
        total = db.execute(
            "SELECT total FROM bookmark_refresh_schedule WHERE id=1 AND owner=?", (owner,)
        ).fetchone()
    if total is None:
        return False
    manual_progress(owner, completed, failed)
    done = completed >= total[0]
    finish(
        owner,
        success=done,
        message=(
            f"{failed} of {total[0]} pages could not be refreshed; each shows its error."
            if failed
            else ""
        )
        if done
        else "Update all stopped before finishing. Retrying in two hours.",
    )
    return done


def targets(settings):
    with connection() as db:
        payload = json.loads(
            db.execute("SELECT payload FROM bookmark_workspace WHERE id=1").fetchone()[
                0
            ]
        )
        downloaded = [
            dict(row)
            for row in db.execute(
                "SELECT folder_key,page_id,url FROM bookmark_downloaded_pages"
            )
        ]
    # Include imported pages on the configured server even if sourceType is absent.
    return [
        ("bookmark", item)
        for item in payload["bookmarks"]
        if confluence.belongs_to_server(settings, item["url"])
    ] + [
        ("download", item)
        for item in downloaded
        if confluence.belongs_to_server(settings, item["url"])
    ]


def save_page(owner, kind, original, data):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM bookmark_refresh_schedule WHERE id=1 AND owner=?", (owner,)
        ).fetchone():
            return
        if kind == "bookmark":
            payload = json.loads(
                db.execute(
                    "SELECT payload FROM bookmark_workspace WHERE id=1"
                ).fetchone()[0]
            )
            item = next(
                (
                    item
                    for item in payload["bookmarks"]
                    if item["id"] == original["id"] and item["url"] == original["url"]
                ),
                None,
            )
            if item is not None:
                # Read the latest workspace inside the transaction: preserve notes,
                # stars, groups, opens and concurrent deletions or URL changes.
                item.update(data, fetchError="")
                db.execute(
                    "UPDATE bookmark_workspace SET payload=?,revision=revision+1 WHERE id=1",
                    (json.dumps(payload),),
                )
        else:
            db.execute(
                "UPDATE bookmark_downloaded_pages SET title=?,content=?,folder_path=? "
                "WHERE folder_key=? AND page_id=? AND url=?",
                (
                    data["title"],
                    data["contentText"],
                    json.dumps(
                        [data.get("space") or "Pages", *data.get("breadcrumb", [])]
                    ),
                    original["folder_key"],
                    original["page_id"],
                    original["url"],
                ),
            )


def connection_lost(error):
    """Errors that stop the whole update: the network, the token or the server is down.

    Confluence's own status is used: OWL reports every Confluence error as HTTP 502.
    """
    if isinstance(error, (httpx.RequestError, TimeoutError)):
        return True
    if isinstance(error, confluence.ConfluenceRequestError):
        return error.upstream_status >= 500 or error.upstream_status in (401, 429)
    return isinstance(error, HTTPException) and error.status_code >= 500


def describe(error):
    if isinstance(error, HTTPException):
        return str(error.detail).rstrip(".") + "."
    if isinstance(error, TimeoutError):
        return "Confluence did not answer within two minutes."
    # Other errors can carry upstream text; name only their type.
    return f"{type(error).__name__}."


def save_failure(owner, original, message):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM bookmark_refresh_schedule WHERE id=1 AND owner=?", (owner,)
        ).fetchone():
            return
        payload = json.loads(
            db.execute("SELECT payload FROM bookmark_workspace WHERE id=1").fetchone()[0]
        )
        for item in payload["bookmarks"]:
            if item["id"] == original["id"] and item["url"] == original["url"]:
                item["fetchError"] = message[:500]
                db.execute(
                    "UPDATE bookmark_workspace SET payload=?,revision=revision+1 WHERE id=1",
                    (json.dumps(payload),),
                )
                break


async def run_due():
    owner = claim()
    if owner is None:
        return
    try:
        settings = confluence.load()
        pages = targets(settings)
        with connection() as db:
            db.execute(
                "UPDATE bookmark_refresh_schedule SET total=? WHERE owner=?",
                (len(pages), owner),
            )
        # Fail fast when VPN, authentication or the server is unavailable.
        await asyncio.wait_for(confluence.test(settings), timeout=120)
        failed = 0
        retry = []

        async def refresh_page(kind, original):
            data = await asyncio.wait_for(confluence.metadata(original["url"]), timeout=120)
            if (
                data.get("sourceType") != "confluence"
                or data.get("confluenceBaseUrl") != settings.base_url
            ):
                raise ValueError("Confluence configuration changed during refresh")
            save_page(owner, kind, original, data)

        def progress(completed):
            with connection() as db:
                return db.execute(
                    "UPDATE bookmark_refresh_schedule SET completed=?,failed=?,lease_until=? WHERE owner=?",
                    (completed, failed, time.time() + LEASE, owner),
                ).rowcount

        for index, (kind, original) in enumerate(pages):
            if not progress(index):
                return
            try:
                await refresh_page(kind, original)
            except Exception as error:
                failed += 1
                event("bookmarks.refresh.page_failed", **error_details(error))
                if connection_lost(error):
                    progress(index + 1)
                    raise
                retry.append((kind, original))
            progress(index + 1)
        # Pages that failed get one more try once every other page is done; only those
        # that fail again keep their error.
        for kind, original in retry:
            if not progress(len(pages)):
                return
            try:
                await refresh_page(kind, original)
                failed -= 1
            except Exception as error:
                event("bookmarks.refresh.page_failed", retry=True, **error_details(error))
                if connection_lost(error):
                    progress(len(pages))
                    raise
                # A deleted or restricted page fails alone; the rest still refresh.
                if kind == "bookmark":
                    save_failure(owner, original, describe(error))
        progress(len(pages))
        if not failed and pages:
            with connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM bookmark_refresh_schedule WHERE owner=?", (owner,)
                ).fetchone():
                    payload = json.loads(
                        db.execute(
                            "SELECT payload FROM bookmark_workspace WHERE id=1"
                        ).fetchone()[0]
                    )
                    payload["last_update_all"] = datetime.now(timezone.utc).isoformat()
                    db.execute(
                        "UPDATE bookmark_workspace SET payload=?,revision=revision+1 WHERE id=1",
                        (json.dumps(payload),),
                    )
        # The update reached every page: that is today's refresh, even if some pages
        # could not be read. Each failed bookmark shows its own error.
        finish(
            owner,
            success=True,
            message=f"{failed} of {len(pages)} pages could not be refreshed; each shows its error."
            if failed
            else "",
        )
    except asyncio.CancelledError:
        finish(
            owner, success=False, message="Update interrupted. Retrying in two hours."
        )
        raise
    except Exception as error:  # noqa: BLE001 - persist background failure and keep retries alive
        event("bookmarks.refresh.failed", **error_details(error))
        finish(
            owner,
            success=False,
            message=f"Confluence connection failed: {describe(error)} Check connection settings and VPN. Retrying in two hours.",
        )


def apply_intervals():
    """Bring a schedule saved under longer intervals forward to the current ones."""
    with connection() as db:
        db.execute(
            "UPDATE bookmark_refresh_schedule SET next_run=MIN(next_run,"
            "CASE WHEN status='retrying' THEN COALESCE(last_attempt,0)+? "
            "ELSE COALESCE(last_success,0)+? END) WHERE id=1 AND owner IS NULL",
            (RETRY, DAY),
        )


async def run_scheduler():
    apply_intervals()
    while True:
        try:
            await run_due()
        except Exception as error:  # noqa: BLE001 - persist background failure and keep retries alive
            event("bookmarks.refresh.scheduler_failed", **error_details(error))
        await asyncio.sleep(60)
