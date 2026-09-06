"""Database-backed frontend workspace payloads."""

import json

from app.core.database import connection
from fastapi import APIRouter, HTTPException
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
def workspace():
    with connection() as db:
        projects = []
        for p in db.execute("SELECT * FROM tracked_projects ORDER BY project"):
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
        contributors = {}
        for row in db.execute(
            "SELECT d.*,r.project_id FROM documents d JOIN repositories r ON r.id=d.repository_id"
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
            if d["author"]:
                key = (str(d["project_id"]), d["repo"], d["author"])
                c = contributors.setdefault(
                    key,
                    {
                        "id": len(contributors) + 1,
                        "projectId": key[0],
                        "repo": key[1],
                        "name": key[2],
                        "email": "",
                        "pdfCount": 0,
                        "commits": set(),
                    },
                )
                c["pdfCount"] += 1
                if d["commit_id"]:
                    c["commits"].add(d["commit_id"])
        people = [{**p, "commits": len(p["commits"])} for p in contributors.values()]
    return {"projects": projects, "documents": documents, "people": people}
