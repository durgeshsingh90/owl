"""Bitbucket-explorer API for Confluence Tracker.

The Confluence Tracker page reuses the Bitbucket explorer UI, so this router answers the
same calls (workspace, documents, notes, search, jobs, failures, deletes, activity) from
the tracker tables. Mapping: project = tracked tree (its top-most page), repository =
top-level section below that page, document = page, commit = the latest create/update.
"""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.bookmarks import confluence
from app.core.database import connection
from app.pdfs.search import CONFLUENCE_INDEX, ranked_matches
from app.tracker import service
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/confluence-library")

WINDOW_DAYS = 730  # The list shows pages created or updated in the last two years.
ACTIVE = ("queued", "discovering", "downloading")
PAGE_FIELDS = """
    p.rowid AS id, p.root_id, p.page_id, p.parent_id, p.notes, p.opens, p.first_seen,
    p.last_checked, p.changed_at, p.change_kind, p.present,
    json_extract(p.metadata,'$.title') AS title,
    json_extract(p.metadata,'$.url') AS url,
    json_extract(p.metadata,'$.author') AS created_by,
    json_extract(p.metadata,'$.lastEditor') AS updated_by,
    json_extract(p.metadata,'$.writtenAt') AS created_at,
    json_extract(p.metadata,'$.confluenceUpdatedAt') AS updated_at,
    json_extract(p.metadata,'$.version') AS version,
    json_extract(p.metadata,'$.rawMetadata.version.message') AS version_message,
    json_extract(p.metadata,'$.space') AS space,
    json_extract(p.metadata,'$.spaceKey') AS space_key,
    json_extract(p.metadata,'$.pageTextSizeBytes') AS size,
    json_extract(p.metadata,'$.ancestors') AS ancestors,
    r.title AS root_title, r.page_id AS root_page, r.base_url, r.last_success
"""


def page_url(base_url, page_id):
    return base_url + "/pages/viewpage.action?pageId=" + str(page_id)


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def latest_activity(row):
    """The newer of created and updated, with who did it."""
    created, updated = parse_time(row["created_at"]), parse_time(row["updated_at"])
    if updated and (not created or updated >= created):
        return row["updated_at"], row["updated_by"] or row["created_by"] or ""
    return row["created_at"], row["created_by"] or ""


def pages_query(db, where="", params=()):
    return db.execute(
        f"SELECT {PAGE_FIELDS} FROM confluence_tracker_pages p "
        "JOIN confluence_tracker_roots r ON r.id=p.root_id " + where,
        params,
    )


def layout(db):
    """Section (top-level page below the tree's top) and path for every page."""
    rows = [dict(row) for row in pages_query(db)]
    parents = {(row["root_id"], row["parent_id"]) for row in rows if row["parent_id"]}
    # Current titles win over the titles stored in each page's ancestor list (renames).
    current = {(row["root_id"], row["page_id"]): row["title"] for row in rows}
    info = {}
    for row in rows:
        ancestors = json.loads(row["ancestors"] or "[]")
        chain = [str(item.get("page_id")) for item in ancestors]
        titles = [
            current.get((row["root_id"], str(item.get("page_id")))) or item.get("title", "")
            for item in ancestors
        ]
        below = chain.index(row["root_page"]) + 1 if row["root_page"] in chain else 0
        chain, titles = chain[below:], titles[below:]
        is_root = row["page_id"] == row["root_page"]
        has_children = (row["root_id"], row["page_id"]) in parents
        if chain:
            section, section_page, path = titles[0], chain[0], " / ".join(titles)
        elif not is_root and has_children:
            section, section_page, path = row["title"], row["page_id"], ""
        else:
            # The top page and loose pages directly below it share the tree's own section.
            section, section_page, path = row["root_title"], row["root_page"], ""
        info[row["id"]] = {
            "section": section or "Untitled",
            "section_page": section_page,
            "path": path,
            "ancestor_ids": chain,
            "has_children": has_children,
        }
    return rows, info


def section_ids(db):
    """Stable numeric IDs for sections: the rowid of the section's page."""
    return {
        (row["root_id"], row["page_id"]): row["rowid"]
        for row in db.execute("SELECT rowid,root_id,page_id FROM confluence_tracker_pages")
    }


