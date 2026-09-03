from __future__ import annotations

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFPipelineRun,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services import repository_sync
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.repository_sync import (
    RepositoryRefreshInProgress,
    queue_all_repository_refreshes,
)


def _repository(name: str, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
        enabled=enabled,
    )


@pytest.fixture(autouse=True)
def _approve_synthetic_repository_host(settings):
    settings.BITBUCKET_ALLOWED_HOSTS = ("bitbucket.example.invalid",)
    settings.BITBUCKET_ALLOWED_HOSTS_EXPLICIT = True
    settings.BITBUCKET_ALLOWED_HOSTS_SOURCE = "explicit"


def test_queue_all_refreshes_enabled_repositories_and_preserves_operations(
    db,
    tmp_path,
    settings,
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    first_clone = _repository("first-clone")
    existing_checkout = _repository("existing-checkout")
    checkout = managed_repository_path(existing_checkout)
    (checkout / ".git").mkdir(parents=True)
    disabled = _repository("disabled", enabled=False)

    queued = queue_all_repository_refreshes()

    assert queued.eligible_total == 2
    assert queued.newly_queued_count == 2
    assert queued.already_active_count == 0
    assert [result.repository.pk for result in queued.results] == [
        first_clone.pk,
        existing_checkout.pk,
    ]
    assert [result.job.operation for result in queued.newly_queued] == [
        RepositorySyncOperation.CLONE,
        RepositorySyncOperation.REFRESH,
    ]
    assert {job.pk for job in queued.fallback_worker_jobs} == {
        result.job.pk for result in queued.newly_queued
    }
    assert not RepositorySyncJob.objects.filter(repository=disabled).exists()
    assert queued.run is not None
    assert queued.run.accepted_repository_count == 2
    assert queued.run.repository_memberships.count() == 2
    assert all(result.job.run_repository_id for result in queued.results)


def test_queue_all_deduplicates_active_jobs_and_only_wakes_queued_work(
    db,
    tmp_path,
    settings,
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    waiting_repository = _repository("waiting")
    running_repository = _repository("running")
    new_repository = _repository("new")
    waiting_job = RepositorySyncJob.objects.create(
        repository=waiting_repository,
        operation=RepositorySyncOperation.CLONE,
    )
    running_job = RepositorySyncJob.objects.create(
        repository=running_repository,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )

    queued = queue_all_repository_refreshes()

    assert queued.eligible_total == 3
    assert queued.newly_queued_count == 1
    assert queued.newly_queued[0].repository == new_repository
    assert queued.already_active_count == 2
    assert queued.already_queued_count == 1
    assert queued.already_running_count == 1
    assert {result.job.pk for result in queued.already_active} == {
        waiting_job.pk,
        running_job.pk,
    }
    assert {job.pk for job in queued.fallback_worker_jobs} == {
        waiting_job.pk,
        queued.newly_queued[0].job.pk,
    }
    assert RepositorySyncJob.objects.count() == 3


def test_queue_all_is_idempotent_across_repeated_requests(db, tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    first = _repository("first")
    second = _repository("second")

    initial = queue_all_repository_refreshes()
    repeated = queue_all_repository_refreshes()

    assert initial.newly_queued_count == 2
    assert repeated.eligible_total == 2
    assert repeated.newly_queued_count == 0
    assert repeated.already_active_count == 2
    assert [result.repository.pk for result in repeated.results] == [first.pk, second.pk]
    assert {job.pk for job in repeated.fallback_worker_jobs} == {
        result.job.pk for result in initial.results
    }
    assert RepositorySyncJob.objects.count() == 2
    assert PDFPipelineRun.objects.count() == 1


@pytest.mark.parametrize(
    ("sync_state", "job_status", "enabled"),
    [
        (RepositorySyncState.QUEUED, None, True),
        (RepositorySyncState.CLONING, None, True),
        (RepositorySyncState.FETCHING, None, True),
        (RepositorySyncState.UPDATING, None, True),
        (RepositorySyncState.READY, RepositorySyncJobStatus.QUEUED, True),
        (RepositorySyncState.READY, RepositorySyncJobStatus.RUNNING, True),
        (RepositorySyncState.CLONING, None, False),
        (RepositorySyncState.READY, RepositorySyncJobStatus.QUEUED, False),
    ],
)
def test_require_idle_rejects_any_busy_repository_before_queueing_work(
    db,
    sync_state,
    job_status,
    enabled,
):
    idle = _repository("idle")
    busy = _repository("busy", enabled=enabled)
    busy.sync_state = sync_state
    busy.save(update_fields={"sync_state"})
    if job_status:
        RepositorySyncJob.objects.create(
            repository=busy,
            operation=RepositorySyncOperation.CLONE,
            status=job_status,
            started_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
            heartbeat_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
        )
    original_job_count = RepositorySyncJob.objects.count()

    with pytest.raises(RepositoryRefreshInProgress):
        queue_all_repository_refreshes(require_idle=True)

    assert RepositorySyncJob.objects.count() == original_job_count
    assert not RepositorySyncJob.objects.filter(repository=idle).exists()
    idle.refresh_from_db()
    assert idle.sync_state == RepositorySyncState.NOT_CLONED


@pytest.mark.parametrize(
    ("sync_state", "job_status"),
    [
        (RepositorySyncState.READY, RepositorySyncJobStatus.SUCCEEDED),
        (RepositorySyncState.FAILED, RepositorySyncJobStatus.FAILED),
        (RepositorySyncState.INTERRUPTED, RepositorySyncJobStatus.INTERRUPTED),
    ],
)
def test_require_idle_allows_refresh_after_previous_jobs_finish(db, sync_state, job_status):
    repository = _repository("finished")
    repository.sync_state = sync_state
    repository.save(update_fields={"sync_state"})
    completed_job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        status=job_status,
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )

    queued = queue_all_repository_refreshes(require_idle=True)

    assert queued.eligible_total == 1
    assert queued.newly_queued_count == 1
    assert queued.already_active_count == 0
    assert queued.newly_queued[0].job.pk != completed_job.pk
    assert queued.newly_queued[0].job.status == RepositorySyncJobStatus.QUEUED

    with pytest.raises(RepositoryRefreshInProgress):
        queue_all_repository_refreshes(require_idle=True)

    assert RepositorySyncJob.objects.count() == 2


def test_queue_all_returns_an_empty_result_when_no_repository_is_enabled(db):
    _repository("disabled", enabled=False)

    queued = queue_all_repository_refreshes()

    assert queued.eligible_total == 0
    assert queued.results == ()
    assert queued.newly_queued == ()
    assert queued.already_active == ()
    assert queued.fallback_worker_jobs == ()


def test_queue_all_revalidates_enabled_repositories_at_the_row_lock(db, monkeypatch):
    retained = _repository("retained")
    disabled_during_queue = _repository("disabled-during-queue")
    original_queue = repository_sync._queue_repository_refresh

    def disable_before_lock(repository_id: int, *, enabled_only: bool):
        if repository_id == disabled_during_queue.pk:
            BitbucketRepository.objects.filter(pk=repository_id).update(enabled=False)
        return original_queue(repository_id, enabled_only=enabled_only)

    monkeypatch.setattr(
        repository_sync,
        "_queue_repository_refresh",
        disable_before_lock,
    )

    queued = queue_all_repository_refreshes()

    assert queued.eligible_total == 1
    assert [result.repository.pk for result in queued.results] == [retained.pk]
    assert not RepositorySyncJob.objects.filter(repository=disabled_during_queue).exists()
    assert queued.run is not None
    assert queued.run.accepted_repository_count == 1
    assert queued.rejected_repository_ids == (disabled_during_queue.pk,)
