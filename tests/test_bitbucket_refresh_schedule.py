from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncTrigger,
)
from bitbucket_search.services.repository_sync import (
    queue_due_daily_repository_refreshes,
    repository_status_snapshot,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _daily_refresh_settings(settings):
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = True
    settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR = 11
    settings.BITBUCKET_DAILY_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    settings.BITBUCKET_DAILY_REFRESH_MAX_RETRIES = 3
    with timezone.override("UTC"):
        yield


def _repository(name: str, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
        enabled=enabled,
    )


def _finish(
    job: RepositorySyncJob,
    *,
    status: str,
    completed_at: datetime,
) -> RepositorySyncJob:
    RepositorySyncJob.objects.filter(pk=job.pk).update(
        status=status,
        started_at=completed_at - timedelta(minutes=1),
        heartbeat_at=completed_at,
        completed_at=completed_at,
    )
    repository_updates = {"last_sync_completed_at": completed_at}
    if status == RepositorySyncJobStatus.SUCCEEDED:
        repository_updates["last_sync_successful_at"] = completed_at
    BitbucketRepository.objects.filter(pk=job.repository_id).update(**repository_updates)
    job.refresh_from_db()
    return job


def _completed_job(
    repository: BitbucketRepository,
    *,
    status: str,
    completed_at: datetime,
    trigger: str = RepositorySyncTrigger.MANUAL,
    scheduled_day: date | None = None,
    retry_number: int = 0,
    worker_retry_number: int = 0,
) -> RepositorySyncJob:
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=trigger,
        scheduled_day=scheduled_day,
        automatic_retry_number=retry_number,
        worker_retry_number=worker_retry_number,
        status=status,
        started_at=completed_at - timedelta(minutes=1),
        heartbeat_at=completed_at,
        completed_at=completed_at,
    )
    repository_updates = {"last_sync_completed_at": completed_at}
    if status == RepositorySyncJobStatus.SUCCEEDED:
        repository_updates["last_sync_successful_at"] = completed_at
    BitbucketRepository.objects.filter(pk=repository.pk).update(**repository_updates)
    repository.refresh_from_db()
    return job


def test_daily_tick_queues_each_enabled_repository_once_and_skips_disabled():
    first = _repository("first")
    second = _repository("second")
    disabled = _repository("disabled", enabled=False)
    observed_at = datetime(2026, 8, 29, 12, tzinfo=UTC)

    queued = queue_due_daily_repository_refreshes(at=observed_at)
    repeated = queue_due_daily_repository_refreshes(at=observed_at)

    assert {result.repository.pk for result in queued} == {first.pk, second.pk}
    assert repeated == ()
    assert not disabled.sync_jobs.exists()
    assert set(
        RepositorySyncJob.objects.values_list(
            "trigger",
            "scheduled_day",
            "automatic_retry_number",
        )
    ) == {(RepositorySyncTrigger.DAILY, observed_at.date(), 0)}
    assert RepositorySyncJob.objects.count() == 2


def test_global_gate_disables_daily_queue_and_status(settings):
    repository = _repository("global-disabled")
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = False
    observed_at = datetime(2026, 8, 29, 8, tzinfo=UTC)

    assert queue_due_daily_repository_refreshes(at=observed_at) == ()
    snapshot = repository_status_snapshot(at=observed_at)

    assert snapshot[0].pk == repository.pk
    assert snapshot[0].automatic_refresh.state == "disabled"
    assert snapshot[0].automatic_refresh.next_action_at is None


def test_any_active_sync_defers_daily_queue():
    repository = _repository("active")
    active = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
    )
    observed_at = datetime(2026, 8, 29, 9, tzinfo=UTC)

    assert queue_due_daily_repository_refreshes(at=observed_at) == ()
    status = repository_status_snapshot(at=observed_at)[0].automatic_refresh

    assert RepositorySyncJob.objects.get() == active
    assert status.state == "active"
    assert status.trigger == RepositorySyncTrigger.MANUAL


def test_start_before_11_catches_up_latest_missed_slot():
    repository = _repository("morning-catch-up")
    observed_at = datetime(2026, 8, 30, 10, tzinfo=UTC)

    queued = queue_due_daily_repository_refreshes(at=observed_at)

    assert len(queued) == 1
    assert queued[0].repository == repository
    assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
    assert queued[0].job.scheduled_day == date(2026, 8, 29)
    assert queued[0].job.automatic_retry_number == 0
    assert queue_due_daily_repository_refreshes(at=observed_at) == ()


