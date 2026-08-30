"""Read-only, compact repository status for the shared notification panel."""

from __future__ import annotations

from django.db.models import Case, Count, F, IntegerField, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from core.logging import redact_log_text

_ACTIVE_SYNC = (RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
_ACTIVE_EXTRACTION = (PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
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
    "dirty_working_tree": "Local changes prevented refresh. Resolve them before trying again.",
    "local_commits_detected": "Local commits prevented a safe refresh. Review the managed checkout.",
    "history_diverged": "Local and remote history diverged. Review the managed checkout.",
    "worker_unavailable": "The background worker could not start. Try refreshing again.",
    "worker_interrupted": "The background worker stopped before finishing. Try refreshing again.",
    "worker_error": "The repository worker encountered an unexpected error. Review local diagnostics.",
    "database_busy": "The local database was busy. Try refreshing again after current work finishes.",
    "missing_pdf_file": "A tracked PDF is missing from the managed checkout. Refresh to restore it.",
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
            "error_code",
            "requested_at",
            "started_at",
            "heartbeat_at",
            "completed_at",
        )
    }
    annotations.update(
        terminal_status=Subquery(terminal.values("status")[:1]),
        terminal_at=Subquery(terminal.values("outcome_at")[:1]),
        successful_at=Subquery(successful.values("completed_at")[:1]),
    )
    # Do not read free-form job messages, errors, repository URLs, or local paths.
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


def _extraction_states():
    # An old revision's queued job must not keep a newly published catalogue busy.
    current = PDFExtractionJob.objects.filter(
        status__in=_ACTIVE_EXTRACTION,
        document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        document__local_policy__isnull=True,
        target_git_blob_id=F("document__git_blob_id"),
        target_relative_path=F("document__relative_path"),
        target_file_size=F("document__file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
    ).filter(Q(document__repository__enabled=True) | Q(status=PDFExtractionJobStatus.RUNNING))
    return {
        row["document__repository_id"]: row
        for row in current.values("document__repository_id")
        .annotate(
            active_count=Count("pk"),
            running_count=Count("pk", filter=Q(status=PDFExtractionJobStatus.RUNNING)),
            updated_at=Max(Coalesce("heartbeat_at", "started_at", "requested_at")),
        )
        .order_by()
    }


def _busy_status(repository):
    phase = repository["job_phase"]
    state = repository["sync_state"]
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


def repository_notification_statuses() -> dict[str, object]:
    """Return every repository's latest state in three SELECT-only queries.

    Unlike the repository rail's recovery-aware snapshot, this reader neither
    interrupts stale leases nor queues work, initializes schedules, or reads Git.
    """

    repositories = _repository_rows()
    if not repositories:
        return {"total": 0, "activeCount": 0, "failedCount": 0, "items": []}
    document_states = _document_states()
    extraction_states = _extraction_states()
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
        items.append(
            {
                "id": repository["id"],
                "name": redact_log_text(name)[:200] or f"Repository {repository['id']}",
                "status": status,
                "statusLabel": label,
                "statusTone": tone,
                "updatedAt": _iso(updated_at),
                "lastSuccessAt": _iso(
                    _latest_time(repository["last_sync_successful_at"], repository["successful_at"])
                ),
                "lastOutcome": repository["terminal_status"]
                if repository["terminal_status"] in _TERMINAL_SYNC
                else None,
                "lastOutcomeAt": _iso(repository["terminal_at"]),
                "detail": detail,
                "targetPath": f"{target}?repository={repository['id']}",
                "statusTargetPath": status_target,
                "active": tone == "progress" or bool(extraction.get("active_count", 0)),
            }
        )
    return {
        "total": len(items),
        "activeCount": sum(item["active"] for item in items),
        "failedCount": sum(item["statusTone"] == "error" for item in items),
        "items": items,
    }
