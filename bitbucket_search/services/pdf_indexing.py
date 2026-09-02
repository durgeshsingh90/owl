"""Durable, isolated PDF extraction orchestration and atomic publication."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositoryOperationLogChannel,
    RepositoryOperationLogEntry,
    RepositoryOperationLogSeverity,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import DocumentActionError, validated_pdf_path
from bitbucket_search.services.filesystem_paths import filesystem_path
from bitbucket_search.services.logging_events import get_logger, log_event, logging_context
from bitbucket_search.services.operation_logs import (
    append_operation_log_entry_safely,
    build_operation_log_entry,
)
from bitbucket_search.services.pdf_extractor import (
    DIAGNOSTIC_REASONS,
    DIAGNOSTIC_STAGES,
    PDF_EXTRACTOR_VERSION,
    PDFExtractionDiagnostic,
)
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    pdf_extraction_claim_lock,
    repository_checkout_lock,
)

PDF_INDEX_VERSION = 1
logger = get_logger("indexing")
_PROGRESS_LOG_STATE: ContextVar[dict[int, tuple[str, int]] | None] = ContextVar(
    "pdf_progress_log_state", default=None
)
_INDEX_LOG_JOB: ContextVar[tuple[int, int, int | None, int | None] | None] = ContextVar(
    "pdf_index_log_job", default=None
)
STALE_EXTRACTION_AFTER = timedelta(minutes=10)
_HASH_CHUNK_BYTES = 1024 * 1024
_HEARTBEAT_SECONDS = 1.0
_QUEUE_WRITE_BATCH_SIZE = 400
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_JOB_STATUSES = (
    PDFExtractionJobStatus.QUEUED,
    PDFExtractionJobStatus.RUNNING,
)
_ACTIVE_REPOSITORY_JOB_STATUSES = (
    RepositorySyncJobStatus.QUEUED,
    RepositorySyncJobStatus.RUNNING,
)
_AUTOMATIC_RETRY_ERROR_CODES = {
    "extraction_worker_interrupted",
    "index_worker_unavailable",
    "pdf_indexing_worker_error",
    "extractor_unavailable",
    "extractor_failed",
}
_PUBLISHABLE_STATES = {
    PDFTextRevisionState.READY,
    PDFTextRevisionState.NO_TEXT,
    PDFTextRevisionState.PARTIAL,
}
_FAILURE_DIAGNOSTICS: Mapping[str, tuple[str, str]] = {
    "encrypted": ("encrypted_pdf", "The PDF is encrypted or password-protected."),
    "corrupt": ("corrupt_pdf", "The PDF is invalid or corrupt."),
    "git_lfs_pointer": (
        "git_lfs_pointer",
        "The file is a Git LFS pointer, not a hydrated PDF.",
    ),
    "disappeared": ("pdf_disappeared", "The PDF disappeared during extraction."),
    "permission_denied": (
        "pdf_permission_denied",
        "OWL does not have permission to read the PDF.",
    ),
    "resource_limit": (
        "pdf_resource_limit",
        "The PDF exceeds a configured extraction resource limit.",
    ),
    "changed_during_extraction": (
        "pdf_changed_during_extraction",
        "The PDF changed during extraction. Refresh its repository and retry.",
    ),
    "unknown_error": ("pdf_unknown_error", "OWL could not extract this PDF safely."),
    "dependency_unavailable": (
        "pdf_dependency_unavailable",
        "The PDF parser or a required dependency could not load. "
        "Install the project requirements in the Python environment running OWL, restart workers, "
        "then Refresh to retry.",
    ),
}
_LOGGABLE_PDF_ERROR_CODES = frozenset(code for code, _summary in _FAILURE_DIAGNOSTICS.values()) | {
    "document_unavailable",
    "extraction_target_changed",
    "extraction_worker_lease_lost",
    "extractor_failed",
    "extractor_output_too_large",
    "extractor_staging_unavailable",
    "extractor_unavailable",
    "invalid_document_path",
    "invalid_extractor_response",
    "invalid_repository_checkout",
    "pdf_checkout_lock_unavailable",
    "pdf_extraction_timeout",
    "pdf_hash_failed",
    "pdf_indexing_worker_error",
    "pdf_revision_conflict",
    "unsupported_document_type",
}


class PDFIndexingError(RuntimeError):
    """A content-free extraction orchestration failure safe for persistence."""

    def __init__(self, code: str, summary: str) -> None:
        self.code = str(code or "pdf_indexing_error")[:64]
        self.summary = " ".join(str(summary or "PDF indexing failed.").split())[:500]
        super().__init__(self.summary)


class PDFExtractionTargetChanged(PDFIndexingError):
    """The durable job no longer describes the document's published source revision."""


class PDFExtractionDeferred(RuntimeError):
    """Repository synchronization takes priority over a claimed extraction."""


class PDFExtractionExcluded(RuntimeError):
    """A local PDF policy supersedes extraction without changing saved text."""


class _PDFRevisionCacheMiss(RuntimeError):
    """A reusable revision was pruned after its unlocked cache lookup."""


@dataclass(frozen=True, slots=True)
class StagedPDFPage:
    page_number: int
    text: str
    character_count: int
    state: str
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class StagedPDFExtraction:
    state: str
    pages: tuple[StagedPDFPage, ...]
    page_count: int
    extracted_character_count: int
    source_size_bytes: int
    content_sha256_before: str
    content_sha256_after: str
    extractor_version: str
    error_code: str = ""
    error_summary: str = ""
    diagnostic: PDFExtractionDiagnostic = field(default_factory=PDFExtractionDiagnostic)

    @property
    def publishable(self) -> bool:
        return self.state in _PUBLISHABLE_STATES


@dataclass(frozen=True, slots=True)
class ExtractionQueueResult:
    queued_job_ids: tuple[int, ...]
    cancelled_job_ids: tuple[int, ...]
    skipped_document_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ExtractionCancellationResult:
    """Exact active-job transitions made by one repository stop request."""

    repository_id: int
    queued_jobs: int
    running_jobs: int
    cancelled_job_ids: tuple[int, ...]

    @property
    def total_jobs(self) -> int:
        return self.queued_jobs + self.running_jobs

    @property
    def state(self) -> str:
        return "cancelled" if self.total_jobs else "idle"


@dataclass(frozen=True, slots=True)
class ExtractionStatusSnapshot:
    queued_jobs: int
    running_jobs: int
    failed_jobs: int
    interrupted_jobs: int
    pending_documents: int
    indexed_documents: int
    stale_documents: int


ExtractionRunner = Callable[[Path, Callable[[], None]], StagedPDFExtraction]


def _logged_indexing_errors(stage: str):
    """Record controller failures even when their durable failure write cannot run."""

    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                log_event(
                    logger, logging.ERROR, "pdf_indexing_operation_failed", error=exc, stage=stage
                )
                raise

        return run

    return decorate


def _safe_summary(value: object, fallback: str) -> str:
    return " ".join(str(value or fallback).split())[:500]


_INDEX_PHASE_MESSAGES = {
    PDFExtractionJobPhase.VALIDATING: "Validating the PDF extraction target.",
    PDFExtractionJobPhase.HASHING: "Hashing the PDF content.",
    PDFExtractionJobPhase.EXTRACTING: "Extracting searchable PDF text.",
    PDFExtractionJobPhase.PUBLISHING: "Publishing searchable PDF text.",
    PDFExtractionJobPhase.COMPLETED: "PDF indexing completed.",
}


def _append_index_log(
    job_id: int,
    *,
    event: str,
    message: str,
    phase: str,
    progress: int | None = None,
    severity: str = RepositoryOperationLogSeverity.INFO,
    job: PDFExtractionJob | None = None,
) -> None:
    """Persist one transition without repeating repository lookups in a live worker."""

    if job is not None:
        repository_id = job.document.repository_id
        sync_job_id = job.repository_sync_job_id
        worker_pid = job.worker_pid
    else:
        context = _INDEX_LOG_JOB.get()
        if context is not None and context[0] == job_id:
            _context_job_id, repository_id, sync_job_id, worker_pid = context
        else:
            identity = (
                PDFExtractionJob.objects.filter(pk=job_id)
                .values(
                    "document__repository_id",
                    "repository_sync_job_id",
                    "worker_pid",
                )
                .first()
            )
            if identity is None:
                return
            repository_id = identity["document__repository_id"]
            sync_job_id = identity["repository_sync_job_id"]
            worker_pid = identity["worker_pid"]
    append_operation_log_entry_safely(
        repository_id=repository_id,
        sync_job_id=sync_job_id,
        extraction_job_id=job_id,
        channel=RepositoryOperationLogChannel.INDEXING,
        severity=severity,
        phase=phase,
        event=event,
        message=message,
        progress=progress,
        worker_pid=worker_pid,
    )


def _job_matches_document(job: PDFExtractionJob, document: PDFDocument) -> bool:
    return (
        job.target_git_blob_id == document.git_blob_id
        and job.target_relative_path == document.relative_path
        and job.target_file_size == document.file_size
        and job.target_extractor_version == PDF_EXTRACTOR_VERSION
    )


def _batches[T](
    values: Sequence[T],
    size: int = _QUEUE_WRITE_BATCH_SIZE,
) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _cancel_active_extraction_jobs(
    job_ids: Sequence[int],
    *,
    observed_at: datetime,
    error_code: str,
    error_summary: str,
    log_message: str,
) -> tuple[int, ...]:
    """Atomically cancel and bulk-log only jobs that win the active-state race."""

    normalized_ids = tuple(
        dict.fromkeys(
            job_id
            for job_id in job_ids
            if isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0
        )
    )
    if not normalized_ids:
        return ()

    with transaction.atomic():
        # Sweep callers are not already inside the repository queue transaction.
        # Reserve SQLite's writer before selecting so two sweepers cannot both
        # observe and log the same active-to-cancelled transition.
        _reserve_sqlite_write()
        identities = tuple(
            PDFExtractionJob.objects.select_for_update()
            .filter(pk__in=normalized_ids, status__in=_ACTIVE_JOB_STATUSES)
            .order_by("id")
            .values(
                "id",
                "document__repository_id",
                "repository_sync_job_id",
                "phase",
                "progress",
                "worker_pid",
            )
        )
        if not identities:
            return ()

        transitioned_ids = tuple(identity["id"] for identity in identities)
        updated = PDFExtractionJob.objects.filter(
            pk__in=transitioned_ids,
            status__in=_ACTIVE_JOB_STATUSES,
        ).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=observed_at,
            error_code=error_code,
            error_summary=error_summary,
        )
        if updated != len(transitioned_ids):
            # Row locks (or SQLite's early writer reservation) make this a
            # defensive path. Never publish a terminal event for a transition
            # that another controller won.
            actual_ids = frozenset(
                PDFExtractionJob.objects.filter(
                    pk__in=transitioned_ids,
                    status=PDFExtractionJobStatus.CANCELLED,
                    completed_at=observed_at,
                    error_code=error_code,
                ).values_list("id", flat=True)
            )
            identities = tuple(identity for identity in identities if identity["id"] in actual_ids)
            transitioned_ids = tuple(identity["id"] for identity in identities)

        RepositoryOperationLogEntry.objects.bulk_create(
            [
                build_operation_log_entry(
                    repository_id=identity["document__repository_id"],
                    sync_job_id=identity["repository_sync_job_id"],
                    extraction_job_id=identity["id"],
                    channel=RepositoryOperationLogChannel.INDEXING,
                    severity=RepositoryOperationLogSeverity.WARNING,
                    phase=identity["phase"],
                    event="indexing_cancelled",
                    message=log_message,
                    progress=identity["progress"],
                    worker_pid=identity["worker_pid"],
                    occurred_at=observed_at,
                )
                for identity in identities
            ],
            batch_size=_QUEUE_WRITE_BATCH_SIZE,
        )
        return transitioned_ids


