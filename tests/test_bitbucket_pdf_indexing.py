from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
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
    RepositoryOperationLogEntry,
    RepositoryOperationLogSeverity,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.pdf_indexing import (
    PDF_INDEX_VERSION,
    _extractor_environment,
    StagedPDFExtraction,
    StagedPDFPage,
    claim_next_extraction_job,
    execute_claimed_extraction_job,
    extraction_status_snapshot,
    launch_index_worker,
    mark_index_worker_launch_failed,
    queue_repository_pdf_extractions,
    run_isolated_pdf_extractor,
    sweep_pdf_extraction_queue,
)

pytestmark = pytest.mark.django_db


def test_extractor_keeps_python_user_site_visible_for_windows_user_installs(monkeypatch):
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")

    environment = _extractor_environment()

    assert "PYTHONNOUSERSITE" not in environment
    assert environment["PYTHONUTF8"] == "1"


@pytest.fixture
def indexed_pdf_target(tmp_path: Path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "tmp"
    settings.PDF_MAX_FILE_BYTES = 1024 * 1024
    settings.PDF_MAX_PAGES = 100
    settings.PDF_MAX_PAGE_TEXT_CHARS = 100_000
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 1_000_000
    repository = BitbucketRepository.objects.create(
        display_name="Architecture Docs",
        canonical_remote_key="example.invalid/workspace/architecture-docs",
        remote_url="https://example.invalid/workspace/architecture-docs.git",
        sync_state=RepositorySyncState.READY,
        last_synced_commit="b" * 40,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs").mkdir()
    pdf_path = checkout / "docs" / "Architecture.pdf"
    pdf_path.write_bytes(b"%PDF synthetic architecture revision")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Architecture.pdf",
        file_size=pdf_path.stat().st_size,
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )
    return repository, document, pdf_path


def _ready_stage(path: Path, *texts: str) -> StagedPDFExtraction:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    pages = tuple(
        StagedPDFPage(
            page_number=index,
            text=text,
            character_count=len(text),
            state=(PDFPageExtractionState.READY if text else PDFPageExtractionState.NO_TEXT),
        )
        for index, text in enumerate(texts, start=1)
    )
    characters = sum(page.character_count for page in pages)
    return StagedPDFExtraction(
        state=PDFTextRevisionState.READY if characters else PDFTextRevisionState.NO_TEXT,
        pages=pages,
        page_count=len(pages),
        extracted_character_count=characters,
        source_size_bytes=path.stat().st_size,
        content_sha256_before=content_hash,
        content_sha256_after=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def _failed_stage(path: Path, state: str = "corrupt") -> StagedPDFExtraction:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return StagedPDFExtraction(
        state=state,
        pages=(),
        page_count=0,
        extracted_character_count=0,
        source_size_bytes=path.stat().st_size,
        content_sha256_before=content_hash,
        content_sha256_after=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def test_queue_is_idempotent_and_same_indexed_blob_queues_zero(indexed_pdf_target):
    repository, document, pdf_path = indexed_pdf_target

    first = queue_repository_pdf_extractions(repository)
    second = queue_repository_pdf_extractions(repository)

    assert len(first.queued_job_ids) == 1
    assert second.queued_job_ids == ()
    assert PDFExtractionJob.objects.count() == 1

    claimed = claim_next_extraction_job()
    execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(
            pdf_path,
            "Private network architecture",
        ),
    )

    after_success = queue_repository_pdf_extractions(repository)
    document.refresh_from_db()
    assert after_success.queued_job_ids == ()
    assert document.indexed_git_blob_id == document.git_blob_id
    assert document.index_state == PDFIndexState.READY


def test_claim_path_never_runs_repository_wide_reconciliation(
    indexed_pdf_target,
    monkeypatch,
):
    repository, _document, _pdf_path = indexed_pdf_target
    queue_repository_pdf_extractions(repository)
    reconcile = Mock(side_effect=AssertionError("claim must stay targeted"))
    monkeypatch.setattr(
        "bitbucket_search.services.pdf_indexing.sweep_pdf_extraction_queue",
        reconcile,
    )

    assert claim_next_extraction_job() is not None
    reconcile.assert_not_called()


def test_empty_claim_does_not_take_a_sqlite_write_lock():
    with CaptureQueriesContext(connection) as queries:
        assert claim_next_extraction_job() is None

    statements = tuple(query["sql"].lstrip().upper() for query in queries)
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for statement in statements
    )


def test_large_repository_queue_and_idle_sweep_use_bounded_queries(
    indexed_pdf_target,
):
    repository, _document, _pdf_path = indexed_pdf_target
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Scale-{index}.pdf",
                relative_path=f"docs/Scale-{index}.pdf",
                file_size=100 + index,
                git_blob_id=f"{index:040x}",
                last_seen_commit="b" * 40,
            )
            for index in range(1, 80)
        ]
    )

    with CaptureQueriesContext(connection) as queue_queries:
        queued = queue_repository_pdf_extractions(repository)

    assert len(queued.queued_job_ids) == 80
    assert len(queue_queries) <= 12

    with CaptureQueriesContext(connection) as sweep_queries:
        swept = sweep_pdf_extraction_queue()

    assert swept.queued_job_ids == ()
    assert len(sweep_queries) <= 8


