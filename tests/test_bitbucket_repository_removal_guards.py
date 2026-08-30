from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import pdf_indexing, repository_sync
from bookmark_manager.models import Notification, NotificationKind, NotificationState

pytestmark = pytest.mark.django_db


@pytest.fixture
def target(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="example.invalid/architecture",
        remote_url="https://example.invalid/architecture.git",
        sync_state=RepositorySyncState.READY,
        last_synced_commit="b" * 40,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Guide.pdf",
        relative_path="Guide.pdf",
        file_size=100,
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )
    return repository, document


def _recovery(repository, database_deleted):
    return RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk,
        display_name=repository.display_name,
        database_deleted=database_deleted,
    )


@pytest.mark.parametrize("database_deleted", [False, True])
def test_pending_removal_cannot_be_requeued_by_pdf_startup(target, database_deleted):
    repository, _document = target
    recovery = _recovery(repository, database_deleted)

    assert pdf_indexing.queue_repository_pdf_extractions(repository).queued_job_ids == ()
    assert pdf_indexing.sweep_pdf_extraction_queue(interrupt_running=True).queued_job_ids == ()
    assert PDFExtractionJob.objects.count() == 0
    assert pdf_indexing.extraction_status_snapshot().pending_documents == 0

    recovery.delete()
    assert len(pdf_indexing.queue_repository_pdf_extractions(repository).queued_job_ids) == 1


@pytest.mark.parametrize("database_deleted", [False, True])
def test_pending_removal_is_skipped_when_claiming_an_existing_pdf_job(target, database_deleted):
    repository, _document = target
    queued = pdf_indexing.queue_repository_pdf_extractions(repository)
    _recovery(repository, database_deleted)

    assert pdf_indexing.claim_next_extraction_job() is None
    assert PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0]).status == (
        PDFExtractionJobStatus.QUEUED
    )
    assert pdf_indexing.extraction_status_snapshot().queued_jobs == 0


@pytest.mark.parametrize("database_deleted", [False, True])
def test_already_claimed_pdf_cannot_read_a_removal_quarantine(target, database_deleted):
    repository, document = target
    pdf_indexing.queue_repository_pdf_extractions(repository)
    job = pdf_indexing.claim_next_extraction_job()
    assert job is not None
    _recovery(repository, database_deleted)
    parser = Mock(side_effect=AssertionError("pending removal must not read or parse files"))

    result = pdf_indexing.execute_claimed_extraction_job(job.pk, extraction_runner=parser)

    assert result.status == PDFExtractionJobStatus.QUEUED
    assert result.error_code == ""
    parser.assert_not_called()
    document.refresh_from_db()
    assert document.index_state == PDFIndexState.PENDING
    assert document.extraction_error_code == ""
    assert pdf_indexing.sweep_pdf_extraction_queue(interrupt_running=True).queued_job_ids == ()


def test_repository_removed_between_pdf_sweep_and_queue_is_ignored(target):
    repository, _document = target
    repository_id = repository.pk
    repository.delete()

    assert pdf_indexing.queue_repository_pdf_extractions(repository_id).queued_job_ids == ()


@pytest.mark.parametrize("removal_state", ["pending", "database_deleted", "removed"])
@pytest.mark.parametrize(
    "operation", ["manual_failure", "manual_recovery", "daily_failure", "daily_recovery"]
)
def test_late_worker_notifications_do_not_restore_removed_repository_data(
    target, removal_state, operation
):
    repository, _document = target
    now = timezone.now()
    daily = operation.startswith("daily_")
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=RepositorySyncTrigger.DAILY if daily else RepositorySyncTrigger.MANUAL,
        scheduled_day=now.date() if daily else None,
        status=RepositorySyncJobStatus.FAILED,
    )
    event_key = (
        f"bitbucket-refresh:{repository.pk}:{now.date().isoformat()}"
        if daily
        else f"bitbucket-connection:{repository.pk}"
    )
    Notification.objects.create(
        event_key=event_key,
        kind=NotificationKind.BITBUCKET_REFRESH,
        state=NotificationState.ERROR,
        title="Original failure",
        message="Original failure details",
        target_path="/pdfs/status/",
        occurred_at=now,
    )
    if removal_state == "removed":
        repository.delete()
        Notification.objects.filter(event_key=event_key).delete()
    else:
        _recovery(repository, removal_state == "database_deleted")
    before = list(Notification.objects.values())

    if operation == "daily_failure":
        repository_sync._publish_automatic_refresh_failure(
            job, summary="Late daily failure", occurred_at=now
        )
    elif operation == "daily_recovery":
        repository_sync._publish_automatic_refresh_recovery(job, occurred_at=now)
    else:
        repository_sync._publish_manual_connection_status(
            job,
            occurred_at=now,
            failure_summary="Late connection failure" if operation == "manual_failure" else "",
        )

    assert list(Notification.objects.values()) == before