@_logged_indexing_errors("repository_extraction_cancellation")
def cancel_repository_pdf_extractions(
    repository: BitbucketRepository | int,
) -> ExtractionCancellationResult:
    """Stop only one repository's queued/running PDF work, without touching Git.

    The PDF controller gate serializes this transition against queue claims,
    heartbeats, and publication. A parser already in flight sees its lease loss
    at the next heartbeat (at most one second for the isolated parser), kills
    its parser child, and cannot publish or requeue the cancelled job.
    """

    repository_id = repository.pk if isinstance(repository, BitbucketRepository) else repository
    observed_at = timezone.now()
    with pdf_extraction_claim_lock(), transaction.atomic():
        # Reserve SQLite's writer before reading active rows so cancellation is
        # not exposed to a deferred read-to-write upgrade race.
        _reserve_sqlite_write()
        if not (
            BitbucketRepository.objects.select_for_update()
            .only("id")
            .filter(pk=repository_id)
            .exists()
        ):
            raise BitbucketRepository.DoesNotExist
        active_rows = tuple(
            PDFExtractionJob.objects.select_for_update()
            .filter(
                document__repository_id=repository_id,
                status__in=_ACTIVE_JOB_STATUSES,
            )
            .order_by("id")
            .values("id", "status")
        )
        transitioned: list[int] = []
        for active_batch in _batches(tuple(row["id"] for row in active_rows)):
            transitioned.extend(
                _cancel_active_extraction_jobs(
                    active_batch,
                    observed_at=observed_at,
                    error_code="indexing_cancelled_by_user",
                    error_summary="PDF indexing was stopped by the user.",
                    log_message="PDF indexing was stopped by the user.",
                )
            )
        transitioned_ids = tuple(transitioned)
        transitioned_id_set = frozenset(transitioned_ids)
        queued_jobs = sum(
            row["id"] in transitioned_id_set and row["status"] == PDFExtractionJobStatus.QUEUED
            for row in active_rows
        )
        running_jobs = sum(
            row["id"] in transitioned_id_set and row["status"] == PDFExtractionJobStatus.RUNNING
            for row in active_rows
        )

    # Remove durable parser hand-offs only after the cancellation transaction
    # commits. If SQLite rejects the transition, the writer must still be able
    # to resume and publish the staged result.
    for transitioned_id in transitioned_ids:
        _publication_staging_path(transitioned_id).unlink(missing_ok=True)

    log_event(
        logger,
        logging.INFO,
        "repository_pdf_indexing_cancelled",
        repository_id=repository_id,
        queued_count=queued_jobs,
        running_count=running_jobs,
        count=len(transitioned_ids),
    )
    return ExtractionCancellationResult(
        repository_id=repository_id,
        queued_jobs=queued_jobs,
        running_jobs=running_jobs,
        cancelled_job_ids=transitioned_ids,
    )


def _removal_repository_ids():
    """Keep pending removal journals out of every worker recovery path."""

    return RepositoryRemovalRecovery.objects.values("repository_id")


