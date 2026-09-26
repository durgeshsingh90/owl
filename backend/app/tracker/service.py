"""Discover first, fetch sequentially, and record durable Confluence changes."""

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.bookmarks import confluence
from app.bookmarks.discovery import discover_pages
from app.core.database import connection
from app.core.logging import error_details, event

DAY = 86400
RETRY = 7200
LEASE = 600
FIELDS = (
    "title",
    "version",
    "author",
    "lastEditor",
    "writtenAt",
    "confluenceUpdatedAt",
    "space",
    "spaceKey",
    "ancestors",
    "contentText",
)


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
    settings = confluence.load()
    target = value.strip()
    if re.fullmatch(r"[0-9]{1,20}", target) and int(target):
        page_id = str(int(target))
    else:
        if not confluence.belongs_to_server(settings, target):
            raise ValueError("Use a root page on the configured Confluence server.")
        ids = confluence.named_ids(target)
        if not ids:
            ids = await confluence.identity_from_url(settings, target)
        if len(set(ids)) != 1:
            raise ValueError(
                "Use a Confluence page URL containing its page ID, or paste the page ID."
            )
        page_id = str(ids[0])
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


def comparison(data):
    result = {key: data.get(key) for key in FIELDS}
    raw = data.get("rawMetadata", {})
    result["labels"] = raw.get("metadata", {}).get("labels", {})
    # Storage content detects links, images, or formatting changes as well as text.
    result["body"] = raw.get("body", {}).get("storage", {}).get("value")
    return result