def document(row, info, unread):
    when, who = latest_activity(row)
    url = row["url"] if str(row["url"] or "").startswith("http") else page_url(row["base_url"], row["page_id"])
    return {
        "id": row["id"],
        "pageId": row["page_id"],
        "projectId": str(row["root_id"]),
        "project": row["root_title"],
        "repo": info["section"],
        "name": row["title"] or "Page " + row["page_id"],
        "path": info["path"],
        "pdfUrl": url,
        "url": url,
        "folderUrl": page_url(row["base_url"], row["parent_id"] or row["page_id"]),
        "committedAt": when,
        "commitAuthor": who,
        "commitId": None,
        "commitCount": row["version"],
        "commitMessage": row["version_message"] or "",
        "createdAt": row["created_at"],
        "createdBy": row["created_by"] or "",
        "editedAt": row["updated_at"],
        "editedBy": row["updated_by"] or "",
        "space": row["space"] or "",
        "fileSize": row["size"],
        "hasChildren": info["has_children"],
        "ancestorIds": info["ancestor_ids"],
        "parentId": row["parent_id"],
        "openCount": row["opens"],
        "opens": row["opens"],
        "notes": row["notes"],
        "changeKind": row["change_kind"],
        "present": bool(row["present"]),
        "unread": unread.get((row["root_id"], row["page_id"]), 0),
        "addedAt": row["first_seen"],
        "updatedAt": row["last_checked"],
    }


def revision(db):
    row = db.execute("SELECT token,revision FROM confluence_tracker_revision WHERE id=1").fetchone()
    return f"{row['token']}:{row['revision']}"


@router.get("/workspace/revision")
def workspace_revision():
    with connection() as db:
        return {"revision": revision(db)}


@router.get("/workspace")
def workspace(
    limit: int | None = Query(default=None, ge=1, le=5000),
    before: int | None = Query(default=None, ge=1),
    summaries: bool = True,
    current_month: bool = False,
    days: int = Query(default=WINDOW_DAYS, ge=0, le=36500),
):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days else None
    month = now.strftime("%Y-%m") if current_month else None
    with connection() as db:
        current = revision(db)
        rows, info = layout(db)
        unread = {
            (row["root_id"], row["page_id"]): row["count"]
            for row in db.execute(
                "SELECT root_id,page_id,COUNT(*) count FROM confluence_tracker_changes WHERE reviewed=0 GROUP BY root_id,page_id"
            )
        }
        roots = [dict(row) for row in db.execute("SELECT * FROM confluence_tracker_roots ORDER BY title")]
        ids = section_ids(db)
    documents, in_window = [], []
    for row in sorted(rows, key=lambda item: item["id"], reverse=True):
        item = document(row, info[row["id"]], unread)
        when = parse_time(item["committedAt"])
        if cutoff and (not when or when < cutoff):
            continue
        in_window.append(item)
        if before and item["id"] >= before:
            continue
        if month and not str(item["committedAt"] or "").startswith(month):
            continue
        documents.append(item)
    has_more = limit is not None and len(documents) > limit
    documents = documents[:limit] if limit is not None else documents
    projects, people, tree = [], [], []
    if summaries:
        by_section = {}
        for item in in_window:
            by_section.setdefault((item["projectId"], item["repo"]), []).append(item)
        for root in roots:
            sections = {}
            for row in rows:
                if row["root_id"] == root["id"]:
                    section = info[row["id"]]
                    sections.setdefault(section["section"], section["section_page"])
            repos = []
            for name, section_page in sorted(sections.items(), key=lambda pair: pair[0].lower()):
                items = by_section.get((str(root["id"]), name), [])
                latest = max((item["committedAt"] or "" for item in items), default="")
                repos.append(
                    {
                        "id": ids.get((root["id"], section_page), 0),
                        "name": name,
                        "pageId": section_page,
                        "pdfCount": len(items),
                        "lastPullAt": root["last_success"],
                        "lastCommitAt": latest or None,
                        "lastCommit": parse_time(latest).strftime("%d %b %Y") if latest else "Unknown",
                        "baseUrl": page_url(root["base_url"], section_page),
                    }
                )
            projects.append(
                {
                    "id": str(root["id"]),
                    "name": root["title"],
                    "rootPageId": root["page_id"],
                    "url": root["url"],
                    "status": root["status"],
                    "repos": repos,
                }
            )
        counts = {}
        for item in in_window:
            for name in {item["createdBy"], item["editedBy"]} - {""}:
                key = (item["projectId"], item["repo"], name)
                entry = counts.setdefault(key, {"pages": 0, "updates": 0})
                entry["pages"] += 1
                entry["updates"] += 1 if name == item["commitAuthor"] else 0
        for (project, repo, name), entry in sorted(counts.items()):
            people.append(
                {
                    "id": len(people) + 1,
                    "projectId": project,
                    "repo": repo,
                    "name": name,
                    "email": "",
                    "pdfCount": entry["pages"],
                    "commits": entry["updates"],
                }
            )
        # Every tracked page (not only the last two years) for the folder and page tree.
        tree = [
            {
                "id": row["id"],
                "pageId": row["page_id"],
                "parentId": row["parent_id"],
                "projectId": str(row["root_id"]),
                "title": row["title"] or "Page " + row["page_id"],
                "section": info[row["id"]]["section"],
                "url": page_url(row["base_url"], row["page_id"]),
            }
            for row in rows
        ]
    last = max((root["last_success"] or "" for root in roots), default="") or None
    return {
        "projects": projects,
        "documents": documents,
        "people": people,
        "tree": tree,
        "windowDays": days,
        "nextBefore": documents[-1]["id"] if has_more else None,
        "backgroundAll": current_month,
        "lastCompletedPull": last,
        "revision": current,
    }