@_logged_indexing_errors("extraction_queue")
@transaction.atomic
def queue_repository_pdf_extractions(
    repository: BitbucketRepository | int,
    *,
    repository_sync_job: RepositorySyncJob | int | None = None,
    retry_failed: bool = False,
    recover_interrupted: bool = False,
) -> ExtractionQueueResult:
    """Reconcile one repository's current PDFs without per-document queries."""

    started = time.monotonic()
    # Standalone sweep/reindex calls start a deferred transaction here too.
    # Reserve before the first read so publication cannot steal the writer
    # slot between that read and this queue's later insert/update. Do not take
    # the PDF file gate: catalogue publication may already own a DB write lock.
    _reserve_sqlite_write()
    repository_id = repository.pk if isinstance(repository, BitbucketRepository) else repository
    locked_repository = (
        BitbucketRepository.objects.select_for_update()
        .exclude(pk__in=_removal_repository_ids())
        .filter(pk=repository_id)
        .first()
    )
    if locked_repository is None:
        return ExtractionQueueResult((), (), ())
    observed_at = timezone.now()
    run_id = uuid.uuid4()
    sync_job_id = (
        repository_sync_job.pk
        if isinstance(repository_sync_job, RepositorySyncJob)
        else repository_sync_job
    )

    unavailable_ids = tuple(
        PDFExtractionJob.objects.filter(
            document__repository=locked_repository,
            status__in=_ACTIVE_JOB_STATUSES,
        )
        .exclude(
            document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            document__repository__enabled=True,
            document__local_policy__isnull=True,
        )
        .values_list("id", flat=True)
    )
    cancelled: list[int] = []
    for unavailable_batch in _batches(unavailable_ids):
        cancelled.extend(
            _cancel_active_extraction_jobs(
                unavailable_batch,
                observed_at=observed_at,
                error_code="extraction_target_unavailable",
                error_summary="The PDF is no longer active in an enabled repository.",
                log_message=(
                    "PDF indexing was cancelled because the document is no longer active "
                    "in an enabled repository."
                ),
            )
        )
    cancelled_ids: tuple[int, ...] = tuple(cancelled)

    if not locked_repository.enabled:
        return ExtractionQueueResult((), cancelled_ids, ())

    skipped_ids: list[int] = []
    active_for_document = PDFExtractionJob.objects.filter(
        document_id=OuterRef("pk"),
        status__in=_ACTIVE_JOB_STATUSES,
    )
    fully_current_active = active_for_document.filter(
        target_git_blob_id=OuterRef("git_blob_id"),
        target_relative_path=OuterRef("relative_path"),
        target_file_size=OuterRef("file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        target_source_commit=OuterRef("last_seen_commit"),
    )
    if sync_job_id is not None:
        fully_current_active = fully_current_active.filter(repository_sync_job_id=sync_job_id)
    latest_current_failure = PDFExtractionJob.objects.filter(
        document_id=OuterRef("pk"),
        target_git_blob_id=OuterRef("git_blob_id"),
        target_relative_path=OuterRef("relative_path"),
        target_file_size=OuterRef("file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        status__in=(
            PDFExtractionJobStatus.FAILED,
            PDFExtractionJobStatus.INTERRUPTED,
        ),
    ).order_by("-requested_at", "-id")
    no_active_failure_filter = Q(latest_failure_id__isnull=True)
    if recover_interrupted:
        no_active_failure_filter |= Q(
            latest_failure_error__in=_AUTOMATIC_RETRY_ERROR_CODES,
            latest_failure_retry_count__lt=(settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES),
        )
    documents = tuple(
        PDFDocument.objects.select_for_update()
        .filter(
            repository=locked_repository,
            lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            local_policy__isnull=True,
        )
        .filter(
            Q(indexed_revision__isnull=True)
            | ~Q(indexed_git_blob_id=F("git_blob_id"))
            | ~Q(extractor_version=PDF_EXTRACTOR_VERSION)
            | ~Q(index_version=PDF_INDEX_VERSION)
        )
        .annotate(
            has_active_job=Exists(active_for_document),
            has_fully_current_active=Exists(fully_current_active),
            latest_failure_id=Subquery(latest_current_failure.values("id")[:1]),
            latest_failure_error=Subquery(latest_current_failure.values("error_code")[:1]),
            latest_failure_retry_count=Subquery(latest_current_failure.values("retry_count")[:1]),
        )
        .filter(
            Q(has_active_job=True, has_fully_current_active=False)
            | Q(has_active_job=False) & (Q() if retry_failed else no_active_failure_filter)
        )
        .only(
            "id",
            "repository_id",
            "relative_path",
            "file_size",
            "git_blob_id",
            "last_seen_commit",
            "indexed_revision_id",
            "indexed_git_blob_id",
            "index_state",
            "index_version",
            "extractor_version",
            "extraction_error_code",
            "extraction_error_summary",
        )
        .order_by("id")
    )
    if not documents:
        return ExtractionQueueResult((), cancelled_ids, ())

    document_ids = tuple(document.pk for document in documents)
    active_by_document: dict[int, PDFExtractionJob] = {}
    for document_id_batch in _batches(document_ids):
        active_by_document.update(
            {
                job.document_id: job
                for job in PDFExtractionJob.objects.filter(
                    document_id__in=document_id_batch,
                    status__in=_ACTIVE_JOB_STATUSES,
                ).order_by("document_id", "requested_at", "id")
            }
        )
    source_commit_updates: list[tuple[int, str]] = []
    mismatched_active_ids: list[int] = []
    candidates: list[PDFDocument] = []
    for document in documents:
        active = active_by_document.get(document.pk)
        if active is not None:
            if _job_matches_document(active, document):
                if active.target_source_commit != document.last_seen_commit or (
                    sync_job_id is not None and active.repository_sync_job_id != sync_job_id
                ):
                    source_commit_updates.append((active.pk, document.last_seen_commit))
                skipped_ids.append(document.pk)
                continue
            mismatched_active_ids.append(active.pk)
        candidates.append(document)

    source_commit_field = PDFExtractionJob._meta.get_field("target_source_commit")
    for update_batch in _batches(source_commit_updates):
        update_ids = tuple(job_id for job_id, _source_commit in update_batch)
        update_values: dict[str, object] = {
            "target_source_commit": Case(
                *(
                    When(pk=job_id, then=Value(source_commit))
                    for job_id, source_commit in update_batch
                ),
                output_field=source_commit_field,
            )
        }
        if sync_job_id is not None:
            update_values["repository_sync_job_id"] = sync_job_id
        update_values["run_id"] = run_id
        PDFExtractionJob.objects.filter(
            pk__in=update_ids,
            status__in=_ACTIVE_JOB_STATUSES,
        ).update(**update_values)

    superseded_ids: list[int] = []
    for cancellation_batch in _batches(mismatched_active_ids):
        superseded_ids.extend(
            _cancel_active_extraction_jobs(
                cancellation_batch,
                observed_at=observed_at,
                error_code="extraction_target_changed",
                error_summary="A newer PDF revision replaced this queued extraction.",
                log_message=(
                    "PDF indexing was cancelled because a newer PDF revision replaced "
                    "this extraction."
                ),
            )
        )
    cancelled_ids = (*cancelled_ids, *superseded_ids)

    proposed_jobs: list[PDFExtractionJob] = []
    proposed_documents: dict[int, PDFDocument] = {}
    for document in candidates:
        retry_count = 0
        current_failure_id = document.latest_failure_id
        if current_failure_id is not None and not retry_failed:
            can_recover = (
                recover_interrupted
                and document.latest_failure_error in _AUTOMATIC_RETRY_ERROR_CODES
                and document.latest_failure_retry_count
                < settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES
            )
            if not can_recover:
                skipped_ids.append(document.pk)
                continue
            retry_count = document.latest_failure_retry_count + 1
        proposed_jobs.append(
            PDFExtractionJob(
                document=document,
                repository_sync_job_id=sync_job_id,
                run_id=run_id,
                target_git_blob_id=document.git_blob_id,
                target_source_commit=document.last_seen_commit,
                target_relative_path=document.relative_path,
                target_file_size=document.file_size,
                target_extractor_version=PDF_EXTRACTOR_VERSION,
                retry_count=retry_count,
                status=PDFExtractionJobStatus.QUEUED,
                phase=PDFExtractionJobPhase.QUEUED,
            )
        )
        proposed_documents[document.pk] = document

    created_jobs: list[PDFExtractionJob] = []
    if proposed_jobs:
        try:
            # The common path uses bounded bulk writes. A uniqueness race is
            # rare because the repository transaction serializes local writers;
            # retain the savepoint fallback for databases where row locks or
            # competing controllers can still expose one.
            with transaction.atomic():
                created_jobs = PDFExtractionJob.objects.bulk_create(
                    proposed_jobs,
                    batch_size=_QUEUE_WRITE_BATCH_SIZE,
                )
        except IntegrityError as exc:
            log_event(
                logger,
                logging.ERROR,
                "pdf_queue_bulk_write_failed",
                error=exc,
                repository_id=repository_id,
                count=len(proposed_jobs),
            )
            for proposed in proposed_jobs:
                try:
                    with transaction.atomic():
                        created_jobs.append(
                            PDFExtractionJob.objects.create(
                                document_id=proposed.document_id,
                                repository_sync_job_id=proposed.repository_sync_job_id,
                                run_id=proposed.run_id,
                                target_git_blob_id=proposed.target_git_blob_id,
                                target_source_commit=proposed.target_source_commit,
                                target_relative_path=proposed.target_relative_path,
                                target_file_size=proposed.target_file_size,
                                target_extractor_version=proposed.target_extractor_version,
                                retry_count=proposed.retry_count,
                                status=PDFExtractionJobStatus.QUEUED,
                                phase=PDFExtractionJobPhase.QUEUED,
                            )
                        )
                except IntegrityError as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_queue_job_write_failed",
                        error=exc,
                        repository_id=repository_id,
                        document_id=proposed.document_id,
                    )
                    skipped_ids.append(proposed.document_id)

    pending_documents = [
        proposed_documents[job.document_id]
        for job in created_jobs
        if job.document_id in proposed_documents
    ]
    for document in pending_documents:
        document.index_state = PDFIndexState.PENDING
        document.extraction_error_code = ""
        document.extraction_error_summary = ""

    if pending_documents:
        PDFDocument.objects.bulk_update(
            pending_documents,
            fields=("index_state", "extraction_error_code", "extraction_error_summary"),
            batch_size=500,
        )
    if created_jobs or cancelled_ids or skipped_ids:
        context = {
            "repository_id": repository_id,
            "queued_count": len(created_jobs),
            "skipped_count": len(skipped_ids),
            "count": len(set(cancelled_ids)),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        transaction.on_commit(
            lambda context=context: log_event(
                logger, logging.INFO, "pdf_extraction_queue_reconciled", **context
            )
        )
    return ExtractionQueueResult(
        tuple(job.pk for job in created_jobs if job.pk is not None),
        tuple(dict.fromkeys(cancelled_ids)),
        tuple(skipped_ids),
    )


@_logged_indexing_errors("worker_launch_failure_record")
@transaction.atomic
def mark_index_worker_launch_failed(job_ids: Sequence[int]) -> tuple[int, ...]:
    """Fail only the queued jobs whose detached worker could not be launched."""

    normalized_ids = tuple(
        dict.fromkeys(
            value
            for value in job_ids
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        )
    )
    if not normalized_ids:
        return ()
    log_event(
        logger,
        logging.ERROR,
        "pdf_index_worker_launch_failed",
        queued_count=len(normalized_ids),
        error_code="index_worker_unavailable",
    )
    now = timezone.now()
    failed_ids: list[int] = []
    failed_jobs: list[PDFExtractionJob] = []
    jobs = PDFExtractionJob.objects.select_related("document__repository").filter(
        pk__in=normalized_ids,
        status=PDFExtractionJobStatus.QUEUED,
    )
    for job in jobs:
        updated = PDFExtractionJob.objects.filter(
            pk=job.pk,
            status=PDFExtractionJobStatus.QUEUED,
        ).update(
            status=PDFExtractionJobStatus.FAILED,
            completed_at=now,
            error_code="index_worker_unavailable",
            error_summary="OWL could not start the background PDF indexing worker.",
        )
        if updated != 1:
            continue
        failed_ids.append(job.pk)
        failed_jobs.append(job)
        document = job.document
        if document.repository.enabled and _job_matches_document(job, document):
            document.index_state = (
                PDFIndexState.STALE_ERROR
                if document.indexed_revision_id is not None
                else PDFIndexState.FAILED
            )
            document.last_index_attempt_at = now
            document.extraction_error_code = "index_worker_unavailable"
            document.extraction_error_summary = (
                "OWL could not start the background PDF indexing worker."
            )
            document.save(
                update_fields=(
                    "index_state",
                    "last_index_attempt_at",
                    "extraction_error_code",
                    "extraction_error_summary",
                )
            )
    for failed_job in failed_jobs:
        transaction.on_commit(
            lambda failed_job=failed_job: _append_index_log(
                failed_job.pk,
                event="indexing_failed",
                message="OWL could not start the background PDF indexing worker.",
                phase=PDFExtractionJobPhase.QUEUED,
                progress=0,
                severity=RepositoryOperationLogSeverity.ERROR,
                job=failed_job,
            )
        )
    return tuple(failed_ids)


def extraction_status_snapshot() -> ExtractionStatusSnapshot:
    """Return compact local-only queue/index counts for polling and status pages."""

    current_jobs = PDFExtractionJob.objects.exclude(
        document__repository_id__in=_removal_repository_ids()
    ).filter(
        target_git_blob_id=F("document__git_blob_id"),
        target_relative_path=F("document__relative_path"),
        target_file_size=F("document__file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        document__repository__enabled=True,
        document__local_policy__isnull=True,
    )
    active_job_counts = {
        row["status"]: row["total"]
        for row in current_jobs.filter(status__in=_ACTIVE_JOB_STATUSES)
        .values("status")
        .annotate(total=Count("id"))
    }
    active_documents = PDFDocument.objects.exclude(
        repository_id__in=_removal_repository_ids()
    ).filter(
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        repository__enabled=True,
    )
    latest_current_status = (
        PDFExtractionJob.objects.filter(
            document_id=OuterRef("pk"),
            target_git_blob_id=OuterRef("git_blob_id"),
            target_relative_path=OuterRef("relative_path"),
            target_file_size=OuterRef("file_size"),
            target_extractor_version=PDF_EXTRACTOR_VERSION,
        )
        .order_by("-requested_at", "-id")
        .values("status")[:1]
    )
    unresolved = active_documents.filter(
        index_state__in=(PDFIndexState.FAILED, PDFIndexState.STALE_ERROR)
    ).annotate(latest_current_job_status=Subquery(latest_current_status))
    return ExtractionStatusSnapshot(
        queued_jobs=active_job_counts.get(PDFExtractionJobStatus.QUEUED, 0),
        running_jobs=active_job_counts.get(PDFExtractionJobStatus.RUNNING, 0),
        failed_jobs=unresolved.filter(
            latest_current_job_status=PDFExtractionJobStatus.FAILED
        ).count(),
        interrupted_jobs=unresolved.filter(
            latest_current_job_status=PDFExtractionJobStatus.INTERRUPTED
        ).count(),
        pending_documents=active_documents.filter(
            index_state=PDFIndexState.PENDING, local_policy__isnull=True
        ).count(),
        indexed_documents=active_documents.filter(
            index_state__in=(
                PDFIndexState.READY,
                PDFIndexState.NO_TEXT,
                PDFIndexState.PARTIAL,
            )
        ).count(),
        stale_documents=active_documents.filter(index_state=PDFIndexState.STALE_ERROR).count(),
    )


@_logged_indexing_errors("extraction_lease_recovery")
def _interrupt_stale_extraction_jobs(*, observed_at=None, force: bool = False) -> None:
    now = observed_at or timezone.now()
    cutoff = now - STALE_EXTRACTION_AFTER
    candidates = PDFExtractionJob.objects.exclude(
        document__repository_id__in=_removal_repository_ids()
    ).filter(status=PDFExtractionJobStatus.RUNNING)
    if not force:
        candidates = candidates.filter(
            Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff)
        )
    candidate_ids = tuple(candidates.values_list("id", flat=True))
    for job_id in candidate_ids:
        with pdf_extraction_claim_lock(), transaction.atomic():
            phase = (
                PDFExtractionJob.objects.filter(pk=job_id)
                .values_list("phase", flat=True)
                .first()
            )
            if (
                phase == PDFExtractionJobPhase.PUBLISHING
                and _publication_staging_path(job_id).is_file()
            ):
                # Parser output is durable already. A restarted supervisor only
                # needs to release the previous publisher lease. Keep the check
                # and lease release behind the same gate used by the writer.
                PDFExtractionJob.objects.filter(
                    pk=job_id,
                    status=PDFExtractionJobStatus.RUNNING,
                    phase=PDFExtractionJobPhase.PUBLISHING,
                ).update(worker_pid=None, heartbeat_at=now)
                continue
            eligible = PDFExtractionJob.objects.filter(
                pk=job_id,
                status=PDFExtractionJobStatus.RUNNING,
            )
            if not force:
                eligible = eligible.filter(
                    Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff)
                )
            interrupted = eligible.update(
                status=PDFExtractionJobStatus.INTERRUPTED,
                completed_at=now,
                error_code="extraction_worker_interrupted",
                error_summary=("The background PDF worker stopped before extraction completed."),
            )
            if interrupted != 1:
                continue
            log_event(
                logger,
                logging.ERROR,
                "pdf_extraction_worker_interrupted",
                job_id=job_id,
                error_code="extraction_worker_interrupted",
                reason="startup_recovery" if force else "heartbeat_expired",
            )
            job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
            document = job.document
            if _job_matches_document(job, document):
                document.index_state = (
                    PDFIndexState.STALE_ERROR
                    if document.indexed_revision_id is not None
                    else PDFIndexState.FAILED
                )
                document.last_index_attempt_at = now
                document.extraction_error_code = job.error_code
                document.extraction_error_summary = job.error_summary
                document.save(
                    update_fields=(
                        "index_state",
                        "last_index_attempt_at",
                        "extraction_error_code",
                        "extraction_error_summary",
                    )
                )
            _append_index_log(
                job.pk,
                event="indexing_interrupted",
                message="The background PDF worker stopped before indexing completed.",
                phase=job.phase,
                progress=job.progress,
                severity=RepositoryOperationLogSeverity.ERROR,
                job=job,
            )


