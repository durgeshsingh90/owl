from __future__ import annotations

from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
)
from bitbucket_search.services import repository_sync
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.repository_sync import queue_all_repository_refreshes


def _repository(name: str, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
        enabled=enabled,
    )


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
