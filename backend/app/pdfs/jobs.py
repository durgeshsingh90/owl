"""One cancellable crawl job, owned by the FastAPI lifecycle."""

import asyncio
import json
import time
import uuid

from app.core.config import load_settings
from app.core.database import connection
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient
from app.pdfs.crawler import crawl_repository, now


class Jobs:
    def __init__(self):
        self.task = None
        self.current = None

    def active(self):
        return self.task is not None and not self.task.done()

    def start(self, project_ids=None):
        if self.active():
            raise ValueError("A crawl is already running.")
        settings = load_settings()
        with connection() as db:
            projects = [
                dict(row) for row in db.execute("SELECT * FROM tracked_projects")
            ]
        if project_ids is not None:
            if set(project_ids) - {p["id"] for p in projects}:
                raise ValueError("Unknown project ID.")
            projects = [p for p in projects if p["id"] in project_ids]
        if not projects:
            raise ValueError("Add a project before starting a crawl.")
        if any(p["server"] != settings.base_url for p in projects):
            raise ValueError(
                "Tracked projects belong to a different server. Select projects for the configured server."
            )
        self.current = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "started_at": now(),
            "completed_at": None,
            "repositories": 0,
            "repositories_done": 0,
            "repositories_failed": 0,
            "found": 0,
            "processed": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "detail": "Queued",
        }
        self.save()
        self.task = asyncio.create_task(self.run(settings, projects))
        return self.current.copy()

    def save(self):
        with connection() as db:
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,progress=excluded.progress",
                (self.current["id"], self.current["status"], json.dumps(self.current)),
            )

    async def run(self, settings, projects):
        client = BitbucketClient(settings)
        started = time.monotonic()
        p = self.current
        p["status"] = "running"
        event("crawl.started", job_id=p["id"])
        try:
            repos = []
            for project in projects:
                async for repo in client.pages(
                    "/projects/" + project["project"] + "/repos"
                ):
                    slug = repo["slug"]
                    with connection() as db:
                        row = db.execute(
                            "INSERT INTO repositories(project_id,repo,name) VALUES(?,?,?) "
                            "ON CONFLICT(project_id,repo) DO UPDATE SET name=excluded.name RETURNING id",
                            (project["id"], slug, repo.get("name", slug)),
                        ).fetchone()
                    repos.append((project["project"], slug, row["id"]))
            p["repositories"] = len(repos)
            self.save()
            # Bounded repository concurrency; HTTP is async and SQLite transactions are short.
            semaphore = asyncio.Semaphore(settings.max_workers)

            async def scan(project, slug, repository_id):
                async with semaphore:
                    p["detail"] = f"Indexing {project}/{slug}"
                    progress = {"save": self.save}

                    class Counters(dict):
                        def __getitem__(self, key):
                            return progress[key] if key == "save" else p[key]

                        def __setitem__(self, key, value):
                            p[key] = value

                    try:
                        await crawl_repository(
                            client, project, slug, repository_id, Counters()
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - isolate parser/upstream failures without leaking secrets
                        event(
                            "crawl.repository_failed",
                            level=40,
                            job_id=p["id"],
                            repository_id=repository_id,
                            **error_details(error),
                        )
                        p["repositories_failed"] += 1
                    finally:
                        p["repositories_done"] += 1
                        p["elapsed_seconds"] = round(time.monotonic() - started, 1)
                        remaining = len(repos) - p["repositories_done"]
                        p["eta_seconds"] = round(
                            p["elapsed_seconds"] / p["repositories_done"] * remaining
                        )
                        self.save()

            await asyncio.gather(*(scan(*repo) for repo in repos))
            p["status"] = (
                "succeeded_with_errors"
                if p["failed"] or p["repositories_failed"]
                else "succeeded"
            )
            p["detail"] = "Crawl completed."
        except asyncio.CancelledError:
            p["status"] = "cancelled"
            p["detail"] = "Crawl stopped; completed documents are saved."
        except Exception as error:  # noqa: BLE001 - isolate parser/upstream failures without leaking secrets
            event("crawl.failed", level=40, job_id=p["id"], **error_details(error))
            p["status"] = "failed"
            p["detail"] = (
                "Repository discovery failed. Check Bitbucket access and retry."
            )
        finally:
            await client.close()
            p["completed_at"] = now()
            p["elapsed_seconds"] = round(time.monotonic() - started, 1)
            p["eta_seconds"] = None
            event(
                "crawl.completed",
                job_id=p["id"],
                status=p["status"],
                processed=p["processed"],
                failed=p["failed"],
                elapsed_seconds=p["elapsed_seconds"],
            )
            self.save()

    async def shutdown(self):
        if self.active():
            self.task.cancel()
            await self.task