@_logged_indexing_errors("extraction_queue_recovery")
def sweep_pdf_extraction_queue(
    *,
    interrupt_running: bool = False,
) -> ExtractionQueueResult:
    """Recover stale leases and queue every stranded current PDF revision."""

    started = time.monotonic()
    _interrupt_stale_extraction_jobs(force=interrupt_running)

    observed_at = timezone.now()
    unavailable_candidate_ids = tuple(
        PDFExtractionJob.objects.filter(status__in=_ACTIVE_JOB_STATUSES)
        .exclude(
            document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            document__repository__enabled=True,
            document__local_policy__isnull=True,
        )
        .values_list("id", flat=True)
    )
    unavailable_job_ids: list[int] = []
    for unavailable_batch in _batches(unavailable_candidate_ids):
        unavailable_job_ids.extend(
            _cancel_active_extraction_jobs(
                unavailable_batch,
                observed_at=observed_at,
                error_code="extraction_target_unavailable",
                error_summary="The PDF is no longer active in an enabled repository.",
                log_message=(
                    "PDF indexing was cancelled because the document is no longer active "
                    "in an enabled repository."
                ),
            )
        )

    active_for_document = PDFExtractionJob.objects.filter(
        document_id=OuterRef("pk"),
        status__in=_ACTIVE_JOB_STATUSES,
    )
    fully_current_active = active_for_document.filter(
        target_git_blob_id=OuterRef("git_blob_id"),
        target_relative_path=OuterRef("relative_path"),
        target_file_size=OuterRef("file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        target_source_commit=OuterRef("last_seen_commit"),
    )
    latest_current_job = PDFExtractionJob.objects.filter(
        document_id=OuterRef("pk"),
        target_git_blob_id=OuterRef("git_blob_id"),
        target_relative_path=OuterRef("relative_path"),
        target_file_size=OuterRef("file_size"),
        target_extractor_version=PDF_EXTRACTOR_VERSION,
    ).order_by("-requested_at", "-id")
    repository_ids = tuple(
        PDFDocument.objects.exclude(repository_id__in=_removal_repository_ids())
        .filter(
            lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            repository__enabled=True,
            local_policy__isnull=True,
        )
        .filter(
            Q(indexed_revision__isnull=True)
            | ~Q(indexed_git_blob_id=F("git_blob_id"))
            | ~Q(extractor_version=PDF_EXTRACTOR_VERSION)
            | ~Q(index_version=PDF_INDEX_VERSION)
        )
        .annotate(
            has_active_job=Exists(active_for_document),
            has_fully_current_active=Exists(fully_current_active),
            latest_job_id=Subquery(latest_current_job.values("id")[:1]),
            latest_job_status=Subquery(latest_current_job.values("status")[:1]),
            latest_job_error=Subquery(latest_current_job.values("error_code")[:1]),
            latest_job_retry_count=Subquery(latest_current_job.values("retry_count")[:1]),
        )
        .filter(
            Q(has_active_job=True, has_fully_current_active=False)
            | Q(has_active_job=False, latest_job_id__isnull=True)
            | Q(
                has_active_job=False,
                latest_job_status__in=(
                    PDFExtractionJobStatus.FAILED,
                    PDFExtractionJobStatus.INTERRUPTED,
                ),
                latest_job_error__in=_AUTOMATIC_RETRY_ERROR_CODES,
                latest_job_retry_count__lt=(settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES),
            )
        )
        .order_by()
        .values_list("repository_id", flat=True)
        .distinct()
    )
    queued: list[int] = []
    cancelled: list[int] = list(unavailable_job_ids)
    skipped: list[int] = []
    for repository_id in repository_ids:
        result = queue_repository_pdf_extractions(
            repository_id,
            recover_interrupted=True,
        )
        queued.extend(result.queued_job_ids)
        cancelled.extend(result.cancelled_job_ids)
        skipped.extend(result.skipped_document_ids)
    if queued or cancelled or skipped:
        log_event(
            logger,
            logging.INFO,
            "pdf_extraction_queue_recovered",
            queued_count=len(queued),
            skipped_count=len(skipped),
            count=len(set(cancelled)),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    return ExtractionQueueResult(
        tuple(queued),
        tuple(dict.fromkeys(cancelled)),
        tuple(skipped),
    )


@_logged_indexing_errors("extraction_claim")
def claim_next_extraction_job() -> PDFExtractionJob | None:
    """Claim one PDF after its own checkout is published, within a global cap."""

    with pdf_extraction_claim_lock(), transaction.atomic():
        # Resident workers poll continuously. Avoid reserving SQLite's single
        # writer when the queue is empty. The cross-process claim gate also
        # keeps this read clear of simultaneous claim/publication writes.
        if not PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.QUEUED).exists():
            return None
        _reserve_sqlite_write()
        active_by_repository = tuple(
            PDFExtractionJob.objects.exclude(document__repository_id__in=_removal_repository_ids())
            .filter(status=PDFExtractionJobStatus.RUNNING)
            .order_by()
            .values("document__repository_id")
            .annotate(total=Count("id"))
        )
        running_by_repository = tuple(
            PDFExtractionJob.objects.exclude(document__repository_id__in=_removal_repository_ids())
            .filter(status=PDFExtractionJobStatus.RUNNING)
            .exclude(phase=PDFExtractionJobPhase.PUBLISHING)
            .order_by()
            .values("document__repository_id")
            .annotate(total=Count("id"))
        )
        if (
            sum(row["total"] for row in running_by_repository)
            >= settings.PDF_MAX_EXTRACTION_WORKERS
        ):
            return None
        if PDFExtractionJob.objects.filter(
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
        ).count() >= settings.PDF_MAX_STAGED_PUBLICATIONS:
            return None
        active_repository_ids = tuple(
            row["document__repository_id"] for row in active_by_repository
        )
        active_sync = RepositorySyncJob.objects.filter(
            repository_id=OuterRef("document__repository_id"),
            status__in=_ACTIVE_REPOSITORY_JOB_STATUSES,
        )
        # At most the configured number of running workers contributes a CASE
        # arm. Do not rescan all extraction history once for every queued PDF.
        repository_running = Case(
            *(
                When(
                    document__repository_id=row["document__repository_id"], then=Value(row["total"])
                )
                for row in running_by_repository
            ),
            default=Value(0),
        )
        candidates = (
            PDFExtractionJob.objects.exclude(document__repository_id__in=_removal_repository_ids())
            .filter(
                status=PDFExtractionJobStatus.QUEUED,
                document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
                document__repository__enabled=True,
                document__repository__sync_state=RepositorySyncState.READY,
                document__local_policy__isnull=True,
            )
            .annotate(
                repository_sync_active=Exists(active_sync),
                repository_running=repository_running,
            )
            .filter(
                repository_sync_active=False,
                repository_running__lt=settings.PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY,
            )
        )
        if active_repository_ids:
            # Once a repository owns the parser pool, preserve locality and
            # avoid concurrent SQLite/FTS publication from other repositories.
            candidates = candidates.filter(
                document__repository_id__in=active_repository_ids[
                    : settings.PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES
                ]
            )
        candidate_id = (
            candidates.order_by("requested_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        log_event(logger, logging.DEBUG, "pdf_extraction_candidate_selected", job_id=candidate_id)
        now = timezone.now()
        claimed = PDFExtractionJob.objects.filter(
            pk=candidate_id,
            status=PDFExtractionJobStatus.QUEUED,
        ).update(
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.VALIDATING,
            progress=1,
            started_at=now,
            heartbeat_at=now,
            worker_pid=os.getpid(),
            error_code="",
            error_summary="",
        )
        if claimed != 1:
            return None
        job = PDFExtractionJob.objects.select_related("document__repository").get(pk=candidate_id)
        _append_index_log(
            job.pk,
            event="indexing_claimed",
            message="A PDF indexing worker claimed this document.",
            phase=PDFExtractionJobPhase.VALIDATING,
            progress=1,
            job=job,
        )
    log_event(
        logger,
        logging.INFO,
        "pdf_extraction_claimed",
        repository_id=job.document.repository_id,
        document_id=job.document_id,
        job_id=job.pk,
        worker_pid=job.worker_pid,
        retry_count=job.retry_count,
    )
    return job


def _reserve_sqlite_write() -> None:
    """Avoid a deferred read-to-write upgrade when PDF controllers publish together."""

    if connection.vendor == "sqlite":
        # A no-op write reserves SQLite's single writer before any SELECT in
        # this short transaction. Other writers can wait for the busy timeout
        # instead of racing an un-retryable snapshot/lock upgrade. No PDF
        # parsing, hashing, file access or subprocess wait occurs in this gate.
        PDFExtractionJob.objects.filter(pk=-1).update(status=F("status"))


def _serialized_index_write(function):
    """Keep brief PDF controller writes serialized while parsers run in parallel."""

    @wraps(function)
    def guarded(*args, **kwargs):
        with pdf_extraction_claim_lock():
            return function(*args, **kwargs)

    return guarded


def _publication_staging_path(job_id: int) -> Path:
    root = Path(settings.BITBUCKET_TEMP_ROOT).resolve() / "pdf-publication"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"job-{int(job_id)}.json"


def _staged_extraction_payload(staged: StagedPDFExtraction) -> dict[str, object]:
    return {
        "state": staged.state,
        "pages": [
            {
                "page_number": page.page_number,
                "text": page.text,
                "character_count": page.character_count,
                "state": page.state,
                "error_code": page.error_code,
            }
            for page in staged.pages
        ],
        "page_count": staged.page_count,
        "extracted_character_count": staged.extracted_character_count,
        "source_size_bytes": staged.source_size_bytes,
        "content_sha256_before": staged.content_sha256_before,
        "content_sha256_after": staged.content_sha256_after,
        "extractor_version": staged.extractor_version,
        "error_code": staged.error_code,
        "error_summary": staged.error_summary,
        "publishable": staged.publishable,
        "diagnostic": staged.diagnostic.to_payload(),
    }


@_serialized_index_write
def _stage_extraction_for_publication(
    job_id: int,
    *,
    content_sha256: str,
    staged: StagedPDFExtraction,
) -> None:
    """Atomically hand validated parser output to the sole SQLite publisher."""

    target = _publication_staging_path(job_id)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = {
        "job_id": int(job_id),
        "content_sha256": content_sha256,
        "extraction": _staged_extraction_payload(staged),
    }
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        with transaction.atomic():
            _reserve_sqlite_write()
            updated = PDFExtractionJob.objects.filter(
                pk=job_id,
                status=PDFExtractionJobStatus.RUNNING,
            ).update(
                phase=PDFExtractionJobPhase.PUBLISHING,
                progress=95,
                heartbeat_at=timezone.now(),
                worker_pid=None,
            )
            if updated != 1:
                raise PDFIndexingError(
                    "extraction_worker_lease_lost",
                    "This PDF extraction was interrupted before publication.",
                )
    except Exception:
        temporary.unlink(missing_ok=True)
        if not PDFExtractionJob.objects.filter(
            pk=job_id,
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
        ).exists():
            target.unlink(missing_ok=True)
        raise


def work_one_publication_job() -> PDFExtractionJob | None:
    """Publish one durably staged extraction through the single SQLite writer."""

    with pdf_extraction_claim_lock(), transaction.atomic():
        _reserve_sqlite_write()
        job_id = (
            PDFExtractionJob.objects.filter(
                status=PDFExtractionJobStatus.RUNNING,
                phase=PDFExtractionJobPhase.PUBLISHING,
                worker_pid__isnull=True,
            )
            .order_by("started_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if job_id is None:
            return None
        claimed = PDFExtractionJob.objects.filter(
            pk=job_id,
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
            worker_pid__isnull=True,
        ).update(worker_pid=os.getpid(), heartbeat_at=timezone.now())
        if claimed != 1:
            return None
    job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    target = _publication_staging_path(job.pk)
    try:
        with target.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload.get("job_id") != job.pk:
            raise PDFIndexingError("invalid_staged_extraction", "Staged PDF result is invalid.")
        staged = _parse_extractor_payload(payload.get("extraction"))
        content_sha256 = str(payload.get("content_sha256") or "")
        if not _SHA256.fullmatch(content_sha256):
            raise PDFIndexingError("invalid_staged_extraction", "Staged PDF hash is invalid.")
        _attach_revision(job.pk, content_sha256=content_sha256, staged=staged)
    except (OperationalError, IntegrityError):
        PDFExtractionJob.objects.filter(
            pk=job.pk,
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
            worker_pid=os.getpid(),
        ).update(worker_pid=None, heartbeat_at=timezone.now())
        raise
    except PDFExtractionTargetChanged as exc:
        _fail_job(job.pk, exc)
        _queue_current_target_after_obsolete_job(job.pk)
    except (OSError, UnicodeError, json.JSONDecodeError, PDFIndexingError) as exc:
        error = exc if isinstance(exc, PDFIndexingError) else PDFIndexingError(
            "staged_extraction_unavailable",
            "The extracted PDF result could not be loaded for publication.",
        )
        _fail_job(job.pk, error)
    finally:
        if PDFExtractionJob.objects.filter(pk=job.pk).exclude(
            status=PDFExtractionJobStatus.RUNNING
        ).exists():
            target.unlink(missing_ok=True)
    return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)


@_serialized_index_write
def _update_job_progress(job_id: int, phase: str, progress: int) -> None:
    progress_state = _PROGRESS_LOG_STATE.get()
    current = (phase, max(1, min(int(progress), 99)))
    previous = progress_state.get(job_id) if progress_state is not None else None
    with transaction.atomic():
        # The sync check reads before the guarded progress write. Reserve
        # SQLite's writer first so concurrent PDF controllers wait rather than
        # failing a deferred read-to-write transaction upgrade.
        _reserve_sqlite_write()
        _raise_if_repository_sync_active(job_id=job_id)
        updated = PDFExtractionJob.objects.filter(
            pk=job_id,
            status=PDFExtractionJobStatus.RUNNING,
        ).update(
            phase=phase,
            progress=current[1],
            heartbeat_at=timezone.now(),
        )
        if updated != 1:
            raise PDFIndexingError(
                "extraction_worker_lease_lost",
                "This PDF extraction was interrupted and will not be published.",
            )
        if previous is None or previous[0] != phase:
            # One short commit publishes the state transition and its durable
            # user-facing event. This avoids an extra serialized SQLite commit
            # for every phase of every PDF.
            _append_index_log(
                job_id,
                event="indexing_phase_changed",
                message=_INDEX_PHASE_MESSAGES.get(
                    phase, "PDF indexing advanced to the next phase."
                ),
                phase=phase,
                progress=current[1],
            )
    if progress_state is None or previous != current:
        log_event(
            logger,
            logging.DEBUG,
            "pdf_extraction_progress",
            job_id=job_id,
            phase=current[0],
            progress=current[1],
        )
        if progress_state is not None:
            progress_state[job_id] = current


def _raise_if_repository_sync_active(*, job_id: int) -> None:
    repository_id = PDFExtractionJob.objects.filter(pk=job_id).values("document__repository_id")[:1]
    if (
        BitbucketRepository.objects.filter(pk=Subquery(repository_id))
        .filter(
            Q(pk__in=_removal_repository_ids())
            | Q(sync_jobs__status__in=_ACTIVE_REPOSITORY_JOB_STATUSES)
        )
        .exists()
    ):
        raise PDFExtractionDeferred


def _validated_job_path(job: PDFExtractionJob) -> Path:
    document = PDFDocument.objects.select_related("repository", "local_policy").get(
        pk=job.document_id
    )
    if getattr(document, "local_policy", None) is not None:
        raise PDFExtractionExcluded
    if document.repository.sync_state != RepositorySyncState.READY:
        # A Git update can succeed before catalogue publication fails. Until a
        # successful refresh publishes that checkout, its bytes need not match
        # even a same-sized PDF in the last known catalogue.
        raise PDFExtractionDeferred
    if not document.repository.enabled or not _job_matches_document(job, document):
        raise PDFExtractionTargetChanged(
            "extraction_target_changed",
            "A newer PDF revision replaced this extraction request.",
        )
    try:
        path = filesystem_path(validated_pdf_path(document))
    except DocumentActionError as exc:
        raise PDFIndexingError(exc.code, exc.summary) from exc
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PDFIndexingError(
            "pdf_disappeared",
            "The PDF disappeared before extraction could start.",
        ) from exc
    if size != job.target_file_size:
        raise PDFExtractionTargetChanged(
            "extraction_target_changed",
            "The PDF changed before extraction could start.",
        )
    return path


def _hash_pdf(path: Path, heartbeat: Callable[[], None]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    last_heartbeat = time.monotonic()
    try:
        with path.open("rb") as source:
            while chunk := source.read(_HASH_CHUNK_BYTES):
                size += len(chunk)
                if size > settings.PDF_MAX_FILE_BYTES:
                    raise PDFIndexingError(
                        "pdf_resource_limit",
                        "The PDF exceeds the configured extraction file-size limit.",
                    )
                digest.update(chunk)
                if time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                    heartbeat()
                    last_heartbeat = time.monotonic()
    except PDFIndexingError:
        raise
    except FileNotFoundError as exc:
        raise PDFIndexingError("pdf_disappeared", "The PDF disappeared during hashing.") from exc
    except PermissionError as exc:
        raise PDFIndexingError(
            "pdf_permission_denied",
            "OWL does not have permission to read the PDF.",
        ) from exc
    except OSError as exc:
        raise PDFIndexingError("pdf_hash_failed", "OWL could not hash this PDF safely.") from exc
    return digest.hexdigest(), size


def _extractor_environment() -> dict[str, str]:
    environment: dict[str, str] = {
        "PYTHONUTF8": "1",
    }
    # Python's Windows user-site location is derived from APPDATA/USERPROFILE.
    # Keep those non-secret path variables so an isolated child launched from a
    # system-wide interpreter can still import requirements installed with
    # ``pip --user`` under AppData\Roaming.
    for name in (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _limit_extractor_process_memory() -> None:
    """Apply the configured address/data ceiling in the parser child on POSIX."""

    try:
        import resource

        requested = int(settings.PDF_MAX_PROCESS_MEMORY_BYTES)
        for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
            limit_kind = getattr(resource, limit_name, None)
            if limit_kind is None:
                continue
            current_soft, current_hard = resource.getrlimit(limit_kind)
            infinity = resource.RLIM_INFINITY
            soft = requested if current_soft == infinity else min(requested, current_soft)
            hard = current_hard
            if hard != infinity:
                soft = min(soft, hard)
            resource.setrlimit(limit_kind, (soft, hard))
    except (ImportError, OSError, TypeError, ValueError):
        # Windows never receives this pre-exec hook. Some POSIX hosts expose a
        # resource name but decline changing it; wall/file/page/text limits still
        # isolate the attempt there.
        return


def _parse_page(payload: object, expected_number: int) -> StagedPDFPage:
    if not isinstance(payload, dict) or set(payload) != {
        "page_number",
        "text",
        "character_count",
        "state",
        "error_code",
    }:
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned invalid page metadata.",
        )
    page_number = payload["page_number"]
    text = payload["text"]
    character_count = payload["character_count"]
    state = payload["state"]
    error_code = payload["error_code"]
    if (
        isinstance(page_number, bool)
        or page_number != expected_number
        or not isinstance(text, str)
        or isinstance(character_count, bool)
        or not isinstance(character_count, int)
        or character_count != len(text)
        or character_count > settings.PDF_MAX_PAGE_TEXT_CHARS
        or character_count > settings.PDF_MAX_PAGE_TEXT_CHARS
        or state not in PDFPageExtractionState.values
        or not isinstance(error_code, str)
        or len(error_code) > 64
    ):
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned invalid page metadata.",
        )
    return StagedPDFPage(page_number, text, character_count, state, error_code)


def _parse_extractor_payload(payload: object) -> StagedPDFExtraction:
    expected_keys = {
        "state",
        "pages",
        "page_count",
        "extracted_character_count",
        "source_size_bytes",
        "content_sha256_before",
        "content_sha256_after",
        "error_code",
        "error_summary",
        "extractor_version",
        "publishable",
    }
    if not isinstance(payload, dict) or set(payload) not in (
        expected_keys,
        expected_keys | {"diagnostic"},
    ):
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned an invalid response.",
        )
    diagnostic = _parse_extractor_diagnostic(payload.get("diagnostic"))
    raw_pages = payload["pages"]
    page_count = payload["page_count"]
    character_count = payload["extracted_character_count"]
    source_size = payload["source_size_bytes"]
    state = payload["state"]
    before = payload["content_sha256_before"]
    after = payload["content_sha256_after"]
    extractor_version = payload["extractor_version"]
    if (
        not isinstance(raw_pages, list)
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or page_count > settings.PDF_MAX_PAGES
        or isinstance(character_count, bool)
        or not isinstance(character_count, int)
        or character_count < 0
        or character_count > settings.PDF_MAX_TOTAL_TEXT_CHARS
        or isinstance(source_size, bool)
        or not isinstance(source_size, int)
        or source_size < 0
        or source_size > settings.PDF_MAX_FILE_BYTES
        or not isinstance(state, str)
        or state not in _PUBLISHABLE_STATES | _FAILURE_DIAGNOSTICS.keys()
        or not isinstance(before, str)
        or not isinstance(after, str)
        or not isinstance(extractor_version, str)
        or extractor_version != PDF_EXTRACTOR_VERSION
    ):
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned invalid summary metadata.",
        )
    pages = tuple(_parse_page(item, index) for index, item in enumerate(raw_pages, start=1))
    if len(pages) != page_count or sum(page.character_count for page in pages) != character_count:
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned inconsistent page totals.",
        )
    error_code, error_summary = _FAILURE_DIAGNOSTICS.get(state, ("", ""))
    if state in _PUBLISHABLE_STATES:
        supplied_publishable = payload["publishable"] is True
        if not supplied_publishable or not _SHA256.fullmatch(before) or before != after:
            raise PDFIndexingError(
                "invalid_extractor_response",
                "The isolated PDF extractor returned an invalid publishable revision.",
            )
        if state == PDFTextRevisionState.READY and character_count == 0:
            raise PDFIndexingError(
                "invalid_extractor_response",
                "The isolated PDF extractor returned an empty ready revision.",
            )
        if state == PDFTextRevisionState.NO_TEXT and character_count != 0:
            raise PDFIndexingError(
                "invalid_extractor_response",
                "The isolated PDF extractor returned text for a no-text revision.",
            )
        if state == PDFTextRevisionState.PARTIAL:
            error_code = "partial_extraction"
            error_summary = "OWL could not extract text from one or more PDF pages."
    elif payload["publishable"] is not False:
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned an invalid failure response.",
        )
    return StagedPDFExtraction(
        state=state,
        pages=pages,
        page_count=page_count,
        extracted_character_count=character_count,
        source_size_bytes=source_size,
        content_sha256_before=before,
        content_sha256_after=after,
        extractor_version=extractor_version,
        error_code=error_code,
        error_summary=error_summary,
        diagnostic=diagnostic,
    )


