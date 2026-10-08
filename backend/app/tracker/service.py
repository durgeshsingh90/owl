"""Keep an eye on Confluence page trees: which pages are new or updated, from the day
tracking starts.

Old pages are never collected. Each sync asks Confluence only for pages in the tree
modified since the last check (since the start day at first) and records each one's
title, path, link, who created and updated it, and when, plus the page's full text.

Only those changed pages are downloaded. A page nobody has created or updated for six
months is outdated and is deleted automatically, so storage does not keep growing.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.bookmarks import confluence
from app.core.database import connection
from app.core.logging import error_details, event
from app.tracker import listing

DAY = 86400
RETRY = 2 * 3600
LEASE = 600
# A page not created or updated in Confluence for this long is no longer maintained: it is
# deleted automatically with its text and change history.
RETENTION_DAYS = 183
PURGE_EVERY = 3600
def stamp():
    return datetime.now(timezone.utc).isoformat()


def safe_error(error, settings=None):
    message = (
        str(error.detail)
        if isinstance(error, HTTPException)
        else str(error)
        if isinstance(error, ValueError)
        else f"{type(error).__name__}: sync could not finish."
    )
    if settings:
        message = message.replace(settings.token.get_secret_value(), "[redacted]")
    return message[:1500]


async def add_root(value):
    """Track the top-most parent of any pasted page, space or short link, with everything below it."""
    settings = confluence.load()
    page_id = await confluence.top_most_page(
        settings, await confluence.resolve_page_id(settings, value)
    )
    url = settings.base_url + "/pages/viewpage.action?pageId=" + page_id
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO confluence_tracker_roots(base_url,page_id,url,title,created_at) VALUES(?,?,?,?,?)",
            (settings.base_url, page_id, url, "Page " + page_id, stamp()),
        )
        root = dict(
            db.execute(
                "SELECT * FROM confluence_tracker_roots WHERE base_url=? AND page_id=?",
                (settings.base_url, page_id),
            ).fetchone()
        )
    return root["id"]


def claim():
    now = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM confluence_tracker_roots WHERE next_run<=? AND lease_until<=? ORDER BY next_run,id LIMIT 1",
            (now, now),
        ).fetchone()
        if row is None:
            return None
        root = dict(row)
        root["owner"] = uuid.uuid4().hex
        db.execute(
            "UPDATE confluence_tracker_roots SET owner=?,lease_until=?,status='discovering',last_attempt=?,total=0,completed=0,failed=0,error='' WHERE id=?",
            (root["owner"], now + LEASE, stamp(), root["id"]),
        )
    return root


def heartbeat(root, total=None):
    with connection() as db:
        changed = db.execute(
            "UPDATE confluence_tracker_roots SET lease_until=?,total=COALESCE(?,total) WHERE id=? AND owner=?",
            (time.time() + LEASE, total, root["id"], root["owner"]),
        ).rowcount
    if not changed:
        raise ValueError("This sync was replaced by another run.")


def fingerprint(page):
    return f"{page.get('version')}|{page.get('confluenceUpdatedAt')}"


def changed_pages(root, pages, start_day):
    """Pages changed since tracking started that are not recorded at this version yet."""
    with connection() as db:
        stored = {
            row["page_id"]: row["fingerprint"]
            for row in db.execute(
                "SELECT page_id,fingerprint FROM confluence_tracker_pages WHERE root_id=?",
                (root["id"],),
            )
        }
    now = datetime.fromtimestamp(time.time(), timezone.utc)
    return [
        page
        for page in pages
        if str(page.get("confluenceUpdatedAt") or "")[:10] >= start_day
        and stored.get(page["page_id"]) != fingerprint(page)
        # Already outdated pages are not recorded again (after a long gap between syncs).
        and (kept_until(page, None) or now) >= now
    ]


async def with_content(settings, root, pages):
    """Download the full text of each changed page (only those).

    A page whose text cannot be read is tried once more at the end; if it fails again
    it keeps its details without text, with the reason.
    """

    async def download(page):
        heartbeat(root)
        try:
            data = await asyncio.wait_for(
                confluence.metadata(page["url"], settings=settings), 120
            )
        except confluence.ConfluenceRequestError as error:
            # A restricted or just-deleted page keeps its details without text.
            if error.upstream_status not in (403, 404):
                raise
            page.update(contentText="", pageTextSizeBytes=0, contentError=safe_error(error, settings))
            return False
        text = data.get("contentText", "")
        page.update(contentText=text, pageTextSizeBytes=len(text.encode()), contentError="")
        return True

    failed = []
    for index, page in enumerate(pages):
        if not await download(page):
            failed.append(page)
        with connection() as db:
            db.execute(
                "UPDATE confluence_tracker_roots SET completed=? WHERE id=? AND owner=?",
                (index + 1, root["id"], root["owner"]),
            )
    for page in failed:
        await download(page)
    return pages


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def last_activity(metadata, recorded_at):
    """When a page was last maintained: the latest of its Confluence created and updated
    dates and the last change OWL recorded."""
    moments = [
        parse_time(value)
        for value in (metadata.get("writtenAt"), metadata.get("confluenceUpdatedAt"), recorded_at)
        if value
    ]
    return max((moment for moment in moments if moment), default=None)


def kept_until(metadata, recorded_at):
    moment = last_activity(metadata, recorded_at)
    return moment + timedelta(days=RETENTION_DAYS) if moment else None


def purge(now=None):
    """Delete pages nobody maintains: no creation or update in Confluence for six months.

    A page that keeps being updated is kept; its page, text and change history go only
    after six months without activity. If it is edited later, it is recorded again.
    """
    cutoff = datetime.fromtimestamp(now or time.time(), timezone.utc) - timedelta(days=RETENTION_DAYS)
    with connection() as db:
        stale = [
            (row["root_id"], row["page_id"])
            for row in db.execute(
                "SELECT root_id,page_id,metadata,changed_at FROM confluence_tracker_pages"
            )
            if (last_activity(json.loads(row["metadata"]), row["changed_at"]) or cutoff) < cutoff
        ]
        changes = 0
        for root_id, page_id in stale:
            db.execute(
                "DELETE FROM confluence_tracker_pages WHERE root_id=? AND page_id=?",
                (root_id, page_id),
            )
            changes += db.execute(
                "DELETE FROM confluence_tracker_changes WHERE root_id=? AND page_id=?",
                (root_id, page_id),
            ).rowcount
    if stale:
        event("tracker.purged", pages=len(stale), changes=changes)
    return len(stale), changes


def save_pages(root, pages, start_day):
    """Record pages that are new or updated since tracking started.

    A page created on or after the start day is new; an older page is updated. A page
    seen again with the same version is not reported twice.
    """
    now = stamp()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM confluence_tracker_roots WHERE id=? AND owner=?",
            (root["id"], root["owner"]),
        ).fetchone():
            raise ValueError("This sync no longer owns the root.")
        stored = {
            row["page_id"]: row
            for row in db.execute(
                "SELECT page_id,fingerprint,metadata FROM confluence_tracker_pages WHERE root_id=?",
                (root["id"],),
            )
        }
        recorded = 0
        for page in pages:
            # Only changes on or after the day tracking started.
            if str(page.get("confluenceUpdatedAt") or "")[:10] < start_day:
                continue
            old = stored.get(page["page_id"])
            digest = fingerprint(page)
            if old and old["fingerprint"] == digest:
                continue
            created = str(page.get("writtenAt") or "")[:10]
            kind = "new" if not old and created >= start_day else "updated"
            before = json.loads(old["metadata"]).get("version") if old else None
            db.execute(
                "INSERT INTO confluence_tracker_changes(root_id,page_id,kind,detected_at,summary) VALUES(?,?,?,?,?)",
                (
                    root["id"],
                    page["page_id"],
                    kind,
                    now,
                    json.dumps(
                        {
                            "title": page["title"],
                            "path": " / ".join(page["breadcrumb"]),
                            "url": page["url"],
                            "updatedBy": page["lastEditor"] or page["author"],
                            "updatedAt": page["confluenceUpdatedAt"],
                            "version": {"before": before, "after": page["version"]},
                        }
                    ),
                ),
            )
            ancestors = page["ancestors"]
            parent = ancestors[-1]["page_id"] if ancestors and page["page_id"] != root["page_id"] else None
            db.execute(
                "INSERT INTO confluence_tracker_pages(root_id,page_id,parent_id,metadata,fingerprint,first_seen,last_checked,changed_at,change_kind) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(root_id,page_id) DO UPDATE SET parent_id=excluded.parent_id,metadata=excluded.metadata,fingerprint=excluded.fingerprint,last_checked=excluded.last_checked,changed_at=excluded.changed_at,change_kind=excluded.change_kind,present=1",
                (root["id"], page["page_id"], parent, json.dumps(page), digest, now, now, now, kind),
            )
            recorded += 1
        db.execute(
            "UPDATE confluence_tracker_roots SET total=?,completed=? WHERE id=? AND owner=?",
            (len(pages), len(pages), root["id"], root["owner"]),
        )
        return recorded


def finish(root, success, error=""):
    with connection() as db:
        db.execute(
            "UPDATE confluence_tracker_roots SET status=?,error=?,next_run=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,owner=NULL,lease_until=0 WHERE id=? AND owner=?",
            (
                "completed" if success else "failed",
                error,
                time.time() + (DAY if success else RETRY),
                success,
                stamp(),
                root["id"],
                root["owner"],
            ),
        )


async def sync(root):
    settings = None
    try:
        settings = confluence.load()
        if settings.base_url != root["base_url"]:
            raise ValueError(
                "This root uses a different Confluence server. Restore its saved connection to sync it."
            )
        await asyncio.wait_for(confluence.test(settings), 120)
        # The tree's title, for the tracker page; it is not recorded as a change.
        top = await confluence.get(settings, "content/" + root["page_id"])
        with connection() as db:
            db.execute(
                "UPDATE confluence_tracker_roots SET title=? WHERE id=? AND owner=?",
                (top.get("title") or root["title"], root["id"], root["owner"]),
            )
        start_day = root["created_at"][:10]
        # Pages modified since the last check, with a day of overlap; at first, since
        # the day tracking started.
        since = (
            (datetime.fromisoformat(root["last_success"]) - timedelta(days=1)).strftime("%Y-%m-%d")
            if root["last_success"]
            else start_day
        )
        since = max(since, start_day)
        try:
            pages = await listing.updated_since(
                settings, root["page_id"], since, lambda count: heartbeat(root, count)
            )
        except confluence.ConfluenceRequestError as error:
            # Servers without this search: list the tree's metadata and keep changed pages.
            if error.upstream_status not in listing.UNSUPPORTED:
                raise
            pages = [
                page
                for page in await listing.tree(
                    settings, root["page_id"], lambda count: heartbeat(root, count)
                )
                if str(page.get("confluenceUpdatedAt") or "")[:10] >= since
            ]
        pages = changed_pages(root, pages, start_day)
        heartbeat(root, len(pages))
        with connection() as db:
            db.execute(
                "UPDATE confluence_tracker_roots SET status='downloading',total=?,completed=0 WHERE id=? AND owner=?",
                (len(pages), root["id"], root["owner"]),
            )
        save_pages(root, await with_content(settings, root, pages), start_day)
        finish(root, True)
    except asyncio.CancelledError:
        finish(root, False, "Sync interrupted. Retrying in two hours.")
        raise
    except Exception as error:  # noqa: BLE001 - durable retry for background sync
        event("tracker.sync.failed", **error_details(error))
        finish(root, False, safe_error(error, settings))


def schedule_status():
    """Combined automatic refresh state of every tracked root, as each OWL app reports it."""

    def epoch(value):
        return datetime.fromisoformat(value).timestamp() if value else None

    now = time.time()
    with connection() as db:
        roots = [
            dict(row)
            for row in db.execute(
                "SELECT status,next_run,last_success,last_attempt,lease_until,total,completed,failed,error FROM confluence_tracker_roots"
            )
        ]
    state = {
        "status": "idle" if not roots else "scheduled",
        "next_run": min((root["next_run"] for root in roots), default=None),
        # The oldest success is when every tree was last fully current.
        "last_success": None
        if any(not root["last_success"] for root in roots)
        else min((epoch(root["last_success"]) for root in roots), default=None),
        "last_attempt": max(
            (epoch(root["last_attempt"]) for root in roots if root["last_attempt"]),
            default=None,
        ),
        "message": "" if roots else "Add a Confluence page to start automatic refreshes.",
        "completed": 0,
        "total": 0,
        "failed": 0,
        "eta_seconds": None,
        "interval_hours": DAY / 3600,
        "retry_hours": RETRY / 3600,
    }
    running = [root for root in roots if root["lease_until"] > now]
    failed = [root for root in roots if root["status"] == "failed"]
    if running:
        state["status"] = "running"
        for key in ("total", "completed", "failed"):
            state[key] = sum(root[key] or 0 for root in running)
    elif failed:
        state["status"] = "retrying"
        state["message"] = failed[0]["error"] or "Sync failed. Retrying in two hours."
    return state


def apply_intervals():
    """Bring failed roots saved under a longer retry interval forward to the current one."""
    with connection() as db:
        db.execute(
            "UPDATE confluence_tracker_roots SET next_run=MIN(next_run,?) WHERE status='failed' AND owner IS NULL",
            (time.time() + RETRY,),
        )


async def run_scheduler():
    apply_intervals()
    purged_at = 0
    while True:
        try:
            if time.time() - purged_at >= PURGE_EVERY:
                purged_at = time.time()
                purge()
            root = claim()
            if root:
                await sync(root)
                continue
        except Exception as error:  # noqa: BLE001 - keep the scheduler alive
            event("tracker.scheduler.failed", **error_details(error))
        await asyncio.sleep(5)
