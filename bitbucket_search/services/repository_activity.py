"""Compact read-only activity descriptions using the same jobs as lifecycle locks."""

from __future__ import annotations

from collections import Counter, defaultdict

from django.db.models import Count, Q

from bitbucket_search.models import (
    BitbucketRepository,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
)
from bitbucket_search.services.repository_worker_timing import current_pdf_extraction_jobs


def _sync_stage(operation: str, phase: str) -> tuple[str, str]:
    if phase == RepositorySyncPhase.CHECKING_CONNECTION:
        return "checking_connection", "Checking connection"
    if phase in (RepositorySyncPhase.DISCOVERING, RepositorySyncPhase.FINALIZING):
        return "cataloguing", "Updating catalogue"
    if phase == RepositorySyncPhase.VALIDATING:
        return "validating", "Preparing sync"
    if operation == RepositorySyncOperation.CLONE:
        return "cloning", "Cloning repository"
    return "pulling", "Pulling updates"


def _pdf_detail(queued: int, running: int, cleanup: int) -> str:
    parts = []
    if running:
        parts.append(f"{running} PDF{'s' if running != 1 else ''} extracting")
    if queued:
        parts.append(f"{queued} PDF{'s' if queued != 1 else ''} queued")
    if cleanup:
        parts.append(f"{cleanup} earlier PDF job{'s' if cleanup != 1 else ''} awaiting cleanup")
    return " · ".join(parts)


def with_repository_activity(
    repositories: tuple[BitbucketRepository, ...],
) -> tuple[BitbucketRepository, ...]:
    """Attach work labels without weakening queued/running-job safety or running Git.

    Repository sync state describes Git only; READY can coexist with active PDF
    work. Count every active job for lifecycle safety, while explicitly labeling
    obsolete PDF targets awaiting the existing worker queue reconciliation.
    """

    repository_ids = tuple(repository.pk for repository in repositories)
    if not repository_ids:
        return repositories
    sync_counts: dict[int, Counter] = defaultdict(Counter)
    running_stages = {}
    for row in (
        RepositorySyncJob.objects.filter(
            repository_id__in=repository_ids,
            status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING),
        )
        .values("repository_id", "status", "operation", "phase")
        .annotate(total=Count("id"))
        .order_by()
    ):
        sync_counts[row["repository_id"]][row["status"]] += row["total"]
        if row["status"] == RepositorySyncJobStatus.RUNNING:
            running_stages[row["repository_id"]] = _sync_stage(row["operation"], row["phase"])

    pdf_counts: dict[int, Counter] = defaultdict(Counter)
    current_pdf_counts: dict[int, Counter] = defaultdict(Counter)
    for row in (
        PDFExtractionJob.objects.filter(
            document__repository_id__in=repository_ids,
            status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
        )
        .values("document__repository_id", "status")
        .annotate(
            total=Count("id"),
            current=Count("id", filter=Q(pk__in=current_pdf_extraction_jobs().values("pk"))),
        )
        .order_by()
    ):
        pdf_counts[row["document__repository_id"]][row["status"]] += row["total"]
        current_pdf_counts[row["document__repository_id"]][row["status"]] += row["current"]

    for repository in repositories:
        sync = sync_counts[repository.pk]
        pdfs = pdf_counts[repository.pk]
        current_pdfs = current_pdf_counts[repository.pk]
        cleanup = sum(pdfs.values()) - sum(current_pdfs.values())
        queued_sync = sync[RepositorySyncJobStatus.QUEUED]
        running_sync = sync[RepositorySyncJobStatus.RUNNING]
        queued_pdfs = pdfs[PDFExtractionJobStatus.QUEUED]
        running_pdfs = pdfs[PDFExtractionJobStatus.RUNNING]
        repository.has_active_work = bool(
            repository.has_active_sync or queued_sync or running_sync or queued_pdfs or running_pdfs
        )
        details = _pdf_detail(
            current_pdfs[PDFExtractionJobStatus.QUEUED],
            current_pdfs[PDFExtractionJobStatus.RUNNING],
            cleanup,
        )
        if running_sync:
            kind = "sync"
            phase, label = running_stages[repository.pk]
        elif current_pdfs[PDFExtractionJobStatus.RUNNING]:
            kind, phase, label = "indexing", "extracting", "Extracting PDF text"
        elif running_pdfs:
            kind, phase, label = "cleanup", "cleanup_pending", "Finishing earlier PDF jobs"
        elif queued_sync:
            kind, phase, label = "sync", "sync_queued", "Git sync queued"
        elif current_pdfs[PDFExtractionJobStatus.QUEUED]:
            kind, phase, label = "indexing", "pdf_queued", "PDF extraction queued"
        elif cleanup:
            kind, phase, label = "cleanup", "cleanup_pending", "PDF queue cleanup pending"
        elif repository.has_active_work:
            kind, phase, label = "sync", "sync_pending", "Git sync pending"
            details = "Repository is marked busy; waiting for a worker job."
        else:
            kind, phase, label = "idle", "idle", "Idle"
            details = "No queued or running Git or PDF jobs."
        if kind == "sync":
            details = f"{label} · {details}" if details else label
        if queued_sync and phase != "sync_queued":
            details = " · ".join(part for part in (details, "Git sync queued") if part)
        repository.activity = {
            "active": repository.has_active_work,
            "kind": kind,
            "phase": phase,
            "label": label,
            "detail": details,
            "queuedPdfs": queued_pdfs,
            "runningPdfs": running_pdfs,
            "queuedSyncJobs": queued_sync,
            "runningSyncJobs": running_sync,
            "pendingCleanupJobs": cleanup,
        }
    return repositories


def repository_work_summary(repositories: tuple[BitbucketRepository, ...]) -> dict[str, object]:
    """Bound the toolbar text while counting activity across every repository."""

    active = tuple(repository for repository in repositories if repository.has_active_work)
    labels = tuple(dict.fromkeys(repository.activity["label"] for repository in active))
    label = " · ".join(labels[:2])
    if len(labels) > 2:
        label += f" · +{len(labels) - 2} stages"
    entries = []
    for repository in active[:2]:
        activity = repository.activity
        detail = f"{repository.display_name}: {activity['label']}"
        if activity["detail"].startswith(f"{activity['label']} · "):
            detail = f"{repository.display_name}: {activity['detail']}"
        elif activity["detail"] and activity["detail"] != activity["label"]:
            detail += f" — {activity['detail']}"
        entries.append(detail)
    if len(active) > 2:
        entries.append(f"+{len(active) - 2} more repositor{'y' if len(active) == 3 else 'ies'}")
    return {
        "active": bool(active),
        "label": label or "Idle",
        "detail": " · ".join(entries) or "Repository and PDF workers are idle.",
        "activeRepositories": len(active),
        "queuedPdfs": sum(repository.activity["queuedPdfs"] for repository in active),
        "runningPdfs": sum(repository.activity["runningPdfs"] for repository in active),
    }
