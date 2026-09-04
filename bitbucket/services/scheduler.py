"""Queue each repository's daily pull once in OWL's local timezone."""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from bitbucket.models import (
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)


def queue_due_daily_pulls(*, at: datetime | None = None) -> tuple[SyncJob, ...]:
    if not getattr(settings, "BITBUCKET_APP_DAILY_PULL_ENABLED", True):
        return ()
    observed_at = at or timezone.now()
    local_time = timezone.localtime(observed_at)
    configured_hour = int(getattr(settings, "BITBUCKET_APP_DAILY_PULL_LOCAL_HOUR", 9))
    if local_time.hour < configured_hour:
        return ()
    scheduled_day = local_time.date()
    jobs: list[SyncJob] = []
    repositories = Repository.objects.filter(
        enabled=True,
        state=RepositoryState.READY,
    ).exclude(last_successful_pull_on=scheduled_day)
    for repository in repositories.iterator():
        try:
            with transaction.atomic():
                job, created = SyncJob.objects.get_or_create(
                    repository=repository,
                    operation=SyncOperation.PULL,
                    scheduled_for=scheduled_day,
                    defaults={"status": SyncJobStatus.QUEUED},
                )
        except IntegrityError:
            continue
        if created:
            Repository.objects.filter(pk=repository.pk).update(
                state=RepositoryState.QUEUED,
                status_message="Today's pull is queued.",
            )
            jobs.append(job)
    return tuple(jobs)


def retry_job(job: SyncJob) -> SyncJob:
    if job.status not in {
        SyncJobStatus.AUTH_REQUIRED,
        SyncJobStatus.FAILED,
        SyncJobStatus.CANCELLED,
    }:
        return job
    job.status = SyncJobStatus.QUEUED
    job.error_code = ""
    job.error_message = ""
    job.started_at = None
    job.finished_at = None
    job.save(
        update_fields=(
            "status",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
        )
    )
    Repository.objects.filter(pk=job.repository_id).update(
        state=RepositoryState.QUEUED,
        status_message="Retry queued.",
        error_message="",
    )
    return job


def cancel_job(job: SyncJob) -> SyncJob:
    if job.status in {
        SyncJobStatus.QUEUED,
        SyncJobStatus.AUTH_REQUIRED,
        SyncJobStatus.FAILED,
    }:
        job.status = SyncJobStatus.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=("status", "finished_at"))
        Repository.objects.filter(pk=job.repository_id).update(
            state=(
                RepositoryState.FAILED
                if job.operation == SyncOperation.CLONE
                else RepositoryState.READY
            ),
            status_message="Connection retry cancelled.",
        )
    return job
