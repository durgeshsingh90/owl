from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPageExtractionState,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositoryOperationLogEntry,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.pdf_indexing import (
    StagedPDFExtraction,
    StagedPDFPage,
    cancel_repository_pdf_extractions,
    claim_next_extraction_job,
    execute_claimed_extraction_job,
    queue_repository_pdf_extractions,
    sweep_pdf_extraction_queue,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def cancellation_target(tmp_path: Path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "tmp"
    settings.PDF_MAX_FILE_BYTES = 1024 * 1024
    settings.PDF_MAX_PAGES = 100
    settings.PDF_MAX_PAGE_TEXT_CHARS = 100_000
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 1_000_000
    repository = BitbucketRepository.objects.create(
        display_name="Cancellation target",
        canonical_remote_key="example.invalid/workspace/cancellation-target",
        remote_url="https://example.invalid/workspace/cancellation-target.git",
        sync_state=RepositorySyncState.READY,
        last_synced_commit="b" * 40,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs").mkdir()
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    return repository, checkout


def _document(repository: BitbucketRepository, checkout: Path, index: int) -> PDFDocument:
    relative_path = f"docs/Guide-{index}.pdf"
    pdf_path = checkout / relative_path
    pdf_path.write_bytes(f"%PDF cancellation fixture {index}".encode())
    return PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path=relative_path,
        file_size=pdf_path.stat().st_size,
        git_blob_id=f"{index:040x}",
        last_seen_commit="b" * 40,
    )


def _job(document: PDFDocument, status: str) -> PDFExtractionJob:
    now = timezone.now()
    return PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id=document.git_blob_id,
        target_source_commit=document.last_seen_commit,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        status=status,
        phase=(
            PDFExtractionJobPhase.EXTRACTING
            if status == PDFExtractionJobStatus.RUNNING
            else PDFExtractionJobPhase.QUEUED
        ),
        progress=60 if status == PDFExtractionJobStatus.RUNNING else 0,
        started_at=now - timedelta(seconds=10)
        if status == PDFExtractionJobStatus.RUNNING
        else None,
        heartbeat_at=now if status == PDFExtractionJobStatus.RUNNING else None,
        worker_pid=4321 if status == PDFExtractionJobStatus.RUNNING else None,
    )


def _ready_stage(path: Path) -> StagedPDFExtraction:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    text = "This text must never be published after cancellation."
    return StagedPDFExtraction(
        state=PDFTextRevisionState.READY,
        pages=(
            StagedPDFPage(
                page_number=1,
                text=text,
                character_count=len(text),
                state=PDFPageExtractionState.READY,
            ),
        ),
        page_count=1,
        extracted_character_count=len(text),
        source_size_bytes=path.stat().st_size,
        content_sha256_before=content_hash,
        content_sha256_after=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def test_repository_cancellation_is_atomic_scoped_idempotent_and_does_not_stop_git(
    cancellation_target,
):
    repository, checkout = cancellation_target
    queued_jobs = tuple(
        _job(_document(repository, checkout, index), PDFExtractionJobStatus.QUEUED)
        for index in (1, 2)
    )
    running_job = _job(_document(repository, checkout, 3), PDFExtractionJobStatus.RUNNING)

    other_repository = BitbucketRepository.objects.create(
        display_name="Other repository",
        canonical_remote_key="example.invalid/workspace/other-repository",
        remote_url="https://example.invalid/workspace/other-repository.git",
        sync_state=RepositorySyncState.READY,
    )
    other_checkout = managed_repository_path(other_repository)
    (other_checkout / ".git").mkdir(parents=True)
    (other_checkout / "docs").mkdir()
    other_job = _job(_document(other_repository, other_checkout, 4), PDFExtractionJobStatus.RUNNING)
    git_job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )

    first = cancel_repository_pdf_extractions(repository)
    second = cancel_repository_pdf_extractions(repository.pk)

    assert first.state == "cancelled"
    assert first.queued_jobs == 2
    assert first.running_jobs == 1
    assert first.total_jobs == 3
    assert set(first.cancelled_job_ids) == {*(job.pk for job in queued_jobs), running_job.pk}
    assert second.state == "idle"
    assert second.cancelled_job_ids == ()
    cancelled_jobs = PDFExtractionJob.objects.filter(pk__in=first.cancelled_job_ids)
    assert (
        cancelled_jobs.filter(
            status=PDFExtractionJobStatus.CANCELLED,
            error_code="indexing_cancelled_by_user",
            error_summary="PDF indexing was stopped by the user.",
            completed_at__isnull=False,
        ).count()
        == 3
    )
    assert (
        RepositoryOperationLogEntry.objects.filter(
            extraction_job_id__in=first.cancelled_job_ids,
            event="indexing_cancelled",
            message="PDF indexing was stopped by the user.",
        ).count()
        == 3
    )
    assert PDFExtractionJob.objects.get(pk=other_job.pk).status == PDFExtractionJobStatus.RUNNING
    assert RepositorySyncJob.objects.get(pk=git_job.pk).status == RepositorySyncJobStatus.RUNNING

    # Periodic/startup reconciliation must not undo an explicit stop request.
    swept = sweep_pdf_extraction_queue()
    assert swept.queued_job_ids == ()
    assert not PDFExtractionJob.objects.filter(
        document__repository=repository,
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
    ).exists()


def test_cancelled_running_controller_cannot_publish_or_requeue(cancellation_target):
    repository, checkout = cancellation_target
    document = _document(repository, checkout, 1)
    queued = queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()
    assert claimed is not None

    def cancel_during_parser(path: Path, _heartbeat):
        cancelled = cancel_repository_pdf_extractions(repository.pk)
        assert cancelled.running_jobs == 1
        return _ready_stage(path)

    result = execute_claimed_extraction_job(
        queued.queued_job_ids[0],
        extraction_runner=cancel_during_parser,
    )

    document.refresh_from_db()
    assert result.status == PDFExtractionJobStatus.CANCELLED
    assert result.error_code == "indexing_cancelled_by_user"
    assert document.indexed_revision_id is None
    assert PDFTextRevision.objects.count() == 0
    assert result.operation_log_entries.filter(event="indexing_cancelled").count() == 1
    assert not PDFExtractionJob.objects.filter(
        document=document,
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
    ).exists()
    assert sweep_pdf_extraction_queue().queued_job_ids == ()


def test_five_thousand_queued_attempts_are_cancelled_in_bounded_batches(
    cancellation_target,
):
    repository, _checkout = cancellation_target
    documents = PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Corpus-{index:05d}.pdf",
                relative_path=f"corpus/Corpus-{index:05d}.pdf",
                file_size=1_024 + index,
                git_blob_id=f"{index:040x}",
                last_seen_commit="b" * 40,
            )
            for index in range(5_000)
        ],
        batch_size=400,
    )
    PDFExtractionJob.objects.bulk_create(
        [
            PDFExtractionJob(
                document=document,
                target_git_blob_id=document.git_blob_id,
                target_source_commit=document.last_seen_commit,
                target_relative_path=document.relative_path,
                target_file_size=document.file_size,
                target_extractor_version=PDF_EXTRACTOR_VERSION,
            )
            for document in documents
        ],
        batch_size=400,
    )

    with CaptureQueriesContext(connection) as queries:
        result = cancel_repository_pdf_extractions(repository.pk)

    assert result.queued_jobs == 5_000
    assert result.running_jobs == 0
    assert len(result.cancelled_job_ids) == 5_000
    assert len(queries) <= 120
    assert PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.CANCELLED).count() == 5_000
    assert RepositoryOperationLogEntry.objects.filter(event="indexing_cancelled").count() == 5_000