def test_exact_11_boundary_advances_from_yesterday_to_today():
    repository = _repository("eleven-boundary")
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.SUCCEEDED,
        completed_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
    )
    before_slot = datetime(2026, 8, 30, 10, 59, 59, 999999, tzinfo=UTC)
    due_slot = datetime(2026, 8, 30, 11, tzinfo=UTC)

    assert queue_due_daily_repository_refreshes(at=before_slot) == ()
    waiting = repository_status_snapshot(at=before_slot)[0].automatic_refresh
    assert waiting.state == "up_to_date"
    assert waiting.scheduled_day == date(2026, 8, 29)
    assert waiting.next_action_at == due_slot

    queued = queue_due_daily_repository_refreshes(at=due_slot)

    assert len(queued) == 1
    assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
    assert queued[0].job.scheduled_day == date(2026, 8, 30)


def test_same_day_success_before_11_satisfies_today_slot():
    repository = _repository("manual-success")
    completed_at = datetime(2026, 8, 29, 7, 30, tzinfo=UTC)
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.SUCCEEDED,
        completed_at=completed_at,
    )
    before_slot = datetime(2026, 8, 29, 10, tzinfo=UTC)
    due_slot = datetime(2026, 8, 29, 11, tzinfo=UTC)

    assert queue_due_daily_repository_refreshes(at=before_slot) == ()
    before_status = repository_status_snapshot(at=before_slot)[0].automatic_refresh
    assert before_status.state == "up_to_date"
    assert before_status.scheduled_day == date(2026, 8, 28)
    assert before_status.next_action_at == due_slot

    assert queue_due_daily_repository_refreshes(at=due_slot) == ()
    due_status = repository_status_snapshot(at=due_slot)[0].automatic_refresh
    assert due_status.state == "up_to_date"
    assert due_status.scheduled_day == date(2026, 8, 29)
    assert due_status.trigger is None
    assert due_status.last_attempt_at == completed_at
    assert due_status.next_action_at == datetime(2026, 8, 30, 11, tzinfo=UTC)


def test_failed_daily_attempt_retries_at_exact_two_hour_boundary():
    _repository("boundary")
    initial_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    initial = queue_due_daily_repository_refreshes(at=initial_at)[0].job
    failed_at = datetime(2026, 8, 29, 12, 30, tzinfo=UTC)
    _finish(initial, status=RepositorySyncJobStatus.FAILED, completed_at=failed_at)

    assert (
        queue_due_daily_repository_refreshes(
            at=failed_at + timedelta(hours=2) - timedelta(microseconds=1)
        )
        == ()
    )
    waiting = repository_status_snapshot(at=failed_at + timedelta(hours=1))[0]
    assert waiting.automatic_refresh.state == "retry_wait"
    assert waiting.automatic_refresh.next_action_at == failed_at + timedelta(hours=2)
    assert waiting.automatic_refresh.retry_count == 0
    assert waiting.automatic_refresh.retries_remaining == 3

    queued = queue_due_daily_repository_refreshes(at=failed_at + timedelta(hours=2))

    assert len(queued) == 1
    assert queued[0].job.trigger == RepositorySyncTrigger.RETRY
    assert queued[0].job.scheduled_day == failed_at.date()
    assert queued[0].job.automatic_retry_number == 1


def test_successful_retry_stops_more_attempts_for_the_day():
    repository = _repository("recovered")
    initial_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    initial = queue_due_daily_repository_refreshes(at=initial_at)[0].job
    failed_at = initial_at + timedelta(minutes=10)
    _finish(initial, status=RepositorySyncJobStatus.INTERRUPTED, completed_at=failed_at)
    retry = queue_due_daily_repository_refreshes(at=failed_at + timedelta(hours=2))[0].job
    succeeded_at = failed_at + timedelta(hours=2, minutes=15)
    _finish(retry, status=RepositorySyncJobStatus.SUCCEEDED, completed_at=succeeded_at)

    assert queue_due_daily_repository_refreshes(at=succeeded_at + timedelta(hours=5)) == ()
    status = repository_status_snapshot(at=succeeded_at + timedelta(hours=5))[0].automatic_refresh

    assert status.state == "up_to_date"
    assert status.retry_count == 1
    assert status.retries_remaining == 2
    assert RepositorySyncJob.objects.filter(repository=repository).count() == 2


