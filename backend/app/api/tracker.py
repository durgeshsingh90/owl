"""Confluence Tracker roots, metadata lists, change history and local open counts."""

import json
import time

from app.core.database import connection
from app.tracker import service
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/confluence-tracker")


def require_root(db, root_id):
    row = db.execute(
        "SELECT * FROM confluence_tracker_roots WHERE id=?", (root_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Tracked root not found.")
    return row


@router.get("/roots")
def roots():
    with connection() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM confluence_tracker_pages p WHERE p.root_id=r.id) page_count, (SELECT COUNT(*) FROM confluence_tracker_changes c WHERE c.root_id=r.id AND reviewed=0) unread FROM confluence_tracker_roots r ORDER BY r.id"
            )
        ]
    for row in rows:
        row.pop("owner")
        row.pop("lease_until")
    return {"roots": rows, "interval_hours": 24, "retry_hours": 2}


class RootInput(BaseModel):
    root: str = Field(min_length=1, max_length=4096)


@router.post("/roots", status_code=202)
async def add_root(value: RootInput):
    return {"id": await service.add_root(value.root)}


@router.post("/roots/{root_id}/sync", status_code=202)
def sync_now(root_id: int):
    with connection() as db:
        root = require_root(db, root_id)
        if root["lease_until"] > time.time():
            return {"status": root["status"]}
        db.execute(
            "UPDATE confluence_tracker_roots SET next_run=0,status='queued',error='' WHERE id=?",
            (root_id,),
        )
    return {"status": "queued"}


@router.get("/roots/{root_id}/pages")
def pages(root_id: int):
    with connection() as db:
        require_root(db, root_id)
        unread = {
            row["page_id"]: row["count"]
            for row in db.execute(
                "SELECT page_id,COUNT(*) count FROM confluence_tracker_changes WHERE root_id=? AND reviewed=0 GROUP BY page_id",
                (root_id,),
            )
        }
        scans = {
            row["page_id"]: dict(row)
            for row in db.execute(
                "SELECT page_id,status,error FROM confluence_tracker_scan_pages WHERE root_id=?",
                (root_id,),
            )
        }
        result = []
        for row in db.execute(
            "SELECT * FROM confluence_tracker_pages WHERE root_id=?", (root_id,)
        ):
            data = json.loads(row["metadata"])
            item = {
                key: value
                for key, value in data.items()
                if key not in ("rawMetadata", "contentText")
            }
            item.update(
                {
                    key: row[key]
                    for key in (
                        "parent_id",
                        "first_seen",
                        "last_checked",
                        "changed_at",
                        "change_kind",
                        "present",
                        "opens",
                        "last_opened",
                    )
                }
            )
            scan = scans.pop(row["page_id"], {})
            item.update(
                unread=unread.get(row["page_id"], 0),
                download_status=scan.get("status", "cached"),
                error=scan.get("error", ""),
            )
            result.append(item)
        for page_id, scan in scans.items():
            result.append(
                {
                    "page_id": page_id,
                    "title": "Page " + page_id,
                    "download_status": scan["status"],
                    "error": scan["error"],
                    "opens": 0,
                    "present": 1,
                    "unread": 0,
                    "change_kind": "pending",
                }
            )
        max_event = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM confluence_tracker_changes WHERE root_id=?",
            (root_id,),
        ).fetchone()[0]
    return {"pages": result, "max_event": max_event}


@router.get("/roots/{root_id}/pages/{page_id}")
def page_details(root_id: int, page_id: str):
    with connection() as db:
        row = db.execute(
            "SELECT * FROM confluence_tracker_pages WHERE root_id=? AND page_id=?",
            (root_id, page_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "This page has not been downloaded yet.")
        changes = [
            {**dict(change), "summary": json.loads(change["summary"])}
            for change in db.execute(
                "SELECT * FROM confluence_tracker_changes WHERE root_id=? AND page_id=? ORDER BY id DESC LIMIT 50",
                (root_id, page_id),
            )
        ]
    return {
        "metadata": json.loads(row["metadata"]),
        "previous_content": row["previous_content"],
        "changes": changes,
    }


@router.post("/roots/{root_id}/pages/{page_id}/open")
def record_open(root_id: int, page_id: str):
    with connection() as db:
        changed = db.execute(
            "UPDATE confluence_tracker_pages SET opens=opens+1,last_opened=? WHERE root_id=? AND page_id=?",
            (service.stamp(), root_id, page_id),
        ).rowcount
        if not changed:
            raise HTTPException(404, "This page has not been downloaded yet.")
        count = db.execute(
            "SELECT opens FROM confluence_tracker_pages WHERE root_id=? AND page_id=?",
            (root_id, page_id),
        ).fetchone()[0]
    return {"opens": count}


class ReviewInput(BaseModel):
    through_id: int = Field(ge=0)


@router.post("/roots/{root_id}/review")
def review(root_id: int, value: ReviewInput):
    with connection() as db:
        require_root(db, root_id)
        db.execute(
            "UPDATE confluence_tracker_changes SET reviewed=1 WHERE root_id=? AND id<=?",
            (root_id, value.through_id),
        )
    return {"ok": True}