def test_repository_cancellation_endpoint_returns_counts_and_current_state(
    cancellation_target,
    client,
):
    repository, checkout = cancellation_target
    _job(_document(repository, checkout, 1), PDFExtractionJobStatus.QUEUED)
    _job(_document(repository, checkout, 2), PDFExtractionJobStatus.RUNNING)
    url = reverse("bitbucket_search:repository_index_cancel", args=(repository.pk,))

    response = client.post(
        url,
        {"confirmed": "yes"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    assert response.json()["repositoryId"] == repository.pk
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancelled"] == {"queued": 1, "running": 1, "total": 2}
    assert response.json()["indexing"]["active"] is False
    assert response.json()["indexing"]["counts"][PDFExtractionJobStatus.CANCELLED] == 2
    assert client.get(url, REMOTE_ADDR="127.0.0.1").status_code == 405

    repeated = client.post(url, {"confirmed": "yes"}, REMOTE_ADDR="127.0.0.1")
    assert repeated.status_code == 200
    assert repeated.json()["state"] == "idle"
    assert repeated.json()["cancelled"] == {"queued": 0, "running": 0, "total": 0}


def test_repository_logs_offer_native_stop_and_return_to_the_same_repository(
    cancellation_target,
    client,
):
    repository, checkout = cancellation_target
    job = _job(_document(repository, checkout, 1), PDFExtractionJobStatus.RUNNING)
    logs_url = reverse("bitbucket_search:index_status")
    cancel_url = reverse("bitbucket_search:repository_index_cancel", args=(repository.pk,))

    page = client.get(logs_url, {"repository": repository.pk}, REMOTE_ADDR="127.0.0.1")
    html = page.content.decode()
    assert page.status_code == 200
    assert f'action="{cancel_url}"' in html
    assert "Stop indexing now" in html
    assert 'name="return_to" value="logs"' in html
    assert 'name="confirmed" value="yes"' in html
    assert "data-confirm-pdf-index-cancel" in html

    response = client.post(
        cancel_url,
        {"return_to": "logs", "confirmed": "yes"},
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 302
    assert response.url == f"{logs_url}?repository={repository.pk}"
    job.refresh_from_db()
    assert job.status == PDFExtractionJobStatus.CANCELLED


def test_repository_cancellation_endpoint_rejects_non_loopback_and_missing_repository(
    cancellation_target,
    client,
):
    repository, _checkout = cancellation_target
    url = reverse("bitbucket_search:repository_index_cancel", args=(repository.pk,))

    assert client.post(url, REMOTE_ADDR="203.0.113.8").status_code == 403
    unconfirmed = client.post(url, REMOTE_ADDR="127.0.0.1")
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["code"] == "indexing_cancellation_confirmation_required"
    assert (
        client.post(
            reverse("bitbucket_search:repository_index_cancel", args=(repository.pk + 999,)),
            {"confirmed": "yes"},
            REMOTE_ADDR="127.0.0.1",
        ).status_code
        == 404
    )