def test_initial_attempt_plus_three_retries_is_the_daily_maximum():
    repository = _repository("exhausted")
    attempt = queue_due_daily_repository_refreshes(at=datetime(2026, 8, 29, 12, tzinfo=UTC))[0].job
    failed_at = datetime(2026, 8, 29, 12, 10, tzinfo=UTC)

    for expected_retry in range(1, 4):
        _finish(attempt, status=RepositorySyncJobStatus.FAILED, completed_at=failed_at)
        queued = queue_due_daily_repository_refreshes(at=failed_at + timedelta(hours=2))
        assert len(queued) == 1
        attempt = queued[0].job
        assert attempt.automatic_retry_number == expected_retry
        failed_at += timedelta(hours=2, minutes=10)

    _finish(attempt, status=RepositorySyncJobStatus.FAILED, completed_at=failed_at)

    assert queue_due_daily_repository_refreshes(at=failed_at + timedelta(hours=2)) == ()
    status = repository_status_snapshot(at=failed_at + timedelta(hours=2))[0].automatic_refresh
    assert status.state == "exhausted"
    assert status.retry_count == 3
    assert status.retries_remaining == 0
    assert list(
        RepositorySyncJob.objects.filter(repository=repository)
        .order_by("automatic_retry_number")
        .values_list("automatic_retry_number", flat=True)
    ) == [0, 1, 2, 3]


def test_retry_budget_resets_on_new_local_day():
    repository = _repository("new-day")
    old_day = date(2026, 8, 29)
    for retry_number in range(4):
        _completed_job(
            repository,
            status=RepositorySyncJobStatus.FAILED,
            completed_at=datetime(2026, 8, 29, 14 + retry_number, tzinfo=UTC),
            trigger=(
                RepositorySyncTrigger.DAILY if retry_number == 0 else RepositorySyncTrigger.RETRY
            ),
            scheduled_day=old_day,
            retry_number=retry_number,
        )

    queued = queue_due_daily_repository_refreshes(at=datetime(2026, 8, 30, 11, tzinfo=UTC))

    assert len(queued) == 1
    assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
    assert queued[0].job.scheduled_day == date(2026, 8, 30)
    assert queued[0].job.automatic_retry_number == 0


def test_new_11_slot_preserves_retry_delay_after_previous_slot_failure():
    repository = _repository("slot-delay")
    failed_at = datetime(2026, 8, 30, 10, 30, tzinfo=UTC)
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.FAILED,
        completed_at=failed_at,
        trigger=RepositorySyncTrigger.RETRY,
        scheduled_day=date(2026, 8, 29),
        retry_number=3,
    )

    assert queue_due_daily_repository_refreshes(at=datetime(2026, 8, 30, 11, tzinfo=UTC)) == ()
    waiting = repository_status_snapshot(at=datetime(2026, 8, 30, 12, 29, 59, tzinfo=UTC))[
        0
    ].automatic_refresh
    assert waiting.state == "retry_wait"
    assert waiting.retry_count == 0
    assert waiting.scheduled_day == date(2026, 8, 30)

    queued = queue_due_daily_repository_refreshes(at=datetime(2026, 8, 30, 12, 30, tzinfo=UTC))

    assert len(queued) == 1
    assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
    assert queued[0].job.scheduled_day == date(2026, 8, 30)
    assert queued[0].job.automatic_retry_number == 0


def test_previous_slot_success_supersedes_an_earlier_failure_delay():
    repository = _repository("slot-recovered")
    old_day = date(2026, 8, 29)
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.FAILED,
        completed_at=datetime(2026, 8, 29, 23, tzinfo=UTC),
        trigger=RepositorySyncTrigger.DAILY,
        scheduled_day=old_day,
    )
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.SUCCEEDED,
        completed_at=datetime(2026, 8, 29, 23, 30, tzinfo=UTC),
        trigger=RepositorySyncTrigger.RETRY,
        scheduled_day=old_day,
        retry_number=1,
    )

    queued = queue_due_daily_repository_refreshes(at=datetime(2026, 8, 30, 11, tzinfo=UTC))

    assert len(queued) == 1
    assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
    assert queued[0].job.scheduled_day == date(2026, 8, 30)


@pytest.mark.parametrize(
    ("due_slot", "previous_slot_day"),
    [
        (datetime(2026, 7, 1, 10, tzinfo=UTC), date(2026, 6, 30)),
        (datetime(2026, 11, 1, 11, tzinfo=UTC), date(2026, 10, 31)),
    ],
)
def test_dublin_11_slot_tracks_daylight_saving_time(due_slot, previous_slot_day):
    with timezone.override("Europe/Dublin"):
        repository = _repository(f"dublin-{due_slot.date()}")
        _completed_job(
            repository,
            status=RepositorySyncJobStatus.SUCCEEDED,
            completed_at=due_slot - timedelta(hours=12),
        )
        before_slot = due_slot - timedelta(microseconds=1)

        assert queue_due_daily_repository_refreshes(at=before_slot) == ()
        waiting = repository_status_snapshot(at=before_slot)[0].automatic_refresh
        assert waiting.state == "up_to_date"
        assert waiting.scheduled_day == previous_slot_day
        assert waiting.next_action_at == due_slot
        assert timezone.localtime(waiting.next_action_at).hour == 11

        queued = queue_due_daily_repository_refreshes(at=due_slot)

        assert len(queued) == 1
        assert queued[0].job.trigger == RepositorySyncTrigger.DAILY
        assert (
            queued[0].job.scheduled_day
            == due_slot.astimezone(timezone.get_current_timezone()).date()
        )