def test_five_thousand_document_queue_keeps_batched_query_shape(indexed_pdf_target):
    repository, _document, _pdf_path = indexed_pdf_target
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Corpus-{index:05d}.pdf",
                relative_path=f"corpus/Corpus-{index:05d}.pdf",
                file_size=1_024 + index,
                git_blob_id=f"{index:040x}",
                last_seen_commit="b" * 40,
            )
            for index in range(1, 5_000)
        ],
        batch_size=500,
    )

    with CaptureQueriesContext(connection) as queries:
        queued = queue_repository_pdf_extractions(repository)

    assert len(queued.queued_job_ids) == 5_000
    assert PDFExtractionJob.objects.filter(document__repository=repository).count() == 5_000
    assert len(queries) <= 50


def test_bulk_unavailable_cancellation_uses_bounded_log_writes(indexed_pdf_target):
    repository, _document, _pdf_path = indexed_pdf_target
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Removed-{index}.pdf",
                relative_path=f"removed/Removed-{index}.pdf",
                file_size=100 + index,
                git_blob_id=f"{index:040x}",
                last_seen_commit="b" * 40,
            )
            for index in range(1, 80)
        ]
    )
    queued = queue_repository_pdf_extractions(repository)
    PDFDocument.objects.filter(repository=repository).update(
        lifecycle_state=PDFDocumentLifecycle.REMOVED
    )

    with CaptureQueriesContext(connection) as queries:
        cancelled = queue_repository_pdf_extractions(repository)

    assert len(cancelled.cancelled_job_ids) == len(queued.queued_job_ids) == 80
    assert (
        PDFExtractionJob.objects.filter(
            pk__in=cancelled.cancelled_job_ids,
            status=PDFExtractionJobStatus.CANCELLED,
            error_code="extraction_target_unavailable",
        ).count()
        == 80
    )
    assert (
        RepositoryOperationLogEntry.objects.filter(
            extraction_job_id__in=cancelled.cancelled_job_ids,
            event="indexing_cancelled",
        ).count()
        == 80
    )
    assert len(queries) <= 20


def test_claim_waits_until_all_repository_sync_jobs_are_terminal(indexed_pdf_target):
    repository, _document, _pdf_path = indexed_pdf_target
    queue_repository_pdf_extractions(repository)
    repository_job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.QUEUED,
    )

    assert claim_next_extraction_job() is None

    repository_job.status = RepositorySyncJobStatus.SUCCEEDED
    repository_job.completed_at = timezone.now()
    repository_job.save(update_fields=("status", "completed_at"))
    assert claim_next_extraction_job() is not None


def test_global_worker_limit_and_stale_lease_recovery_are_durable(
    indexed_pdf_target,
    settings,
):
    repository, first_document, first_path = indexed_pdf_target
    settings.PDF_MAX_EXTRACTION_WORKERS = 1
    second_path = first_path.with_name("Second.pdf")
    second_path.write_bytes(b"%PDF second queued target")
    PDFDocument.objects.create(
        repository=repository,
        filename=second_path.name,
        relative_path="docs/Second.pdf",
        file_size=second_path.stat().st_size,
        git_blob_id="2" * 40,
        last_seen_commit="b" * 40,
    )
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()

    assert claim_next_extraction_job() is None

    stale_at = timezone.now() - timedelta(minutes=11)
    PDFExtractionJob.objects.filter(pk=claimed.pk).update(heartbeat_at=stale_at)
    sweep_pdf_extraction_queue()
    replacement_claim = claim_next_extraction_job()
    claimed.refresh_from_db()
    first_document.refresh_from_db()
    assert claimed.status == PDFExtractionJobStatus.INTERRUPTED
    assert first_document.index_state == PDFIndexState.PENDING
    assert PDFExtractionJob.objects.filter(
        document=first_document,
        status=PDFExtractionJobStatus.QUEUED,
        retry_count=1,
    ).exists()
    assert replacement_claim is not None
    assert replacement_claim.pk != claimed.pk


