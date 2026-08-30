"""Read-only elapsed-time evidence from durable repository and PDF workers."""

from __future__ import annotations

from datetime import datetime

from django.db.models import F, Min, Q
from django.utils import timezone

from bitbucket_search.models import (
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
)
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION


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


def worker_timing(
    *,
    observed_at: datetime,
    sync_status: str | None = None,
    sync_started_at: datetime | None = None,
    sync_operation: str | None = None,
    sync_phase: str | None = None,
    indexing_started_at: datetime | None = None,
) -> dict[str, str] | None:
    """Use a real RUNNING job's start, never queue time or repository state."""

    if sync_status == RepositorySyncJobStatus.RUNNING:
        started_at = sync_started_at
        kind = "sync"
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
    }
