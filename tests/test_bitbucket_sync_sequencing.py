from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.utils import timezone

from bitbucket_search import views as bitbucket_views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import pdf_catalog, pdf_indexing, repository_sync
from bitbucket_search.services.git_sync import (
    DocumentStats,
    RepositorySyncResult,
    managed_repository_path,
)
from bitbucket_search.services.pdf_catalog import CatalogBuild, CatalogPDF
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    repository_checkout_lock,
)

pytestmark = pytest.mark.django_db
_CONTENT = b"%PDF synthetic isolated sequencing fixture"
_COMMIT = "b" * 40
_BLOB = "a" * 40


@pytest.fixture(autouse=True)
def isolated_repository_roots(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"


def _repository(name: str = "Sequence Docs") -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{name.casefold().replace(' ', '-')}",
        remote_url="https://example.invalid/team/documents.git",
        sync_state=RepositorySyncState.READY,
    )


def _checkout(repository: BitbucketRepository) -> Path:
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    (checkout / "docs").mkdir(exist_ok=True)
    (checkout / "docs" / "Sequence.pdf").write_bytes(_CONTENT)
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    return checkout


def _result() -> RepositorySyncResult:
    return RepositorySyncResult(
        branch="main",
        source_commit="",
        result_commit=_COMMIT,
        documents=DocumentStats(pdf_count=1, vsdx_count=0, document_bytes=len(_CONTENT)),
    )


def _catalog() -> CatalogBuild:
    return CatalogBuild(
        documents=(
            CatalogPDF(
                filename="Sequence.pdf",
                relative_path="docs/Sequence.pdf",
                file_size=len(_CONTENT),
                git_blob_id=_BLOB,
                added_evidence=PDFDocumentAddedEvidence.NOT_FOUND,
                added_commit=None,
                last_commit=None,
            ),
        ),
        history_is_shallow=False,
    )


def _stage(path: Path) -> pdf_indexing.StagedPDFExtraction:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    text = "Synthetic searchable text"
    return pdf_indexing.StagedPDFExtraction(
        state=PDFTextRevisionState.READY,
        pages=(
            pdf_indexing.StagedPDFPage(
                page_number=1,
                text=text,
                character_count=len(text),
                state=PDFPageExtractionState.READY,
            ),
        ),
        page_count=1,
        extracted_character_count=len(text),
        source_size_bytes=path.stat().st_size,
        content_sha256_before=digest,
        content_sha256_after=digest,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def _queued_pdf(repository: BitbucketRepository) -> PDFExtractionJob:
    _checkout(repository)
    PDFDocument.objects.create(
        repository=repository,
        filename="Sequence.pdf",
        relative_path="docs/Sequence.pdf",
        file_size=len(_CONTENT),
        git_blob_id=_BLOB,
        last_seen_commit=_COMMIT,
    )
    queued = pdf_indexing.queue_repository_pdf_extractions(repository)
    return PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0])


def _active_sync(repository: BitbucketRepository, status: str) -> RepositorySyncJob:
    return RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        status=status,
        started_at=timezone.now() if status == RepositorySyncJobStatus.RUNNING else None,
        heartbeat_at=timezone.now(),
    )


