"""Track Confluence page trees by metadata only: who created and updated each page, and when.

The first sync of a tree records every page's title, path, link, authors and dates.
Later syncs ask Confluence only for pages modified since the last success and record
which were updated or added, again without downloading any page content.
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


def save_pages(root, pages, complete):
    """Store page metadata; once a tree has a baseline, record new and updated pages.

    complete says the list holds the whole tree, so pages missing from it are gone.
    """
    now = stamp()
    baseline = not root["last_success"]
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
                "SELECT page_id,fingerprint,present,change_kind,changed_at,metadata FROM confluence_tracker_pages WHERE root_id=?",
                (root["id"],),
            )
        }
        for page in pages:
            old = stored.get(page["page_id"])
            digest = fingerprint(page)
            if baseline:
                kind, changed_at = "baseline", now
            elif not old:
                kind, changed_at = "new", now
            elif not old["present"]:
                kind, changed_at = "returned", now
            elif old["fingerprint"] != digest:
                kind, changed_at = "updated", now
            else:
                kind, changed_at = old["change_kind"], old["changed_at"]
            if not baseline and changed_at == now:
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
                (root["id"], page["page_id"], parent, json.dumps(page), digest, now, now, changed_at, kind),
            )
            if page["page_id"] == root["page_id"]:
                db.execute(
                    "UPDATE confluence_tracker_roots SET title=? WHERE id=?",
                    (page["title"], root["id"]),
                )
        if complete and not baseline:
            listed = {page["page_id"] for page in pages}
            for page_id, row in stored.items():
                if row["present"] and page_id not in listed:
                    db.execute(
                        "UPDATE confluence_tracker_pages SET present=0,change_kind='missing',changed_at=? WHERE root_id=? AND page_id=?",
                        (now, root["id"], page_id),
                    )
        db.execute(
            "UPDATE confluence_tracker_roots SET total=?,completed=? WHERE id=? AND owner=?",
            (len(pages), len(pages), root["id"], root["owner"]),
        )


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
        pages, complete = None, True
        if root["last_success"]:
            # Ask only for pages modified since the last success, with a day of margin;
            # unchanged versions among them are ignored when saving.
            day = (
                datetime.fromisoformat(root["last_success"]) - timedelta(days=1)
            ).strftime("%Y-%m-%d")
            try:
                pages = await listing.updated_since(
                    settings, root["page_id"], day, lambda count: heartbeat(root, count)
                )
                complete = False
            except confluence.ConfluenceRequestError as error:
                # Servers without this search fall back to listing the whole tree.
                if error.upstream_status not in listing.UNSUPPORTED:
                    raise
        if pages is None:
            pages = await listing.tree(
                settings, root["page_id"], lambda count: heartbeat(root, count)
            )
        heartbeat(root, len(pages))
        save_pages(root, pages, complete)
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
    while True:
        try:
            root = claim()
            if root:
                await sync(root)
                continue
        except Exception as error:  # noqa: BLE001 - keep the scheduler alive
            event("tracker.scheduler.failed", **error_details(error))
        await asyncio.sleep(5)
