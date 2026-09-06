"""HTTP interface for the PDF crawler and document library."""

import asyncio
import json

from app.core.config import Settings, load_settings, parse_project, save_settings
from app.core.database import connection
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
    client = BitbucketClient(value)
    try:
        return await client.test()
    except (BitbucketError, asyncio.TimeoutError) as error:
        raise HTTPException(502, str(error) or "Connection timed out.") from None
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


@router.delete("/repositories/{repository_id}")
def delete_repo(repository_id: int, value: DeleteRequest, request: Request):
    if value.confirmation != "delete all":
        raise HTTPException(
            400, "Type delete all to confirm local repository deletion."
        )
    if request.app.state.jobs.active():
        raise HTTPException(409, "Stop the active crawl before deleting a repository.")
    with connection() as db:
        if (
            db.execute("DELETE FROM repositories WHERE id=?", (repository_id,)).rowcount
            == 0
        ):
            raise HTTPException(404, "Repository not found.")
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
                "SELECT * FROM failed_documents ORDER BY last_attempt DESC LIMIT ? OFFSET ?",
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
