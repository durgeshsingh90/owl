"""One cancellable crawl job, owned by the FastAPI lifecycle."""

import asyncio
import json
import multiprocessing
import time
import uuid
from concurrent.futures import ProcessPoolExecutor

from app.core.config import load_settings
from app.core.database import connection
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient
from app.pdfs.crawler import crawl_repository, discover_pdfs, now


class Jobs:
    def __init__(self):
        self.task = None
        self.current = None

    def active(self):
        return self.task is not None and not self.task.done()

    def start(self, project_ids=None, targets=None, auto_retry=True):
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
            "repository_statuses": {},
            "repositories_succeeded": 0,
            "discovery_complete": False,
            "discovery_failed": False,
            "repositories_failed": 0,
            "found": 0,
            "processed": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "failed": 0,
            "retry_total": 0,
            "retry_processed": 0,
            "retry_recovered": 0,
            "retry_active": False,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "detail": "Queued",
            "bitbucket_connected": False,
        }
        self.save()
        self.task = asyncio.create_task(
            self.run(settings, projects, targets, auto_retry)
        )
        return self.current.copy()

    def save(self):
        with connection() as db:
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,progress=excluded.progress",
                (self.current["id"], self.current["status"], json.dumps(self.current)),
            )

    async def run(self, settings, projects, targets=None, auto_retry=True):
        client = BitbucketClient(settings)
        extractor = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        )
        client.extractor = extractor
        started = time.monotonic()
        p = self.current

        def repo_status(repository_id, status):
            p["repository_statuses"][str(repository_id)]["status"] = status
            self.save()

        def connected():
            if not p["bitbucket_connected"]:
                p["bitbucket_connected"] = True
                self.save()

        client.on_connected = connected
        p["status"] = "running"
        p["detail"] = "Discovering repositories"
        self.save()
        event("crawl.started", job_id=p["id"])
        try:
            repos = []
            for project in projects:
                selected = [
                    t for t in (targets or []) if t["project"] == project["project"]
                ]

                async def repositories(selected=selected, project=project):
                    if selected and all(t["repo"] for t in selected):
                        for slug in dict.fromkeys(t["repo"] for t in selected):
                            yield {"slug": slug}
                    else:
                        async for item in client.pages(
                            "/projects/" + project["project"] + "/repos"
                        ):
                            yield item

                async for repo in repositories():
                    slug = repo["slug"]
                    with connection() as db:
                        row = db.execute(
                            "INSERT INTO repositories(project_id,repo,name) VALUES(?,?,?) "
                            "ON CONFLICT(project_id,repo) DO UPDATE SET name=excluded.name RETURNING id",
                            (project["id"], slug, repo.get("name", slug)),
                        ).fetchone()
                    repos.append((project["project"], slug, row["id"]))
                    p["repository_statuses"][str(row["id"])] = {
                        "project_id": str(project["id"]),
                        "repo": slug,
                        "status": "queued",
                    }
                    self.save()
            p["repositories"] = len(repos)
            self.save()
            # Bounded repository concurrency; HTTP is async and SQLite transactions are short.
            semaphore = asyncio.Semaphore(settings.max_workers)

            work = []

            async def discover(project, slug, repository_id):
                async with semaphore:
                    repo_status(repository_id, "scanning")
                    p["detail"] = f"Finding PDFs in {project}/{slug}"
                    self.save()
                    paths = []
                    try:
                        selected = [
                            t
                            for t in (targets or [])
                            if t["project"] == project and t["repo"] in (None, slug)
                        ]
                        if selected and all(t["path"] for t in selected):
                            paths = list(dict.fromkeys(t["path"] for t in selected))
                        else:
                            async for path in discover_pdfs(client, project, slug):
                                paths.append(path)
                        work.append((project, slug, repository_id, paths))
                        p["found"] += len(paths)
                        repo_status(repository_id, "queued")
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - isolate repository discovery
                        p["repositories_failed"] += 1
                        p["repositories_done"] += 1
                        p["discovery_failed"] = True
                        repo_status(repository_id, "failed")
                        event(
                            "crawl.discovery_failed",
                            level=40,
                            repository_id=repository_id,
                            **error_details(error),
                        )
                    self.save()

            await asyncio.gather(*(discover(*repo) for repo in repos))
            p["discovery_complete"] = True
            processing_started = time.monotonic()
            self.save()

            def progress_save():
                p["elapsed_seconds"] = round(time.monotonic() - started, 1)
                if p["processed"]:
                    remaining = p["found"] - p["processed"]
                    p["eta_seconds"] = round(
                        (time.monotonic() - processing_started)
                        / p["processed"]
                        * remaining
                    )
                self.save()

            failed_paths = {}
            hard_failures = set()

            async def scan(project, slug, repository_id, paths):
                async with semaphore:
                    repo_status(repository_id, "processing")
                    p["detail"] = f"Indexing {project}/{slug}"
                    local_failed = False

                    class Counters(dict):
                        def __getitem__(self, key):
                            if key == "failure":
                                return lambda path: failed_paths.setdefault(
                                    (project, slug, repository_id), set()
                                ).add(path)
                            return progress_save if key == "save" else p[key]

                        def __setitem__(self, key, value):
                            nonlocal local_failed
                            if key == "failed":
                                local_failed = True
                            p[key] = value

                    try:
                        await crawl_repository(
                            client, project, slug, repository_id, Counters(), paths
                        )
                    except asyncio.CancelledError:
                        repo_status(repository_id, "cancelled")
                        raise
                    except Exception as error:  # noqa: BLE001 - isolate repository processing
                        local_failed = True
                        hard_failures.add(repository_id)
                        event(
                            "crawl.repository_failed",
                            level=40,
                            repository_id=repository_id,
                            **error_details(error),
                        )
                    else:
                        if not local_failed:
                            p["repositories_succeeded"] += 1
                    finally:
                        if local_failed:
                            p["repositories_failed"] += 1
                        if (
                            p["repository_statuses"][str(repository_id)]["status"]
                            != "cancelled"
                        ):
                            repo_status(
                                repository_id, "failed" if local_failed else "succeeded"
                            )
                        p["repositories_done"] += 1
                        progress_save()

            await asyncio.gather(*(scan(*repo) for repo in work))
            # Exactly one additional pass, scoped to failures from this job.
            if auto_retry and failed_paths:
                p["retry_active"] = True
                p["retry_total"] = sum(len(paths) for paths in failed_paths.values())
                retry_started = time.monotonic()
                self.save()
                for (project, slug, repository_id), paths in failed_paths.items():
                    repo_status(repository_id, "retrying")
                    remaining_failures = len(paths)
                    for path in sorted(paths):
                        p["detail"] = f"Automatic retry: {project}/{slug}/{path}"
                        self.save()
                        counters = {
                            "new": 0,
                            "updated": 0,
                            "unchanged": 0,
                            "failed": 0,
                            "processed": 0,
                            "save": lambda: None,
                            "failure": lambda path: None,
                        }
                        await crawl_repository(
                            client, project, slug, repository_id, counters, [path]
                        )
                        p["retry_processed"] += 1
                        if not counters["failed"]:
                            remaining_failures -= 1
                            p["failed"] -= 1
                            p["retry_recovered"] += 1
                            for outcome in ("new", "updated", "unchanged"):
                                p[outcome] += counters[outcome]
                        p["elapsed_seconds"] = round(time.monotonic() - started, 1)
                        p["eta_seconds"] = round(
                            (time.monotonic() - retry_started)
                            / p["retry_processed"]
                            * (p["retry_total"] - p["retry_processed"])
                        )
                        self.save()
                    if not remaining_failures and repository_id not in hard_failures:
                        p["repositories_failed"] -= 1
                        p["repositories_succeeded"] += 1
                        repo_status(repository_id, "succeeded")
                    else:
                        repo_status(repository_id, "failed")
                p["retry_active"] = False

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
            for repository in p["repository_statuses"].values():
                if repository["status"] in {
                    "queued",
                    "scanning",
                    "processing",
                    "retrying",
                }:
                    repository["status"] = (
                        "cancelled" if p["status"] == "cancelled" else "failed"
                    )
            extractor.shutdown(wait=False, cancel_futures=True)
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
