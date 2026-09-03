from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPageExtractionState,
    PDFTextRevisionState,
    RepositorySyncState,
)
from bitbucket_search.services import pdf_indexing
from bitbucket_search.services import pdf_jsonl_staging as pdf_jsonl
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION

pytestmark = pytest.mark.django_db


class SimulatedProcessCrash(BaseException):
    """Bypass ordinary exception cleanup like an abruptly stopped process."""


@pytest.fixture
def staged_recovery_target(tmp_path: Path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "tmp"
    settings.PDF_MAX_FILE_BYTES = 1024 * 1024
    settings.PDF_MAX_PAGES = 100
    settings.PDF_MAX_PAGE_TEXT_CHARS = 100_000
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 1_000_000
    repository = BitbucketRepository.objects.create(
        display_name="Staging recovery",
        canonical_remote_key="example.invalid/owl/staging-recovery",
        remote_url="https://example.invalid/owl/staging-recovery.git",
        sync_state=RepositorySyncState.READY,
        last_synced_commit="b" * 40,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs").mkdir()
    pdf_path = checkout / "docs" / "Recovery.pdf"
    pdf_path.write_bytes(b"%PDF durable staging recovery")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Recovery.pdf",
        file_size=pdf_path.stat().st_size,
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )
    return repository, document, pdf_path


def _ready_stage(path: Path, text: str = "Recovered durable text"):
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
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
        content_sha256_before=content_sha256,
        content_sha256_after=content_sha256,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def _claimed_job(repository: BitbucketRepository) -> PDFExtractionJob:
    queued = pdf_indexing.queue_repository_pdf_extractions(repository)
    assert len(queued.queued_job_ids) == 1
    claimed = pdf_indexing.claim_next_extraction_job()
    assert claimed is not None
    return claimed


def _seal_staged_records() -> pdf_jsonl.JSONLChunk:
    stager = pdf_jsonl.JSONLStager()
    staged = pdf_indexing.work_one_staging_job(stager)
    assert staged is not None
    sealed = getattr(staged, "sealed_chunk", None) or stager.seal_current()
    assert sealed is not None
    return sealed


def _leave_post_rename_orphan(
    monkeypatch,
    repository: BitbucketRepository,
    pdf_path: Path,
) -> tuple[PDFExtractionJob, Path]:
    claimed = _claimed_job(repository)
    original_fsync_directory = pdf_indexing._fsync_directory

    def crash_after_durable_rename(directory: Path) -> None:
        original_fsync_directory(directory)
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        pdf_indexing,
        "_fsync_directory",
        crash_after_durable_rename,
    )
    with pytest.raises(SimulatedProcessCrash):
        pdf_indexing.execute_claimed_extraction_job(
            claimed.pk,
            extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
            defer_publication=True,
        )
    monkeypatch.setattr(
        pdf_indexing,
        "_fsync_directory",
        original_fsync_directory,
    )
    claimed.refresh_from_db()
    target = pdf_indexing._publication_staging_path(claimed.pk)
    assert target.is_file()
    assert claimed.phase == PDFExtractionJobPhase.EXTRACTING
    return claimed, target


def test_crash_after_rename_before_phase_commit_promotes_matching_orphan(
    staged_recovery_target,
    monkeypatch,
):
    repository, document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["job"] == {
        "id": job.pk,
        "document_id": document.pk,
        "target_git_blob_id": job.target_git_blob_id,
        "target_source_commit": job.target_source_commit,
        "target_relative_path": job.target_relative_path,
        "target_file_size": job.target_file_size,
        "target_extractor_version": PDF_EXTRACTOR_VERSION,
    }
    assert len(payload["content_sha256"]) == 64
    assert len(payload["extraction_sha256"]) == 64

    recovery = pdf_indexing.sweep_pdf_extraction_queue(interrupt_running=True)

    job.refresh_from_db()
    assert recovery.queued_job_ids == ()
    assert PDFExtractionJob.objects.count() == 1
    assert job.status == PDFExtractionJobStatus.RUNNING
    assert job.phase == PDFExtractionJobPhase.PUBLISHING
    assert job.worker_pid is None

    _seal_staged_records()
    published = pdf_indexing.work_one_publication_job()
    document.refresh_from_db()
    assert published is not None
    assert published.status == PDFExtractionJobStatus.SUCCEEDED
    assert document.indexed_revision.pages.get().extracted_text == "Recovered durable text"
    assert not target.exists()