@pytest.mark.parametrize("existing_checkout", [False, True], ids=["clone", "pull"])
def test_sync_catalogue_publication_and_extraction_are_ordered(monkeypatch, existing_checkout):
    repository = _repository()
    if existing_checkout:
        _checkout(repository)
    queued = repository_sync.queue_repository_refresh(repository.pk)
    claimed = repository_sync.claim_next_job()
    assert claimed.pk == queued.job.pk
    assert claimed.operation == (
        RepositorySyncOperation.REFRESH if existing_checkout else RepositorySyncOperation.CLONE
    )
    events = []

    def synchronize(target, **_kwargs):
        events.append("sync")
        _checkout(target)
        assert not PDFDocument.objects.exists()
        assert not PDFExtractionJob.objects.exists()
        return _result()

    def catalogue(target, **_kwargs):
        events.append("catalogue")
        assert (managed_repository_path(target) / "docs" / "Sequence.pdf").is_file()
        assert not PDFDocument.objects.exists()
        return _catalog()

    def publish(*args, **kwargs):
        events.append("publish")
        assert not PDFExtractionJob.objects.exists()
        return pdf_catalog.publish_repository_pdf_catalog(*args, **kwargs)

    def queue_extractions(*args, **kwargs):
        events.append("queue extraction")
        assert PDFDocument.objects.get().git_blob_id == _BLOB
        assert not PDFTextRevision.objects.exists()
        return pdf_indexing.queue_repository_pdf_extractions(*args, **kwargs)

    monkeypatch.setattr(repository_sync, "synchronize_repository", synchronize)
    monkeypatch.setattr(repository_sync, "build_repository_pdf_catalog", catalogue)
    monkeypatch.setattr(repository_sync, "publish_repository_pdf_catalog", publish)
    monkeypatch.setattr(repository_sync, "queue_repository_pdf_extractions", queue_extractions)

    completed = repository_sync.execute_claimed_job(claimed.pk)

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert events == ["sync", "catalogue", "publish", "queue extraction"]
    repository.refresh_from_db()
    assert repository.local_path == str(managed_repository_path(repository))
    assert repository.pdf_count == 1
    assert repository.metadata_indexed_commit == _COMMIT
    document = PDFDocument.objects.get()
    assert document.index_state == PDFIndexState.PENDING
    extraction = PDFExtractionJob.objects.get()
    assert extraction.status == PDFExtractionJobStatus.QUEUED
    assert extraction.repository_sync_job_id == completed.pk
    assert not PDFTextRevision.objects.exists()

    def extract(path, _heartbeat):
        events.append("extract")
        assert RepositorySyncJob.objects.get(pk=completed.pk).status == "succeeded"
        return _stage(path)

    extraction = pdf_indexing.claim_next_extraction_job()
    finished = pdf_indexing.execute_claimed_extraction_job(extraction.pk, extraction_runner=extract)
    assert finished.status == PDFExtractionJobStatus.SUCCEEDED
    assert events[-1] == "extract"
    document.refresh_from_db()
    assert document.index_state == PDFIndexState.READY


@pytest.mark.parametrize(
    ("trigger", "automatic_retry_number", "retry_failed", "recover_interrupted"),
    [
        (RepositorySyncTrigger.MANUAL, 0, True, False),
        (RepositorySyncTrigger.DAILY, 0, False, True),
        (RepositorySyncTrigger.RETRY, 1, False, True),
    ],
)
def test_sync_trigger_selects_explicit_or_bounded_pdf_recovery_policy(
    monkeypatch, trigger, automatic_retry_number, retry_failed, recover_interrupted
):
    repository = _repository()
    _checkout(repository)
    # Persist valid trigger metadata rather than modifying only the Python job:
    # automatic runs require a day, and retry runs require a positive attempt.
    queued = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=trigger,
        scheduled_day=timezone.localdate() if trigger != RepositorySyncTrigger.MANUAL else None,
        automatic_retry_number=automatic_retry_number,
    )
    claimed = repository_sync.claim_next_job()
    assert claimed is not None and claimed.pk == queued.pk
    monkeypatch.setattr(repository_sync, "synchronize_repository", lambda *_a, **_k: _result())
    monkeypatch.setattr(
        repository_sync, "build_repository_pdf_catalog", lambda *_a, **_k: _catalog()
    )
    queue_extractions = Mock(wraps=pdf_indexing.queue_repository_pdf_extractions)
    monkeypatch.setattr(repository_sync, "queue_repository_pdf_extractions", queue_extractions)

    completed = repository_sync.execute_claimed_job(claimed.pk)

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    queue_extractions.assert_called_once_with(
        repository.pk,
        repository_sync_job=completed.pk,
        retry_failed=retry_failed,
        recover_interrupted=recover_interrupted,
    )
    extraction = PDFExtractionJob.objects.get()
    assert extraction.repository_sync_job_id == completed.pk
    assert extraction.status == PDFExtractionJobStatus.QUEUED
    assert completed.trigger == trigger
    assert completed.automatic_retry_number == automatic_retry_number


