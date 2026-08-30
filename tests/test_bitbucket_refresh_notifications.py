from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import repository_sync
from bitbucket_search.services.git_sync import (
    DocumentStats,
    RepositorySyncError,
    RepositorySyncResult,
)
from bitbucket_search.services.pdf_catalog import CatalogBuild
from bookmark_manager.models import Notification, NotificationKind, NotificationState

pytestmark = pytest.mark.django_db

SCHEDULED_DAY = date(2026, 8, 30)


@pytest.fixture(autouse=True)
def daily_refresh_settings(settings):
    settings.BITBUCKET_DAILY_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    settings.BITBUCKET_DAILY_REFRESH_MAX_RETRIES = 3


def _repository(name: str = "architecture") -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.org/workspace/{name}",
        remote_url=f"ssh://git@bitbucket.org/workspace/{name}.git",
        sync_state=RepositorySyncState.FETCHING,
    )


def _automatic_job(
    repository: BitbucketRepository,
    *,
    retry_number: int = 0,
    status: str = RepositorySyncJobStatus.RUNNING,
    heartbeat_at=None,
) -> RepositorySyncJob:
    return RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=(RepositorySyncTrigger.DAILY if retry_number == 0 else RepositorySyncTrigger.RETRY),
        scheduled_day=SCHEDULED_DAY,
        automatic_retry_number=retry_number,
        status=status,
        started_at=timezone.now() if status == RepositorySyncJobStatus.RUNNING else None,
        heartbeat_at=heartbeat_at,
    )


def _fail_sync(*_args, **_kwargs):
    raise RepositorySyncError("remote_unavailable", "The repository host is unavailable.")


def _stub_successful_sync(monkeypatch) -> None:
    monkeypatch.setattr(
        repository_sync,
        "synchronize_repository",
        lambda *_args, **_kwargs: RepositorySyncResult(
            branch="main",
            source_commit="a" * 40,
            result_commit="b" * 40,
            documents=DocumentStats(pdf_count=0, vsdx_count=0, document_bytes=0),
        ),
    )
    monkeypatch.setattr(
        repository_sync,
        "build_repository_pdf_catalog",
        lambda *_args, **_kwargs: CatalogBuild(documents=(), history_is_shallow=False),
    )
    monkeypatch.setattr(repository_sync, "publish_repository_pdf_catalog", lambda *_a, **_k: None)
    monkeypatch.setattr(repository_sync, "queue_repository_pdf_extractions", lambda *_a, **_k: ())


def test_daily_failures_update_one_card_and_exhaustion_becomes_error(monkeypatch):
    repository = _repository()
    monkeypatch.setattr(repository_sync, "synchronize_repository", _fail_sync)

    first = repository_sync.execute_claimed_job(_automatic_job(repository).pk)
    notification = Notification.objects.get()

    assert first.status == RepositorySyncJobStatus.FAILED
    assert notification.event_key == (
        f"bitbucket-refresh:{repository.pk}:{SCHEDULED_DAY.isoformat()}"
    )
    assert notification.kind == NotificationKind.BITBUCKET_REFRESH
    assert notification.state == NotificationState.WARNING
    assert notification.target_path == "/pdfs/status/"
    assert "retry automatically after 2 hours" in notification.message
    assert "3 automatic retries remain" in notification.message

    notification.read_at = timezone.now()
    notification.save(update_fields=("read_at",))
    second = repository_sync.execute_claimed_job(
        _automatic_job(repository, retry_number=1).pk,
    )
    notification.refresh_from_db()

    assert second.status == RepositorySyncJobStatus.FAILED
    assert Notification.objects.count() == 1
    assert notification.state == NotificationState.WARNING
    assert "2 automatic retries remain" in notification.message
    assert notification.read_at is None

    final = repository_sync.execute_claimed_job(
        _automatic_job(repository, retry_number=3).pk,
    )
    notification.refresh_from_db()

    assert final.status == RepositorySyncJobStatus.FAILED
    assert Notification.objects.count() == 1
    assert notification.state == NotificationState.ERROR
    assert "No automatic retries remain" in notification.message
    assert "Select Refresh" in notification.message


def test_successful_automatic_retry_resolves_only_an_existing_failure_card(monkeypatch):
    repository = _repository("recovery")
    monkeypatch.setattr(repository_sync, "synchronize_repository", _fail_sync)
    repository_sync.execute_claimed_job(_automatic_job(repository).pk)
    notification = Notification.objects.get()
    original_notification_id = notification.pk

    _stub_successful_sync(monkeypatch)
    completed = repository_sync.execute_claimed_job(
        _automatic_job(repository, retry_number=1).pk,
    )
    notification.refresh_from_db()

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert Notification.objects.count() == 1
    assert notification.pk == original_notification_id
    assert notification.state == NotificationState.SUCCESS
    assert notification.title.startswith("Daily refresh recovered:")

    Notification.objects.all().delete()
    no_failure_repository = _repository("already-healthy")
    completed = repository_sync.execute_claimed_job(
        _automatic_job(no_failure_repository).pk,
    )

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert Notification.objects.count() == 0


def test_manual_failure_does_not_publish_a_daily_refresh_notification(monkeypatch):
    repository = _repository("manual")
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=RepositorySyncTrigger.MANUAL,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
    )
    monkeypatch.setattr(repository_sync, "synchronize_repository", _fail_sync)

    completed = repository_sync.execute_claimed_job(job.pk)

    assert completed.status == RepositorySyncJobStatus.FAILED
    assert Notification.objects.count() == 0


def test_automatic_launch_failure_and_stale_worker_interruption_publish_cards():
    launch_repository = _repository("launch-failure")
    launch_job = _automatic_job(
        launch_repository,
        status=RepositorySyncJobStatus.QUEUED,
    )

    repository_sync.mark_worker_launch_failed(launch_job.pk)

    launch_notification = Notification.objects.get()
    assert launch_notification.state == NotificationState.WARNING
    assert "could not start" in launch_notification.message

    stale_repository = _repository("stale-worker")
    observed_at = datetime(2026, 8, 30, 15, tzinfo=UTC)
    stale_job = _automatic_job(
        stale_repository,
        heartbeat_at=observed_at - timedelta(minutes=11),
    )

    repository_sync._interrupt_stale_jobs(at=observed_at)

    stale_job.refresh_from_db()
    assert stale_job.status == RepositorySyncJobStatus.INTERRUPTED
    stale_notification = Notification.objects.get(
        event_key=f"bitbucket-refresh:{stale_repository.pk}:{SCHEDULED_DAY.isoformat()}"
    )
    assert stale_notification.state == NotificationState.WARNING
    assert "stopped before this sync completed" in stale_notification.message


def test_notification_empty_state_mentions_bitbucket_refreshes(client):
    response = client.get("/pdfs/", HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")

    assert response.status_code == 200
    assert (
        "Confluence, and Bitbucket refresh updates will appear here." in response.content.decode()
    )
