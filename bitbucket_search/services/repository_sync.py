"""Durable queue and worker orchestration for Bitbucket repository syncs."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services.git_sync import (
    RepositorySyncError,
    managed_repository_path,
    synchronize_repository,
)
from bitbucket_search.services.pdf_catalog import (
    build_repository_pdf_catalog,
    publish_repository_pdf_catalog,
)
from bitbucket_search.services.pdf_indexing import queue_repository_pdf_extractions
from bitbucket_search.services.repository_lock import repository_worker_wakeup_lock
from bitbucket_search.services.repository_urls import normalize_repository_url
from bookmark_manager.models import Notification, NotificationKind, NotificationState
from bookmark_manager.services.notifications import publish_notification

logger = logging.getLogger("owl.bitbucket.sync")

ACTIVE_JOB_STATUSES = (RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
AUTOMATIC_JOB_TRIGGERS = (RepositorySyncTrigger.DAILY, RepositorySyncTrigger.RETRY)
AUTOMATIC_RETRYABLE_STATUSES = (
    RepositorySyncJobStatus.FAILED,
    RepositorySyncJobStatus.INTERRUPTED,
)
STALE_JOB_AFTER = timedelta(minutes=10)
WORKER_WAKE_RESERVATION_AFTER = timedelta(seconds=30)
_RESIDENT_REPOSITORY_WORKERS_ACTIVE = threading.Event()


@dataclass(frozen=True, slots=True)
class QueueResult:
    repository: BitbucketRepository
    job: RepositorySyncJob
    repository_created: bool
    job_created: bool


@dataclass(frozen=True, slots=True)
class QueueAllRepositoriesResult:
    """One idempotent queue pass across all currently enabled repositories."""

    eligible_total: int
    results: tuple[QueueResult, ...]
    fallback_worker_jobs: tuple[RepositorySyncJob, ...]

    @property
    def newly_queued(self) -> tuple[QueueResult, ...]:
        return tuple(result for result in self.results if result.job_created)

    @property
    def already_active(self) -> tuple[QueueResult, ...]:
        return tuple(result for result in self.results if not result.job_created)

    @property
    def already_queued(self) -> tuple[QueueResult, ...]:
        return tuple(
            result
            for result in self.already_active
            if result.job.status == RepositorySyncJobStatus.QUEUED
        )

    @property
    def already_running(self) -> tuple[QueueResult, ...]:
        return tuple(
            result
            for result in self.already_active
            if result.job.status == RepositorySyncJobStatus.RUNNING
        )

    @property
    def newly_queued_count(self) -> int:
        return len(self.newly_queued)

    @property
    def already_active_count(self) -> int:
        return len(self.already_active)

    @property
    def already_queued_count(self) -> int:
        return len(self.already_queued)

    @property
    def already_running_count(self) -> int:
        return len(self.already_running)


@dataclass(frozen=True, slots=True)
class RepositoryAutomationStatus:
    """Compact daily-refresh state attached to a repository status snapshot."""

    state: str
    label: str
    detail: str
    next_action_at: datetime | None
    retry_count: int
    max_retries: int
    retries_remaining: int
    scheduled_day: date | None
    trigger: str | None
    last_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkerWakeReservation:
    """Queued jobs reserved briefly while a detached helper starts."""

    job_ids: tuple[int, ...]
    reserved_at: datetime
    launcher_pid: int


def _daily_refresh_retry_delay() -> timedelta:
    return timedelta(seconds=settings.BITBUCKET_DAILY_REFRESH_RETRY_SECONDS)


def _daily_refresh_max_retries() -> int:
    return max(0, int(settings.BITBUCKET_DAILY_REFRESH_MAX_RETRIES))


def _daily_refresh_retry_delay_label() -> str:
    seconds = max(0, int(_daily_refresh_retry_delay().total_seconds()))
    if seconds % 86_400 == 0:
        count = seconds // 86_400
        unit = "day"
    elif seconds % 3_600 == 0:
        count = seconds // 3_600
        unit = "hour"
    elif seconds % 60 == 0:
        count = seconds // 60
        unit = "minute"
    else:
        count = seconds
        unit = "second"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _automatic_refresh_notification_event_key(job: RepositorySyncJob) -> str | None:
    if job.trigger not in AUTOMATIC_JOB_TRIGGERS or job.scheduled_day is None:
        return None
    return f"bitbucket-refresh:{job.repository_id}:{job.scheduled_day.isoformat()}"


def _notification_text(*parts: object) -> str:
    return " ".join(" ".join(str(part or "").split()) for part in parts if part)[:500]


def _publish_automatic_refresh_failure(
    job: RepositorySyncJob,
    *,
    summary: str,
    occurred_at: datetime,
) -> None:
    """Update one durable notification for this repository's failed daily cycle."""

    event_key = _automatic_refresh_notification_event_key(job)
    if event_key is None:
        return
    max_retries = _daily_refresh_max_retries()
    retries_remaining = max(0, max_retries - job.automatic_retry_number)
    if retries_remaining:
        state = NotificationState.WARNING
        retry_word = "retry" if retries_remaining == 1 else "retries"
        next_step = (
            f"OWL will retry automatically after {_daily_refresh_retry_delay_label()}; "
            f"{retries_remaining} automatic {retry_word} remain in this daily cycle."
        )
    else:
        state = NotificationState.ERROR
        next_step = (
            "No automatic retries remain for this daily cycle. Select Refresh to try again now."
        )
    try:
        publish_notification(
            event_key=event_key,
            kind=NotificationKind.BITBUCKET_REFRESH,
            state=state,
            title=_notification_text(
                "Daily refresh failed:",
                job.repository.display_name,
            )[:200],
            message=_notification_text(summary, next_step),
            target_path="/pdfs/status/",
            occurred_at=occurred_at,
            # A later failure can remain a warning while its retry count changes.
            # Make that new failure visible again without creating another card.
            mark_unread=True,
        )
    except Exception:
        logger.exception(
            "The daily Bitbucket refresh failure notification could not be recorded",
        )


