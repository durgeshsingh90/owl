"""Search-only copies of Confluence folder pages, separate from bookmarks."""

import json
import logging
from datetime import datetime, timezone

from app.bookmarks import confluence
from app.bookmarks.discovery import discover_pages
from app.core.database import connection
from fastapi import HTTPException


def stamp():
    return datetime.now(timezone.utc).isoformat()


async def download(settings, key, space_key, root_ids, root_title=""):
    seen = set()
    request_url = settings.base_url + "/rest/api/content"
    try:
        if root_title:
            if not root_ids:
                listing = await confluence.get(
                    settings,
                    "content",
                    {
                        "spaceKey": space_key,
                        "title": root_title,
                        "type": "page",
                        "status": "current",
                        "limit": 2,
                    },
                )
                results = listing.get("results", [])
                if len(results) != 1:
                    raise ValueError("Folder page could not be uniquely resolved.")
                root_ids = [str(results[0]["id"])]
            space_key = ""

        async def save(page_id):
            nonlocal request_url
            page_id = str(page_id)
            if page_id in seen:
                return
            request_url = settings.base_url + "/rest/api/content/" + page_id
            data = await confluence.resolved_content(settings, "", page_id)
            parser = confluence.TextContent()
            body = data.get("body", {})
            parser.feed(
                body.get("view", {}).get("value")
                or body.get("storage", {}).get("value", "")
            )
            url = settings.base_url + "/pages/viewpage.action?pageId=" + page_id
            with connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO bookmark_downloaded_pages (folder_key,page_id,title,url,content,folder_path) VALUES(?,?,?,?,?,?)",
                    (
                        key,
                        page_id,
                        data.get("title", ""),
                        url,
                        parser.text(),
                        json.dumps(
                            [
                                data.get("space", {}).get("name", "Pages"),
                                *[
                                    a.get("title", "")
                                    for a in data.get("ancestors", [])
                                ],
                            ]
                        ),
                    ),
                )
                db.execute(
                    "UPDATE bookmark_downloads SET count=?,updated_at=? WHERE folder_key=?",
                    (len(seen) + 1, stamp(), key),
                )
            seen.add(page_id)

        def discovery_progress(count, path):
            nonlocal request_url
            request_url = settings.base_url + "/rest/api/" + path
            with connection() as db:
                db.execute(
                    "UPDATE bookmark_downloads SET total=?,updated_at=? WHERE folder_key=?",
                    (count, stamp(), key),
                )

        page_ids = await discover_pages(
            settings, root_ids, space_key, discovery_progress
        )
        with connection() as db:
            db.execute(
                "UPDATE bookmark_downloads SET total=?,phase='downloading',updated_at=? WHERE folder_key=?",
                (len(page_ids), stamp(), key),
            )
        for page_id in page_ids:
            await save(page_id)
        with connection() as db:
            existing = db.execute(
                "SELECT page_id FROM bookmark_downloaded_pages WHERE folder_key=?",
                (key,),
            ).fetchall()
            for row in existing:
                if row[0] not in seen:
                    db.execute(
                        "DELETE FROM bookmark_downloaded_pages WHERE folder_key=? AND page_id=?",
                        (key, row[0]),
                    )
            db.execute(
                "UPDATE bookmark_downloads SET status='completed',count=?,error=NULL,updated_at=? WHERE folder_key=?",
                (len(seen), stamp(), key),
            )
    except Exception as error:  # noqa: BLE001 - persist failure for background downloads
        if isinstance(error, HTTPException):
            reason = str(error.detail)
        elif isinstance(error, (ValueError, TypeError)):
            reason = str(error)
        else:
            reason = f"Unexpected {type(error).__name__} while downloading pages."
        # Never include credentials in the persisted message or log.
        token = settings.token.get_secret_value()
        if token:
            reason = reason.replace(token, "[redacted]")
        request_detail = "" if "Request:" in reason else f" Request: {request_url}"
        message = (
            f"Downloaded {len(seen)} pages before failure. {reason}{request_detail}"
        )
        logging.getLogger(__name__).error("%s %s", stamp(), message)
        with connection() as db:
            db.execute(
                "UPDATE bookmark_downloads SET status='failed',error=?,updated_at=? WHERE folder_key=?",
                (
                    message,
                    stamp(),
                    key,
                ),
            )
