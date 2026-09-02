"""Compact read-only activity descriptions using the same jobs as lifecycle locks."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime

from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
)
from bitbucket_search.services.repository_worker_timing import current_pdf_extraction_jobs

_VISIBLE_OPERATIONS = ("clone", "pull", "indexing")
_OPERATION_LABELS = {
    "clone": "Git clone",
    "pull": "Git pull",
    "indexing": "PDF indexing",
}


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


def _iso_start(started_at: object, observed_at: datetime) -> str | None:
    if (
        not isinstance(started_at, datetime)
        or timezone.is_naive(started_at)
        or started_at > observed_at
    ):
        return None
    return started_at.isoformat()


def _operation_activity(
    *,
    operation: str,
    state: str,
    phase: str,
    phase_label: str,
    detail: str,
    progress: object,
    progress_scope: str,
    started_at: object,
    observed_at: datetime,
    queued_jobs: int,
    running_jobs: int,
) -> dict[str, object]:
    started = _iso_start(started_at, observed_at) if state == "running" else None
    known_progress = (
        max(0, min(int(round(float(progress))), 100))
        if state == "running" and progress is not None
        else None
    )
    return {
        "operation": operation,
        "active": True,
        "state": state,
        "phase": phase,
        "phaseLabel": phase_label,
        "detail": detail,
        "progress": known_progress,
        "progressScope": progress_scope if known_progress is not None else None,
        "startedAt": started,
        "observedAt": observed_at.isoformat() if started else None,
        "queuedJobs": queued_jobs,
        "runningJobs": running_jobs,
        "count": queued_jobs + running_jobs,
    }


def summarize_operation_activities(
    activities: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Aggregate the three user-visible operations without fabricating queued progress."""

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for activity in activities:
        operation = str(activity.get("operation") or "")
        if operation in _VISIBLE_OPERATIONS and activity.get("active"):
            grouped[operation].append(activity)
    summaries = []
    for operation in _VISIBLE_OPERATIONS:
        entries = grouped[operation]
        if not entries:
            continue
        queued_jobs = sum(max(0, int(entry.get("queuedJobs") or 0)) for entry in entries)
        running_jobs = sum(max(0, int(entry.get("runningJobs") or 0)) for entry in entries)
        progress_total = 0
        progress_weight = 0
        for entry in entries:
            progress = entry.get("progress")
            weight = max(0, int(entry.get("runningJobs") or 0))
            if progress is not None and weight:
                progress_total += int(progress) * weight
                progress_weight += weight
        progress = round(progress_total / progress_weight) if progress_weight else None
        starts = [str(entry["startedAt"]) for entry in entries if entry.get("startedAt")]
        observations = [str(entry["observedAt"]) for entry in entries if entry.get("observedAt")]
        phase_labels = tuple(
            dict.fromkeys(str(entry["phaseLabel"]) for entry in entries if entry.get("phaseLabel"))
        )
        label = _OPERATION_LABELS[operation]
        parts = [
            f"{len(entries)} repositor{'y' if len(entries) == 1 else 'ies'}",
        ]
        if running_jobs:
            parts.append(f"{running_jobs} running")
        if queued_jobs:
            parts.append(f"{queued_jobs} queued")
        if progress is not None:
            scope = "worker" if running_jobs == 1 else "running-worker average"
            parts.append(f"{progress}% {scope}")
        if phase_labels:
            parts.append(" / ".join(phase_labels[:2]))
        summaries.append(
            {
                "operation": operation,
                "active": True,
                "state": "running" if running_jobs else "queued",
                "label": label,
                "detail": " · ".join(parts),
                "count": queued_jobs + running_jobs,
                "activeRepositories": len(entries),
                "queuedJobs": queued_jobs,
                "runningJobs": running_jobs,
                "progress": progress,
                "progressScope": "running_jobs_average" if progress is not None else None,
                "startedAt": min(starts) if starts else None,
                "observedAt": max(observations) if observations else None,
            }
        )
    return summaries