def test_startup_sweep_backfills_active_pending_document_without_a_job(
    indexed_pdf_target,
):
    _repository, document, _pdf_path = indexed_pdf_target

    result = sweep_pdf_extraction_queue()

    assert len(result.queued_job_ids) == 1
    job = PDFExtractionJob.objects.get(pk=result.queued_job_ids[0])
    assert job.document_id == document.pk
    assert job.status == PDFExtractionJobStatus.QUEUED
    assert job.retry_count == 0


def test_startup_sweep_immediately_retires_running_lease_and_queues_retry(
    indexed_pdf_target,
    settings,
):
    repository, document, _pdf_path = indexed_pdf_target
    settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES = 2
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()

    result = sweep_pdf_extraction_queue(interrupt_running=True)

    claimed.refresh_from_db()
    document.refresh_from_db()
    assert claimed.status == PDFExtractionJobStatus.INTERRUPTED
    assert claimed.error_code == "extraction_worker_interrupted"
    assert document.index_state == PDFIndexState.PENDING
    retry = PDFExtractionJob.objects.get(pk=result.queued_job_ids[0])
    assert retry.document_id == document.pk
    assert retry.status == PDFExtractionJobStatus.QUEUED
    assert retry.retry_count == 1


def test_crashed_current_job_retries_are_bounded(indexed_pdf_target, settings):
    repository, document, _pdf_path = indexed_pdf_target
    settings.PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES = 2
    queue_repository_pdf_extractions(repository)

    for expected_retry in (1, 2):
        claimed = claim_next_extraction_job()
        assert claimed.retry_count == expected_retry - 1
        recovered = sweep_pdf_extraction_queue(interrupt_running=True)
        retry = PDFExtractionJob.objects.get(pk=recovered.queued_job_ids[0])
        assert retry.retry_count == expected_retry

    final_claim = claim_next_extraction_job()
    assert final_claim.retry_count == 2
    exhausted = sweep_pdf_extraction_queue(interrupt_running=True)

    document.refresh_from_db()
    assert exhausted.queued_job_ids == ()
    assert document.index_state == PDFIndexState.FAILED
    assert not PDFExtractionJob.objects.filter(
        document=document,
        status__in=(
            PDFExtractionJobStatus.QUEUED,
            PDFExtractionJobStatus.RUNNING,
        ),
    ).exists()
    snapshot = extraction_status_snapshot()
    assert snapshot.interrupted_jobs == 1
    assert snapshot.failed_jobs == 0


def test_sweep_cancels_mismatched_running_job_and_queues_current_target(
    indexed_pdf_target,
):
    repository, document, _pdf_path = indexed_pdf_target
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()
    document.git_blob_id = "9" * 40
    document.last_seen_commit = "8" * 40
    document.save(update_fields=("git_blob_id", "last_seen_commit"))

    result = sweep_pdf_extraction_queue()

    claimed.refresh_from_db()
    assert claimed.status == PDFExtractionJobStatus.CANCELLED
    replacement = PDFExtractionJob.objects.get(pk=result.queued_job_ids[0])
    assert replacement.document_id == document.pk
    assert replacement.target_git_blob_id == document.git_blob_id
    assert replacement.target_source_commit == document.last_seen_commit
    cancellation = claimed.operation_log_entries.get(event="indexing_cancelled")
    assert claimed.error_code == "extraction_target_changed"
    assert cancellation.severity == RepositoryOperationLogSeverity.WARNING
    assert cancellation.phase == claimed.phase
    assert cancellation.progress == claimed.progress
    assert cancellation.occurred_at == claimed.completed_at
    assert cancellation.message == (
        "PDF indexing was cancelled because a newer PDF revision replaced this extraction."
    )

    sweep_pdf_extraction_queue()

    assert claimed.operation_log_entries.filter(event="indexing_cancelled").count() == 1