def page_row(db, document_id):
    row = pages_query(db, "WHERE p.rowid=?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Page not found.")
    return dict(row)


@router.get("/document/{document_id}")
def document_details(document_id: int):
    with connection() as db:
        row = page_row(db, document_id)
        text = db.execute(
            "SELECT json_extract(metadata,'$.contentText') FROM confluence_tracker_pages WHERE rowid=?",
            (document_id,),
        ).fetchone()[0]
        _, info = layout(db)
    when, who = latest_activity(row)
    return {
        "pdf_name": row["title"],
        "page_id": row["page_id"],
        "project": row["root_title"],
        "repo": info[document_id]["section"],
        "path": info[document_id]["path"],
        "url": page_url(row["base_url"], row["page_id"]),
        "space": row["space"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "version": row["version"],
        "version_message": row["version_message"],
        "file_size": row["size"],
        "change_kind": row["change_kind"],
        "first_seen": row["first_seen"],
        "last_checked": row["last_checked"],
        "open_count": row["opens"],
        "notes": row["notes"],
        "latest_activity": when,
        "latest_by": who,
        "pdf_text": text or "",
    }


class NotesRequest(BaseModel):
    notes: str = Field(max_length=100000)


@router.patch("/document/{document_id}/notes")
def save_notes(document_id: int, value: NotesRequest):
    with connection() as db:
        page_row(db, document_id)
        db.execute(
            "UPDATE confluence_tracker_pages SET notes=? WHERE rowid=?", (value.notes, document_id)
        )
    return {"ok": True, "notes": value.notes}


@router.post("/document/{document_id}/open")
def record_open(document_id: int):
    with connection() as db:
        row = page_row(db, document_id)
        db.execute(
            "UPDATE confluence_tracker_pages SET opens=opens+1,last_opened=? WHERE rowid=?",
            (service.stamp(), document_id),
        )
    return {"open_count": row["opens"] + 1, "url": page_url(row["base_url"], row["page_id"])}


@router.get("/document/{document_id}/versions")
async def versions(document_id: int):
    """Version history straight from Confluence, newest first."""
    with connection() as db:
        row = page_row(db, document_id)
    settings = confluence.load()
    items = []
    try:
        data = await confluence.get(
            settings, f"content/{row['page_id']}/version", {"limit": 200, "expand": "by"}
        )
        for version in data.get("results", []):
            items.append(
                {
                    "number": version.get("number"),
                    "author": (version.get("by") or {}).get("displayName", ""),
                    "when": version.get("when"),
                    "message": version.get("message", ""),
                }
            )
    except (HTTPException, ValueError):
        # Older servers lack the version list; show the versions known locally.
        items = [
            {"number": row["version"], "author": row["updated_by"], "when": row["updated_at"], "message": row["version_message"] or ""},
        ]
        if row["version"] != 1:
            items.append({"number": 1, "author": row["created_by"], "when": row["created_at"], "message": "Created"})
    items.sort(key=lambda item: item["number"] or 0, reverse=True)
    for item in items:
        item["url"] = (
            row["base_url"] + f"/pages/viewpage.action?pageId={row['page_id']}&pageVersion={item['number']}"
        )
    return {"name": row["title"], "pageId": row["page_id"], "versions": items}


class SearchRequest(BaseModel):
    q: str = Field(max_length=1000)
    fields: list[str] = Field(default_factory=lambda: ["name", "path", "content", "notes"], max_length=4)
    mode: str = "separate"


@router.post("/search/matches")
def search(value: SearchRequest):
    ranked = ranked_matches(value.q, value.fields, value.mode, CONFLUENCE_INDEX)
    return {"ids": [id for id, _ in ranked], "tiers": {str(id): tier for id, tier in ranked}}


# Sync jobs: a job asks the tracker scheduler to check some trees now and reports their
# combined progress. The scheduler downloads every page on each check.
def root_ids_for(db, project_ids=None, repository_ids=None):
    if repository_ids:
        marks = ",".join("?" * len(repository_ids))
        found = [
            row[0]
            for row in db.execute(
                f"SELECT DISTINCT root_id FROM confluence_tracker_pages WHERE rowid IN ({marks})",
                repository_ids,
            )
        ]
        if not found:
            raise HTTPException(404, "Section not found.")
        return found
    rows = db.execute("SELECT id FROM confluence_tracker_roots").fetchall()
    known = [row[0] for row in rows]
    if project_ids:
        missing = set(project_ids) - set(known)
        if missing:
            raise HTTPException(404, "Tracked tree not found.")
        return list(project_ids)
    return known


def start_job(db, root_ids, kind):
    if not root_ids:
        raise HTTPException(400, "Add a Confluence page URL first.")
    marks = ",".join("?" * len(root_ids))
    db.execute(
        f"UPDATE confluence_tracker_roots SET next_run=0,status=CASE WHEN status IN ('discovering','downloading') THEN status ELSE 'queued' END WHERE id IN ({marks})",
        root_ids,
    )
    job_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO confluence_tracker_jobs(id,kind,root_ids,started_at,change_floor) VALUES(?,?,?,?,(SELECT COALESCE(MAX(id),0) FROM confluence_tracker_changes))",
        (job_id, kind, json.dumps(root_ids), service.stamp()),
    )
    return job_view(db, job_id)