def _publish_automatic_refresh_recovery(
    job: RepositorySyncJob,
    *,
    occurred_at: datetime,
) -> None:
    """Resolve an existing daily failure card after an automatic retry succeeds."""

    event_key = _automatic_refresh_notification_event_key(job)
    if event_key is None:
        return
    try:
        if not Notification.objects.filter(event_key=event_key).exists():
            return
        publish_notification(
            event_key=event_key,
            kind=NotificationKind.BITBUCKET_REFRESH,
            state=NotificationState.SUCCESS,
            title=_notification_text(
                "Daily refresh recovered:",
                job.repository.display_name,
            )[:200],
            message=("The repository refreshed successfully after an earlier automatic failure."),
            target_path="/pdfs/status/",
            occurred_at=occurred_at,
            mark_unread=True,
        )
    except Exception:
        logger.exception(
            "The daily Bitbucket refresh recovery notification could not be recorded",
        )


def set_resident_repository_workers_active(active: bool) -> None:
    """Publish whether this web process owns the supervised ``run_owl`` pool."""

    if active:
        _RESIDENT_REPOSITORY_WORKERS_ACTIVE.set()
    else:
        _RESIDENT_REPOSITORY_WORKERS_ACTIVE.clear()


def resident_repository_workers_active() -> bool:
    return _RESIDENT_REPOSITORY_WORKERS_ACTIVE.is_set()


def _daily_refresh_local_time_label() -> str:
    hour = int(settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR)
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:00 {suffix}"


def _daily_refresh_window(observed_at: datetime) -> tuple[date, datetime, datetime]:
    """Return the latest due daily slot, its success boundary, and next slot.

    A slot becomes due at the configured local hour. Before today's slot, the
    latest due slot is yesterday's, which lets an OWL process that starts the
    following morning catch up immediately. Success is measured from the slot
    day's local midnight so a clone or manual pull earlier that day avoids a
    redundant automatic pull at the configured slot.
    """

    local_zone = timezone.get_current_timezone()
    local_now = timezone.localtime(observed_at, local_zone)
    local_day = local_now.date()
    refresh_time = datetime_time(hour=settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR)
    today_slot = datetime.combine(local_day, refresh_time, tzinfo=local_zone)
    scheduled_day = local_day if local_now >= today_slot else local_day - timedelta(days=1)
    success_boundary = datetime.combine(
        scheduled_day,
        datetime_time.min,
        tzinfo=local_zone,
    )
    next_slot_day = scheduled_day + timedelta(days=1)
    next_slot = datetime.combine(next_slot_day, refresh_time, tzinfo=local_zone)
    return scheduled_day, success_boundary, next_slot


def _job_attempt_at(job: RepositorySyncJob) -> datetime:
    return job.completed_at or job.started_at or job.requested_at


def _repository_succeeded_since(
    repository: BitbucketRepository,
    *,
    since: datetime,
    until: datetime,
    jobs: tuple[RepositorySyncJob, ...] | None = None,
) -> RepositorySyncJob | None:
    if jobs is None:
        return (
            repository.sync_jobs.filter(
                status=RepositorySyncJobStatus.SUCCEEDED,
                completed_at__gte=since,
                completed_at__lte=until,
            )
            .order_by("-completed_at", "-id")
            .first()
        )
    successes = tuple(
        job
        for job in jobs
        if job.status == RepositorySyncJobStatus.SUCCEEDED
        and job.completed_at is not None
        and since <= job.completed_at <= until
    )
    return max(successes, key=lambda job: (job.completed_at, job.pk)) if successes else None


def _status_value(
    *,
    state: str,
    label: str,
    detail: str,
    next_action_at: datetime | None,
    retry_count: int,
    max_retries: int,
    scheduled_day: date | None,
    trigger: str | None,
    last_attempt_at: datetime | None,
) -> RepositoryAutomationStatus:
    return RepositoryAutomationStatus(
        state=state,
        label=label,
        detail=detail,
        next_action_at=next_action_at,
        retry_count=retry_count,
        max_retries=max_retries,
        retries_remaining=max(0, max_retries - retry_count),
        scheduled_day=scheduled_day,
        trigger=trigger,
        last_attempt_at=last_attempt_at,
    )