def test_repository_queue_logs_unavailable_target_cancellation_once(indexed_pdf_target):
    repository, document, _pdf_path = indexed_pdf_target
    queued = queue_repository_pdf_extractions(repository)
    job = PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0])
    PDFDocument.objects.filter(pk=document.pk).update(lifecycle_state=PDFDocumentLifecycle.REMOVED)

    first = queue_repository_pdf_extractions(repository)
    second = queue_repository_pdf_extractions(repository)

    job.refresh_from_db()
    assert first.cancelled_job_ids == (job.pk,)
    assert second.cancelled_job_ids == ()
    assert job.status == PDFExtractionJobStatus.CANCELLED
    assert job.error_code == "extraction_target_unavailable"
    cancellation = job.operation_log_entries.get(event="indexing_cancelled")
    assert cancellation.severity == RepositoryOperationLogSeverity.WARNING
    assert cancellation.phase == PDFExtractionJobPhase.QUEUED
    assert cancellation.progress == 0
    assert cancellation.occurred_at == job.completed_at
    assert cancellation.message == (
        "PDF indexing was cancelled because the document is no longer active in an enabled "
        "repository."
    )


def test_successful_parent_publication_attaches_pages_and_fts_atomically(indexed_pdf_target):
    repository, document, pdf_path = indexed_pdf_target
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()

    result = execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(
            pdf_path,
            "Private Link reference",
            "DDoS edge controls",
        ),
    )

    document.refresh_from_db()
    assert result.status == PDFExtractionJobStatus.SUCCEEDED
    assert result.pages_processed == 2
    assert document.index_state == PDFIndexState.READY
    assert document.page_count == 2
    assert document.extracted_character_count == len("Private Link referenceDDoS edge controls")
    assert document.extractor_version == PDF_EXTRACTOR_VERSION
    assert document.index_version == PDF_INDEX_VERSION
    assert list(document.indexed_revision.pages.values_list("page_number", flat=True)) == [1, 2]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rowid FROM bitbucket_search_pdf_page_fts "
            "WHERE bitbucket_search_pdf_page_fts MATCH %s",
            ['"private link"'],
        )
        assert cursor.fetchall()


def test_existing_content_revision_is_reused_without_invoking_parser(indexed_pdf_target):
    repository, document, pdf_path = indexed_pdf_target
    content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    revision = PDFTextRevision.objects.create(
        content_sha256=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
        source_byte_size=pdf_path.stat().st_size,
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=16,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text="Reusable content",
        character_count=16,
        extraction_state=PDFPageExtractionState.READY,
    )
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()
    parser = Mock(side_effect=AssertionError("parser must not run"))

    result = execute_claimed_extraction_job(claimed.pk, extraction_runner=parser)

    document.refresh_from_db()
    assert result.status == PDFExtractionJobStatus.SUCCEEDED
    assert document.indexed_revision_id == revision.pk
    parser.assert_not_called()


def test_failed_changed_pdf_keeps_prior_revision_but_initial_failure_does_not(
    indexed_pdf_target,
):
    repository, stale_document, stale_path = indexed_pdf_target
    old_revision = PDFTextRevision.objects.create(
        content_sha256="c" * 64,
        extractor_version=PDF_EXTRACTOR_VERSION,
        source_byte_size=10,
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=8,
    )
    PDFTextPage.objects.create(
        revision=old_revision,
        page_number=1,
        extracted_text="old text",
        character_count=8,
        extraction_state=PDFPageExtractionState.READY,
    )
    stale_document.indexed_revision = old_revision
    stale_document.indexed_git_blob_id = "f" * 40
    stale_document.indexed_source_commit = "e" * 40
    stale_document.index_state = PDFIndexState.READY
    stale_document.extractor_version = PDF_EXTRACTOR_VERSION
    stale_document.index_version = PDF_INDEX_VERSION
    stale_document.save()

    new_path = stale_path.with_name("Unindexed.pdf")
    new_path.write_bytes(b"%PDF another broken document")
    new_document = PDFDocument.objects.create(
        repository=repository,
        filename=new_path.name,
        relative_path="docs/Unindexed.pdf",
        file_size=new_path.stat().st_size,
        git_blob_id="d" * 40,
        last_seen_commit="b" * 40,
    )
    queue_repository_pdf_extractions(repository)

    first = claim_next_extraction_job()
    first_path = stale_path if first.document_id == stale_document.pk else new_path
    execute_claimed_extraction_job(
        first.pk,
        extraction_runner=lambda _path, _heartbeat: _failed_stage(first_path),
    )
    second = claim_next_extraction_job()
    second_path = stale_path if second.document_id == stale_document.pk else new_path
    execute_claimed_extraction_job(
        second.pk,
        extraction_runner=lambda _path, _heartbeat: _failed_stage(second_path),
    )

    stale_document.refresh_from_db()
    new_document.refresh_from_db()
    assert stale_document.index_state == PDFIndexState.STALE_ERROR
    assert stale_document.indexed_revision_id == old_revision.pk
    assert new_document.index_state == PDFIndexState.FAILED
    assert new_document.indexed_revision_id is None
    assert PDFTextPage.objects.filter(revision=old_revision, extracted_text="old text").exists()