def with_repository_activity(
    repositories: tuple[BitbucketRepository, ...],
    *,
    at: datetime | None = None,
) -> tuple[BitbucketRepository, ...]:
    """Attach work labels without weakening queued/running-job safety or running Git.

    Repository sync state describes Git only; READY can coexist with active PDF
    work. Count every active job for lifecycle safety, while explicitly labeling
    obsolete PDF targets awaiting the existing worker queue reconciliation.
    """

    repository_ids = tuple(repository.pk for repository in repositories)
    if not repository_ids:
        return repositories
    observed_at = at or timezone.now()
    failed_pdf_counts = dict(
        PDFDocument.objects.filter(
            repository_id__in=repository_ids,
            lifecycle_state="active",
            index_state__in=(PDFIndexState.FAILED, PDFIndexState.STALE_ERROR),
        )
        .values("repository_id")
        .annotate(total=Count("id"))
        .values_list("repository_id", "total")
    )
    pdf_attempt_counts: dict[int, Counter] = defaultdict(Counter)
    for row in (
        PDFExtractionJob.objects.filter(document__repository_id__in=repository_ids)
        .values("document__repository_id", "status")
        .annotate(total=Count("id"))
        .order_by()
    ):
        pdf_attempt_counts[row["document__repository_id"]][row["status"]] = row["total"]
    sync_counts: dict[int, Counter] = defaultdict(Counter)
    sync_jobs: dict[int, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in (
        RepositorySyncJob.objects.filter(
            repository_id__in=repository_ids,
            status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING),
        )
        .values(
            "id",
            "repository_id",
            "status",
            "operation",
            "phase",
            "progress",
            "started_at",
            "heartbeat_at",
        )
        .order_by("repository_id", "id")
    ):
        sync_counts[row["repository_id"]][row["status"]] += 1
        sync_jobs[row["repository_id"]][row["status"]] = row

    pdf_counts: dict[int, Counter] = defaultdict(Counter)
    current_pdf_counts: dict[int, Counter] = defaultdict(Counter)
    pdf_metrics: dict[int, dict[str, dict[str, object]]] = defaultdict(dict)
    current_job_ids = current_pdf_extraction_jobs().values("pk")
    current_running = Q(
        pk__in=current_job_ids,
        status=PDFExtractionJobStatus.RUNNING,
        started_at__lte=observed_at,
    )
    for row in (
        PDFExtractionJob.objects.filter(
            document__repository_id__in=repository_ids,
            status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
        )
        .values("document__repository_id", "status")
        .annotate(
            total=Count("id"),
            current=Count("id", filter=Q(pk__in=current_job_ids)),
            current_progress=Avg("progress", filter=current_running),
            current_started_at=Min("started_at", filter=current_running),
            current_heartbeat_at=Max("heartbeat_at", filter=current_running),
        )
        .order_by()
    ):
        pdf_counts[row["document__repository_id"]][row["status"]] += row["total"]
        current_pdf_counts[row["document__repository_id"]][row["status"]] += row["current"]
        pdf_metrics[row["document__repository_id"]][row["status"]] = row

    for repository in repositories:
        repository.pdf_index_failed_count = failed_pdf_counts.get(repository.pk, 0)
        repository.git_sync_failed = bool(
            repository.last_error_code or repository.sync_state == "failed"
        )
        sync = sync_counts[repository.pk]
        pdfs = pdf_counts[repository.pk]
        current_pdfs = current_pdf_counts[repository.pk]
        cleanup = sum(pdfs.values()) - sum(current_pdfs.values())
        queued_sync = sync[RepositorySyncJobStatus.QUEUED]
        running_sync = sync[RepositorySyncJobStatus.RUNNING]
        queued_pdfs = pdfs[PDFExtractionJobStatus.QUEUED]
        running_pdfs = pdfs[PDFExtractionJobStatus.RUNNING]
        running_sync_job = sync_jobs[repository.pk].get(RepositorySyncJobStatus.RUNNING)
        queued_sync_job = sync_jobs[repository.pk].get(RepositorySyncJobStatus.QUEUED)
        repository.has_active_work = bool(
            repository.has_active_sync or queued_sync or running_sync or queued_pdfs or running_pdfs
        )
        details = _pdf_detail(
            current_pdfs[PDFExtractionJobStatus.QUEUED],
            current_pdfs[PDFExtractionJobStatus.RUNNING],
            cleanup,
        )
        operations = []
        sync_job = running_sync_job or queued_sync_job
        sync_activity = None
        if sync_job is not None:
            sync_phase, sync_label = _sync_stage(sync_job["operation"], sync_job["phase"])
            sync_operation = (
                "clone" if sync_job["operation"] == RepositorySyncOperation.CLONE else "pull"
            )
            sync_running = sync_job["status"] == RepositorySyncJobStatus.RUNNING
            operation_phase = sync_phase if sync_running else "queued"
            operation_label = (
                sync_label
                if sync_running
                else f"Git {'clone' if sync_operation == 'clone' else 'pull'} queued"
            )
            sync_activity = _operation_activity(
                operation=sync_operation,
                state="running" if sync_running else "queued",
                phase=operation_phase,
                phase_label=operation_label,
                detail=operation_label,
                progress=sync_job["progress"],
                progress_scope="job",
                started_at=sync_job["started_at"],
                observed_at=observed_at,
                queued_jobs=queued_sync,
                running_jobs=running_sync,
            )
            operations.append(sync_activity)
        current_queued = current_pdfs[PDFExtractionJobStatus.QUEUED]
        current_running = current_pdfs[PDFExtractionJobStatus.RUNNING]
        indexing_activity = None
        if current_queued or current_running:
            running_metrics = pdf_metrics[repository.pk].get(PDFExtractionJobStatus.RUNNING, {})
            indexing_activity = _operation_activity(
                operation="indexing",
                state="running" if current_running else "queued",
                phase="extracting" if current_running else "pdf_queued",
                phase_label="Extracting PDF text" if current_running else "PDF extraction queued",
                detail=_pdf_detail(current_queued, current_running, 0),
                progress=running_metrics.get("current_progress"),
                progress_scope="running_workers_average",
                started_at=running_metrics.get("current_started_at"),
                observed_at=observed_at,
                queued_jobs=current_queued,
                running_jobs=current_running,
            )
            operations.append(indexing_activity)
        if running_sync:
            kind = "sync"
            phase, label = _sync_stage(running_sync_job["operation"], running_sync_job["phase"])
            primary_activity = sync_activity
        elif current_pdfs[PDFExtractionJobStatus.RUNNING]:
            kind, phase, label = "indexing", "extracting", "Extracting PDF text"
            primary_activity = indexing_activity
        elif running_pdfs:
            kind, phase, label = "cleanup", "cleanup_pending", "Finishing earlier PDF jobs"
            primary_activity = None
        elif queued_sync:
            kind, phase, label = "sync", "sync_queued", "Git sync queued"
            primary_activity = sync_activity
        elif current_pdfs[PDFExtractionJobStatus.QUEUED]:
            kind, phase, label = "indexing", "pdf_queued", "PDF extraction queued"
            primary_activity = indexing_activity
        elif cleanup:
            kind, phase, label = "cleanup", "cleanup_pending", "PDF queue cleanup pending"
            primary_activity = None
        elif repository.has_active_work:
            kind, phase, label = "sync", "sync_pending", "Git sync pending"
            details = "Repository is marked busy; waiting for a worker job."
            primary_activity = None
        else:
            kind, phase, label = "idle", "idle", "Idle"
            details = "No queued or running Git or PDF jobs."
            primary_activity = None
        if kind == "sync":
            details = f"{label} · {details}" if details else label
        if queued_sync and phase != "sync_queued":
            details = " · ".join(part for part in (details, "Git sync queued") if part)
        repository.activity = {
            "active": repository.has_active_work,
            "kind": kind,
            "operation": primary_activity["operation"] if primary_activity else None,
            "phase": phase,
            "phaseLabel": label,
            "label": label,
            "detail": details,
            "progress": primary_activity["progress"] if primary_activity else None,
            "progressScope": primary_activity["progressScope"] if primary_activity else None,
            "startedAt": primary_activity["startedAt"] if primary_activity else None,
            "observedAt": primary_activity["observedAt"] if primary_activity else None,
            "operations": operations,
            "queuedPdfs": queued_pdfs,
            "runningPdfs": running_pdfs,
            "queuedSyncJobs": queued_sync,
            "runningSyncJobs": running_sync,
            "pendingCleanupJobs": cleanup,
            "pdfCounts": {
                "queued": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.QUEUED],
                "running": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.RUNNING],
                "passed": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.SUCCEEDED],
                "failed": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.FAILED],
                "interrupted": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.INTERRUPTED],
                "cancelled": pdf_attempt_counts[repository.pk][PDFExtractionJobStatus.CANCELLED],
            },
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
        "activities": summarize_operation_activities(
            operation for repository in active for operation in repository.activity["operations"]
        ),
    }
