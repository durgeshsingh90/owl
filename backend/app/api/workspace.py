"""Database-backed frontend workspace payloads."""

import json
from datetime import datetime, timezone

from app.core.database import connection
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api")


class BookmarkWorkspace(BaseModel):
    last_update_all: str | None = Field(default=None, max_length=64)
    revision: int = 0
    bookmarks: list[dict] = Field(default_factory=list, max_length=50000)
    groups: list[dict] = Field(default_factory=list, max_length=1000)
    notes: dict = Field(default_factory=dict)
    starred_people: list[str] = Field(default_factory=list, max_length=50000)
    starred_folders: list[str] = Field(default_factory=list, max_length=50000)


@router.get("/bookmarks/workspace")
def bookmarks():
    with connection() as db:
        row = db.execute(
            "SELECT payload,revision FROM bookmark_workspace WHERE id=1"
        ).fetchone()
    return {**json.loads(row["payload"]), "revision": row["revision"]}


@router.put("/bookmarks/workspace")
def save_bookmarks(value: BookmarkWorkspace):
    from urllib.parse import urlsplit

    ids = []
    for item in value.bookmarks:
        url = urlsplit(item.get("url", ""))
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
        ):
            raise HTTPException(422, "Invalid bookmark URL.")
        if not isinstance(item.get("id"), int):
            raise HTTPException(422, "Bookmark ID must be an integer.")
        ids.append(item["id"])
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Duplicate bookmark IDs.")
    payload = value.model_dump(exclude={"revision"})
    with connection() as db:
        changed = db.execute(
            "UPDATE bookmark_workspace SET payload=?,revision=revision+1 WHERE id=1 AND revision=?",
            (json.dumps(payload), value.revision),
        ).rowcount
        if not changed:
            raise HTTPException(
                409, "Bookmarks changed in another tab. Reload before editing."
            )
    return {"ok": True, "revision": value.revision + 1}


@router.get("/workspace")
def workspace(
    limit: int | None = Query(default=None, ge=1, le=5000),
    before: int | None = Query(default=None, ge=1),
    summaries: bool = True,
    current_month: bool = False,
):
    month = datetime.now(timezone.utc).strftime("%Y-%m") if current_month else None
    with connection() as db:
        completed = db.execute(
            "SELECT last_completed_at FROM sync_metadata WHERE id=1"
        ).fetchone()
        if completed is None:
            completed = db.execute(
                "SELECT json_extract(progress, '$.completed_at') FROM jobs WHERE status IN ('succeeded','succeeded_with_errors') ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        projects = []
        for p in (
            db.execute("SELECT * FROM tracked_projects ORDER BY project")
            if summaries
            else []
        ):
            repos = []
            for r in db.execute(
                "SELECT r.*,COUNT(d.id) pdf_count,MAX(d.commit_date) last_commit FROM repositories r "
                "LEFT JOIN documents d ON r.id=d.repository_id WHERE project_id=? GROUP BY r.id",
                (p["id"],),
            ):
                repos.append(
                    {
                        "id": r["id"],
                        "name": r["repo"],
                        "pdfCount": r["pdf_count"],
                        "lastPullAt": r["last_pull_at"],
                        "lastCommit": __import__("datetime")
                        .datetime.fromisoformat(r["last_commit"])
                        .strftime("%d %b %Y")
                        if r["last_commit"]
                        else "Unknown",
                        "baseUrl": p["server"]
                        + "/projects/"
                        + p["project"]
                        + "/repos/"
                        + r["repo"],
                    }
                )
            projects.append({"id": str(p["id"]), "name": p["project"], "repos": repos})
        documents = []
        for row in db.execute(
            "SELECT d.id,d.project,d.file_size,d.page_count,d.commit_id,d.commit_count,"
            "d.commit_message,d.last_scanned,d.repo,d.pdf_name,d.path,d.url,d.commit_date,"
            "d.author,d.open_count,d.notes,d.added_at,d.updated_at,r.project_id "
            "FROM documents d JOIN repositories r ON r.id=d.repository_id "
            "WHERE (? IS NULL OR d.id < ?) AND (? IS NULL OR strftime('%Y-%m', d.commit_date)=?) ORDER BY d.id DESC LIMIT ?",
            (before, before, month, month, limit + 1 if limit is not None else -1),
        ):
            d = dict(row)
            documents.append(
                {
                    "id": d["id"],
                    "projectId": str(d["project_id"]),
                    "project": d["project"],
                    "fileSize": d["file_size"],
                    "pageCount": d["page_count"],
                    "commitId": d["commit_id"],
                    "commitCount": d["commit_count"],
                    "commitMessage": d["commit_message"],
                    "lastScanned": d["last_scanned"],
                    "repo": d["repo"],
                    "name": d["pdf_name"],
                    "path": d["path"],
                    "pdfUrl": d["url"],
                    "url": d["url"],
                    "folderUrl": d["url"].rsplit("/", 1)[0],
                    "committedAt": d["commit_date"],
                    "commitAuthor": d["author"],
                    "openCount": d["open_count"],
                    "opens": d["open_count"],
                    "notes": d["notes"],
                    "addedAt": d["added_at"],
                    "updatedAt": d["updated_at"],
                }
            )
        has_more = limit is not None and len(documents) > limit
        if has_more:
            documents.pop()
        people = []
        if summaries:
            for row in db.execute(
                "SELECT r.project_id,d.repo,d.author,COUNT(*) pdf_count,"
                "COUNT(DISTINCT NULLIF(d.commit_id,'')) commits "
                "FROM documents d JOIN repositories r ON r.id=d.repository_id "
                "WHERE d.author IS NOT NULL AND d.author != '' "
                "GROUP BY r.project_id,d.repo,d.author ORDER BY r.project_id,d.repo,d.author"
            ):
                people.append(
                    {
                        "id": len(people) + 1,
                        "projectId": str(row["project_id"]),
                        "repo": row["repo"],
                        "name": row["author"],
                        "email": "",
                        "pdfCount": row["pdf_count"],
                        "commits": row["commits"],
                    }
                )
    return {
        "projects": projects,
        "documents": documents,
        "people": people,
        "nextBefore": documents[-1]["id"] if has_more else None,
        "backgroundAll": current_month,
        "lastCompletedPull": completed[0] if completed else None,
    }
