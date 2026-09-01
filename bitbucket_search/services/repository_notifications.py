"""Read-only, compact repository status for the shared notification panel."""

from __future__ import annotations

from django.db.models import (
    Avg,
    Case,
    Count,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services.git_output import bounded_git_output
from bitbucket_search.services.repository_activity import summarize_operation_activities
from bitbucket_search.services.repository_worker_timing import (
    current_pdf_extraction_jobs,
    running_pdf_start_filter,
    worker_timing,
)
from core.logging import redact_log_text

_ACTIVE_SYNC = (RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
_FAILED_SYNC = (RepositorySyncJobStatus.FAILED, RepositorySyncJobStatus.INTERRUPTED)
_TERMINAL_SYNC = (
    RepositorySyncJobStatus.SUCCEEDED,
    RepositorySyncJobStatus.FAILED,
    RepositorySyncJobStatus.INTERRUPTED,
    RepositorySyncJobStatus.CANCELLED,
)
_FAILED_REPOSITORY = (
    RepositorySyncState.FAILED,
    RepositorySyncState.INTERRUPTED,
    RepositorySyncState.BLOCKED_DIRTY,
)
_BUSY_REPOSITORY = (
    RepositorySyncState.QUEUED,
    RepositorySyncState.CLONING,
    RepositorySyncState.FETCHING,
    RepositorySyncState.UPDATING,
)
_FAILURE_DETAILS = {
    "connection_timeout": "Connection check timed out. Connect to VPN if required, then Refresh.",
    "connection_unreachable": "The repository is unreachable. Check VPN/network access, then Refresh.",
    "connection_auth_failed": "Git authentication or repository permission failed. Check Git access.",
    "connection_tls_failed": "Git could not verify the server certificate. Check corporate TLS setup.",
    "connection_check_failed": "Git connection check failed. Check VPN, network and repository access.",
    "dirty_working_tree": "Local changes prevented refresh. Resolve them before trying again.",
    "local_commits_detected": "Local commits prevented a safe refresh. Review the managed checkout.",
    "history_diverged": "Local and remote history diverged. Review the managed checkout.",
    "worker_unavailable": "The background worker could not start. Try refreshing again.",
    "worker_interrupted": "The background worker stopped before finishing. Try refreshing again.",
    "worker_error": "The repository worker encountered an unexpected error. Review local diagnostics.",
    "database_busy": "The local database was busy. Try refreshing again after current work finishes.",
    "missing_pdf_file": "A tracked PDF is missing from the managed checkout. Refresh to restore it.",
    "document_missing": (
        "A downloaded file or folder could not be read. Check storage availability, then retry."
    ),
    "document_path_too_long": (
        "A document path is too long. Check Windows long-path support or use shorter storage paths."
    ),
    "document_path_unavailable": (
        "Windows could not access a long document path. Check long-path support and storage access."
    ),
    "document_access_denied": (
        "A downloaded file or folder is inaccessible. Check permissions or programs locking it."
    ),
    "document_scan_failed": "The downloaded files could not be scanned. Check storage access.",
    "checkout_publish_failed": (
        "Git downloaded the repository, but its folder could not be moved into place. "
        "Check folder locks, permissions, and that temporary storage is on the same drive."
    ),
    "invalid_git_environment": "Git's inherited configuration is invalid. Check GIT_CONFIG_COUNT.",
    "pdf_path_too_long": "A tracked PDF path is too long. Review storage and Git long-path support.",
    "pdf_file_access_denied": "A tracked PDF could not be accessed. Check permissions or file locks.",
    "pdf_file_read_failed": "A tracked PDF could not be read. Check storage access and retry.",
    "pdf_catalog_mismatch": "The synchronized PDF inventory could not be reconciled safely.",
    "pdf_catalog_failed": "The PDF catalogue could not be read from the managed repository.",
    "clone_failed": "The repository download failed. Check Git access and connectivity, then retry.",
    "fetch_failed": "Repository updates could not be fetched. Check Git access and connectivity.",
    "fast_forward_failed": "Repository updates could not be applied safely. Review the checkout.",
    "sparse_checkout_failed": "The document checkout could not be updated safely.",
    "git_lfs_download_failed": "Git LFS document content could not be downloaded.",
}


def _latest_time(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _iso(value):
    return value.isoformat() if value is not None else None


def _repository_rows():
    jobs = RepositorySyncJob.objects.filter(repository_id=OuterRef("pk"))
    selected = jobs.order_by(
        Case(
            When(status__in=_ACTIVE_SYNC, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        "-requested_at",
        "-id",
    )
    terminal = (
        jobs.exclude(status__in=_ACTIVE_SYNC)
        .annotate(outcome_at=Coalesce("completed_at", "started_at", "requested_at"))
        .order_by("-outcome_at", "-id")
    )
    successful = jobs.filter(
        status=RepositorySyncJobStatus.SUCCEEDED, completed_at__isnull=False
    ).order_by("-completed_at", "-id")
    annotations = {
        f"job_{field}": Subquery(selected.values(field)[:1])
        for field in (
            "status",
            "phase",
            "operation",
            "progress",
            "error_code",
            "requested_at",
            "started_at",
            "heartbeat_at",
            "completed_at",
            "output_log",
            "output_log_updated_at",
        )
    }
    annotations.update(
        terminal_status=Subquery(terminal.values("status")[:1]),
        terminal_at=Subquery(terminal.values("outcome_at")[:1]),
        successful_at=Subquery(successful.values("completed_at")[:1]),
    )
    # Only the selected job's output is read, and it is sanitized before exposing
    # its two-line preview. Do not read message/error or repository URL/path fields.
    return tuple(
        BitbucketRepository.objects.annotate(**annotations)
        .order_by("display_name", "id")
        .values(
            "id",
            "display_name",
            "enabled",
            "sync_state",
            "last_error_code",
            "created_at",
            "updated_at",
            "last_sync_started_at",
            "last_sync_completed_at",
            "last_sync_successful_at",
            *annotations,
        )
    )


def _document_states():
    current = PDFDocument.objects.filter(
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        local_policy__isnull=True,
    )
    return {
        row["repository_id"]: row
        for row in current.values("repository_id")
        .annotate(
            pending_count=Count("pk", filter=Q(index_state=PDFIndexState.PENDING)),
            failed_count=Count(
                "pk",
                filter=Q(
                    index_state__in=(
                        PDFIndexState.FAILED,
                        PDFIndexState.STALE_ERROR,
                        PDFIndexState.PARTIAL,
                    )
                ),
            ),
            last_attempt_at=Max("last_index_attempt_at"),
            last_indexed_at=Max("last_indexed_at"),
        )
        .order_by()
    }


def _extraction_states(*, observed_at):
    # An old revision's queued job must not keep a newly published catalogue busy.
    current = current_pdf_extraction_jobs()
    return {
        row["document__repository_id"]: row
        for row in current.values("document__repository_id")
        .annotate(
            active_count=Count("pk"),
            running_count=Count("pk", filter=Q(status=PDFExtractionJobStatus.RUNNING)),
            running_started_at=Min("started_at", filter=running_pdf_start_filter(observed_at)),
            running_progress=Avg("progress", filter=running_pdf_start_filter(observed_at)),
            queued_count=Count("pk", filter=Q(status=PDFExtractionJobStatus.QUEUED)),
            updated_at=Max(Coalesce("heartbeat_at", "started_at", "requested_at")),
        )
        .order_by()
    }


def _busy_status(repository):
    phase = repository["job_phase"]
    state = repository["sync_state"]
    if phase == RepositorySyncPhase.CHECKING_CONNECTION:
        return (
            "checking_connection",
            "Checking connection",
            "Testing Git access before downloading.",
        )
    if repository["job_status"] == RepositorySyncJobStatus.QUEUED or (
        repository["job_status"] is None and state == RepositorySyncState.QUEUED
    ):
        return "queued", "Queued", "Waiting for a background repository worker."
    if phase in (RepositorySyncPhase.DISCOVERING, RepositorySyncPhase.FINALIZING):
        return "cataloging", "Updating catalogue", "Finding and publishing repository documents."
    if phase == RepositorySyncPhase.CLONING or (
        phase in (None, RepositorySyncPhase.QUEUED, RepositorySyncPhase.VALIDATING)
        and (
            repository["job_operation"] == RepositorySyncOperation.CLONE
            or state == RepositorySyncState.CLONING
        )
    ):
        return (
            "cloning",
            "Downloading repository",
            "The initial repository download is in progress.",
        )
    return "refreshing", "Refreshing repository", "Checking for and applying repository updates."


def _status(repository, documents, extraction):
    job_status = repository["job_status"]
    state = repository["sync_state"]
    if job_status in _ACTIVE_SYNC or (job_status is None and state in _BUSY_REPOSITORY):
        return (*_busy_status(repository), "progress")
    if job_status in _FAILED_SYNC or (job_status is None and state in _FAILED_REPOSITORY):
        error_code = (
            repository["job_error_code"]
            if job_status is not None
            else repository["last_error_code"]
        )
        if error_code in {"dirty_working_tree", "history_diverged", "local_commits_detected"} or (
            job_status is None and state == RepositorySyncState.BLOCKED_DIRTY
        ):
            return (
                "failed",
                "Blocked by repository history"
                if error_code in {"history_diverged", "local_commits_detected"}
                else "Blocked by local changes",
                _FAILURE_DETAILS.get(
                    error_code,
                    "Local changes prevented refresh. Resolve them before trying again.",
                ),
                "error",
            )
        label = (
            "Refresh interrupted"
            if job_status == RepositorySyncJobStatus.INTERRUPTED
            or (job_status is None and state == RepositorySyncState.INTERRUPTED)
            else "Refresh failed"
        )
        detail = _FAILURE_DETAILS.get(
            error_code,
            "The repository could not be synchronized. Open repository status for details.",
        )
        return "failed", label, detail, "error"
    if extraction.get("active_count", 0):
        label = "Reading PDF text" if extraction["running_count"] else "PDF indexing queued"
        return (
            "indexing",
            label,
            "Repository files are available; PDF text indexing is pending.",
            "progress",
        )
    if documents.get("failed_count", 0):
        count = documents["failed_count"]
        noun = "PDF needs" if count == 1 else "PDFs need"
        return (
            "failed",
            "PDF indexing needs attention",
            f"{count} {noun} another text-indexing attempt.",
            "error",
        )
    if not repository["enabled"] or state == RepositorySyncState.DISABLED:
        return (
            "disabled",
            "Disabled",
            "This repository is excluded from automatic refresh.",
            "neutral",
        )
    if documents.get("pending_count", 0):
        return (
            "indexing",
            "PDF indexing pending",
            "Repository files are available; PDF text indexing is pending.",
            "progress",
        )
    if job_status == RepositorySyncJobStatus.CANCELLED:
        return (
            "cancelled",
            "Refresh cancelled",
            "The latest repository sync was cancelled.",
            "neutral",
        )
    if job_status == RepositorySyncJobStatus.SUCCEEDED or state == RepositorySyncState.READY:
        return (
            "ready",
            "Up to date",
            "The latest repository sync completed successfully.",
            "success",
        )
    return (
        "pending",
        "Not synced yet",
        "The repository has not completed its first sync.",
        "neutral",
    )


def _valid_running_start(value, observed_at):
    if value is None or timezone.is_naive(value) or value > observed_at:
        return None
    return value.isoformat()


def _sync_activity(repository, *, observed_at):
    status = repository["job_status"]
    operation_value = repository["job_operation"]
    if status not in _ACTIVE_SYNC or operation_value not in RepositorySyncOperation.values:
        return None
    operation = "clone" if operation_value == RepositorySyncOperation.CLONE else "pull"
    state = "running" if status == RepositorySyncJobStatus.RUNNING else "queued"
    display_status, label, detail = _busy_status(repository)
    phase = repository["job_phase"] or display_status
    if state == "queued":
        phase = "queued"
        label = f"Git {'clone' if operation == 'clone' else 'pull'} queued"
    progress_value = repository["job_progress"] if state == "running" else None
    progress = max(0, min(int(progress_value), 100)) if progress_value is not None else None
    started_at = (
        _valid_running_start(repository["job_started_at"], observed_at)
        if state == "running"
        else None
    )
    return {
        "operation": operation,
        "active": True,
        "state": state,
        "phase": phase,
        "phaseLabel": label,
        "detail": detail,
        "progress": progress,
        "progressScope": "job" if progress is not None else None,
        "startedAt": started_at,
        "observedAt": observed_at.isoformat() if started_at else None,
        "queuedJobs": 1 if state == "queued" else 0,
        "runningJobs": 1 if state == "running" else 0,
        "count": 1,
    }


def _indexing_activity(extraction, *, observed_at):
    active_count = max(0, int(extraction.get("active_count") or 0))
    if not active_count:
        return None
    running_jobs = max(0, int(extraction.get("running_count") or 0))
    queued_jobs = max(0, int(extraction.get("queued_count") or 0))
    state = "running" if running_jobs else "queued"
    progress_value = extraction.get("running_progress") if running_jobs else None
    progress = (
        max(0, min(int(round(float(progress_value))), 100)) if progress_value is not None else None
    )
    started_at = (
        _valid_running_start(extraction.get("running_started_at"), observed_at)
        if running_jobs
        else None
    )
    if running_jobs:
        phase = "extracting"
        label = "Extracting PDF text"
    else:
        phase = "pdf_queued"
        label = "PDF indexing queued"
    details = []
    if running_jobs:
        details.append(f"{running_jobs} PDF{'s' if running_jobs != 1 else ''} extracting")
    if queued_jobs:
        details.append(f"{queued_jobs} PDF{'s' if queued_jobs != 1 else ''} queued")
    return {
        "operation": "indexing",
        "active": True,
        "state": state,
        "phase": phase,
        "phaseLabel": label,
        "detail": " · ".join(details) or label,
        "progress": progress,
        "progressScope": "running_workers_average" if progress is not None else None,
        "startedAt": started_at,
        "observedAt": observed_at.isoformat() if started_at else None,
        "queuedJobs": queued_jobs,
        "runningJobs": running_jobs,
        "count": active_count,
    }


def repository_notification_statuses(*, at=None) -> dict[str, object]:
    """Return every repository's latest state in three SELECT-only queries.

    Unlike the repository rail's recovery-aware snapshot, this reader neither
    interrupts stale leases nor queues work, initializes schedules, or reads Git.
    """

    observed_at = at or timezone.now()
    repositories = _repository_rows()
    if not repositories:
        return {
            "total": 0,
            "activeCount": 0,
            "failedCount": 0,
            "activities": [],
            "items": [],
        }
    document_states = _document_states()
    extraction_states = _extraction_states(observed_at=observed_at)
    target = reverse("bitbucket_search:index")
    status_target = reverse("bitbucket_search:index_status")
    items = []
    for repository in repositories:
        documents = document_states.get(repository["id"], {})
        extraction = extraction_states.get(repository["id"], {})
        status, label, detail, tone = _status(repository, documents, extraction)
        updated_at = (
            _latest_time(
                repository["job_completed_at"],
                repository["job_output_log_updated_at"],
                repository["job_heartbeat_at"],
                repository["job_started_at"],
                repository["job_requested_at"],
                repository["last_sync_completed_at"],
                repository["last_sync_started_at"],
                documents.get("last_attempt_at"),
                documents.get("last_indexed_at"),
                extraction.get("updated_at"),
            )
            or repository["updated_at"]
            or repository["created_at"]
        )
        name = " ".join(
            "".join(
                character for character in repository["display_name"] if character.isprintable()
            ).split()
        )
        log_output, _ = bounded_git_output(repository["job_output_log"] or "")
        activities = tuple(
            activity
            for activity in (
                _sync_activity(repository, observed_at=observed_at),
                _indexing_activity(extraction, observed_at=observed_at),
            )
            if activity is not None
        )
        primary_activity = activities[0] if activities else None
        items.append(
            {
                "id": repository["id"],
                "name": redact_log_text(name)[:200] or f"Repository {repository['id']}",
                "status": status,
                "statusLabel": label,
                "statusTone": tone,
                "workerTiming": worker_timing(
                    observed_at=observed_at,
                    sync_status=repository["job_status"],
                    sync_started_at=repository["job_started_at"],
                    sync_operation=repository["job_operation"],
                    sync_phase=repository["job_phase"],
                    sync_progress=repository["job_progress"],
                    indexing_started_at=extraction.get("running_started_at"),
                    indexing_progress=extraction.get("running_progress"),
                ),
                "operation": primary_activity["operation"] if primary_activity else None,
                "phase": primary_activity["phase"] if primary_activity else None,
                "phaseLabel": primary_activity["phaseLabel"] if primary_activity else None,
                "progress": primary_activity["progress"] if primary_activity else None,
                "activity": primary_activity,
                "activities": activities,
                "updatedAt": _iso(updated_at),
                "lastSuccessAt": _iso(
                    _latest_time(repository["last_sync_successful_at"], repository["successful_at"])
                ),
                "lastOutcome": repository["terminal_status"]
                if repository["terminal_status"] in _TERMINAL_SYNC
                else None,
                "lastOutcomeAt": _iso(repository["terminal_at"]),
                "detail": detail,
                "logPreview": log_output.splitlines()[-2:],
                "targetPath": f"{target}?repository={repository['id']}",
                "statusTargetPath": f"{status_target}?repository={repository['id']}",
                "logsUrl": reverse("bitbucket_search:repository_logs", args=(repository["id"],)),
                "cancelIndexingUrl": reverse(
                    "bitbucket_search:repository_index_cancel", args=(repository["id"],)
                ),
                "active": tone == "progress" or bool(extraction.get("active_count", 0)),
            }
        )
    return {
        "total": len(items),
        "activeCount": sum(item["active"] for item in items),
        "failedCount": sum(item["statusTone"] == "error" for item in items),
        "activities": summarize_operation_activities(
            activity for item in items for activity in item["activities"]
        ),
        "items": items,
    }