def job_view(db, job_id):
    job = db.execute("SELECT * FROM confluence_tracker_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "Sync not found.")
    root_ids = json.loads(job["root_ids"])
    marks = ",".join("?" * len(root_ids))
    roots = [
        dict(row)
        for row in db.execute(f"SELECT * FROM confluence_tracker_roots WHERE id IN ({marks})", root_ids)
    ]
    started = job["started_at"]
    finished = [root for root in roots if (root["last_attempt"] or "") >= started and root["status"] not in ACTIVE and root["lease_until"] <= time.time()]
    running = [root for root in roots if root["status"] in ACTIVE and (root["last_attempt"] or "") >= started]
    if len(finished) == len(roots):
        status = "failed" if any(root["status"] == "failed" for root in roots) else "succeeded"
    elif running:
        status = "running"
    else:
        status = "queued"
    changes = {
        row["kind"]: row["count"]
        for row in db.execute(
            f"SELECT kind,COUNT(*) count FROM confluence_tracker_changes WHERE id>? AND root_id IN ({marks}) GROUP BY kind",
            (job["change_floor"], *root_ids),
        )
    }
    total = sum(root["total"] or 0 for root in roots)
    done = sum(root["completed"] or 0 for root in roots)
    failed = sum(root["failed"] or 0 for root in roots)
    new = changes.get("new", 0) + changes.get("returned", 0)
    updated = changes.get("updated", 0)
    elapsed = max(0.0, (datetime.now(timezone.utc) - parse_time(started)).total_seconds())
    eta = (elapsed / done) * (total - done) if status == "running" and done and total > done else None
    completed_at = max((root["last_attempt"] or "" for root in finished), default="") if status in ("succeeded", "failed") else None
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": status,
        "background": False,
        "started_at": started,
        "completed_at": completed_at,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "eta_updated_at": service.stamp() if eta is not None else None,
        "paused_seconds": 0,
        "repositories": total,
        "repositories_done": done,
        "repositories_failed": 0,
        "processed": done,
        "new": new,
        "updated": updated,
        "unchanged": max(0, done - new - updated - failed),
        "failed": failed,
        "retry_recovered": 0,
        "bitbucket_connected": status == "running",
        "repository_statuses": {
            str(root["id"]): {
                "project_id": str(root["id"]),
                "repo": root["title"],
                "status": "succeeded" if root in finished and root["status"] == "completed" else root["status"],
                "processed": root["completed"],
                "found": root["total"],
                "failed": root["failed"],
            }
            for root in roots
        },
        "roots": [{"id": root["id"], "title": root["title"], "status": root["status"], "error": root["error"]} for root in roots],
        "checkpoint": None,
    }


