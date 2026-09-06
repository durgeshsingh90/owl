"""HTTP interface for the PDF crawler and document library."""

import asyncio
import json

from app.core.config import (
    Settings,
    load_settings,
    parse_project,
    parse_target,
    save_settings,
)
from app.core.database import connection, exclude_repositories, repository_url
from app.core.logging import error_details, event, request_id
from app.pdfs.client import BitbucketClient, BitbucketError
from app.pdfs.search import search_documents
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter()


class ProjectRequest(BaseModel):
    project_url: str = Field(max_length=2000)


class CrawlRequest(BaseModel):
    project_ids: list[int] | None = None


class DeleteRequest(BaseModel):
    confirmation: str


class NotesRequest(BaseModel):
    notes: str = Field(max_length=100000)


@router.get("/health")
def health():
    with connection() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@router.get("/settings")
def settings():
    try:
        value = load_settings()
    except ValueError:
        return {"configured": False}
    return {
        "configured": True,
        **value.model_dump(exclude={"token"}),
        "has_token": True,
    }


@router.post("/settings")
def save(value: Settings, request: Request):
    if request.app.state.jobs.active():
        raise HTTPException(409, "Wait for the active crawl before changing settings.")
    save_settings(value)
    return {"ok": True}


async def test_value(value):
    event("connection.test_started")
    client = BitbucketClient(value)
    try:
        result = await client.test()
        event("connection.test_succeeded")
        return result
    except (BitbucketError, asyncio.TimeoutError) as error:
        event("connection.test_failed", level=40, **error_details(error))
        detail = (
            str(error)
            or "Connection test exceeded its 8-second deadline. See backend logs for the last network attempt."
        )
        raise HTTPException(502, detail + f" Request ID: {request_id.get()}") from None
    finally:
        await client.close()


@router.post("/settings/test")
async def test_settings(value: Settings):
    return await test_value(value)


@router.post("/connection/test")
async def test_saved():
    return await test_value(load_settings())


@router.post("/project", status_code=201)
def add_project(value: ProjectRequest):
    config = load_settings()
    project, url = parse_project(value.project_url, config)
    with connection() as db:
        row = db.execute(
            "INSERT INTO tracked_projects(project_url,server,project) VALUES(?,?,?) "
            "ON CONFLICT(server,project) DO UPDATE SET project_url=excluded.project_url RETURNING *",
            (url, config.base_url, project),
        ).fetchone()
        return dict(row)


@router.get("/projects")
def projects():
    with connection() as db:
        return [
            dict(row)
            for row in db.execute("SELECT * FROM tracked_projects ORDER BY project")
        ]


@router.post("/crawl", status_code=202)
async def crawl(value: CrawlRequest, request: Request):
    if request.app.state.jobs.active():
        raise HTTPException(409, "A crawl is already running.")
    return request.app.state.jobs.start(value.project_ids)


@router.get("/crawl/hard-retry/preview")
def hard_retry_preview(request: Request):
    settings = load_settings()
    with connection() as db:
        projects = [
            dict(row)
            for row in db.execute(
                "SELECT id,project FROM tracked_projects WHERE server=? ORDER BY project",
                (settings.base_url,),
            )
        ]
        scope = "SELECT r.id FROM repositories r JOIN tracked_projects p ON p.id=r.project_id WHERE p.server=?"
        counts = {
            table: db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE repository_id IN ({scope})",
                (settings.base_url,),
            ).fetchone()[0]
            for table in ("documents", "failed_documents")
        }
        repositories = db.execute(
            f"SELECT COUNT(*) FROM repositories WHERE id IN ({scope})",
            (settings.base_url,),
        ).fetchone()[0]
    return {
        "server": settings.base_url,
        "projects": projects,
        "repositories": repositories,
        **counts,
        "active": request.app.state.jobs.active(),
    }


@router.post("/crawl/hard-retry", status_code=202)
async def hard_retry(value: DeleteRequest, request: Request):
    if request.app.state.jobs.active():
        raise HTTPException(
            409, "Wait for the current crawl to finish or stop it before hard retry."
        )
    if value.confirmation != "HARD RETRY":
        raise HTTPException(
            400, "Confirm with HARD RETRY before clearing the PDF index."
        )
    preview = hard_retry_preview(request)
    return request.app.state.jobs.start(
        [project["id"] for project in preview["projects"]], hard_retry=True
    )


@router.get("/jobs/latest")
def latest_job():
    with connection() as db:
        row = db.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()
    if row is None:
        return {"job": None}
    return {"job": {**json.loads(row["progress"]), "status": row["status"]}}


