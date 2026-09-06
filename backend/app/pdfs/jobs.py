"""One cancellable crawl job, owned by the FastAPI lifecycle."""

import asyncio
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from app.core.config import load_settings
from app.core.database import connection, database_path, repository_url
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient, BitbucketError
from app.pdfs.crawler import crawl_repository, discover_pdfs, now


def remaining_eta(elapsed, processed, total):
    if processed >= total:
        return 0
    if not processed:
        return None
    return round(max(0, elapsed) / processed * (total - processed))


class Jobs:
    def __init__(self):
        self.task = None
        self.current = None
        self.extractor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="owl-pdf")

    def active(self):
        return self.task is not None and not self.task.done()

    def start(self, project_ids=None, targets=None, auto_retry=True, hard_retry=False):
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
        backup_path = None
        if hard_retry:
            if targets:
                raise ValueError("Hard retry must scan whole projects.")
            # Preserve a recoverable snapshot before clearing the selected index.
            backup_path = (
                database_path().parent
                / "backups"
                / f"before-hard-retry-{uuid.uuid4().hex}.db"
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with connection() as source:
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            ids = [project["id"] for project in projects]
            placeholders = ",".join("?" for _ in ids)
            with connection() as db:
                scope = (
                    f"SELECT id FROM repositories WHERE project_id IN ({placeholders})"
                )
                db.execute(
                    f"DELETE FROM documents WHERE repository_id IN ({scope})", ids
                )
                db.execute(
                    f"DELETE FROM failed_documents WHERE repository_id IN ({scope})",
                    ids,
                )
                db.execute(
                    f"UPDATE repositories SET last_scanned=NULL WHERE project_id IN ({placeholders})",
                    ids,
                )
            event("crawl.hard_retry_reset", project_ids=ids, backup=str(backup_path))
        self.current = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "hard_retry": hard_retry,
            "backup_path": str(backup_path) if backup_path else None,
            "started_at": now(),
            "completed_at": None,
            "repositories": 0,
            "repositories_done": 0,
            "repository_statuses": {},
            "repositories_succeeded": 0,
            "discovery_complete": False,
            "discovery_failed": False,
            "folder_failures": [],
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
        client.extractor = self.extractor
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
            for project in sorted(
                projects, key=lambda item: (item["project"].casefold(), item["project"])
            ):
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
                        if db.execute(
                            "SELECT 1 FROM excluded_repositories WHERE url=?",
                            (
                                repository_url(
                                    settings.base_url, project["project"], slug
                                ),
                            ),
                        ).fetchone():
                            continue
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
                        "found": 0,
                        "failed": 0,
                    }
                    self.save()
            p["repositories"] = len(repos)
            self.save()
            # Stable order across API pages, regardless of configured worker count.
            repos.sort(
                key=lambda repo: (
                    repo[0].casefold(),
                    repo[0],
                    repo[1].casefold(),
                    repo[1],
                )
            )

            partial_repositories = set()

            async def discover(project, slug, repository_id):
                repo_status(repository_id, "scanning")
                p["detail"] = f"Finding PDFs in {project}/{slug}"
                self.save()
                paths = []
                seen = set()

                def found(path):
                    if path in seen:
                        return
                    seen.add(path)
                    paths.append(path)
                    p["found"] += 1
                    p["repository_statuses"][str(repository_id)]["found"] = len(paths)
                    p["detail"] = (
                        f"Finding PDFs in {project}/{slug}: {len(paths)} found"
                    )
                    p["elapsed_seconds"] = round(time.monotonic() - started, 1)
                    event(
                        "crawl.pdf_found",
                        repository_id=repository_id,
                        project=project,
                        repo=slug,
                        path=path,
                        found=len(paths),
                    )
                    self.save()

                def folder_failed(folder, error):
                    partial_repositories.add(repository_id)
                    p["discovery_failed"] = True
                    message = (
                        str(error)
                        if isinstance(error, BitbucketError)
                        else "Folder discovery failed; check backend logs."
                    )
                    url = (
                        client.base
                        + client.repo_path(project, slug)
                        + "/browse/"
                        + quote(folder, safe="/")
                    )
                    request_url = getattr(error, "request_url", "")
                    p["folder_failures"].append(
                        {
                            "project": project,
                            "repo": slug,
                            "path": folder or "/",
                            "url": url,
                            "request_url": request_url,
                            "error": message,
                        }
                    )
                    event(
                        "crawl.folder_failed",
                        level=40,
                        repository_id=repository_id,
                        project=project,
                        repo=slug,
                        path=folder or "/",
                        url=url,
                        request_url=request_url,
                        error=message,
                    )
                    self.save()

                try:
                    selected = [
                        t
                        for t in (targets or [])
                        if t["project"] == project and t["repo"] in (None, slug)
                    ]
                    if selected and all(t["path"] for t in selected):
                        for target in selected:
                            found(target["path"])
                    else:
                        async for path in discover_pdfs(
                            client, project, slug, on_folder_error=folder_failed
                        ):
                            found(path)
                    return sorted(paths, key=lambda path: (path.casefold(), path))
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - isolate repository discovery
                    p["repositories_failed"] += 1
                    p["repositories_done"] += 1
                    p["discovery_failed"] = True
                    folder_failed("", error)
                    repo_status(repository_id, "failed")
                    event(
                        "crawl.discovery_failed",
                        level=40,
                        repository_id=repository_id,
                        **error_details(error),
                    )
                self.save()
                return None

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
            hard_failures = partial_repositories

            async def scan(project, slug, repository_id, paths):
                repo_status(repository_id, "processing")
                p["detail"] = f"Indexing {project}/{slug}"
                local_failed = repository_id in partial_repositories
                repository = p["repository_statuses"][str(repository_id)]
                repo_started = time.monotonic()
                repository.update(
                    processed=0, eta_seconds=remaining_eta(0, 0, len(paths))
                )

                def repo_progress():
                    repository["eta_seconds"] = remaining_eta(
                        time.monotonic() - repo_started,
                        repository["processed"],
                        len(paths),
                    )
                    progress_save()

                self.save()

                class Counters(dict):
                    def __getitem__(self, key):
                        if key == "failure":
                            return lambda path: failed_paths.setdefault(
                                (project, slug, repository_id), set()
                            ).add(path)
                        return repo_progress if key == "save" else p[key]

                    def __setitem__(self, key, value):
                        nonlocal local_failed
                        if key == "failed":
                            local_failed = True
                            repository["failed"] += value - p[key]
                        if key == "processed":
                            repository["processed"] += value - p[key]
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
                            repository_id,
                            "retrying"
                            if auto_retry
                            and failed_paths.get((project, slug, repository_id))
                            else "failed"
                            if local_failed
                            else "succeeded",
                        )
                    progress_save()

            async def retry_repository_failures(project, slug, repository_id):
                paths = failed_paths.get((project, slug, repository_id), set())
                if not auto_retry or not paths:
                    return
                p["retry_active"] = True
                p["retry_total"] += len(paths)
                retry_started = time.monotonic()
                self.save()
                repo_status(repository_id, "retrying")
                remaining_failures = len(paths)
                retry_repo_started = time.monotonic()
                retry_repository = p["repository_statuses"][str(repository_id)]
                retry_repository.update(
                    retry_processed=0, retry_total=len(paths), eta_seconds=None
                )
                for path in sorted(paths, key=lambda path: (path.casefold(), path)):
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
                    retry_repository["retry_processed"] += 1
                    retry_repository["eta_seconds"] = remaining_eta(
                        time.monotonic() - retry_repo_started,
                        retry_repository["retry_processed"],
                        len(paths),
                    )
                    if not counters["failed"]:
                        remaining_failures -= 1
                        retry_repository["failed"] -= 1
                        p["failed"] -= 1
                        p["retry_recovered"] += 1
                        for outcome in ("new", "updated", "unchanged"):
                            p[outcome] += counters[outcome]
                    p["elapsed_seconds"] = round(time.monotonic() - started, 1)
                    p["eta_seconds"] = round(
                        (time.monotonic() - retry_started)
                        / retry_repository["retry_processed"]
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

            for repo in repos:
                paths = await discover(*repo)
                if paths is not None:
                    repo_processing_started = time.monotonic()
                    repository = p["repository_statuses"][str(repo[2])]
                    try:
                        await scan(*repo, paths)
                        await retry_repository_failures(*repo)
                    finally:
                        repository["processing_seconds"] = round(
                            time.monotonic() - repo_processing_started, 1
                        )
                        repository["eta_seconds"] = None
                        self.save()
                    p["repositories_done"] += 1
                    self.save()
            p["discovery_complete"] = True
            self.save()

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