@pytest.mark.parametrize("pending_count", (1, 3))
def test_sync_summary_does_not_call_pending_documents_up_to_date(pending_count):
    repository = _repository()
    repository.last_sync_successful_at = timezone.now()
    extraction = pdf_indexing.ExtractionStatusSnapshot(
        queued_jobs=0,
        running_jobs=0,
        failed_jobs=0,
        interrupted_jobs=0,
        pending_documents=pending_count,
        indexed_documents=0,
        stale_documents=0,
    )

    summary = bitbucket_views._sync_summary(
        (repository,), extraction, automation={"state": "up_to_date"}
    )

    assert summary["state"] == "attention"
    assert summary["label"] == "Indexing pending"
    assert summary["detail"] == (
        f"{pending_count} PDF{'s' if pending_count != 1 else ''} awaiting indexing"
    )
    assert summary["last_completed"] == repository.last_sync_successful_at


def test_same_named_repositories_have_separate_managed_folders(settings):
    first = _repository("Shared Name")
    second = _repository("Another Repository")
    second.display_name = first.display_name
    second.save(update_fields=("display_name", "updated_at"))

    first_path = _checkout(first)
    second_path = _checkout(second)

    assert first_path != second_path
    assert first_path.parent == second_path.parent == settings.BITBUCKET_REPOSITORIES_ROOT
    assert first_path.name.startswith(f"{first.pk}-")
    assert second_path.name.startswith(f"{second.pk}-")


@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_extraction_claims_wait_for_repository_sync(status):
    repository = _repository()
    extraction = _queued_pdf(repository)
    _active_sync(repository, status)

    assert pdf_indexing.claim_next_extraction_job() is None
    extraction.refresh_from_db()
    assert extraction.status == PDFExtractionJobStatus.QUEUED


def test_extraction_workers_are_capped_per_repository_and_reuse_released_slot(settings):
    settings.PDF_MAX_EXTRACTION_WORKERS = 10
    settings.PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY = 3
    repository = _repository("Worker distribution")
    checkout = _checkout(repository)
    for number in range(5):
        relative_path = f"docs/Worker-{number}.pdf"
        path = checkout / relative_path
        path.write_bytes(_CONTENT + str(number).encode())
        PDFDocument.objects.create(
            repository=repository,
            filename=path.name,
            relative_path=relative_path,
            file_size=path.stat().st_size,
            git_blob_id=f"{number + 1:040x}",
            last_seen_commit=_COMMIT,
        )
    pdf_indexing.queue_repository_pdf_extractions(repository)

    running = [pdf_indexing.claim_next_extraction_job() for _number in range(3)]

    assert all(running)
    assert pdf_indexing.claim_next_extraction_job() is None
    PDFExtractionJob.objects.filter(pk=running[0].pk).update(
        status=PDFExtractionJobStatus.SUCCEEDED,
        completed_at=timezone.now(),
    )
    replacement = pdf_indexing.claim_next_extraction_job()
    assert replacement is not None
    assert replacement.document.repository_id == repository.pk
    assert (
        PDFExtractionJob.objects.filter(
            document__repository=repository,
            status=PDFExtractionJobStatus.RUNNING,
        ).count()
        == 3
    )


@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_new_sync_after_extraction_claim_defers_execution_without_failure(status):
    repository = _repository()
    _queued_pdf(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    sync = _active_sync(repository, status)
    runner = Mock(side_effect=AssertionError("extractor must not start during sync"))

    deferred = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=runner)

    runner.assert_not_called()
    assert deferred.status == PDFExtractionJobStatus.QUEUED
    assert deferred.retry_count == claimed.retry_count
    assert deferred.started_at is None
    assert deferred.worker_pid is None
    assert deferred.error_code == deferred.error_summary == ""
    assert not PDFTextRevision.objects.exists()
    sync.status = RepositorySyncJobStatus.SUCCEEDED
    sync.completed_at = timezone.now()
    sync.save(update_fields=("status", "completed_at"))
    resumed = pdf_indexing.claim_next_extraction_job()
    assert resumed.pk == claimed.pk
    finished = pdf_indexing.execute_claimed_extraction_job(
        resumed.pk,
        extraction_runner=lambda path, _heartbeat: _stage(path),
    )
    assert finished.status == PDFExtractionJobStatus.SUCCEEDED