def _parse_extractor_diagnostic(payload: object) -> PDFExtractionDiagnostic:
    """Accept a small fixed vocabulary, never child-supplied error text or names."""

    if payload is None:
        return PDFExtractionDiagnostic()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"stage", "reason", "errno", "winerror"}
        or not isinstance(payload["stage"], str)
        or payload["stage"] not in DIAGNOSTIC_STAGES
        or not isinstance(payload["reason"], str)
        or payload["reason"] not in DIAGNOSTIC_REASONS
        or any(
            value is not None and (type(value) is not int or not 0 <= value <= 2**31 - 1)
            for value in (payload["errno"], payload["winerror"])
        )
    ):
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned invalid diagnostic metadata.",
        )
    return PDFExtractionDiagnostic(**payload)


def _log_extractor_diagnostic(staged: StagedPDFExtraction, *, job: PDFExtractionJob) -> None:
    diagnostic = _parse_extractor_diagnostic(staged.diagnostic.to_payload())
    if not diagnostic.reason:
        return
    # No raw child exception is re-raised: even its class name could come from
    # PDF content. The reason and stage came from a fixed vocabulary above.
    context = {
        "repository_id": job.document.repository_id,
        "document_id": job.document_id,
        "job_id": job.pk,
        "stage": diagnostic.stage,
        "reason": diagnostic.reason,
        "error_code": staged.error_code
        if staged.error_code in _LOGGABLE_PDF_ERROR_CODES
        else "pdf_page_extraction_failed"
        if staged.publishable
        else "pdf_unknown_error",
        "errno": diagnostic.errno,
        "winerror": diagnostic.winerror,
    }
    log_event(logger, logging.ERROR, "pdf_parser_diagnostic", **context)