def test_obsolete_running_target_is_cancelled_and_current_target_is_requeued(
    indexed_pdf_target,
):
    repository, document, pdf_path = indexed_pdf_target
    queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()
    document.git_blob_id = "9" * 40
    document.last_seen_commit = "8" * 40
    document.save(update_fields=("git_blob_id", "last_seen_commit"))

    result = execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path, "unused"),
    )

    assert result.status == PDFExtractionJobStatus.CANCELLED
    replacement = PDFExtractionJob.objects.get(status=PDFExtractionJobStatus.QUEUED)
    assert replacement.document_id == document.pk
    assert replacement.target_git_blob_id == "9" * 40
    assert replacement.target_source_commit == "8" * 40


def test_launch_index_worker_is_detached(monkeypatch, settings):
    settings.PDF_EXTRACTION_WORKER_IDLE_SECONDS = 23
    process = object()
    popen = Mock(return_value=process)
    monkeypatch.setattr("bitbucket_search.services.pdf_indexing.subprocess.Popen", popen)

    assert launch_index_worker() is process
    arguments = popen.call_args.args[0]
    assert arguments[-4:] == [
        "bitbucket_index_worker",
        "--idle-timeout",
        "23",
        "--no-startup-sweep",
    ]
    assert "bitbucket_index_worker" in arguments
    assert popen.call_args.kwargs["stdin"] is not None
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")


def test_worker_launch_failure_is_visible_and_preserves_prior_text(indexed_pdf_target):
    repository, document, _pdf_path = indexed_pdf_target
    revision = PDFTextRevision.objects.create(
        content_sha256="7" * 64,
        extractor_version=PDF_EXTRACTOR_VERSION,
        source_byte_size=10,
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=9,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text="old index",
        character_count=9,
        extraction_state=PDFPageExtractionState.READY,
    )
    document.indexed_revision = revision
    document.indexed_git_blob_id = "0" * 40
    document.index_state = PDFIndexState.READY
    document.save()
    queued = queue_repository_pdf_extractions(repository)

    assert mark_index_worker_launch_failed(queued.queued_job_ids) == queued.queued_job_ids

    document.refresh_from_db()
    job = PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0])
    snapshot = extraction_status_snapshot()
    assert job.status == PDFExtractionJobStatus.FAILED
    assert job.error_code == "index_worker_unavailable"
    assert document.index_state == PDFIndexState.STALE_ERROR
    assert document.indexed_revision_id == revision.pk
    assert snapshot.failed_jobs == 1
    assert snapshot.stale_documents == 1


def test_status_snapshot_ignores_historical_failure_after_success(indexed_pdf_target):
    repository, document, pdf_path = indexed_pdf_target
    first = queue_repository_pdf_extractions(repository)
    assert mark_index_worker_launch_failed(first.queued_job_ids) == first.queued_job_ids
    retried = queue_repository_pdf_extractions(repository, retry_failed=True)
    assert len(retried.queued_job_ids) == 1
    claimed = claim_next_extraction_job()

    execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(
            pdf_path,
            "Recovered searchable content",
        ),
    )

    document.refresh_from_db()
    snapshot = extraction_status_snapshot()
    assert document.index_state == PDFIndexState.READY
    assert PDFExtractionJob.objects.filter(
        document=document,
        status=PDFExtractionJobStatus.FAILED,
    ).exists()
    assert snapshot.failed_jobs == 0
    assert snapshot.interrupted_jobs == 0
    assert snapshot.stale_documents == 0


def test_isolated_runner_uses_the_database_free_json_protocol(tmp_path, settings):
    from pypdf import PdfWriter

    settings.BITBUCKET_TEMP_ROOT = tmp_path / "private-stage"
    settings.PDF_MAX_FILE_BYTES = 1024 * 1024
    settings.PDF_MAX_PAGES = 10
    settings.PDF_MAX_PAGE_TEXT_CHARS = 10_000
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 100_000
    settings.PDF_EXTRACTION_TIMEOUT_SECONDS = 10
    settings.PDF_MAX_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as destination:
        writer.write(destination)

    result = run_isolated_pdf_extractor(pdf_path, Mock())

    assert result.state == PDFTextRevisionState.NO_TEXT
    assert result.publishable is True
    assert result.page_count == 1
    assert result.pages[0].page_number == 1
    assert result.pages[0].state == PDFPageExtractionState.NO_TEXT
    assert result.content_sha256_before == result.content_sha256_after