def test_scheduled_attempt_identity_is_database_deduplicated():
    repository = _repository("unique")
    scheduled_day = date(2026, 8, 29)
    _completed_job(
        repository,
        status=RepositorySyncJobStatus.FAILED,
        completed_at=datetime(2026, 8, 29, 7, tzinfo=UTC),
        trigger=RepositorySyncTrigger.DAILY,
        scheduled_day=scheduled_day,
    )
    worker_retry = _completed_job(
        repository,
        status=RepositorySyncJobStatus.FAILED,
        completed_at=datetime(2026, 8, 29, 7, 30, tzinfo=UTC),
        trigger=RepositorySyncTrigger.DAILY,
        scheduled_day=scheduled_day,
        worker_retry_number=1,
    )

    assert worker_retry.worker_retry_number == 1

    with pytest.raises(IntegrityError), transaction.atomic():
        _completed_job(
            repository,
            status=RepositorySyncJobStatus.FAILED,
            completed_at=datetime(2026, 8, 29, 8, tzinfo=UTC),
            trigger=RepositorySyncTrigger.DAILY,
            scheduled_day=scheduled_day,
        )


@pytest.mark.parametrize(
    ("trigger", "scheduled_day", "retry_number"),
    [
        (RepositorySyncTrigger.MANUAL, date(2026, 8, 29), 0),
        (RepositorySyncTrigger.MANUAL, None, 1),
        (RepositorySyncTrigger.DAILY, None, 0),
        (RepositorySyncTrigger.DAILY, date(2026, 8, 29), 1),
        (RepositorySyncTrigger.RETRY, None, 1),
        (RepositorySyncTrigger.RETRY, date(2026, 8, 29), 0),
    ],
)
def test_trigger_metadata_consistency_is_database_enforced(
    trigger,
    scheduled_day,
    retry_number,
):
    repository = _repository(f"invalid-{trigger}-{retry_number}-{scheduled_day}")

    with pytest.raises(IntegrityError), transaction.atomic():
        RepositorySyncJob.objects.create(
            repository=repository,
            operation=RepositorySyncOperation.REFRESH,
            trigger=trigger,
            scheduled_day=scheduled_day,
            automatic_retry_number=retry_number,
            status=RepositorySyncJobStatus.FAILED,
            completed_at=datetime(2026, 8, 29, 9, tzinfo=UTC),
        )


def test_repository_snapshot_always_attaches_frozen_automation_status():
    enabled = _repository("snapshot-enabled")
    disabled = _repository("snapshot-disabled", enabled=False)
    observed_at = datetime(2026, 8, 29, 11, tzinfo=UTC)

    snapshots = repository_status_snapshot(at=observed_at)

    assert {repository.pk for repository in snapshots} == {enabled.pk, disabled.pk}
    states = {repository.pk: repository.automatic_refresh.state for repository in snapshots}
    assert states == {enabled.pk: "due", disabled.pk: "disabled"}
    with pytest.raises((AttributeError, TypeError)):
        snapshots[0].automatic_refresh.state = "changed"


def test_repository_snapshot_prefetches_only_bounded_automation_history():
    repository = _repository("bounded-snapshot")
    observed_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    for offset in range(1, 21):
        _completed_job(
            repository,
            status=RepositorySyncJobStatus.FAILED,
            completed_at=observed_at - timedelta(days=offset),
        )
    for offset in range(1, 11):
        old_day = observed_at.date() - timedelta(days=offset)
        _completed_job(
            repository,
            status=RepositorySyncJobStatus.FAILED,
            completed_at=observed_at - timedelta(days=offset),
            trigger=RepositorySyncTrigger.DAILY,
            scheduled_day=old_day,
        )
    current = _completed_job(
        repository,
        status=RepositorySyncJobStatus.FAILED,
        completed_at=observed_at - timedelta(minutes=30),
        trigger=RepositorySyncTrigger.DAILY,
        scheduled_day=observed_at.date(),
    )

    snapshot = repository_status_snapshot(at=observed_at)[0]

    assert [job.pk for job in snapshot._automatic_refresh_jobs] == [current.pk]
    assert snapshot.automatic_refresh.state == "retry_wait"
    assert snapshot.automatic_refresh.last_attempt_at == current.completed_at