def run_isolated_pdf_extractor(
    path: Path,
    heartbeat: Callable[[], None],
) -> StagedPDFExtraction:
    """Run the DB-free parser module with bounded output and a wall timeout."""

    request = json.dumps(
        {
            "path": str(path),
            "max_file_bytes": settings.PDF_MAX_FILE_BYTES,
            "max_pages": settings.PDF_MAX_PAGES,
            "max_characters": settings.PDF_MAX_TOTAL_TEXT_CHARS,
            "max_page_characters": settings.PDF_MAX_PAGE_TEXT_CHARS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    temp_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
    try:
        temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_file = tempfile.TemporaryFile(  # noqa: SIM115 - manager below closes it.
            mode="w+b",
            prefix="pdf-extraction-",
            dir=temp_root,
        )
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "pdf_parser_staging_failed",
            error=exc,
            error_code="extractor_staging_unavailable",
        )
        raise PDFIndexingError(
            "extractor_staging_unavailable",
            "OWL could not create a private PDF extraction staging file.",
        ) from exc

    with output_file as output:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "bitbucket_search.services.pdf_extractor"],
                cwd=settings.BASE_DIR,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=_extractor_environment(),
                shell=False,
                close_fds=True,
                preexec_fn=_limit_extractor_process_memory if os.name == "posix" else None,
            )
        except OSError as exc:
            log_event(
                logger,
                logging.ERROR,
                "pdf_parser_spawn_failed",
                error=exc,
                error_code="extractor_unavailable",
            )
            raise PDFIndexingError(
                "extractor_unavailable",
                "OWL could not start the isolated PDF extractor.",
            ) from exc
        try:
            assert process.stdin is not None
            process.stdin.write(request)
            process.stdin.close()
            deadline = time.monotonic() + settings.PDF_EXTRACTION_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        process.args,
                        settings.PDF_EXTRACTION_TIMEOUT_SECONDS,
                    )
                try:
                    process.wait(timeout=min(_HEARTBEAT_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    heartbeat()
                    continue
                break
        except subprocess.TimeoutExpired as exc:
            log_event(
                logger,
                logging.ERROR,
                "pdf_parser_timeout",
                error=exc,
                error_code="pdf_extraction_timeout",
                delay_seconds=settings.PDF_EXTRACTION_TIMEOUT_SECONDS,
            )
            if process.poll() is None:
                process.kill()
            process.wait()
            raise PDFIndexingError(
                "pdf_extraction_timeout",
                "PDF extraction exceeded the configured time limit.",
            ) from exc
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        if process.returncode != 0:
            log_event(
                logger,
                logging.ERROR,
                "pdf_parser_process_failed",
                return_code=process.returncode,
                error_code="extractor_failed",
            )
            raise PDFIndexingError(
                "extractor_failed",
                "The isolated PDF extractor stopped without a valid result.",
            )

        maximum_output_bytes = (
            settings.PDF_MAX_TOTAL_TEXT_CHARS * 4 + settings.PDF_MAX_PAGES * 512 + 65_536
        )
        output_size = output.tell()
        if output_size > maximum_output_bytes:
            log_event(
                logger,
                logging.ERROR,
                "pdf_parser_output_limit_exceeded",
                byte_count=output_size,
                error_code="extractor_output_too_large",
            )
            raise PDFIndexingError(
                "extractor_output_too_large",
                "The isolated PDF extractor exceeded its output limit.",
            )
        output.seek(0)
        try:
            payload = json.load(output)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "pdf_parser_output_invalid",
                error=exc,
                error_code="invalid_extractor_response",
            )
            raise PDFIndexingError(
                "invalid_extractor_response",
                "The isolated PDF extractor returned an invalid response.",
            ) from exc
    return _parse_extractor_payload(payload)


def _existing_revision_is_valid(revision: PDFTextRevision) -> bool:
    totals = revision.pages.aggregate(count=Sum("character_count"))
    return (
        revision.state in _PUBLISHABLE_STATES
        and revision.pages.count() == revision.page_count
        and (totals["count"] or 0) == revision.extracted_character_count
    )


@_serialized_index_write
def _attach_revision(
    job_id: int,
    *,
    content_sha256: str,
    staged: StagedPDFExtraction | None,
    existing_revision: PDFTextRevision | None = None,
) -> bool:
    now = timezone.now()
    if (staged is not None and staged.state == PDFTextRevisionState.PARTIAL) or (
        existing_revision is not None and existing_revision.state == PDFTextRevisionState.PARTIAL
    ):
        log_event(
            logger,
            logging.ERROR,
            "pdf_partial_publication",
            job_id=job_id,
            error_code="partial_extraction",
            stage="text_publication",
        )
    with transaction.atomic():
        _reserve_sqlite_write()
        job = (
            PDFExtractionJob.objects.select_for_update()
            .select_related("document__repository", "document__local_policy")
            .get(pk=job_id)
        )
        document = job.document
        if job.status != PDFExtractionJobStatus.RUNNING:
            log_event(
                logger,
                logging.WARNING,
                "pdf_publication_skipped",
                job_id=job_id,
                reason="lease_not_running",
            )
            return False
        if getattr(document, "local_policy", None) is not None:
            raise PDFExtractionExcluded
        _raise_if_repository_sync_active(job_id=job_id)
        if document.repository.sync_state != RepositorySyncState.READY:
            raise PDFExtractionDeferred
        if not document.repository.enabled or not _job_matches_document(job, document):
            raise PDFExtractionTargetChanged(
                "extraction_target_changed",
                "A newer PDF revision replaced this extraction request.",
            )

        revision = existing_revision
        if revision is not None:
            try:
                revision = PDFTextRevision.objects.select_for_update().get(pk=revision.pk)
            except PDFTextRevision.DoesNotExist as exc:
                # Repository removal may prune its last reference while a
                # different repository is hashing the same PDF. The source PDF
                # remains valid; release this short write gate and parse it.
                raise _PDFRevisionCacheMiss from exc
            if (
                revision.content_sha256 != content_sha256
                or revision.extractor_version != PDF_EXTRACTOR_VERSION
                or not _existing_revision_is_valid(revision)
            ):
                raise PDFIndexingError(
                    "pdf_revision_conflict",
                    "OWL found inconsistent reusable PDF text and did not publish it.",
                )
        else:
            if staged is None or not staged.publishable:
                raise PDFIndexingError(
                    "invalid_extractor_response",
                    "OWL did not receive publishable PDF text.",
                )
            revision, created = PDFTextRevision.objects.get_or_create(
                content_sha256=content_sha256,
                extractor_version=PDF_EXTRACTOR_VERSION,
                defaults={
                    "source_byte_size": staged.source_size_bytes,
                    "state": staged.state,
                    "page_count": staged.page_count,
                    "extracted_character_count": staged.extracted_character_count,
                },
            )
            if created:
                PDFTextPage.objects.bulk_create(
                    [
                        PDFTextPage(
                            revision=revision,
                            page_number=page.page_number,
                            extracted_text=page.text,
                            character_count=page.character_count,
                            extraction_state=page.state,
                            error_code=page.error_code,
                        )
                        for page in staged.pages
                    ],
                    batch_size=100,
                )
            elif not _existing_revision_is_valid(revision):
                raise PDFIndexingError(
                    "pdf_revision_conflict",
                    "OWL found inconsistent reusable PDF text and did not publish it.",
                )

        previous_first_indexed = document.first_indexed_at
        document.content_sha256 = content_sha256
        document.indexed_revision = revision
        document.indexed_git_blob_id = job.target_git_blob_id
        document.indexed_source_commit = job.target_source_commit
        document.page_count = revision.page_count
        document.extracted_character_count = revision.extracted_character_count
        document.index_state = revision.state
        document.first_indexed_at = previous_first_indexed or now
        document.last_indexed_at = now
        document.last_index_attempt_at = now
        document.index_version = PDF_INDEX_VERSION
        document.extractor_version = PDF_EXTRACTOR_VERSION
        document.extraction_error_code = (
            staged.error_code
            if staged is not None and staged.state == PDFTextRevisionState.PARTIAL
            else ""
        )
        document.extraction_error_summary = (
            staged.error_summary
            if staged is not None and staged.state == PDFTextRevisionState.PARTIAL
            else ""
        )
        document.save(
            update_fields=(
                "content_sha256",
                "indexed_revision",
                "indexed_git_blob_id",
                "indexed_source_commit",
                "page_count",
                "extracted_character_count",
                "index_state",
                "first_indexed_at",
                "last_indexed_at",
                "last_index_attempt_at",
                "index_version",
                "extractor_version",
                "extraction_error_code",
                "extraction_error_summary",
            )
        )
        job.status = PDFExtractionJobStatus.SUCCEEDED
        job.phase = PDFExtractionJobPhase.COMPLETED
        job.progress = 100
        job.pages_processed = revision.page_count
        job.characters_extracted = revision.extracted_character_count
        job.heartbeat_at = now
        job.completed_at = now
        job.error_code = document.extraction_error_code
        job.error_summary = document.extraction_error_summary
        job.save(
            update_fields=(
                "status",
                "phase",
                "progress",
                "pages_processed",
                "characters_extracted",
                "heartbeat_at",
                "completed_at",
                "error_code",
                "error_summary",
            )
        )
        _append_index_log(
            job.pk,
            event="indexing_completed",
            message="PDF indexing completed with some pages unavailable."
            if revision.state == PDFTextRevisionState.PARTIAL
            else _INDEX_PHASE_MESSAGES[PDFExtractionJobPhase.COMPLETED],
            phase=PDFExtractionJobPhase.COMPLETED,
            progress=100,
            severity=(
                RepositoryOperationLogSeverity.WARNING
                if revision.state == PDFTextRevisionState.PARTIAL
                else RepositoryOperationLogSeverity.INFO
            ),
            job=job,
        )
    log_event(
        logger,
        logging.INFO,
        "pdf_text_published",
        repository_id=document.repository_id,
        document_id=document.pk,
        job_id=job.pk,
        page_count=revision.page_count,
        byte_count=revision.source_byte_size,
        status=revision.state,
        indexed_count=1,
    )
    return True


@_serialized_index_write
def _fail_job(
    job_id: int,
    error: PDFIndexingError,
    *,
    content_sha256: str = "",
) -> None:
    # Emit the actual failure first: the following database transaction can fail
    # too, and must not erase the only diagnostic for the original failure.
    log_event(
        logger,
        logging.ERROR,
        "pdf_extraction_failed",
        error=error,
        job_id=job_id,
        error_code=error.code if error.code in _LOGGABLE_PDF_ERROR_CODES else "pdf_indexing_error",
    )
    now = timezone.now()
    with transaction.atomic():
        _reserve_sqlite_write()
        job = (
            PDFExtractionJob.objects.select_for_update()
            .select_related("document__repository")
            .get(pk=job_id)
        )
        if job.status != PDFExtractionJobStatus.RUNNING:
            return
        document = job.document
        target_is_current = document.repository.enabled and _job_matches_document(job, document)
        job.status = (
            PDFExtractionJobStatus.FAILED if target_is_current else PDFExtractionJobStatus.CANCELLED
        )
        job.heartbeat_at = now
        job.completed_at = now
        job.error_code = error.code
        job.error_summary = error.summary
        job.save(
            update_fields=(
                "status",
                "heartbeat_at",
                "completed_at",
                "error_code",
                "error_summary",
            )
        )
        if target_is_current:
            if content_sha256 and _SHA256.fullmatch(content_sha256):
                document.content_sha256 = content_sha256
            document.index_state = (
                PDFIndexState.STALE_ERROR
                if document.indexed_revision_id is not None
                else PDFIndexState.FAILED
            )
            document.last_index_attempt_at = now
            document.extraction_error_code = error.code
            document.extraction_error_summary = error.summary
            document.save(
                update_fields=(
                    "content_sha256",
                    "index_state",
                    "last_index_attempt_at",
                    "extraction_error_code",
                    "extraction_error_summary",
                )
            )
        _append_index_log(
            job.pk,
            event=(
                "indexing_failed"
                if job.status == PDFExtractionJobStatus.FAILED
                else "indexing_cancelled"
            ),
            message=(
                "PDF indexing stopped with error code "
                f"{error.code if error.code in _LOGGABLE_PDF_ERROR_CODES else 'pdf_indexing_error'}."
                if job.status == PDFExtractionJobStatus.FAILED
                else "PDF indexing was cancelled because its document target changed."
            ),
            phase=job.phase,
            progress=job.progress,
            severity=(
                RepositoryOperationLogSeverity.ERROR
                if job.status == PDFExtractionJobStatus.FAILED
                else RepositoryOperationLogSeverity.WARNING
            ),
            job=job,
        )


def _queue_current_target_after_obsolete_job(job_id: int) -> None:
    job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    if (
        job.document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
        and job.document.repository.enabled
    ):
        queue_repository_pdf_extractions(job.document.repository_id)


def _execute_claimed_extraction_job(
    job_id: int,
    *,
    extraction_runner: ExtractionRunner = run_isolated_pdf_extractor,
    defer_publication: bool = False,
) -> PDFExtractionJob:
    """Execute one lease and let only the controller publish SQLite/FTS rows."""

    job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    if job.status != PDFExtractionJobStatus.RUNNING:
        return job

    started = time.monotonic()
    log_event(
        logger,
        logging.INFO,
        "pdf_extraction_started",
        repository_id=job.document.repository_id,
        document_id=job.document_id,
        job_id=job.pk,
        worker_pid=job.worker_pid,
        byte_count=job.target_file_size,
    )
    content_sha256 = ""

    def hashing_heartbeat() -> None:
        _update_job_progress(job.pk, PDFExtractionJobPhase.HASHING, 10)

    def extraction_heartbeat() -> None:
        _update_job_progress(job.pk, PDFExtractionJobPhase.EXTRACTING, 60)

    try:
        _update_job_progress(job.pk, PDFExtractionJobPhase.VALIDATING, 5)
        path = _validated_job_path(job)
        _update_job_progress(job.pk, PDFExtractionJobPhase.HASHING, 10)
        content_sha256, source_size = _hash_pdf(path, hashing_heartbeat)
        if source_size != job.target_file_size:
            raise PDFExtractionTargetChanged(
                "extraction_target_changed",
                "The PDF changed before extraction could start.",
            )

        reusable = PDFTextRevision.objects.filter(
            content_sha256=content_sha256,
            extractor_version=PDF_EXTRACTOR_VERSION,
        ).first()
        if reusable is not None:
            log_event(
                logger,
                logging.DEBUG,
                "pdf_text_revision_reuse_selected",
                document_id=job.document_id,
                job_id=job.pk,
                page_count=reusable.page_count,
            )
            second_hash, second_size = _hash_pdf(path, hashing_heartbeat)
            if second_hash != content_sha256 or second_size != source_size:
                raise PDFExtractionTargetChanged(
                    "extraction_target_changed",
                    "The PDF changed while OWL was validating reusable text.",
                )
            _update_job_progress(job.pk, PDFExtractionJobPhase.PUBLISHING, 95)
            try:
                _attach_revision(
                    job.pk,
                    content_sha256=content_sha256,
                    staged=None,
                    existing_revision=reusable,
                )
            except _PDFRevisionCacheMiss:
                reusable = None
                log_event(
                    logger,
                    logging.DEBUG,
                    "pdf_text_revision_cache_miss",
                    document_id=job.document_id,
                    job_id=job.pk,
                )
        if reusable is None:
            _update_job_progress(job.pk, PDFExtractionJobPhase.EXTRACTING, 20)
            expected_content_sha256 = content_sha256
            staged = extraction_runner(path, extraction_heartbeat)
            if not isinstance(staged, StagedPDFExtraction):
                raise PDFIndexingError(
                    "invalid_extractor_response",
                    "The isolated PDF extractor returned an invalid response.",
                )
            _log_extractor_diagnostic(staged, job=job)
            failed_pages = sum(page.state == PDFPageExtractionState.FAILED for page in staged.pages)
            if failed_pages:
                log_event(
                    logger,
                    logging.ERROR,
                    "pdf_page_extraction_failed",
                    repository_id=job.document.repository_id,
                    document_id=job.document_id,
                    job_id=job.pk,
                    failed_count=failed_pages,
                    page_count=staged.page_count,
                    error_code="pdf_page_extraction_failed",
                )
            log_event(
                logger,
                logging.DEBUG,
                "pdf_parser_result_received",
                job_id=job.pk,
                page_count=staged.page_count,
                byte_count=staged.source_size_bytes,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            if not staged.publishable:
                if _SHA256.fullmatch(staged.content_sha256_before):
                    content_sha256 = staged.content_sha256_before
                code, summary = _FAILURE_DIAGNOSTICS.get(
                    staged.state,
                    ("pdf_unknown_error", "OWL could not extract this PDF safely."),
                )
                raise PDFIndexingError(code, summary)
            if (
                staged.content_sha256_before != staged.content_sha256_after
                or staged.content_sha256_before != expected_content_sha256
                or staged.source_size_bytes != source_size
            ):
                raise PDFExtractionTargetChanged(
                    "extraction_target_changed",
                    "The PDF changed during extraction.",
                )
            if defer_publication:
                _stage_extraction_for_publication(
                    job.pk,
                    content_sha256=content_sha256,
                    staged=staged,
                )
            else:
                _update_job_progress(job.pk, PDFExtractionJobPhase.PUBLISHING, 95)
                _attach_revision(
                    job.pk,
                    content_sha256=content_sha256,
                    staged=staged,
                )
    except PDFExtractionExcluded:
        log_event(
            logger,
            logging.WARNING,
            "pdf_extraction_cancelled_by_policy",
            repository_id=job.document.repository_id,
            document_id=job.document_id,
            job_id=job.pk,
            reason="local_policy",
        )
        with pdf_extraction_claim_lock(), transaction.atomic():
            cancelled = PDFExtractionJob.objects.filter(
                pk=job.pk, status=PDFExtractionJobStatus.RUNNING
            ).update(
                status=PDFExtractionJobStatus.CANCELLED,
                completed_at=timezone.now(),
                error_code="pdf_refresh_excluded",
                error_summary="This PDF is excluded from refresh and text extraction.",
            )
            if cancelled == 1:
                cancelled_job = PDFExtractionJob.objects.select_related("document__repository").get(
                    pk=job.pk
                )
                _append_index_log(
                    cancelled_job.pk,
                    event="indexing_cancelled",
                    message="This PDF is excluded from refresh and text extraction.",
                    phase=cancelled_job.phase,
                    progress=cancelled_job.progress,
                    severity=RepositoryOperationLogSeverity.WARNING,
                    job=cancelled_job,
                )
    except PDFExtractionDeferred:
        raise
    except PDFExtractionTargetChanged as exc:
        _fail_job(job.pk, exc, content_sha256=content_sha256)
        _queue_current_target_after_obsolete_job(job.pk)
    except PDFIndexingError as exc:
        _fail_job(job.pk, exc, content_sha256=content_sha256)
    except (OperationalError, IntegrityError) as exc:
        log_event(
            logger,
            logging.ERROR,
            "pdf_extraction_database_failed",
            error=exc,
            repository_id=job.document.repository_id,
            document_id=job.document_id,
            job_id=job.pk,
        )
        raise
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "pdf_extraction_unexpected_error",
            error=exc,
            repository_id=job.document.repository_id,
            document_id=job.document_id,
            job_id=job.pk,
            error_code="pdf_indexing_worker_error",
        )
        _fail_job(
            job.pk,
            PDFIndexingError(
                "pdf_indexing_worker_error",
                "The background PDF worker stopped unexpectedly. Retry this PDF.",
            ),
            content_sha256=content_sha256,
        )
    completed = PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)
    log_event(
        logger,
        logging.INFO,
        "pdf_extraction_finished",
        repository_id=job.document.repository_id,
        document_id=job.document_id,
        job_id=job.pk,
        status=completed.status,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return completed


@_logged_indexing_errors("extraction_execution")
def _execute_claimed_extraction_with_checkout(
    job: PDFExtractionJob,
    *,
    extraction_runner: ExtractionRunner = run_isolated_pdf_extractor,
    defer_publication: bool = False,
) -> PDFExtractionJob:
    """Keep a checkout stable and defer if synchronization overtakes the claim."""

    if job.status != PDFExtractionJobStatus.RUNNING:
        return job
    try:
        # Git uses this same cross-process gate. A sync queued after a PDF was
        # claimed is detected before reads, on parser heartbeats, and before
        # publication. An in-flight parser is stopped by its heartbeat handler
        # before this lock is released for a waiting repository worker.
        with repository_checkout_lock(job.document.repository_id, blocking=False, shared=True):
            _raise_if_repository_sync_active(job_id=job.pk)
            return _execute_claimed_extraction_job(
                job.pk,
                extraction_runner=extraction_runner,
                defer_publication=defer_publication,
            )
    except (PDFExtractionDeferred, RepositoryCheckoutBusy) as exc:
        log_event(
            logger,
            logging.WARNING,
            "pdf_extraction_deferred",
            repository_id=job.document.repository_id,
            document_id=job.document_id,
            job_id=job.pk,
            reason="checkout_busy"
            if isinstance(exc, RepositoryCheckoutBusy)
            else "repository_sync",
        )
        with pdf_extraction_claim_lock(), transaction.atomic():
            deferred = PDFExtractionJob.objects.filter(
                pk=job.pk,
                status=PDFExtractionJobStatus.RUNNING,
            ).update(
                status=PDFExtractionJobStatus.QUEUED,
                phase=PDFExtractionJobPhase.QUEUED,
                progress=0,
                pages_processed=0,
                characters_extracted=0,
                started_at=None,
                heartbeat_at=None,
                completed_at=None,
                worker_pid=None,
                error_code="",
                error_summary="",
            )
            if deferred == 1:
                _append_index_log(
                    job.pk,
                    event="indexing_deferred",
                    message=(
                        "PDF indexing was deferred until repository synchronization finishes."
                    ),
                    phase=PDFExtractionJobPhase.QUEUED,
                    progress=0,
                    severity=RepositoryOperationLogSeverity.WARNING,
                    job=job,
                )
        return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "pdf_checkout_lock_failed",
            error=exc,
            repository_id=job.document.repository_id,
            document_id=job.document_id,
            job_id=job.pk,
            error_code="pdf_checkout_lock_unavailable",
        )
        _fail_job(
            job.pk,
            PDFIndexingError(
                "pdf_checkout_lock_unavailable",
                "OWL could not safely lock the repository folder for PDF extraction. Retry this PDF.",
            ),
        )
        return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)