def _repository_automation_status(
    repository: BitbucketRepository,
    *,
    at: datetime,
) -> RepositoryAutomationStatus:
    scheduled_day, success_boundary, next_slot = _daily_refresh_window(at)
    max_retries = _daily_refresh_max_retries()
    jobs = tuple(
        getattr(
            repository,
            "_automatic_refresh_jobs",
            repository.sync_jobs.all(),
        )
    )
    active = next((job for job in jobs if job.status in ACTIVE_JOB_STATUSES), None)
    automatic_jobs = tuple(job for job in jobs if job.trigger in AUTOMATIC_JOB_TRIGGERS)
    latest_automatic = (
        max(automatic_jobs, key=lambda job: (_job_attempt_at(job), job.pk))
        if automatic_jobs
        else None
    )
    latest_job = max(jobs, key=lambda job: (_job_attempt_at(job), job.pk)) if jobs else None
    latest_attempt_at = (
        max(
            value
            for value in (
                _job_attempt_at(latest_job) if latest_job else None,
                repository.last_sync_completed_at,
            )
            if value is not None
        )
        if latest_job is not None or repository.last_sync_completed_at is not None
        else None
    )

    if not settings.BITBUCKET_DAILY_REFRESH_ENABLED or not repository.enabled:
        detail = (
            "Automatic daily repository refresh is disabled in OWL settings."
            if not settings.BITBUCKET_DAILY_REFRESH_ENABLED
            else "This repository is disabled and is excluded from automatic refresh."
        )
        return _status_value(
            state="disabled",
            label="Automatic refresh disabled",
            detail=detail,
            next_action_at=None,
            retry_count=0,
            max_retries=max_retries,
            scheduled_day=None,
            trigger=latest_job.trigger if latest_job else None,
            last_attempt_at=latest_attempt_at,
        )

    if active is not None:
        retry_count = (
            active.automatic_retry_number
            if active.trigger in AUTOMATIC_JOB_TRIGGERS
            else (
                latest_automatic.automatic_retry_number
                if latest_automatic is not None and latest_automatic.scheduled_day == scheduled_day
                else 0
            )
        )
        detail = {
            RepositorySyncTrigger.DAILY: "The scheduled automatic repository refresh is in progress.",
            RepositorySyncTrigger.RETRY: (
                f"Automatic retry {active.automatic_retry_number} of {max_retries} is in progress."
            ),
        }.get(active.trigger, "A manual repository refresh is in progress.")
        return _status_value(
            state="active",
            label="Repository refresh active",
            detail=detail,
            next_action_at=None,
            retry_count=retry_count,
            max_retries=max_retries,
            scheduled_day=active.scheduled_day,
            trigger=active.trigger,
            last_attempt_at=_job_attempt_at(active),
        )

    success = _repository_succeeded_since(
        repository,
        since=success_boundary,
        until=at,
        jobs=jobs,
    )
    repository_success = repository.last_sync_successful_at
    if success is not None or (
        repository_success is not None and success_boundary <= repository_success <= at
    ):
        successful_job = success
        retry_count = (
            successful_job.automatic_retry_number
            if successful_job is not None and successful_job.trigger in AUTOMATIC_JOB_TRIGGERS
            else (
                latest_automatic.automatic_retry_number
                if latest_automatic is not None and latest_automatic.scheduled_day == scheduled_day
                else 0
            )
        )
        return _status_value(
            state="up_to_date",
            label="Up to date",
            detail=(
                "A repository sync succeeded in this daily cycle. "
                f"The next check is at {_daily_refresh_local_time_label()} local time."
            ),
            next_action_at=next_slot,
            retry_count=retry_count,
            max_retries=max_retries,
            scheduled_day=successful_job.scheduled_day if successful_job else scheduled_day,
            trigger=successful_job.trigger if successful_job else None,
            last_attempt_at=(
                _job_attempt_at(successful_job) if successful_job else repository_success
            ),
        )

    today_jobs = tuple(job for job in automatic_jobs if job.scheduled_day == scheduled_day)
    latest_today = (
        max(today_jobs, key=lambda job: (job.automatic_retry_number, job.pk))
        if today_jobs
        else None
    )
    if latest_today is not None:
        retry_count = latest_today.automatic_retry_number
        last_attempt_at = _job_attempt_at(latest_today)
        if latest_today.status in AUTOMATIC_RETRYABLE_STATUSES:
            if retry_count >= max_retries:
                return _status_value(
                    state="exhausted",
                    label="Retries exhausted",
                    detail=(
                        "This daily refresh used its retry allowance. "
                        "OWL will try again at the next scheduled "
                        f"{_daily_refresh_local_time_label()} check."
                    ),
                    next_action_at=next_slot,
                    retry_count=retry_count,
                    max_retries=max_retries,
                    scheduled_day=scheduled_day,
                    trigger=latest_today.trigger,
                    last_attempt_at=last_attempt_at,
                )
            retry_at = last_attempt_at + _daily_refresh_retry_delay()
            waiting = retry_at > at
            return _status_value(
                state="retry_wait" if waiting else "due",
                label="Retry scheduled" if waiting else "Automatic retry due",
                detail=(
                    f"Automatic retry {retry_count + 1} of {max_retries} will run after its "
                    "configured recovery delay."
                    if waiting
                    else f"Automatic retry {retry_count + 1} of {max_retries} is ready to queue."
                ),
                next_action_at=retry_at,
                retry_count=retry_count,
                max_retries=max_retries,
                scheduled_day=scheduled_day,
                trigger=RepositorySyncTrigger.RETRY,
                last_attempt_at=last_attempt_at,
            )
        return _status_value(
            state="exhausted",
            label="Automatic refresh stopped",
            detail="This automatic refresh ended without an eligible recovery retry.",
            next_action_at=next_slot,
            retry_count=retry_count,
            max_retries=max_retries,
            scheduled_day=scheduled_day,
            trigger=latest_today.trigger,
            last_attempt_at=last_attempt_at,
        )

    if latest_automatic is not None and latest_automatic.status in AUTOMATIC_RETRYABLE_STATUSES:
        retry_at = _job_attempt_at(latest_automatic) + _daily_refresh_retry_delay()
        if retry_at > at:
            return _status_value(
                state="retry_wait",
                label="Daily refresh waiting",
                detail=(
                    "The daily retry allowance has reset, but OWL is preserving the "
                    "two-hour delay after the last failure."
                ),
                next_action_at=retry_at,
                retry_count=0,
                max_retries=max_retries,
                scheduled_day=scheduled_day,
                trigger=RepositorySyncTrigger.DAILY,
                last_attempt_at=_job_attempt_at(latest_automatic),
            )

    return _status_value(
        state="due",
        label="Daily refresh due",
        detail="The scheduled automatic repository refresh is ready to queue.",
        next_action_at=at,
        retry_count=0,
        max_retries=max_retries,
        scheduled_day=scheduled_day,
        trigger=RepositorySyncTrigger.DAILY,
        last_attempt_at=latest_attempt_at,
    )