def test_phase_commit_before_writer_claim_remains_ready_and_publishes_once(
    staged_recovery_target,
):
    repository, document, pdf_path = staged_recovery_target
    claimed = _claimed_job(repository)
    staged = pdf_indexing.execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
        defer_publication=True,
    )
    target = pdf_indexing._publication_staging_path(staged.pk)

    result = pdf_indexing.reconcile_staged_publications(force=True)

    staged.refresh_from_db()
    assert result.ready_job_ids == (staged.pk,)
    assert result.promoted_job_ids == ()
    assert staged.phase == PDFExtractionJobPhase.PUBLISHING
    assert staged.worker_pid is None
    assert target.exists()

    _seal_staged_records()
    published = pdf_indexing.work_one_publication_job()
    document.refresh_from_db()
    assert published is not None
    assert published.status == PDFExtractionJobStatus.SUCCEEDED
    assert document.indexed_revision.pages.count() == 1


def test_stage_handoff_fsyncs_file_and_parent_directory(
    staged_recovery_target,
    monkeypatch,
):
    repository, _document, pdf_path = staged_recovery_target
    claimed = _claimed_job(repository)
    original_fsync = os.fsync
    flushed_types: list[str] = []

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        flushed_types.append("directory" if stat.S_ISDIR(mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(pdf_indexing.os, "fsync", recording_fsync)

    pdf_indexing.execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
        defer_publication=True,
    )

    assert "file" in flushed_types
    if os.name != "nt":
        assert "directory" in flushed_types


def test_publication_commit_before_chunk_status_is_replayed_without_duplicate_publication(
    staged_recovery_target,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    from semantic_search import signals

    repository, _document, pdf_path = staged_recovery_target
    claimed = _claimed_job(repository)
    staged = pdf_indexing.execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
        defer_publication=True,
    )
    chunk = _seal_staged_records()
    queued_semantic: list[tuple[str, int]] = []
    monkeypatch.setattr(
        signals,
        "_queue_safely",
        lambda source_type, source_id: queued_semantic.append((source_type, source_id)),
    )
    original_mark_imported = pdf_jsonl.mark_chunk_imported

    def crash_before_imported_status(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        pdf_jsonl,
        "mark_chunk_imported",
        crash_before_imported_status,
    )
    with django_capture_on_commit_callbacks(execute=True), pytest.raises(SimulatedProcessCrash):
        pdf_indexing.work_one_publication_job()

    staged.refresh_from_db()
    assert staged.status == PDFExtractionJobStatus.SUCCEEDED
    assert chunk.path.exists()
    assert pdf_jsonl.list_chunks()[0].status == pdf_jsonl.CHUNK_STATUS_IMPORTING
    assert len(queued_semantic) == 1

    monkeypatch.setattr(
        pdf_jsonl,
        "mark_chunk_imported",
        original_mark_imported,
    )
    attach_revision = Mock(side_effect=AssertionError("terminal output must not republish"))
    monkeypatch.setattr(pdf_indexing, "_attach_revision", attach_revision)
    with django_capture_on_commit_callbacks(execute=True):
        result = pdf_indexing.work_one_publication_job()

    assert result is not None
    assert result.pk == staged.pk
    assert pdf_jsonl.list_chunks()[0].status == pdf_jsonl.CHUNK_STATUS_IMPORTED
    assert len(queued_semantic) == 1
    attach_revision.assert_not_called()


def test_crash_during_orphan_reconciliation_rolls_back_then_resumes(
    staged_recovery_target,
    monkeypatch,
):
    repository, _document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    original_reserve = pdf_indexing._reserve_sqlite_write
    monkeypatch.setattr(
        pdf_indexing,
        "_reserve_sqlite_write",
        Mock(side_effect=SimulatedProcessCrash),
    )

    with pytest.raises(SimulatedProcessCrash):
        pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert job.phase == PDFExtractionJobPhase.EXTRACTING
    assert target.exists()

    monkeypatch.setattr(pdf_indexing, "_reserve_sqlite_write", original_reserve)
    resumed = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert resumed.promoted_job_ids == (job.pk,)
    assert job.phase == PDFExtractionJobPhase.PUBLISHING
    assert job.worker_pid is None
    assert target.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("git_blob_id", "c" * 40),
        ("last_seen_commit", "d" * 40),
        ("relative_path", "docs/Renamed.pdf"),
        ("file_size", 999),
        ("lifecycle_state", PDFDocumentLifecycle.REMOVED),
    ),
)
def test_orphan_is_never_promoted_when_current_document_identity_changed(
    staged_recovery_target,
    monkeypatch,
    field,
    value,
):
    repository, document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    setattr(document, field, value)
    document.save(update_fields=(field,))

    result = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert result.promoted_job_ids == ()
    assert result.reextract_job_ids == (job.pk,)
    assert job.status == PDFExtractionJobStatus.INTERRUPTED
    assert not target.exists()


