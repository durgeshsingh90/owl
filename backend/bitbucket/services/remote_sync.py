"""Queue processing for clone-free Bitbucket REST API metadata refreshes."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from bitbucket.models import (
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
)
from bitbucket.services.api import BitbucketAPIClient, BitbucketAPIError
from bitbucket.services.catalog import refresh_catalog
from bitbucket.services.credentials import CredentialError, resolve_credential

_worker_lock = threading.Lock()
_worker_process: subprocess.Popen[bytes] | None = None
logger = logging.getLogger("owl.bitbucket.metadata_sync")


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
            output="",
        )
        if claimed:
            return SyncJob.objects.select_related("repository").get(pk=candidate.pk)


def _progress(repository: Repository):
    def publish(completed: int, total: int, failed: int) -> None:
        Repository.objects.filter(pk=repository.pk).update(
            status_message=(f"Crawling PDFs · {completed} of {total} · {failed} failed")[:500]
        )

    return publish


def _fail_job(job: SyncJob, *, code: str, message: str, authentication: bool) -> None:
    now = timezone.now()
    status = SyncJobStatus.AUTH_REQUIRED if authentication else SyncJobStatus.FAILED
    state = RepositoryState.AUTH_REQUIRED if authentication else RepositoryState.FAILED
    SyncJob.objects.filter(pk=job.pk).update(
        status=status,
        error_code=code,
        error_message=message[:1000],
        finished_at=now,
    )
    Repository.objects.filter(pk=job.repository_id).update(
        state=state,
        status_message=message[:500],
        error_message=message[:1000],
    )


def process_job(job: SyncJob) -> SyncJob:
    repository = job.repository
    logger.info(
        "metadata_sync_started job_id=%s repository_id=%s operation=%s",
        job.pk,
        repository.pk,
        job.operation,
    )
    Repository.objects.filter(pk=repository.pk).update(
        state=RepositoryState.TESTING,
        status_message="Testing authenticated Bitbucket API access…",
        error_message="",
    )
    try:
        credential = resolve_credential(repository)
        with BitbucketAPIClient(
            repository,
            credential.token,
            username=credential.username,
            api_base_url=getattr(credential, "api_base_url", ""),
            verify_ssl=getattr(
                credential,
                "verify_ssl",
                bool(getattr(settings, "BITBUCKET_APP_VERIFY_SSL", True)),
            ),
        ) as client:
            client.test_connection()
            Repository.objects.filter(pk=repository.pk).update(
                state=RepositoryState.FETCHING,
                status_message="Fetching PDF and VSDX metadata from Bitbucket…",
            )
            stats = refresh_catalog(
                repository,
                client,
                on_progress=_progress(repository),
            )
    except CredentialError as exc:
        _fail_job(
            job,
            code="authentication_required",
            message=str(exc),
            authentication=True,
        )
    except BitbucketAPIError as exc:
        _fail_job(
            job,
            code=exc.code,
            message=str(exc),
            authentication=exc.code == "authentication_required",
        )
    except Exception:
        logger.exception(
            "metadata_sync_failed job_id=%s repository_id=%s",
            job.pk,
            repository.pk,
        )
        _fail_job(
            job,
            code="catalog_failed",
            message="Repository metadata could not be refreshed.",
            authentication=False,
        )
    else:
        now = timezone.now()
        Repository.objects.filter(pk=repository.pk).update(
            state=RepositoryState.READY,
            status_message=(
                f"Ready · {stats.indexed_pdf_count}/{stats.found_pdf_count} PDFs indexed · "
                f"{stats.failed_pdf_count} failed · {stats.vsdx_count} VSDX"
            )[:500],
            error_message="",
            pdf_count=stats.found_pdf_count,
            indexed_pdf_count=stats.indexed_pdf_count,
            failed_pdf_count=stats.failed_pdf_count,
            vsdx_count=stats.vsdx_count,
            last_successful_sync_at=now,
            last_successful_refresh_on=job.scheduled_for or timezone.localdate(now),
        )
        SyncJob.objects.filter(pk=job.pk).update(
            status=SyncJobStatus.SUCCEEDED,
            output=(
                f"Bitbucket PDF crawl completed: {stats.downloaded_pdf_count} downloaded, "
                f"{stats.unchanged_pdf_count} unchanged, {stats.failed_pdf_count} failed."
            ),
            finished_at=now,
        )
        logger.info(
            "metadata_sync_finished job_id=%s repository_id=%s pdf_count=%s vsdx_count=%s",
            job.pk,
            repository.pk,
            stats.found_pdf_count,
            stats.vsdx_count,
        )
    return SyncJob.objects.select_related("repository").get(pk=job.pk)


def work_one_job() -> SyncJob | None:
    job = claim_next_job()
    return process_job(job) if job else None


def wake_sync_worker() -> bool:
    """Start one short-lived worker when OWL is not already servicing the queue."""

    global _worker_process
    with _worker_lock:
        if _worker_process is not None and _worker_process.poll() is None:
            return False
        manage_py = Path(settings.BASE_DIR) / "manage.py"
        kwargs: dict[str, object] = {
            "cwd": str(settings.BASE_DIR),
            "stdin": subprocess.DEVNULL,
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
                str(getattr(settings, "BITBUCKET_APP_WORKER_IDLE_SECONDS", 30)),
            ),
            **kwargs,
        )
        logger.info("metadata_worker_started pid=%s", _worker_process.pid)
        return True