def _stale_running_job_ids(cutoff) -> tuple[int, ...]:
    return tuple(
        RepositorySyncJob.objects.filter(
            status=RepositorySyncJobStatus.RUNNING,
            heartbeat_at__lt=cutoff,
        ).values_list("id", flat=True)
    )


def _interrupt_stale_jobs(*, at=None) -> None:
    observed_at = at or timezone.now()
    cutoff = observed_at - STALE_JOB_AFTER
    candidate_ids = _stale_running_job_ids(cutoff)
    if not candidate_ids:
        return

    interrupted_ids: list[int] = []
    with transaction.atomic():
        for job_id in candidate_ids:
            # The worker may have refreshed its heartbeat after the candidate
            # query. Repeat every stale predicate in this compare-and-swap so a
            # healthy long-running Git process is never interrupted by that race.
            updated = RepositorySyncJob.objects.filter(
                pk=job_id,
                status=RepositorySyncJobStatus.RUNNING,
                heartbeat_at__lt=cutoff,
            ).update(
                status=RepositorySyncJobStatus.INTERRUPTED,
                completed_at=observed_at,
                error_code="worker_interrupted",
                error_summary=(
                    "The background repository worker stopped before this sync completed."
                ),
            )
            if updated == 1:
                interrupted_ids.append(job_id)

        if interrupted_ids:
            # Do not overwrite repository progress for a candidate whose worker
            # won the heartbeat race; only jobs transitioned above belong here.
            BitbucketRepository.objects.filter(sync_jobs__id__in=interrupted_ids).update(
                sync_state=RepositorySyncState.INTERRUPTED,
                status_message="Background sync was interrupted. Select Refresh to retry.",
                last_error_code="worker_interrupted",
                last_error_summary=(
                    "The background repository worker stopped before this sync completed."
                ),
                last_sync_completed_at=observed_at,
            )
    if interrupted_ids:
        interrupted_jobs = RepositorySyncJob.objects.select_related("repository").filter(
            pk__in=interrupted_ids,
        )
        for interrupted_job in interrupted_jobs:
            _publish_automatic_refresh_failure(
                interrupted_job,
                summary=interrupted_job.error_summary,
                occurred_at=observed_at,
            )


def _operation_for(repository: BitbucketRepository) -> str:
    target = managed_repository_path(repository)
    return (
        RepositorySyncOperation.REFRESH
        if target.is_dir() and (target / ".git").is_dir()
        else RepositorySyncOperation.CLONE
    )


def _queue_job(
    repository: BitbucketRepository,
    *,
    trigger: str = RepositorySyncTrigger.MANUAL,
    scheduled_day: date | None = None,
    automatic_retry_number: int = 0,
) -> tuple[RepositorySyncJob, bool]:
    active = repository.sync_jobs.filter(status__in=ACTIVE_JOB_STATUSES).first()
    if active is not None:
        return active, False
    operation = _operation_for(repository)
    if trigger == RepositorySyncTrigger.RETRY:
        waiting_message = (
            f"Waiting for automatic retry {automatic_retry_number} of "
            f"{_daily_refresh_max_retries()}…"
        )
    elif trigger == RepositorySyncTrigger.DAILY:
        waiting_message = "Waiting for the scheduled automatic repository refresh…"
    else:
        waiting_message = (
            "Waiting for the background worker to start the first clone…"
            if operation == RepositorySyncOperation.CLONE
            else "Waiting for the background worker to refresh this repository…"
        )
    try:
        with transaction.atomic():
            job = RepositorySyncJob.objects.create(
                repository=repository,
                operation=operation,
                trigger=trigger,
                scheduled_day=scheduled_day,
                automatic_retry_number=automatic_retry_number,
                status_message=waiting_message,
            )
    except IntegrityError:
        active = repository.sync_jobs.filter(status__in=ACTIVE_JOB_STATUSES).first()
        if active is not None:
            return active, False
        if scheduled_day is not None:
            scheduled = repository.sync_jobs.filter(
                scheduled_day=scheduled_day,
                automatic_retry_number=automatic_retry_number,
            ).first()
            if scheduled is not None:
                return scheduled, False
        raise
    repository.sync_state = RepositorySyncState.QUEUED
    repository.sync_progress = 0
    repository.status_message = job.status_message
    repository.last_error_code = ""
    repository.last_error_summary = ""
    repository.save(
        update_fields=(
            "sync_state",
            "sync_progress",
            "status_message",
            "last_error_code",
            "last_error_summary",
            "updated_at",
        )
    )
    return job, True