@router.get("/jobs/{job_id}")
def job(job_id: str):
    with connection() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found.")
    return {**json.loads(row["progress"]), "status": row["status"]}


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, request: Request):
    jobs = request.app.state.jobs
    if jobs.current is None or jobs.current["id"] != job_id:
        raise HTTPException(404, "Active job not found.")
    await jobs.shutdown()
    return jobs.current


@router.get("/search")
def search(
    q: str = Query(default="", max_length=1000),
    project: str | None = None,
    repo: str | None = None,
    author: str | None = None,
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    return search_documents(q, project, repo, author, limit, offset)


@router.get("/repo")
def repo_documents(
    project: str,
    repo: str = "all",
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    return search_documents(
        project=project,
        repo=None if repo == "all" else repo,
        limit=limit,
        offset=offset,
    )


@router.get("/document/{doc_id}")
def document(doc_id: int):
    with connection() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Document not found.")
    return dict(row)


@router.patch("/document/{doc_id}/notes")
def notes(doc_id: int, value: NotesRequest):
    with connection() as db:
        if (
            db.execute(
                "UPDATE documents SET notes=? WHERE id=?", (value.notes, doc_id)
            ).rowcount
            == 0
        ):
            raise HTTPException(404, "Document not found.")
    return {"ok": True}


@router.post("/document/{doc_id}/open")
def opened(doc_id: int):
    with connection() as db:
        row = db.execute(
            "UPDATE documents SET open_count=open_count+1 WHERE id=? RETURNING url,open_count",
            (doc_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Document not found.")
    return dict(row)


class RepositorySelection(BaseModel):
    repository_ids: list[int] = Field(min_length=1, max_length=10000)


class RepositoryDeletion(RepositorySelection):
    confirmation: str


def repository_delete_scope(value, db):
    ids = sorted(set(value.repository_ids))
    placeholders = ",".join("?" for _ in ids)
    rows = [
        dict(row)
        for row in db.execute(
            f"SELECT r.id,r.repo,p.project,p.server FROM repositories r JOIN tracked_projects p ON p.id=r.project_id WHERE r.id IN ({placeholders}) ORDER BY p.project,r.repo",
            ids,
        )
    ]
    if len(rows) != len(ids):
        raise HTTPException(
            404, "A selected repository no longer exists. Refresh the selection."
        )
    return ids, placeholders, rows


@router.post("/repositories/delete-preview")
def preview_repository_deletion(value: RepositorySelection):
    with connection() as db:
        ids, placeholders, rows = repository_delete_scope(value, db)
        counts = {
            table: db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE repository_id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            for table in ("documents", "failed_documents")
        }
    return {"repositories": rows, **counts}


@router.post("/repositories/delete")
async def delete_repositories(value: RepositoryDeletion, request: Request):
    if value.confirmation != "delete all":
        raise HTTPException(
            400, "Type delete all to confirm deletion of the selected repositories."
        )
    if request.app.state.jobs.active():
        raise HTTPException(409, "Stop the active crawl before deleting repositories.")
    with connection() as db:
        _, _, rows = repository_delete_scope(value, db)
        # Foreign keys remove documents and failures; document triggers remove FTS entries.
        exclude_repositories(db, rows)
    return {"ok": True, "deleted": len(rows)}


@router.delete("/repositories/{repository_id}")
def delete_repo(repository_id: int, value: DeleteRequest, request: Request):
    if value.confirmation != "delete all":
        raise HTTPException(
            400, "Type delete all to confirm local repository deletion."
        )
    if request.app.state.jobs.active():
        raise HTTPException(409, "Stop the active crawl before deleting a repository.")
    with connection() as db:
        _, _, rows = repository_delete_scope(
            RepositorySelection(repository_ids=[repository_id]), db
        )
        exclude_repositories(db, rows)
    return {
        "ok": True,
        "detail": "Removed local indexed records. Remote repository is unchanged.",
    }


@router.get("/stats")
def stats():
    with connection() as db:
        return {
            **dict(
                db.execute(
                    "SELECT COUNT(*) documents,COALESCE(SUM(open_count),0) opens FROM documents"
                ).fetchone()
            ),
            "repos": db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0],
            "projects": db.execute("SELECT COUNT(*) FROM tracked_projects").fetchone()[
                0
            ],
            "failed": db.execute("SELECT COUNT(*) FROM failed_documents").fetchone()[0],
        }


@router.get("/sidebar")
def sidebar():
    with connection() as db:
        result = []
        for project in db.execute("SELECT * FROM tracked_projects ORDER BY project"):
            repos = [
                dict(row)
                for row in db.execute(
                    "SELECT r.id,r.repo,r.name,r.last_scanned,COUNT(d.id) pdf_count "
                    "FROM repositories r LEFT JOIN documents d ON d.repository_id=r.id WHERE r.project_id=? GROUP BY r.id ORDER BY r.repo",
                    (project["id"],),
                )
            ]
            result.append(
                {
                    "id": project["id"],
                    "project": project["project"],
                    "pdf_count": sum(r["pdf_count"] for r in repos),
                    "repos": repos,
                }
            )
        return result


@router.get("/contributors")
def contributors():
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT author,COUNT(*) total FROM documents WHERE author IS NOT NULL AND author != '' GROUP BY author ORDER BY total DESC LIMIT 20"
            )
        ]


@router.get("/failed")
def failed(
    limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)
):
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT f.*,r.repo,p.project,p.server FROM failed_documents f "
                "JOIN repositories r ON r.id=f.repository_id "
                "JOIN tracked_projects p ON p.id=r.project_id "
                "ORDER BY f.last_attempt DESC,f.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        ]