def execute_claimed_extraction_job(
    job_id: int,
    *,
    extraction_runner: ExtractionRunner = run_isolated_pdf_extractor,
    defer_publication: bool = False,
) -> PDFExtractionJob:
    """Keep safe correlation IDs attached to every nested controller diagnostic."""

    try:
        job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    except Exception as exc:
        log_event(
            logger, logging.ERROR, "pdf_extraction_job_lookup_failed", error=exc, job_id=job_id
        )
        raise
    progress_token = _PROGRESS_LOG_STATE.set({})
    identity_token = _INDEX_LOG_JOB.set(
        (
            job.pk,
            job.document.repository_id,
            job.repository_sync_job_id,
            job.worker_pid,
        )
    )
    try:
        with logging_context(
            repository_id=job.document.repository_id, document_id=job.document_id, job_id=job.pk
        ):
            return _execute_claimed_extraction_with_checkout(
                job,
                extraction_runner=extraction_runner,
                defer_publication=defer_publication,
            )
    finally:
        _INDEX_LOG_JOB.reset(identity_token)
        _PROGRESS_LOG_STATE.reset(progress_token)


def work_one_extraction_job() -> PDFExtractionJob | None:
    job = claim_next_extraction_job()
    if job is None:
        return None
    completed = execute_claimed_extraction_job(
        job.pk,
        defer_publication=True,
    )
    # A deferred lease is idle work, not a completion. Let worker commands use
    # their normal polling delay rather than repeatedly reclaiming a busy PDF.
    return None if completed.status == PDFExtractionJobStatus.QUEUED else completed


def launch_index_worker() -> subprocess.Popen[bytes]:
    """Start the detached durable PDF queue worker without blocking the request."""

    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(settings.BASE_DIR) / "manage.py"),
                "bitbucket_index_worker",
                "--idle-timeout",
                str(settings.PDF_EXTRACTION_WORKER_IDLE_SECONDS),
                "--no-startup-sweep",
            ],
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        log_event(
            logger, logging.ERROR, "pdf_index_worker_spawn_failed", error=exc, stage="worker_launch"
        )
        raise
    log_event(
        logger, logging.INFO, "pdf_index_worker_spawned", worker_pid=getattr(process, "pid", None)
    )
    return process


def launch_pdf_writer() -> subprocess.Popen[bytes]:
    """Start the detached sole publisher used outside the resident supervisor."""

    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(settings.BASE_DIR) / "manage.py"),
            "bitbucket_pdf_writer",
            "--idle-timeout",
            str(settings.PDF_EXTRACTION_WORKER_IDLE_SECONDS),
        ],
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    log_event(logger, logging.INFO, "pdf_writer_spawned", worker_pid=process.pid)
    return process