def register_and_queue_repository(remote_url: object) -> QueueResult:
    """Register a canonical repository once and queue clone/refresh idempotently."""

    normalized = normalize_repository_url(remote_url)
    _interrupt_stale_jobs()
    with transaction.atomic():
        repository, repository_created = BitbucketRepository.objects.get_or_create(
            canonical_remote_key=normalized.canonical_remote_key,
            defaults={
                "display_name": normalized.display_name,
                "remote_url": normalized.remote_url,
            },
        )
        if repository_created:
            repository.local_path = str(managed_repository_path(repository))
            repository.save(update_fields=("local_path", "updated_at"))
        job, job_created = _queue_job(repository)
    return QueueResult(repository, job, repository_created, job_created)


def _queue_repository_refresh(
    repository_id: int,
    *,
    enabled_only: bool,
) -> QueueResult:
    _interrupt_stale_jobs()
    with transaction.atomic():
        repositories = BitbucketRepository.objects.select_for_update()
        if enabled_only:
            repositories = repositories.filter(enabled=True)
        repository = repositories.get(pk=repository_id)
        job, job_created = _queue_job(repository)
    return QueueResult(repository, job, False, job_created)


def queue_repository_refresh(repository_id: int) -> QueueResult:
    """Queue the next safe clone/refresh for an existing repository."""

    return _queue_repository_refresh(repository_id, enabled_only=False)


def queue_all_repository_refreshes() -> QueueAllRepositoriesResult:
    """Queue clone/refresh work once for every enabled managed repository.

    Existing queued or running jobs are returned as active rather than
    duplicated. A queued job, including one created by an earlier request, is
    included in ``fallback_worker_jobs`` so an explicit web request can wake a
    detached worker when OWL's resident supervisor is not running.
    """

    repository_ids = tuple(
        BitbucketRepository.objects.filter(enabled=True).order_by("id").values_list("id", flat=True)
    )
    results: list[QueueResult] = []
    fallback_worker_jobs: dict[int, RepositorySyncJob] = {}
    for repository_id in repository_ids:
        try:
            queued = _queue_repository_refresh(repository_id, enabled_only=True)
        except BitbucketRepository.DoesNotExist:
            # Deletion or disabling between the eligible snapshot and this
            # repository's row lock is harmless; no job should be queued.
            continue
        results.append(queued)
        if queued.job.status == RepositorySyncJobStatus.QUEUED:
            fallback_worker_jobs[queued.job.pk] = queued.job

    return QueueAllRepositoriesResult(
        # Repositories disabled or deleted after the initial ordered snapshot
        # were never eligible at their row lock and must not inflate user-facing
        # queue totals.
        eligible_total=len(results),
        results=tuple(results),
        fallback_worker_jobs=tuple(fallback_worker_jobs.values()),
    )


def queue_due_daily_repository_refreshes(*, at=None) -> tuple[QueueResult, ...]:
    """Queue due daily repository attempts and delayed retries idempotently."""

    if not settings.BITBUCKET_DAILY_REFRESH_ENABLED:
        return ()
    observed_at = at or timezone.now()
    scheduled_day, success_boundary, _next_slot = _daily_refresh_window(observed_at)
    retry_delay = _daily_refresh_retry_delay()
    max_retries = _daily_refresh_max_retries()
    _interrupt_stale_jobs(at=observed_at)
    active_job = RepositorySyncJob.objects.filter(
        repository_id=OuterRef("pk"),
        status__in=ACTIVE_JOB_STATUSES,
    )
    repository_ids = tuple(
        BitbucketRepository.objects.filter(
            enabled=True,
        )
        .filter(
            Q(last_sync_successful_at__isnull=True)
            | Q(last_sync_successful_at__lt=success_boundary)
        )
        .annotate(has_active_sync_job=Exists(active_job))
        .filter(has_active_sync_job=False)
        .order_by("id")
        .values_list("id", flat=True)
    )
    queued: list[QueueResult] = []

    for repository_id in repository_ids:
        with transaction.atomic():
            repository = (
                BitbucketRepository.objects.select_for_update()
                .filter(pk=repository_id, enabled=True)
                .first()
            )
            if repository is None:
                continue
            if repository.sync_jobs.filter(status__in=ACTIVE_JOB_STATUSES).exists():
                continue
            repository_success = repository.last_sync_successful_at
            if (
                repository_success is not None
                and success_boundary <= repository_success <= observed_at
            ) or _repository_succeeded_since(
                repository,
                since=success_boundary,
                until=observed_at,
            ) is not None:
                continue

            latest_today = (
                repository.sync_jobs.filter(
                    trigger__in=AUTOMATIC_JOB_TRIGGERS,
                    scheduled_day=scheduled_day,
                )
                .order_by("-automatic_retry_number", "-id")
                .first()
            )
            if latest_today is None:
                latest_automatic = (
                    repository.sync_jobs.filter(
                        trigger__in=AUTOMATIC_JOB_TRIGGERS,
                        completed_at__isnull=False,
                    )
                    .order_by("-completed_at", "-id")
                    .first()
                )
                if (
                    latest_automatic is not None
                    and latest_automatic.status in AUTOMATIC_RETRYABLE_STATUSES
                    and latest_automatic.completed_at + retry_delay > observed_at
                ):
                    continue
                trigger = RepositorySyncTrigger.DAILY
                retry_number = 0
            else:
                if latest_today.status not in AUTOMATIC_RETRYABLE_STATUSES:
                    continue
                retry_number = latest_today.automatic_retry_number + 1
                if retry_number > max_retries:
                    continue
                attempt_at = latest_today.completed_at or _job_attempt_at(latest_today)
                if attempt_at + retry_delay > observed_at:
                    continue
                trigger = RepositorySyncTrigger.RETRY

            job, job_created = _queue_job(
                repository,
                trigger=trigger,
                scheduled_day=scheduled_day,
                automatic_retry_number=retry_number,
            )
            if job_created:
                queued.append(QueueResult(repository, job, False, True))

    return tuple(queued)