@router.get("/projects_summary")
def summary():
    return [
        {
            "project": p["project"],
            "pdf_count": p["pdf_count"],
            "repo_count": len(p["repos"]),
        }
        for p in sidebar()
    ]


class ImportRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)


@router.post("/imports", status_code=202)
async def import_urls(value: ImportRequest, request: Request):
    jobs = request.app.state.jobs
    if jobs.active():
        raise HTTPException(409, "A crawl is already running.")
    settings = load_settings()
    targets = [parse_target(url, settings) for url in value.urls]
    ids = set()
    with connection() as db:
        for target in targets:
            row = db.execute(
                "INSERT INTO tracked_projects(project_url,server,project) VALUES(?,?,?) "
                "ON CONFLICT(server,project) DO UPDATE SET project_url=excluded.project_url RETURNING id",
                (target["url"], settings.base_url, target["project"]),
            ).fetchone()
            ids.add(row["id"])
    return jobs.start(list(ids), targets)


@router.post("/failed/retry", status_code=202)
async def retry_failed(request: Request):
    jobs = request.app.state.jobs
    if jobs.active():
        raise HTTPException(
            409, "Wait for the current crawl to finish before retrying."
        )
    settings = load_settings()
    with connection() as db:
        rows = db.execute(
            "SELECT f.path,r.repo,r.project_id,p.project,p.project_url FROM failed_documents f "
            "JOIN repositories r ON r.id=f.repository_id JOIN tracked_projects p ON p.id=r.project_id "
            "WHERE p.server=?",
            (settings.base_url,),
        ).fetchall()
    if not rows:
        raise HTTPException(400, "No failed PDFs for the configured Bitbucket server.")
    targets = [
        {
            "project": r["project"],
            "repo": r["repo"],
            "path": r["path"],
            "url": r["project_url"],
        }
        for r in rows
    ]
    return jobs.start(list({r["project_id"] for r in rows}), targets, auto_retry=False)


class RestoreRepository(BaseModel):
    url: str = Field(max_length=2000)


@router.get("/excluded-repositories")
def excluded_repositories():
    with connection() as db:
        return [
            dict(row)
            for row in db.execute("SELECT url FROM excluded_repositories ORDER BY url")
        ]


@router.post("/excluded-repositories/restore", status_code=202)
async def restore_repository(value: RestoreRepository, request: Request):
    if request.app.state.jobs.active():
        raise HTTPException(
            409, "Wait for the active crawl to finish before restoring."
        )
    settings = load_settings()
    target = parse_target(value.url, settings)
    if not target["repo"] or target["path"]:
        raise HTTPException(400, "Select a repository URL from the excluded list.")
    url = repository_url(settings.base_url, target["project"], target["repo"])
    with connection() as db:
        if not db.execute(
            "SELECT 1 FROM excluded_repositories WHERE url=?", (url,)
        ).fetchone():
            raise HTTPException(404, "Repository is not in the excluded list.")
        row = db.execute(
            "INSERT INTO tracked_projects(project_url,server,project) VALUES(?,?,?) "
            "ON CONFLICT(server,project) DO UPDATE SET project_url=excluded.project_url RETURNING id",
            (target["url"], settings.base_url, target["project"]),
        ).fetchone()
        project_id = row["id"]
        db.execute("DELETE FROM excluded_repositories WHERE url=?", (url,))
    try:
        return request.app.state.jobs.start([project_id], [target])
    except Exception:
        with connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO excluded_repositories(url) VALUES(?)", (url,)
            )
        raise
