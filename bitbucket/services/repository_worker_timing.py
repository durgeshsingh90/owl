"""Read-only elapsed-time evidence from durable repository and PDF workers."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Avg, Count, F, Min, OuterRef, Q, Subquery
from django.utils import timezone

from bitbucket.models import (
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
)
from bitbucket.services.pdf_extractor import PDF_EXTRACTOR_VERSION


def current_pdf_extraction_jobs():
    """Select active jobs that still describe the current, locally eligible PDF."""

    return PDFExtractionJob.objects.filter(
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
        document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        document__local_policy__isnull=True,
        target_git_blob_id=F("document__git_blob_id"),
        target_relative_path=F("document__relative_path"),
        target_file_size=F("document__file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
    ).filter(Q(document__repository__enabled=True) | Q(status=PDFExtractionJobStatus.RUNNING))


def latest_repository_pdf_run_jobs():
    """Select attempts belonging to each repository's newest explicit indexing run."""

    latest_run = (
        PDFExtractionJob.objects.filter(
            document__repository_id=OuterRef("document__repository_id"),
            run_id__isnull=False,
        )
        .order_by("-requested_at", "-id")
        .values("run_id")[:1]
    )
    return (
        PDFExtractionJob.objects.annotate(_latest_repository_run_id=Subquery(latest_run))
        .filter(
            Q(run_id=F("_latest_repository_run_id"))
            | Q(run_id__isnull=True, _latest_repository_run_id__isnull=True)
        )
        .filter(
            document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            document__local_policy__isnull=True,
            target_git_blob_id=F("document__git_blob_id"),
            target_relative_path=F("document__relative_path"),
            target_file_size=F("document__file_size"),
            target_extractor_version=PDF_EXTRACTOR_VERSION,
        )
    )


def running_pdf_start_filter(observed_at: datetime) -> Q:
    return Q(status=PDFExtractionJobStatus.RUNNING, started_at__lte=observed_at)


def running_pdf_worker_starts(*, observed_at: datetime) -> dict[int, datetime]:
    """One batched query for the longest-running current PDF worker per repository."""

    return {
        row["document__repository_id"]: row["started_at"]
        for row in current_pdf_extraction_jobs()
        .filter(running_pdf_start_filter(observed_at))
        .values("document__repository_id")
        .annotate(started_at=Min("started_at"))
        .order_by()
    }


def running_pdf_worker_activity(*, observed_at: datetime) -> dict[int, dict[str, object]]:
    """One batched query for the timer and honest active-worker progress per repository."""

    return {
        row["document__repository_id"]: {
            "started_at": row["started_at"],
            "progress": int(round(row["progress"])) if row["progress"] is not None else None,
            "running_jobs": row["running_jobs"],
        }
        for row in current_pdf_extraction_jobs()
        .filter(running_pdf_start_filter(observed_at))
        .values("document__repository_id")
        .annotate(
            started_at=Min("started_at"),
            progress=Avg("progress"),
            running_jobs=Count("id"),
        )
        .order_by()
    }


def worker_timing(
    *,
    observed_at: datetime,
    sync_status: str | None = None,
    sync_started_at: datetime | None = None,
    sync_operation: str | None = None,
    sync_phase: str | None = None,
    sync_progress: int | None = None,
    indexing_started_at: datetime | None = None,
    indexing_progress: int | None = None,
) -> dict[str, object] | None:
    """Use a real RUNNING job's start, never queue time or repository state."""

    if sync_status == RepositorySyncJobStatus.RUNNING:
        started_at = sync_started_at
        kind = "sync"
        operation = "clone" if sync_operation == RepositorySyncOperation.CLONE else "pull"
        progress = sync_progress
        if sync_phase == RepositorySyncPhase.CHECKING_CONNECTION:
            label = "Checking connection"
        elif sync_phase in (RepositorySyncPhase.DISCOVERING, RepositorySyncPhase.FINALIZING):
            label = "Updating catalogue"
        elif sync_operation == RepositorySyncOperation.CLONE:
            label = "Downloading"
        else:
            label = "Refreshing"
    else:
        started_at = indexing_started_at
        kind = "indexing"
        operation = "indexing"
        progress = indexing_progress
        label = "Indexing PDFs"
    if (
        not isinstance(started_at, datetime)
        or timezone.is_naive(started_at)
        or started_at > observed_at
    ):
        return None
    return {
        "startedAt": started_at.isoformat(),
        "observedAt": observed_at.isoformat(),
        "label": label,
        "kind": kind,
        "operation": operation,
        "phase": sync_phase if kind == "sync" else "extracting",
        "phaseLabel": label,
        "progress": max(0, min(int(progress), 100)) if progress is not None else None,
        "progressScope": "job" if kind == "sync" else "running_workers_average",
    }