class CrawlRequest(BaseModel):
    project_ids: list[int] | None = None
    repository_ids: list[int] | None = Field(default=None, max_length=10000)


@router.post("/crawl", status_code=202)
def crawl(value: CrawlRequest):
    with connection() as db:
        return start_job(db, root_ids_for(db, value.project_ids, value.repository_ids), "sync")


class ImportRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)


@router.post("/imports", status_code=202)
async def imports(value: ImportRequest):
    """Add any page, space or short-link URL: its top-most parent is tracked with every page below."""
    root_ids = []
    for url in value.urls:
        try:
            root_ids.append(await service.add_root(url))
        except ValueError as error:
            raise HTTPException(400, f"{url}: {error}") from None
    with connection() as db:
        return start_job(db, list(dict.fromkeys(root_ids)), "import")


@router.get("/jobs/latest")
def latest_job():
    with connection() as db:
        row = db.execute("SELECT id FROM confluence_tracker_jobs ORDER BY started_at DESC LIMIT 1").fetchone()
        return {"job": job_view(db, row["id"]) if row else None}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    with connection() as db:
        return job_view(db, job_id)


@router.post("/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str):
    raise HTTPException(409, "A Confluence check cannot be paused or stopped; it finishes on its own.")


@router.get("/failed")
def failed(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    with connection() as db:
        rows = db.execute(
            "SELECT s.page_id,s.error,r.title,r.id root_id FROM confluence_tracker_scan_pages s "
            "JOIN confluence_tracker_roots r ON r.id=s.root_id WHERE s.status='failed' "
            "ORDER BY r.title,s.page_id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        titles = {
            (row["root_id"], row["page_id"]): row["title"]
            for row in pages_query(db, "WHERE p.page_id IN (SELECT page_id FROM confluence_tracker_scan_pages WHERE status='failed')")
        }
    return [
        {
            "project": row["title"],
            "repo": row["page_id"],
            "path": titles.get((row["root_id"], row["page_id"])) or row["page_id"],
            "error": row["error"],
        }
        for row in rows
    ]


@router.post("/failed/retry", status_code=202)
def retry_failed():
    with connection() as db:
        roots = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT root_id FROM confluence_tracker_scan_pages WHERE status='failed'"
            )
        ]
        if not roots:
            raise HTTPException(400, "There are no failed pages to retry.")
        return start_job(db, roots, "retry")


@router.get("/crawl/hard-retry/preview")
def hard_retry_preview():
    with connection() as db:
        roots = db.execute("SELECT id,title FROM confluence_tracker_roots ORDER BY title").fetchall()
        pages = db.execute("SELECT COUNT(*) FROM confluence_tracker_pages").fetchone()[0]
        failed_pages = db.execute(
            "SELECT COUNT(*) FROM confluence_tracker_scan_pages WHERE status='failed'"
        ).fetchone()[0]
        active = db.execute(
            f"SELECT 1 FROM confluence_tracker_roots WHERE status IN {ACTIVE}"
        ).fetchone()
    try:
        server = confluence.load().base_url
    except ValueError:
        server = "Confluence"
    return {
        "server": server,
        "projects": [{"project": row["title"]} for row in roots],
        "repositories": len(roots),
        "documents": pages,
        "failed_documents": failed_pages,
        "active": bool(active),
    }


