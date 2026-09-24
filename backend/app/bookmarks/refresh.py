"""Durable weekly refresh of saved and downloaded Confluence pages."""

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

WEEK = 7 * 24 * 60 * 60
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
    row.pop("owner")
    row.pop("lease_until")
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
                now + (WEEK if success else RETRY),
                "scheduled" if success else "retrying",
                message,
                success,
                now,
                owner,
            ),
        )


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
        for index, (kind, original) in enumerate(pages):
            with connection() as db:
                renewed = db.execute(
                    "UPDATE bookmark_refresh_schedule SET lease_until=? WHERE owner=?",
                    (time.time() + LEASE, owner),
                ).rowcount
            if not renewed:
                return
            try:
                data = await asyncio.wait_for(
                    confluence.metadata(original["url"]), timeout=120
                )
                if (
                    data.get("sourceType") != "confluence"
                    or data.get("confluenceBaseUrl") != settings.base_url
                ):
                    raise ValueError("Confluence configuration changed during refresh")
                save_page(owner, kind, original, data)
            except Exception as error:
                failed += 1
                event("bookmarks.refresh.page_failed", **error_details(error))
                if isinstance(error, (httpx.RequestError, TimeoutError)) or (
                    isinstance(error, HTTPException)
                    and (
                        error.status_code >= 500 or error.status_code in (401, 403, 429)
                    )
                ):
                    with connection() as db:
                        db.execute(
                            "UPDATE bookmark_refresh_schedule SET completed=?,failed=? WHERE owner=?",
                            (index + 1, failed, owner),
                        )
                    raise
            with connection() as db:
                db.execute(
                    "UPDATE bookmark_refresh_schedule SET completed=?,failed=? WHERE owner=?",
                    (index + 1, failed, owner),
                )
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
        finish(
            owner,
            success=not failed,
            message=f"{failed} pages failed. Retrying in two hours." if failed else "",
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
            message="Confluence connection or update failed. Check connection settings and VPN. Retrying in two hours.",
        )


async def run_scheduler():
    while True:
        try:
            await run_due()
        except Exception as error:  # noqa: BLE001 - persist background failure and keep retries alive
            event("bookmarks.refresh.scheduler_failed", **error_details(error))
        await asyncio.sleep(60)
