"""Durable, isolated PDF extraction orchestration and atomic publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
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
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import DocumentActionError, validated_pdf_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    repository_checkout_lock,
)

PDF_INDEX_VERSION = 1
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

    @property
    def publishable(self) -> bool:
        return self.state in _PUBLISHABLE_STATES


@dataclass(frozen=True, slots=True)
class ExtractionQueueResult:
    queued_job_ids: tuple[int, ...]
    cancelled_job_ids: tuple[int, ...]
    skipped_document_ids: tuple[int, ...]


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


def _safe_summary(value: object, fallback: str) -> str:
    return " ".join(str(value or fallback).split())[:500]


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


@transaction.atomic
def queue_repository_pdf_extractions(
    repository: BitbucketRepository | int,
    *,
    repository_sync_job: RepositorySyncJob | int | None = None,
    retry_failed: bool = False,
    recover_interrupted: bool = False,
) -> ExtractionQueueResult:
    """Reconcile one repository's current PDFs without per-document queries."""

    repository_id = repository.pk if isinstance(repository, BitbucketRepository) else repository
    locked_repository = BitbucketRepository.objects.select_for_update().get(pk=repository_id)
    observed_at = timezone.now()
    sync_job_id = (
        repository_sync_job.pk
        if isinstance(repository_sync_job, RepositorySyncJob)
        else repository_sync_job
    )

    cancelled_ids = tuple(
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
    if cancelled_ids:
        PDFExtractionJob.objects.filter(pk__in=cancelled_ids).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=observed_at,
            error_code="extraction_target_unavailable",
            error_summary="The PDF is no longer active in an enabled repository.",
        )

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
        PDFExtractionJob.objects.filter(
            pk__in=update_ids,
            status__in=_ACTIVE_JOB_STATUSES,
        ).update(**update_values)

    for cancellation_batch in _batches(mismatched_active_ids):
        PDFExtractionJob.objects.filter(
            pk__in=cancellation_batch,
            status__in=_ACTIVE_JOB_STATUSES,
        ).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=observed_at,
            error_code="extraction_target_changed",
            error_summary="A newer PDF revision replaced this queued extraction.",
        )
    cancelled_ids = (*cancelled_ids, *mismatched_active_ids)

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
        except IntegrityError:
            for proposed in proposed_jobs:
                try:
                    with transaction.atomic():
                        created_jobs.append(
                            PDFExtractionJob.objects.create(
                                document_id=proposed.document_id,
                                repository_sync_job_id=proposed.repository_sync_job_id,
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
                except IntegrityError:
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
    return ExtractionQueueResult(
        tuple(job.pk for job in created_jobs if job.pk is not None),
        tuple(dict.fromkeys(cancelled_ids)),
        tuple(skipped_ids),
    )


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
    now = timezone.now()
    failed_ids: list[int] = []
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
    return tuple(failed_ids)


def extraction_status_snapshot() -> ExtractionStatusSnapshot:
    """Return compact local-only queue/index counts for polling and status pages."""

    current_jobs = PDFExtractionJob.objects.filter(
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
    active_documents = PDFDocument.objects.filter(
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


def _interrupt_stale_extraction_jobs(*, observed_at=None, force: bool = False) -> None:
    now = observed_at or timezone.now()
    cutoff = now - STALE_EXTRACTION_AFTER
    candidates = PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.RUNNING)
    if not force:
        candidates = candidates.filter(
            Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff)
        )
    candidate_ids = tuple(candidates.values_list("id", flat=True))
    for job_id in candidate_ids:
        with transaction.atomic():
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


def sweep_pdf_extraction_queue(
    *,
    interrupt_running: bool = False,
) -> ExtractionQueueResult:
    """Recover stale leases and queue every stranded current PDF revision."""

    _interrupt_stale_extraction_jobs(force=interrupt_running)

    observed_at = timezone.now()
    unavailable_job_ids = tuple(
        PDFExtractionJob.objects.filter(status__in=_ACTIVE_JOB_STATUSES)
        .exclude(
            document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            document__repository__enabled=True,
            document__local_policy__isnull=True,
        )
        .values_list("id", flat=True)
    )
    for unavailable_batch in _batches(unavailable_job_ids):
        PDFExtractionJob.objects.filter(
            pk__in=unavailable_batch,
            status__in=_ACTIVE_JOB_STATUSES,
        ).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=observed_at,
            error_code="extraction_target_unavailable",
            error_summary="The PDF is no longer active in an enabled repository.",
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
    repository_ids = tuple(
        PDFDocument.objects.filter(
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
            latest_failure_id=Subquery(latest_current_failure.values("id")[:1]),
            latest_failure_error=Subquery(latest_current_failure.values("error_code")[:1]),
            latest_failure_retry_count=Subquery(latest_current_failure.values("retry_count")[:1]),
        )
        .filter(
            Q(has_active_job=True, has_fully_current_active=False)
            | Q(has_active_job=False, latest_failure_id__isnull=True)
            | Q(
                has_active_job=False,
                latest_failure_error__in=_AUTOMATIC_RETRY_ERROR_CODES,
                latest_failure_retry_count__lt=(settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES),
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
    return ExtractionQueueResult(
        tuple(queued),
        tuple(dict.fromkeys(cancelled)),
        tuple(skipped),
    )


def claim_next_extraction_job() -> PDFExtractionJob | None:
    """Claim one PDF job while honoring the global sync and extraction barriers."""

    with transaction.atomic():
        if RepositorySyncJob.objects.filter(status__in=_ACTIVE_REPOSITORY_JOB_STATUSES).exists():
            return None
        if (
            PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.RUNNING).count()
            >= settings.PDF_MAX_EXTRACTION_WORKERS
        ):
            return None
        candidate_id = (
            PDFExtractionJob.objects.filter(
                status=PDFExtractionJobStatus.QUEUED,
                document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
                document__repository__enabled=True,
                document__repository__sync_state=RepositorySyncState.READY,
                document__local_policy__isnull=True,
            )
            .order_by("requested_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
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
    return PDFExtractionJob.objects.select_related("document__repository").get(pk=candidate_id)


def _update_job_progress(job_id: int, phase: str, progress: int) -> None:
    _raise_if_repository_sync_active()
    updated = PDFExtractionJob.objects.filter(
        pk=job_id,
        status=PDFExtractionJobStatus.RUNNING,
    ).update(
        phase=phase,
        progress=max(1, min(int(progress), 99)),
        heartbeat_at=timezone.now(),
    )
    if updated != 1:
        raise PDFIndexingError(
            "extraction_worker_lease_lost",
            "This PDF extraction was interrupted and will not be published.",
        )


def _raise_if_repository_sync_active() -> None:
    if RepositorySyncJob.objects.filter(status__in=_ACTIVE_REPOSITORY_JOB_STATUSES).exists():
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
        path = validated_pdf_path(document)
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
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP"):
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
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise PDFIndexingError(
            "invalid_extractor_response",
            "The isolated PDF extractor returned an invalid response.",
        )
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
    )


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
            raise PDFIndexingError(
                "extractor_failed",
                "The isolated PDF extractor stopped without a valid result.",
            )

        maximum_output_bytes = (
            settings.PDF_MAX_TOTAL_TEXT_CHARS * 4 + settings.PDF_MAX_PAGES * 512 + 65_536
        )
        output_size = output.tell()
        if output_size > maximum_output_bytes:
            raise PDFIndexingError(
                "extractor_output_too_large",
                "The isolated PDF extractor exceeded its output limit.",
            )
        output.seek(0)
        try:
            payload = json.load(output)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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


def _attach_revision(
    job_id: int,
    *,
    content_sha256: str,
    staged: StagedPDFExtraction | None,
    existing_revision: PDFTextRevision | None = None,
) -> bool:
    now = timezone.now()
    with transaction.atomic():
        job = (
            PDFExtractionJob.objects.select_for_update()
            .select_related("document__repository", "document__local_policy")
            .get(pk=job_id)
        )
        document = job.document
        if job.status != PDFExtractionJobStatus.RUNNING:
            return False
        if getattr(document, "local_policy", None) is not None:
            raise PDFExtractionExcluded
        _raise_if_repository_sync_active()
        if document.repository.sync_state != RepositorySyncState.READY:
            raise PDFExtractionDeferred
        if not document.repository.enabled or not _job_matches_document(job, document):
            raise PDFExtractionTargetChanged(
                "extraction_target_changed",
                "A newer PDF revision replaced this extraction request.",
            )

        revision = existing_revision
        if revision is not None:
            revision = PDFTextRevision.objects.select_for_update().get(pk=revision.pk)
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
    return True


def _fail_job(
    job_id: int,
    error: PDFIndexingError,
    *,
    content_sha256: str = "",
) -> None:
    now = timezone.now()
    with transaction.atomic():
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
) -> PDFExtractionJob:
    """Execute one lease and let only the controller publish SQLite/FTS rows."""

    job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    if job.status != PDFExtractionJobStatus.RUNNING:
        return job

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
            second_hash, second_size = _hash_pdf(path, hashing_heartbeat)
            if second_hash != content_sha256 or second_size != source_size:
                raise PDFExtractionTargetChanged(
                    "extraction_target_changed",
                    "The PDF changed while OWL was validating reusable text.",
                )
            _update_job_progress(job.pk, PDFExtractionJobPhase.PUBLISHING, 95)
            _attach_revision(
                job.pk,
                content_sha256=content_sha256,
                staged=None,
                existing_revision=reusable,
            )
        else:
            _update_job_progress(job.pk, PDFExtractionJobPhase.EXTRACTING, 20)
            expected_content_sha256 = content_sha256
            staged = extraction_runner(path, extraction_heartbeat)
            if not isinstance(staged, StagedPDFExtraction):
                raise PDFIndexingError(
                    "invalid_extractor_response",
                    "The isolated PDF extractor returned an invalid response.",
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
            _update_job_progress(job.pk, PDFExtractionJobPhase.PUBLISHING, 95)
            _attach_revision(
                job.pk,
                content_sha256=content_sha256,
                staged=staged,
            )
    except PDFExtractionExcluded:
        PDFExtractionJob.objects.filter(pk=job.pk, status=PDFExtractionJobStatus.RUNNING).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=timezone.now(),
            error_code="pdf_refresh_excluded",
            error_summary="This PDF is excluded from refresh and text extraction.",
        )
    except PDFExtractionDeferred:
        raise
    except PDFExtractionTargetChanged as exc:
        _fail_job(job.pk, exc, content_sha256=content_sha256)
        _queue_current_target_after_obsolete_job(job.pk)
    except PDFIndexingError as exc:
        _fail_job(job.pk, exc, content_sha256=content_sha256)
    except (OperationalError, IntegrityError):
        raise
    except Exception:
        _fail_job(
            job.pk,
            PDFIndexingError(
                "pdf_indexing_worker_error",
                "The background PDF worker stopped unexpectedly. Retry this PDF.",
            ),
            content_sha256=content_sha256,
        )
    return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)


def execute_claimed_extraction_job(
    job_id: int,
    *,
    extraction_runner: ExtractionRunner = run_isolated_pdf_extractor,
) -> PDFExtractionJob:
    """Keep a checkout stable and defer if synchronization overtakes the claim."""

    job = PDFExtractionJob.objects.select_related("document__repository").get(pk=job_id)
    if job.status != PDFExtractionJobStatus.RUNNING:
        return job
    try:
        # Git uses this same cross-process gate. A sync queued after a PDF was
        # claimed is detected before reads, on parser heartbeats, and before
        # publication. An in-flight parser is stopped by its heartbeat handler
        # before this lock is released for a waiting repository worker.
        with repository_checkout_lock(job.document.repository_id, blocking=False):
            _raise_if_repository_sync_active()
            return _execute_claimed_extraction_job(
                job.pk,
                extraction_runner=extraction_runner,
            )
    except (PDFExtractionDeferred, RepositoryCheckoutBusy):
        PDFExtractionJob.objects.filter(
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
        return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)
    except OSError:
        _fail_job(
            job.pk,
            PDFIndexingError(
                "pdf_checkout_lock_unavailable",
                "OWL could not safely lock the repository folder for PDF extraction. Retry this PDF.",
            ),
        )
        return PDFExtractionJob.objects.select_related("document__repository").get(pk=job.pk)


def work_one_extraction_job() -> PDFExtractionJob | None:
    job = claim_next_extraction_job()
    if job is None:
        return None
    completed = execute_claimed_extraction_job(job.pk)
    # A deferred lease is idle work, not a completion. Let worker commands use
    # their normal polling delay rather than repeatedly reclaiming a busy PDF.
    return None if completed.status == PDFExtractionJobStatus.QUEUED else completed


def launch_index_worker() -> subprocess.Popen[bytes]:
    """Start the detached durable PDF queue worker without blocking the request."""

    return subprocess.Popen(
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
