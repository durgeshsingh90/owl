"""HTTPS Git preflight, clone, pull, and durable job processing."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from bitbucket.models import (
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)
from bitbucket.services.catalog import refresh_catalog
from bitbucket.services.repository_urls import parse_repository_url

_OUTPUT_LIMIT = 16_000
_worker_lock = threading.Lock()
_worker_process: subprocess.Popen[bytes] | None = None


class GitSyncError(RuntimeError):
    def __init__(self, message: str, *, code: str = "git_failed", output: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.output = output[-_OUTPUT_LIMIT:]


def repository_path(repository: Repository) -> Path:
    root = Path(settings.BITBUCKET_APP_REPOSITORIES_ROOT).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_slug = (
        "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in repository.slug
        ).strip("-")[:80]
        or "repository"
    )
    target = (root / f"{repository.pk}-{safe_slug}").resolve()
    if target.parent != root:
        raise GitSyncError("The managed repository path is unsafe.", code="unsafe_path")
    return target


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "LC_ALL": "C",
        }
    )
    return environment


def _run(arguments: Sequence[str], *, timeout: int) -> str:
    completed = subprocess.run(
        tuple(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_environment(),
        timeout=timeout,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace")[-_OUTPUT_LIMIT:]
    if completed.returncode:
        raise GitSyncError(
            output.strip() or "Git did not complete successfully.",
            output=output,
        )
    return output


def test_connection(repository: Repository) -> str:
    """Always run before clone or pull; this is the sole connection proof."""

    timeout = int(getattr(settings, "BITBUCKET_APP_CONNECTION_TIMEOUT_SECONDS", 30))
    try:
        return _run(
            (
                "git",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "ls-remote",
                "--symref",
                "--",
                repository.url,
                "HEAD",
            ),
            timeout=timeout,
        )
    except (GitSyncError, subprocess.TimeoutExpired, OSError) as exc:
        output = exc.output if isinstance(exc, GitSyncError) else str(exc)
        raise GitSyncError(
            "Git could not reach or authenticate to this HTTPS repository.",
            code="connection_failed",
            output=output,
        ) from exc


def _clone(repository: Repository, target: Path) -> str:
    if target.exists():
        raise GitSyncError("A managed checkout already exists.", code="checkout_exists")
    temporary = target.parent / f".{target.name}-{uuid.uuid4().hex}.clone"
    try:
        output = _run(
            (
                "git",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "clone",
                "--no-tags",
                "--",
                repository.url,
                str(temporary),
            ),
            timeout=int(getattr(settings, "BITBUCKET_APP_GIT_TIMEOUT_SECONDS", 3600)),
        )
        os.replace(temporary, target)
        return output
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _pull(repository: Repository, target: Path) -> str:
    if not (target / ".git").is_dir():
        raise GitSyncError("The managed checkout is missing.", code="checkout_missing")
    origin = _run(
        ("git", "-C", str(target), "remote", "get-url", "origin"),
        timeout=30,
    ).strip()
    try:
        canonical_origin = parse_repository_url(origin).url
    except ValueError as exc:
        raise GitSyncError(
            "The checkout origin is not a safe HTTPS URL.", code="origin_changed"
        ) from exc
    if canonical_origin != repository.canonical_url:
        raise GitSyncError(
            "The checkout origin no longer matches the saved URL.", code="origin_changed"
        )
    return _run(
        ("git", "-C", str(target), "pull", "--ff-only"),
        timeout=int(getattr(settings, "BITBUCKET_APP_GIT_TIMEOUT_SECONDS", 3600)),
    )


def claim_next_job() -> SyncJob | None:
    while True:
        candidate = (
            SyncJob.objects.filter(status=SyncJobStatus.QUEUED).order_by("created_at").first()
        )
        if candidate is None:
            return None
        now = timezone.now()
        claimed = SyncJob.objects.filter(pk=candidate.pk, status=SyncJobStatus.QUEUED).update(
            status=SyncJobStatus.RUNNING,
            started_at=now,
            finished_at=None,
            attempt_count=candidate.attempt_count + 1,
            error_code="",
            error_message="",
        )
        if claimed:
            return SyncJob.objects.select_related("repository").get(pk=candidate.pk)


def process_job(job: SyncJob) -> SyncJob:
    repository = job.repository
    target = repository_path(repository)
    Repository.objects.filter(pk=repository.pk).update(
        state=RepositoryState.TESTING,
        status_message="Testing HTTPS access with git ls-remote…",
        error_message="",
    )
    try:
        preflight_output = test_connection(repository)
        next_state = (
            RepositoryState.CLONING
            if job.operation == SyncOperation.CLONE
            else RepositoryState.PULLING
        )
        Repository.objects.filter(pk=repository.pk).update(
            state=next_state,
            status_message=(
                "Cloning repository…"
                if job.operation == SyncOperation.CLONE
                else "Pulling today's changes…"
            ),
        )
        transport_output = (
            _clone(repository, target)
            if job.operation == SyncOperation.CLONE
            else _pull(repository, target)
        )
        pdf_count, vsdx_count = refresh_catalog(repository, target)
    except GitSyncError as exc:
        now = timezone.now()
        if exc.code == "connection_failed":
            job_status = SyncJobStatus.AUTH_REQUIRED
            repository_state = RepositoryState.AUTH_REQUIRED
            message = "Authentication or firewall access is required."
        else:
            job_status = SyncJobStatus.FAILED
            repository_state = RepositoryState.FAILED
            message = str(exc)[:1000]
        SyncJob.objects.filter(pk=job.pk).update(
            status=job_status,
            error_code=exc.code,
            error_message=message,
            output=exc.output,
            finished_at=now,
        )
        Repository.objects.filter(pk=repository.pk).update(
            state=repository_state,
            status_message=message[:500],
            error_message=message,
        )
    except Exception as exc:
        now = timezone.now()
        message = "Repository documents could not be catalogued."
        SyncJob.objects.filter(pk=job.pk).update(
            status=SyncJobStatus.FAILED,
            error_code="catalog_failed",
            error_message=message,
            output=str(exc)[-_OUTPUT_LIMIT:],
            finished_at=now,
        )
        Repository.objects.filter(pk=repository.pk).update(
            state=RepositoryState.FAILED,
            status_message=message,
            error_message=message,
        )
    else:
        now = timezone.now()
        repository_updates: dict[str, object] = {
            "state": RepositoryState.READY,
            "status_message": f"Ready · {pdf_count} PDF · {vsdx_count} VSDX",
            "error_message": "",
            "pdf_count": pdf_count,
            "vsdx_count": vsdx_count,
            "last_successful_sync_at": now,
        }
        if job.operation == SyncOperation.CLONE:
            repository_updates["last_successful_pull_on"] = timezone.localdate(now)
        elif job.scheduled_for:
            repository_updates["last_successful_pull_on"] = job.scheduled_for
        Repository.objects.filter(pk=repository.pk).update(**repository_updates)
        SyncJob.objects.filter(pk=job.pk).update(
            status=SyncJobStatus.SUCCEEDED,
            output=(preflight_output + transport_output)[-_OUTPUT_LIMIT:],
            finished_at=now,
        )
    return SyncJob.objects.select_related("repository").get(pk=job.pk)


def work_one_job() -> SyncJob | None:
    job = claim_next_job()
    return process_job(job) if job else None


def wake_sync_worker() -> bool:
    """Start one short-lived worker when OWL is not already servicing this queue."""

    global _worker_process
    with _worker_lock:
        if _worker_process is not None and _worker_process.poll() is None:
            return False
        manage_py = Path(settings.BASE_DIR) / "manage.py"
        kwargs: dict[str, object] = {
            "cwd": str(settings.BASE_DIR),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        _worker_process = subprocess.Popen(
            (
                sys.executable,
                str(manage_py),
                "bitbucket_document_worker",
                "--idle-timeout",
                "30",
            ),
            **kwargs,
        )
        return True