@pytest.mark.parametrize("state", [RepositorySyncState.FAILED, RepositorySyncState.INTERRUPTED])
def test_failed_catalogue_repository_does_not_block_claims_for_a_ready_repository(state):
    failed_repository = _repository("Failed catalogue")
    waiting = _queued_pdf(failed_repository)
    failed_repository.sync_state = state
    failed_repository.save(update_fields=("sync_state", "updated_at"))
    ready_repository = _repository("Ready catalogue")
    ready = _queued_pdf(ready_repository)

    claimed = pdf_indexing.claim_next_extraction_job()

    assert claimed.pk == ready.pk
    waiting.refresh_from_db()
    assert waiting.status == PDFExtractionJobStatus.QUEUED


@pytest.mark.parametrize("state", [RepositorySyncState.FAILED, RepositorySyncState.INTERRUPTED])
def test_claimed_extraction_waits_for_a_successfully_published_checkout(state):
    repository = _repository()
    _queued_pdf(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    # Simulate a pull changing bytes but the subsequent catalogue publication
    # failing. Equal file sizes cannot establish that the old blob is current.
    pdf_path = managed_repository_path(repository) / "docs" / "Sequence.pdf"
    pdf_path.write_bytes(b"X" * len(_CONTENT))
    repository.sync_state = state
    repository.save(update_fields=("sync_state", "updated_at"))
    runner = Mock(side_effect=AssertionError("unpublished checkout must not be extracted"))

    deferred = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=runner)

    runner.assert_not_called()
    assert deferred.status == PDFExtractionJobStatus.QUEUED
    assert not PDFTextRevision.objects.exists()
    assert pdf_indexing.claim_next_extraction_job() is None


@pytest.mark.parametrize("use_heartbeat", [False, True], ids=["before-publish", "heartbeat"])
def test_new_sync_during_extraction_defers_before_publication(use_heartbeat):
    repository = _repository()
    _queued_pdf(repository)
    claimed = pdf_indexing.claim_next_extraction_job()

    def extract(path, heartbeat):
        with (
            pytest.raises(RepositoryCheckoutBusy),
            repository_checkout_lock(repository.pk, blocking=False),
        ):
            pytest.fail("extraction must retain its exclusive checkout lock")
        _active_sync(repository, RepositorySyncJobStatus.QUEUED)
        if use_heartbeat:
            heartbeat()
            pytest.fail("a sync must defer the parser on its next heartbeat")
        return _stage(path)

    deferred = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=extract)

    assert deferred.status == PDFExtractionJobStatus.QUEUED
    assert not PDFTextRevision.objects.exists()
    deferred.document.refresh_from_db()
    assert deferred.document.index_state == PDFIndexState.PENDING
    with repository_checkout_lock(repository.pk, blocking=False):
        pass


def test_busy_checkout_defers_extraction_without_reading_the_pdf():
    repository = _repository()
    _queued_pdf(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    runner = Mock(side_effect=AssertionError("busy checkout must not be read"))

    with repository_checkout_lock(repository.pk, blocking=False):
        deferred = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=runner)

    runner.assert_not_called()
    assert deferred.status == PDFExtractionJobStatus.QUEUED
    assert deferred.error_code == ""


def test_checkout_lock_io_failure_is_reported_without_raw_exception_text(monkeypatch):
    repository = _repository()
    _queued_pdf(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    monkeypatch.setattr(
        pdf_indexing,
        "repository_checkout_lock",
        Mock(side_effect=PermissionError("simulated-token-never-valid /private/repository")),
    )
    runner = Mock(side_effect=AssertionError("unlocked checkout must not be extracted"))

    failed = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=runner)

    runner.assert_not_called()
    assert failed.status == PDFExtractionJobStatus.FAILED
    assert failed.error_code == "pdf_checkout_lock_unavailable"
    assert "simulated-token" not in failed.error_summary
    assert "/private/repository" not in failed.error_summary


def test_deferred_extraction_reports_idle_work_to_avoid_a_worker_hot_loop():
    repository = _repository()
    queued = _queued_pdf(repository)

    with repository_checkout_lock(repository.pk, blocking=False):
        assert pdf_indexing.work_one_extraction_job() is None

    queued.refresh_from_db()
    assert queued.status == PDFExtractionJobStatus.QUEUED
    assert queued.retry_count == 0


