from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFPipelineRepositoryPhase,
    PDFPipelineRunRepository,
    PDFPipelineRunState,
    PDFPipelineRunTrigger,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bitbucket_search.services.pdf_pipeline_runs import (
    accept_pipeline_run,
    latest_current_run,
    reconcile_open_pipeline_runs,
    reconcile_run_repository,
    run_memberships_by_repository,
)

pytestmark = pytest.mark.django_db


def _repository(name: str) -> BitbucketRepository:
    slug = name.casefold().replace(" ", "-")
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{slug}",
        remote_url=f"ssh://git@example.invalid/team/{slug}.git",
    )


def _document(repository: BitbucketRepository, name: str, marker: str) -> PDFDocument:
    return PDFDocument.objects.create(
        repository=repository,
        filename=name,
        relative_path=f"docs/{name}",
        file_size=100,
        git_blob_id=marker * 40,
        last_seen_commit="f" * 40,
    )


def _job(
    document: PDFDocument,
    membership: PDFPipelineRunRepository,
    *,
    status: str,
    phase: str = PDFExtractionJobPhase.COMPLETED,
) -> PDFExtractionJob:
    return PDFExtractionJob.objects.create(
        document=document,
        run_repository=membership,
        run_id=membership.run_id,
        target_git_blob_id=document.git_blob_id,
        target_source_commit=document.last_seen_commit,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version="test-extractor",
        status=status,
        phase=phase,
    )


def test_accept_run_deduplicates_ids_and_records_only_existing_repositories():
    first = _repository("First")
    second = _repository("Second")
    accepted_at = timezone.now()

    run = accept_pipeline_run(
        [second.pk, first.pk, second.pk, -1, 999_999, True],
        trigger=PDFPipelineRunTrigger.REFRESH_ALL,
        accepted_at=accepted_at,
    )

    assert run.accepted_repository_count == 2
    assert run.accepted_at == accepted_at
    assert set(run_memberships_by_repository(run)) == {first.pk, second.pk}


def test_reconcile_counts_only_latest_attempt_per_pdf_and_confirmed_current_documents():
    repository = _repository("Latest attempts")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    first = _document(repository, "first.pdf", "a")
    second = _document(repository, "second.pdf", "b")
    revision = PDFTextRevision.objects.create(
        content_sha256="c" * 64,
        extractor_version="test-extractor",
        state=PDFTextRevisionState.READY,
    )
    second.indexed_revision = revision
    second.indexed_git_blob_id = second.git_blob_id
    second.index_state = PDFIndexState.READY
    second.save(update_fields=("indexed_revision", "indexed_git_blob_id", "index_state"))

    older = _job(first, membership, status=PDFExtractionJobStatus.FAILED)
    newer = _job(first, membership, status=PDFExtractionJobStatus.SUCCEEDED)
    PDFExtractionJob.objects.filter(pk=older.pk).update(
        requested_at=timezone.now() - timedelta(minutes=1)
    )
    PDFExtractionJob.objects.filter(pk=newer.pk).update(
        requested_at=timezone.now(),
        published_at=timezone.now(),
    )
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        inventory_final=True,
        total_pdfs=2,
    )

    result = reconcile_run_repository(membership)

    assert result.successful_pdfs == 2
    assert result.permanent_failed_pdfs == 0
    assert result.cancelled_pdfs == 0
    assert result.remaining_pdfs == 0
    assert result.lifecycle_state == PDFPipelineRunState.COMPLETE
    assert result.phase == PDFPipelineRepositoryPhase.COMPLETE
    run.refresh_from_db()
    assert run.state == PDFPipelineRunState.COMPLETE


def test_reconcile_never_assumes_unrepresented_pending_pdf_succeeded():
    repository = _repository("Pending inventory")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    _document(repository, "pending.pdf", "d")
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        inventory_final=True,
        total_pdfs=1,
    )

    result = reconcile_run_repository(membership)

    assert result.successful_pdfs == 0
    assert result.remaining_pdfs == 1
    assert result.lifecycle_state == PDFPipelineRunState.ACTIVE
    assert result.terminal_outcome == ""


def test_reconcile_partition_prioritizes_nonterminal_and_errors_over_stale_success():
    repository = _repository("Defensive partition")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    failed_document = _document(repository, "failed.pdf", "e")
    queued_document = _document(repository, "queued.pdf", "f")
    successful_document = _document(repository, "successful.pdf", "1")
    _job(failed_document, membership, status=PDFExtractionJobStatus.FAILED)
    _job(
        queued_document,
        membership,
        status=PDFExtractionJobStatus.QUEUED,
        phase=PDFExtractionJobPhase.QUEUED,
    )
    _job(successful_document, membership, status=PDFExtractionJobStatus.SUCCEEDED)
    # Simulate a later catalogue mismatch: the frozen run total is smaller
    # than the current repository lookup. Safety outcomes must win over green.
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        inventory_final=True,
        total_pdfs=2,
    )

    result = reconcile_run_repository(membership)

    assert result.permanent_failed_pdfs == 1
    assert result.remaining_pdfs == 1
    assert result.successful_pdfs == 0
    assert (
        result.successful_pdfs
        + result.permanent_failed_pdfs
        + result.cancelled_pdfs
        + result.remaining_pdfs
        == result.total_pdfs
    )
    assert result.lifecycle_state == PDFPipelineRunState.ACTIVE


def test_latest_current_run_prefers_newest_nonterminal_then_latest_terminal():
    repository = _repository("Current run")
    terminal = accept_pipeline_run([repository.pk])
    PDFPipelineRunRepository.objects.filter(run=terminal).update(
        lifecycle_state=PDFPipelineRunState.COMPLETE
    )
    terminal.state = PDFPipelineRunState.COMPLETE
    terminal.save(update_fields=("state",))
    active = accept_pipeline_run([repository.pk])

    assert latest_current_run() == active

    PDFPipelineRunRepository.objects.filter(run=active).update(
        lifecycle_state=PDFPipelineRunState.COMPLETE
    )
    active.state = PDFPipelineRunState.COMPLETE
    active.save(update_fields=("state",))
    assert latest_current_run() == active


def test_supervisor_reconciliation_is_bounded_idempotent_and_does_not_fake_progress():
    repository = _repository("Supervisor repair")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    _document(repository, "pending.pdf", "2")
    original_progress = timezone.now() - timedelta(minutes=2)
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        lifecycle_state=PDFPipelineRunState.ACTIVE,
        inventory_final=True,
        total_pdfs=1,
        successful_pdfs=1,
        remaining_pdfs=0,
        last_progress_at=original_progress,
    )
    first_at = timezone.now()

    first = reconcile_open_pipeline_runs(at=first_at, limit=1)
    membership.refresh_from_db()

    assert first == {"membershipsExamined": 1, "runsExamined": 1}
    assert membership.successful_pdfs == 0
    assert membership.remaining_pdfs == 1
    assert membership.last_progress_at == first_at

    second_at = first_at + timedelta(minutes=1)
    second = reconcile_open_pipeline_runs(at=second_at, limit=1)
    membership.refresh_from_db()

    assert second == {"membershipsExamined": 1, "runsExamined": 1}
    assert membership.last_progress_at == first_at