def test_orphan_is_never_promoted_when_source_bytes_no_longer_match_checksum(
    staged_recovery_target,
    monkeypatch,
):
    repository, _document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    pdf_path.write_bytes(b"%PDF changed without matching durable catalogue metadata")

    result = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert result.promoted_job_ids == ()
    assert result.reextract_job_ids == (job.pk,)
    assert job.status == PDFExtractionJobStatus.INTERRUPTED
    assert not target.exists()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.__setitem__("schema_version", 999),
        lambda payload: payload["job"].__setitem__("target_git_blob_id", "f" * 40),
        lambda payload: payload.__setitem__("extraction_sha256", "0" * 64),
        lambda payload: payload["extraction"]["pages"][0].__setitem__("text", "tampered"),
    ),
)
def test_unverified_manifest_is_cleaned_and_only_its_pdf_is_requeued(
    staged_recovery_target,
    monkeypatch,
    mutation,
):
    repository, _document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    mutation(payload)
    target.write_text(json.dumps(payload), encoding="utf-8")

    result = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert result.promoted_job_ids == ()
    assert result.reextract_job_ids == (job.pk,)
    assert job.status == PDFExtractionJobStatus.INTERRUPTED
    assert not target.exists()

    queued = pdf_indexing.sweep_pdf_extraction_queue()
    assert len(queued.queued_job_ids) == 1
    replacement = PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0])
    assert replacement.document_id == job.document_id


def test_stager_rejects_tampered_manifest_and_requeues_only_that_pdf(
    staged_recovery_target,
):
    repository, _document, pdf_path = staged_recovery_target
    claimed = _claimed_job(repository)
    staged = pdf_indexing.execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
        defer_publication=True,
    )
    target = pdf_indexing._publication_staging_path(staged.pk)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["extraction_sha256"] = "0" * 64
    target.write_text(json.dumps(payload), encoding="utf-8")

    result = pdf_indexing.work_one_staging_job(pdf_jsonl.JSONLStager())

    staged.refresh_from_db()
    assert result is None
    assert staged.status == PDFExtractionJobStatus.INTERRUPTED
    assert not target.exists()
    replacement = PDFExtractionJob.objects.get(
        document_id=staged.document_id,
        status=PDFExtractionJobStatus.QUEUED,
    )
    assert replacement.retry_count == staged.retry_count + 1


def test_writer_retains_failed_chunk_and_tracks_its_pdf_failure(
    staged_recovery_target,
):
    repository, _document, pdf_path = staged_recovery_target
    claimed = _claimed_job(repository)
    staged = pdf_indexing.execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=lambda _path, _heartbeat: _ready_stage(pdf_path),
        defer_publication=True,
    )
    chunk = _seal_staged_records()
    record = json.loads(chunk.path.read_text(encoding="utf-8"))
    record["content"] = "X" + record["content"][1:]
    chunk.path.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert chunk.path.stat().st_size == chunk.byte_count

    assert pdf_indexing.work_one_publication_job() is None

    staged.refresh_from_db()
    failed_chunk = pdf_jsonl.list_chunks()[0]
    assert staged.status == PDFExtractionJobStatus.FAILED
    assert staged.error_code == "invalid_staged_extraction"
    assert failed_chunk.status == pdf_jsonl.CHUNK_STATUS_FAILED
    assert failed_chunk.path.exists()


def test_cancelled_job_stage_is_removed_without_promotion(
    staged_recovery_target,
    monkeypatch,
):
    repository, _document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    PDFExtractionJob.objects.filter(pk=job.pk).update(
        status=PDFExtractionJobStatus.CANCELLED,
    )

    result = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert result.promoted_job_ids == ()
    assert result.cleaned_job_ids == (job.pk,)
    assert job.status == PDFExtractionJobStatus.CANCELLED
    assert not target.exists()


def test_manifest_size_bound_rejects_oversized_stage_before_json_loading(
    staged_recovery_target,
    monkeypatch,
):
    repository, _document, pdf_path = staged_recovery_target
    job, target = _leave_post_rename_orphan(monkeypatch, repository, pdf_path)
    with target.open("wb") as stream:
        stream.truncate(pdf_indexing._maximum_staged_manifest_bytes() + 1)

    result = pdf_indexing.reconcile_staged_publications(force=True)

    job.refresh_from_db()
    assert result.reextract_job_ids == (job.pk,)
    assert job.status == PDFExtractionJobStatus.INTERRUPTED
    assert not target.exists()