def test_sync_deferral_heartbeat_terminates_and_reaps_the_isolated_parser(monkeypatch, tmp_path):
    parser = Mock()
    parser.args = ["synthetic-extractor"]
    parser.poll.return_value = None
    parser.wait.side_effect = [subprocess.TimeoutExpired(parser.args, 1), None]
    monkeypatch.setattr(pdf_indexing.subprocess, "Popen", Mock(return_value=parser))
    heartbeat = Mock(side_effect=pdf_indexing.PDFExtractionDeferred)

    with pytest.raises(pdf_indexing.PDFExtractionDeferred):
        pdf_indexing.run_isolated_pdf_extractor(tmp_path / "synthetic.pdf", heartbeat)

    heartbeat.assert_called_once_with()
    parser.kill.assert_called_once_with()
    assert parser.wait.call_count == 2


@pytest.mark.parametrize(
    ("function", "stage", "stage_label", "error_type"),
    [
        ("synchronize_repository", "git_sync", "Git synchronization", RuntimeError),
        (
            "build_repository_pdf_catalog",
            "pdf_catalog_build",
            "PDF catalogue discovery",
            ValueError,
        ),
        (
            "publish_repository_pdf_catalog",
            "pdf_catalog_publish",
            "PDF catalogue publication",
            TypeError,
        ),
        (
            "queue_repository_pdf_extractions",
            "pdf_extraction_queue",
            "PDF extraction queueing",
            RuntimeError,
        ),
    ],
)
def test_unexpected_sync_failure_records_safe_stage_and_type(
    monkeypatch, caplog, function, stage, stage_label, error_type
):
    repository = _repository()
    _checkout(repository)
    repository_sync.queue_repository_refresh(repository.pk)
    claimed = repository_sync.claim_next_job()
    monkeypatch.setattr(repository_sync, "synchronize_repository", lambda *_a, **_k: _result())
    monkeypatch.setattr(
        repository_sync, "build_repository_pdf_catalog", lambda *_a, **_k: _catalog()
    )
    private_message = "simulated-token-never-valid /private/checkout synthetic-private-message"

    def fail(*_args, **_kwargs):
        raise error_type(private_message)

    monkeypatch.setattr(repository_sync, function, fail)
    completed = repository_sync.execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.INTERRUPTED
    assert completed.error_code == repository.last_error_code == "worker_error"
    assert stage_label in completed.error_summary
    assert error_type.__name__ in completed.error_summary
    assert repository.last_error_summary == completed.error_summary
    assert f"stage={stage}" in caplog.text
    assert f"error_type={error_type.__name__}" in caplog.text
    assert private_message not in completed.error_summary + caplog.text
    assert "simulated-token" not in completed.error_summary + caplog.text
    assert "Traceback" not in caplog.text
    assert not PDFDocument.objects.exists()
    assert not PDFExtractionJob.objects.exists()
    if function in {"publish_repository_pdf_catalog", "queue_repository_pdf_extractions"}:
        assert completed.phase == RepositorySyncPhase.FINALIZING


@pytest.mark.parametrize("sqlite_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_driver_confirmed_database_contention_has_a_specific_safe_diagnostic(
    monkeypatch, sqlite_code
):
    repository = _repository()
    repository_sync.queue_repository_refresh(repository.pk)
    claimed = repository_sync.claim_next_job()

    def busy(*_args, **_kwargs):
        cause = sqlite3.OperationalError("simulated-token-never-valid")
        cause.sqlite_errorcode = sqlite_code
        raise OperationalError("simulated-token-never-valid") from cause

    monkeypatch.setattr(repository_sync, "synchronize_repository", busy)
    completed = repository_sync.execute_claimed_job(claimed.pk)

    assert completed.error_code == "database_busy"
    assert "local database was busy" in completed.error_summary
    assert f"SQLite code {sqlite_code}" in completed.error_summary
    assert "simulated-token" not in completed.error_summary


def test_unconfirmed_operational_error_is_not_mislabeled_as_database_contention(monkeypatch):
    repository = _repository()
    repository_sync.queue_repository_refresh(repository.pk)
    claimed = repository_sync.claim_next_job()

    def unknown_failure(*_args, **_kwargs):
        raise OperationalError("database is locked; simulated-token-never-valid")

    monkeypatch.setattr(repository_sync, "synchronize_repository", unknown_failure)
    completed = repository_sync.execute_claimed_job(claimed.pk)

    assert completed.error_code == "worker_error"
    assert "OperationalError" in completed.error_summary
    assert "database was busy" not in completed.error_summary
    assert "simulated-token" not in completed.error_summary