def save_page(root, data):
    now = stamp()
    digest = hashlib.sha256(
        json.dumps(comparison(data), sort_keys=True).encode()
    ).hexdigest()
    page_id = str(data["page_id"])
    ancestors = data.get("ancestors", [])
    parent = str(ancestors[-1]["page_id"]) if ancestors else None
    if page_id == root["page_id"]:
        parent = None
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM confluence_tracker_roots WHERE id=? AND owner=?",
            (root["id"], root["owner"]),
        ).fetchone():
            raise ValueError("This sync no longer owns the root.")
        old = db.execute(
            "SELECT * FROM confluence_tracker_pages WHERE root_id=? AND page_id=?",
            (root["id"], page_id),
        ).fetchone()
        kind = "baseline" if not root["last_success"] else "new"
        previous_content = None
        changed_at = now
        if old:
            previous = json.loads(old["metadata"])
            kind = (
                "returned"
                if not old["present"]
                else "updated"
                if old["fingerprint"] != digest
                else old["change_kind"]
            )
            changed_at = (
                now
                if not old["present"] or old["fingerprint"] != digest
                else old["changed_at"]
            )
            previous_content = (
                previous.get("contentText", "")
                if old["fingerprint"] != digest
                else old["previous_content"]
            )
        if not root["last_success"]:
            kind = "baseline"
        if root["last_success"] and (
            not old or old["fingerprint"] != digest or not old["present"]
        ):
            before = comparison(json.loads(old["metadata"])) if old else {}
            summary = {
                key: {"before": before.get(key), "after": data.get(key)}
                for key in FIELDS
                if key != "contentText" and before.get(key) != data.get(key)
            }
            summary["content_changed"] = bool(
                old and before.get("contentText") != data.get("contentText")
            )
            db.execute(
                "INSERT INTO confluence_tracker_changes(root_id,page_id,kind,detected_at,summary) VALUES(?,?,?,?,?)",
                (root["id"], page_id, kind, now, json.dumps(summary)),
            )
        db.execute(
            "INSERT INTO confluence_tracker_pages(root_id,page_id,parent_id,metadata,fingerprint,previous_content,first_seen,last_checked,changed_at,change_kind) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(root_id,page_id) DO UPDATE SET parent_id=excluded.parent_id,metadata=excluded.metadata,fingerprint=excluded.fingerprint,previous_content=excluded.previous_content,last_checked=excluded.last_checked,changed_at=excluded.changed_at,change_kind=excluded.change_kind,present=1",
            (
                root["id"],
                page_id,
                parent,
                json.dumps(data),
                digest,
                previous_content,
                now,
                now,
                changed_at,
                kind,
            ),
        )
        if page_id == root["page_id"]:
            db.execute(
                "UPDATE confluence_tracker_roots SET title=? WHERE id=?",
                (data["title"], root["id"]),
            )
        db.execute(
            "UPDATE confluence_tracker_scan_pages SET status='completed',error='' WHERE root_id=? AND page_id=?",
            (root["id"], page_id),
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
        ids = await discover_pages(
            settings,
            [root["page_id"]],
            progress=lambda count, path: heartbeat(root, count),
        )
        heartbeat(root, len(ids))
        with connection() as db:
            db.execute(
                "DELETE FROM confluence_tracker_scan_pages WHERE root_id=?",
                (root["id"],),
            )
            db.executemany(
                "INSERT INTO confluence_tracker_scan_pages(root_id,page_id) VALUES(?,?)",
                [(root["id"], page_id) for page_id in ids],
            )
            db.execute(
                "UPDATE confluence_tracker_roots SET status='downloading',total=? WHERE id=? AND owner=?",
                (len(ids), root["id"], root["owner"]),
            )
        failed = 0
        for index, page_id in enumerate(ids):
            heartbeat(root)
            try:
                url = settings.base_url + "/pages/viewpage.action?pageId=" + page_id
                data = await asyncio.wait_for(
                    confluence.metadata(url, settings=settings, include_raw=True), 120
                )
                save_page(root, data)
            except Exception as error:
                failed += 1
                with connection() as db:
                    db.execute(
                        "UPDATE confluence_tracker_scan_pages SET status='failed',error=? WHERE root_id=? AND page_id=?",
                        (safe_error(error, settings), root["id"], page_id),
                    )
                if not isinstance(
                    error, confluence.ConfluenceRequestError
                ) or error.upstream_status not in (403, 404):
                    raise
            finally:
                with connection() as db:
                    db.execute(
                        "UPDATE confluence_tracker_roots SET completed=?,failed=? WHERE id=? AND owner=?",
                        (index + 1, failed, root["id"], root["owner"]),
                    )
        if not failed:
            with connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute(
                    "SELECT 1 FROM confluence_tracker_roots WHERE id=? AND owner=?",
                    (root["id"], root["owner"]),
                ).fetchone():
                    return
                missing = db.execute(
                    "SELECT page_id FROM confluence_tracker_pages WHERE root_id=? AND present=1 AND page_id NOT IN (SELECT page_id FROM confluence_tracker_scan_pages WHERE root_id=?)",
                    (root["id"], root["id"]),
                ).fetchall()
                for row in missing:
                    now = stamp()
                    db.execute(
                        "UPDATE confluence_tracker_pages SET present=0,change_kind='missing',changed_at=? WHERE root_id=? AND page_id=?",
                        (now, root["id"], row["page_id"]),
                    )
                    db.execute(
                        "INSERT INTO confluence_tracker_changes(root_id,page_id,kind,detected_at,summary) VALUES(?,?,'missing',?,?)",
                        (
                            root["id"],
                            row["page_id"],
                            now,
                            json.dumps(
                                {
                                    "notice": "Not in the latest subtree scan. It may have moved or become inaccessible."
                                }
                            ),
                        ),
                    )
        finish(
            root,
            not failed,
            f"{failed} pages could not be downloaded. Retrying in two hours."
            if failed
            else "",
        )
    except asyncio.CancelledError:
        finish(root, False, "Sync interrupted. Retrying in two hours.")
        raise
    except Exception as error:  # noqa: BLE001 - durable retry for background sync
        event("tracker.sync.failed", **error_details(error))
        finish(root, False, safe_error(error, settings))


async def run_scheduler():
    while True:
        try:
            root = claim()
            if root:
                await sync(root)
                continue
        except Exception as error:  # noqa: BLE001 - keep the scheduler alive
            event("tracker.scheduler.failed", **error_details(error))
        await asyncio.sleep(5)