class Confirmation(BaseModel):
    confirmation: str = ""


@router.post("/crawl/hard-retry", status_code=202)
def hard_retry(value: Confirmation):
    if value.confirmation != "HARD RETRY":
        raise HTTPException(400, "Type HARD RETRY to confirm.")
    with connection() as db:
        return start_job(db, root_ids_for(db), "hard-retry")


class ProjectDeletion(BaseModel):
    project_id: int
    confirmation: str = ""


def project_preview(db, project_id):
    root = db.execute("SELECT * FROM confluence_tracker_roots WHERE id=?", (project_id,)).fetchone()
    if root is None:
        raise HTTPException(404, "Tracked tree not found.")
    pages = db.execute(
        "SELECT COUNT(*) FROM confluence_tracker_pages WHERE root_id=?", (project_id,)
    ).fetchone()[0]
    failed_pages = db.execute(
        "SELECT COUNT(*) FROM confluence_tracker_scan_pages WHERE root_id=? AND status='failed'",
        (project_id,),
    ).fetchone()[0]
    return {
        "project": {"id": root["id"], "project": root["title"]},
        "repositories": [{"project": root["title"], "repo": root["title"]}],
        "documents": pages,
        "failed_documents": failed_pages,
    }


@router.post("/projects/delete-preview")
def delete_preview(value: ProjectDeletion):
    with connection() as db:
        return project_preview(db, value.project_id)


@router.post("/projects/delete")
def delete_project(value: ProjectDeletion):
    if value.confirmation != "delete all":
        raise HTTPException(400, "Type delete all to confirm.")
    with connection() as db:
        preview = project_preview(db, value.project_id)
        if db.execute(
            f"SELECT 1 FROM confluence_tracker_roots WHERE id=? AND status IN {ACTIVE}",
            (value.project_id,),
        ).fetchone():
            raise HTTPException(409, "Wait for this tree's check to finish.")
        db.execute("DELETE FROM confluence_tracker_roots WHERE id=?", (value.project_id,))
    return {"ok": True, **preview}


@router.post("/repositories/delete-preview")
@router.post("/repositories/delete")
def delete_sections():
    raise HTTPException(
        400, "Sections come from Confluence and return on the next check. Delete the whole tracked tree instead."
    )


@router.get("/activity")
def activity(limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    with connection() as db:
        total = db.execute("SELECT COUNT(*) FROM confluence_tracker_jobs").fetchone()[0]
        ids = [
            row[0]
            for row in db.execute(
                "SELECT id FROM confluence_tracker_jobs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        ]
        items = []
        for job_id in ids:
            view = job_view(db, job_id)
            items.append(
                {
                    "started_at": view["started_at"],
                    "completed_at": view["completed_at"],
                    "status": view["status"],
                    "repositories": [
                        {
                            "operation": view["kind"].replace("-", " ").title(),
                            "project": root["title"],
                            "repo": "",
                            "status": root["status"],
                            "new": view["new"],
                            "updated": view["updated"],
                            "deleted": 0,
                            "unchanged": view["unchanged"],
                            "failed": view["failed"],
                        }
                        for root in view["roots"]
                    ],
                }
            )
    return {"total": total, "items": items}


class ReviewRequest(BaseModel):
    project_id: int | None = None


@router.post("/review")
def review(value: ReviewRequest):
    """Mark detected changes as reviewed (one tree, or every tree)."""
    with connection() as db:
        if value.project_id is None:
            count = db.execute("UPDATE confluence_tracker_changes SET reviewed=1 WHERE reviewed=0").rowcount
        else:
            count = db.execute(
                "UPDATE confluence_tracker_changes SET reviewed=1 WHERE reviewed=0 AND root_id=?",
                (value.project_id,),
            ).rowcount
    return {"ok": True, "reviewed": count}