def reserve_queued_repository_worker_wakeups(
    *,
    job_ids: tuple[int, ...] | None = None,
    at=None,
) -> WorkerWakeReservation:
    """Reserve bounded helper launches across tabs and Django worker processes.

    A queued job's existing heartbeat/PID columns form a short launch lease. The
    actual sync worker replaces both values when it atomically claims the job.
    A helper that never reaches the queue therefore becomes eligible again after
    a brief timeout without changing the durable QUEUED state.
    """

    observed_at = at or timezone.now()
    launcher_pid = os.getpid()
    stale_before = observed_at - WORKER_WAKE_RESERVATION_AFTER
    eligible_job_ids = None if job_ids is None else frozenset(job_ids)
    reserved_ids: list[int] = []
    # SQLite does not implement row-level SELECT ... FOR UPDATE locking. Hold a
    # named OS lock across the capacity read and lease updates so separate Django
    # processes cannot reserve the same worker slot from stale snapshots.
    with repository_worker_wakeup_lock(), transaction.atomic():
        queued_jobs = tuple(
            RepositorySyncJob.objects.select_for_update()
            .filter(status=RepositorySyncJobStatus.QUEUED)
            .order_by("requested_at", "id")
        )
        recent_reservations = sum(
            job.worker_pid is not None
            and job.heartbeat_at is not None
            and job.heartbeat_at >= stale_before
            for job in queued_jobs
        )
        running_jobs = RepositorySyncJob.objects.filter(
            status=RepositorySyncJobStatus.RUNNING,
        ).count()
        available = max(
            0,
            settings.BITBUCKET_MAX_REPO_WORKERS - running_jobs - recent_reservations,
        )
        for job in queued_jobs:
            if len(reserved_ids) >= available:
                break
            if eligible_job_ids is not None and job.pk not in eligible_job_ids:
                continue
            if (
                job.worker_pid is not None
                and job.heartbeat_at is not None
                and job.heartbeat_at >= stale_before
            ):
                continue
            updated = (
                RepositorySyncJob.objects.filter(
                    pk=job.pk,
                    status=RepositorySyncJobStatus.QUEUED,
                )
                .filter(
                    Q(worker_pid__isnull=True)
                    | Q(heartbeat_at__isnull=True)
                    | Q(heartbeat_at__lt=stale_before)
                )
                .update(
                    worker_pid=launcher_pid,
                    heartbeat_at=observed_at,
                )
            )
            if updated != 1:
                # A resident worker can still claim work without this web-only
                # reservation lock. Stop rather than use a stale capacity count.
                break
            reserved_ids.append(job.pk)
    return WorkerWakeReservation(tuple(reserved_ids), observed_at, launcher_pid)


def release_repository_worker_wakeups(
    reservation: WorkerWakeReservation,
    *,
    job_ids: tuple[int, ...] | None = None,
) -> None:
    """Release only this launcher's still-queued reservations after spawn failure."""

    selected_ids = reservation.job_ids if job_ids is None else job_ids
    if not selected_ids:
        return
    RepositorySyncJob.objects.filter(
        pk__in=selected_ids,
        status=RepositorySyncJobStatus.QUEUED,
        worker_pid=reservation.launcher_pid,
        heartbeat_at=reservation.reserved_at,
    ).update(worker_pid=None, heartbeat_at=None)


def launch_sync_worker() -> subprocess.Popen[bytes]:
    """Start a detached queue worker that remains active while work arrives."""

    command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "bitbucket_sync_worker",
        "--idle-timeout",
        str(settings.BITBUCKET_WORKER_IDLE_SECONDS),
    ]
    return subprocess.Popen(
        command,
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )


def mark_worker_launch_failed(job_id: int) -> None:
    now = timezone.now()
    job = RepositorySyncJob.objects.filter(
        pk=job_id,
        status=RepositorySyncJobStatus.QUEUED,
    ).first()
    if job is None:
        return
    with transaction.atomic():
        updated = RepositorySyncJob.objects.filter(
            pk=job.pk,
            status=RepositorySyncJobStatus.QUEUED,
        ).update(
            status=RepositorySyncJobStatus.FAILED,
            completed_at=now,
            error_code="worker_unavailable",
            error_summary="OWL could not start the background repository worker.",
        )
        if updated != 1:
            return
        BitbucketRepository.objects.filter(pk=job.repository_id).update(
            sync_state=RepositorySyncState.FAILED,
            status_message="Background worker could not start.",
            last_error_code="worker_unavailable",
            last_error_summary="OWL could not start the background repository worker.",
            last_sync_completed_at=now,
        )
    _publish_automatic_refresh_failure(
        job,
        summary="OWL could not start the background repository worker.",
        occurred_at=now,
    )


def claim_next_job() -> RepositorySyncJob | None:
    """Atomically claim one queued job while respecting the configured worker limit."""

    _interrupt_stale_jobs()
    with transaction.atomic():
        if (
            RepositorySyncJob.objects.filter(status=RepositorySyncJobStatus.RUNNING).count()
            >= settings.BITBUCKET_MAX_REPO_WORKERS
        ):
            return None
        candidate_id = (
            RepositorySyncJob.objects.filter(status=RepositorySyncJobStatus.QUEUED)
            .order_by("requested_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        now = timezone.now()
        claimed = RepositorySyncJob.objects.filter(
            pk=candidate_id,
            status=RepositorySyncJobStatus.QUEUED,
        ).update(
            status=RepositorySyncJobStatus.RUNNING,
            phase=RepositorySyncPhase.VALIDATING,
            started_at=now,
            heartbeat_at=now,
            worker_pid=os.getpid(),
            progress=1,
            status_message="Background worker is validating the repository…",
        )
        if claimed != 1:
            return None
    job = RepositorySyncJob.objects.select_related("repository").get(pk=candidate_id)
    initial_state = (
        RepositorySyncState.CLONING
        if job.operation == RepositorySyncOperation.CLONE
        else RepositorySyncState.FETCHING
    )
    BitbucketRepository.objects.filter(pk=job.repository_id).update(
        sync_state=initial_state,
        sync_progress=1,
        status_message=job.status_message,
        last_sync_started_at=job.started_at,
    )
    return job


def _repository_state_for_phase(job: RepositorySyncJob, phase: str) -> str:
    if phase == RepositorySyncPhase.CLONING:
        return RepositorySyncState.CLONING
    if phase == RepositorySyncPhase.FETCHING:
        return RepositorySyncState.FETCHING
    if phase in {RepositorySyncPhase.UPDATING, RepositorySyncPhase.DISCOVERING}:
        return RepositorySyncState.UPDATING
    return (
        RepositorySyncState.CLONING
        if job.operation == RepositorySyncOperation.CLONE
        else RepositorySyncState.FETCHING
    )


def execute_claimed_job(job_id: int) -> RepositorySyncJob:
    """Execute one already-claimed job and publish only sanitized durable state."""

    job = RepositorySyncJob.objects.select_related("repository").get(pk=job_id)
    if job.status != RepositorySyncJobStatus.RUNNING:
        return job

    last_saved_at = 0.0
    last_phase = job.phase
    last_progress = job.progress

    def save_progress(phase: str, progress: int, message: str) -> None:
        nonlocal last_saved_at, last_phase, last_progress
        now_monotonic = time.monotonic()
        progress = max(last_progress, min(int(progress), 99))
        changed = phase != last_phase or progress != last_progress
        if not changed and now_monotonic - last_saved_at < 1.5:
            return
        heartbeat = timezone.now()
        safe_message = " ".join(message.split())[:500]
        with transaction.atomic():
            updated = RepositorySyncJob.objects.filter(
                pk=job.pk,
                status=RepositorySyncJobStatus.RUNNING,
            ).update(
                phase=phase,
                progress=progress,
                status_message=safe_message,
                heartbeat_at=heartbeat,
            )
            if updated != 1:
                raise RepositorySyncError(
                    "worker_lease_lost",
                    "This repository sync was interrupted. Select Refresh to retry.",
                )
            BitbucketRepository.objects.filter(pk=job.repository_id).update(
                sync_state=_repository_state_for_phase(job, phase),
                sync_progress=progress,
                status_message=safe_message,
            )
        last_saved_at = now_monotonic
        last_phase = phase
        last_progress = progress

    try:
        result = synchronize_repository(
            job.repository,
            operation=job.operation,
            progress_callback=save_progress,
        )
        catalog = build_repository_pdf_catalog(
            job.repository,
            result_commit=result.result_commit,
            progress_callback=save_progress,
        )
        if len(catalog.documents) != result.documents.pdf_count:
            raise RepositorySyncError(
                "pdf_catalog_mismatch",
                "OWL could not reconcile the synchronized PDF inventory safely.",
            )

        now = timezone.now()
        local_path = str(managed_repository_path(job.repository))
        with transaction.atomic():
            # A stale worker must never publish a catalog after another process
            # has interrupted its lease. The job transition, inventory switch,
            # and repository summary therefore commit as one guarded unit.
            updated = RepositorySyncJob.objects.filter(
                pk=job.pk,
                status=RepositorySyncJobStatus.RUNNING,
            ).update(
                status=RepositorySyncJobStatus.SUCCEEDED,
                phase=RepositorySyncPhase.COMPLETED,
                progress=100,
                status_message="Repository is ready.",
                source_commit=result.source_commit,
                result_commit=result.result_commit,
                heartbeat_at=now,
                completed_at=now,
                error_code="",
                error_summary="",
            )
            if updated == 1:
                publish_repository_pdf_catalog(
                    job.repository,
                    catalog,
                    result_commit=result.result_commit,
                    observed_at=now,
                )
                # Extraction has its own durable queue and never runs inside the
                # repository transaction. Publishing both the catalog and these
                # target revisions together prevents a changed PDF being missed
                # after a worker restart.
                queue_repository_pdf_extractions(
                    job.repository_id,
                    repository_sync_job=job.pk,
                )
                BitbucketRepository.objects.filter(pk=job.repository_id).update(
                    local_path=local_path,
                    default_branch=result.branch,
                    sync_state=RepositorySyncState.READY,
                    sync_progress=100,
                    status_message="Repository is ready.",
                    last_error_code="",
                    last_error_summary="",
                    pdf_count=len(catalog.documents),
                    vsdx_count=result.documents.vsdx_count,
                    document_bytes=result.documents.document_bytes,
                    last_synced_commit=result.result_commit,
                    history_is_shallow=catalog.history_is_shallow,
                    metadata_indexed_commit=result.result_commit,
                    last_sync_completed_at=now,
                    last_sync_successful_at=now,
                )
        if updated == 1:
            _publish_automatic_refresh_recovery(job, occurred_at=now)
    except RepositorySyncError as exc:
        now = timezone.now()
        state = (
            RepositorySyncState.BLOCKED_DIRTY if exc.blocked_dirty else RepositorySyncState.FAILED
        )
        with transaction.atomic():
            updated = RepositorySyncJob.objects.filter(
                pk=job.pk,
                status=RepositorySyncJobStatus.RUNNING,
            ).update(
                status=RepositorySyncJobStatus.FAILED,
                heartbeat_at=now,
                completed_at=now,
                error_code=exc.code,
                error_summary=exc.summary,
                status_message=exc.summary,
            )
            if updated == 1:
                BitbucketRepository.objects.filter(pk=job.repository_id).update(
                    sync_state=state,
                    status_message=exc.summary,
                    last_error_code=exc.code,
                    last_error_summary=exc.summary,
                    last_sync_completed_at=now,
                )
        if updated == 1:
            _publish_automatic_refresh_failure(
                job,
                summary=exc.summary,
                occurred_at=now,
            )
    except Exception:
        now = timezone.now()
        summary = "The background repository worker stopped unexpectedly. Select Refresh to retry."
        with transaction.atomic():
            updated = RepositorySyncJob.objects.filter(
                pk=job.pk,
                status=RepositorySyncJobStatus.RUNNING,
            ).update(
                status=RepositorySyncJobStatus.INTERRUPTED,
                heartbeat_at=now,
                completed_at=now,
                error_code="worker_error",
                error_summary=summary,
                status_message=summary,
            )
            if updated == 1:
                BitbucketRepository.objects.filter(pk=job.repository_id).update(
                    sync_state=RepositorySyncState.INTERRUPTED,
                    status_message=summary,
                    last_error_code="worker_error",
                    last_error_summary=summary,
                    last_sync_completed_at=now,
                )
        if updated == 1:
            _publish_automatic_refresh_failure(
                job,
                summary=summary,
                occurred_at=now,
            )
    return RepositorySyncJob.objects.select_related("repository").get(pk=job.pk)


def work_one_job() -> RepositorySyncJob | None:
    job = claim_next_job()
    return execute_claimed_job(job.pk) if job is not None else None


def repository_status_snapshot(*, at=None) -> tuple[BitbucketRepository, ...]:
    observed_at = at or timezone.now()
    _interrupt_stale_jobs(at=observed_at)
    scheduled_day, _success_boundary, _next_slot = _daily_refresh_window(observed_at)
    recent_retry_cutoff = observed_at - _daily_refresh_retry_delay()
    relevant_jobs = RepositorySyncJob.objects.filter(
        Q(status__in=ACTIVE_JOB_STATUSES)
        | Q(trigger__in=AUTOMATIC_JOB_TRIGGERS, scheduled_day=scheduled_day)
        | Q(
            trigger__in=AUTOMATIC_JOB_TRIGGERS,
            status__in=AUTOMATIC_RETRYABLE_STATUSES,
            completed_at__gte=recent_retry_cutoff,
            completed_at__lte=observed_at,
        )
    ).order_by("-requested_at", "-id")
    repositories = tuple(
        BitbucketRepository.objects.prefetch_related(
            Prefetch(
                "sync_jobs",
                queryset=relevant_jobs,
                to_attr="_automatic_refresh_jobs",
            )
        ).order_by("display_name", "id")
    )
    for repository in repositories:
        repository.automatic_refresh = _repository_automation_status(
            repository,
            at=observed_at,
        )
    return repositories
