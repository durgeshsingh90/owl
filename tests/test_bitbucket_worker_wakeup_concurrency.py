from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Barrier, Event
from time import sleep

import pytest
from django.db import close_old_connections, connection, transaction

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services import repository_sync


@pytest.mark.django_db(transaction=True)
def test_repository_registration_waits_for_a_short_sqlite_writer(settings, tmp_path):
    """A foreground add must not perform a deferred read-to-write upgrade."""

    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"
    settings.BITBUCKET_ALLOWED_HOSTS = ("bitbucket.org",)
    writer_ready = Event()
    release_writer = Event()

    def hold_independent_write():
        close_old_connections()
        try:
            with transaction.atomic():
                BitbucketRepository.objects.create(
                    display_name="short-background-write",
                    canonical_remote_key="bitbucket.org/workspace/short-background-write",
                    remote_url="ssh://git@bitbucket.org/workspace/short-background-write.git",
                )
                writer_ready.set()
                assert release_writer.wait(timeout=5)
        finally:
            close_old_connections()

    def register_on_independent_connection():
        close_old_connections()
        try:
            return repository_sync.register_and_queue_repository(
                "ssh://git@bitbucket.org/workspace/foreground-add.git"
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_independent_write)
        assert writer_ready.wait(timeout=5)
        registration = executor.submit(register_on_independent_connection)
        sleep(0.1)
        release_writer.set()
        holder.result(timeout=5)
        result = registration.result(timeout=5)

    assert result.repository.display_name == "foreground-add"
    assert result.repository_created is True
    assert result.job_created is True


@pytest.mark.django_db(transaction=True)
def test_worker_wakeup_reservation_waits_for_a_short_sqlite_writer(settings, tmp_path):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="queued-foreground-add",
        canonical_remote_key="bitbucket.org/workspace/queued-foreground-add",
        remote_url="ssh://git@bitbucket.org/workspace/queued-foreground-add.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    writer_ready = Event()
    release_writer = Event()

    def hold_independent_write():
        close_old_connections()
        try:
            with transaction.atomic():
                BitbucketRepository.objects.filter(pk=repository.pk).update(
                    status_message="Brief background update"
                )
                writer_ready.set()
                assert release_writer.wait(timeout=5)
        finally:
            close_old_connections()

    def reserve_on_independent_connection():
        close_old_connections()
        try:
            return repository_sync.reserve_queued_repository_worker_wakeups(job_ids=(job.pk,))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_independent_write)
        assert writer_ready.wait(timeout=5)
        reservation = executor.submit(reserve_on_independent_connection)
        sleep(0.1)
        release_writer.set()
        holder.result(timeout=5)
        result = reservation.result(timeout=5)

    assert result.job_ids == (job.pk,)


@pytest.mark.django_db(transaction=True)
def test_concurrent_worker_wakeup_reservations_share_one_sqlite_capacity_slot(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"
    observed_at = datetime(2026, 8, 30, 11, tzinfo=UTC)
    jobs = []
    for number in range(2):
        repository = BitbucketRepository.objects.create(
            display_name=f"concurrent-wakeup-{number}",
            canonical_remote_key=f"bitbucket.org/workspace/concurrent-wakeup-{number}",
            remote_url=f"ssh://git@bitbucket.org/workspace/concurrent-wakeup-{number}.git",
            sync_state=RepositorySyncState.QUEUED,
        )
        jobs.append(
            RepositorySyncJob.objects.create(
                repository=repository,
                operation=RepositorySyncOperation.CLONE,
            )
        )

    callers_ready = Barrier(2)
    real_lock = repository_sync.repository_worker_wakeup_lock

    @contextmanager
    def start_lock_attempts_together():
        callers_ready.wait(timeout=5)
        with real_lock():
            yield

    monkeypatch.setattr(
        repository_sync,
        "repository_worker_wakeup_lock",
        start_lock_attempts_together,
    )

    def reserve_on_independent_connection():
        close_old_connections()
        connection.ensure_connection()
        connection_identity = id(connection.connection)
        try:
            reservation = repository_sync.reserve_queued_repository_worker_wakeups(at=observed_at)
            return connection_identity, reservation
        finally:
            connection.close()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve_on_independent_connection) for _ in range(2)]
        results = tuple(future.result(timeout=10) for future in futures)

    connection_ids = {connection_id for connection_id, _reservation in results}
    reservations = tuple(reservation for _connection_id, reservation in results)
    reserved_job_ids = tuple(
        job_id for reservation in reservations for job_id in reservation.job_ids
    )

    assert len(connection_ids) == 2
    assert len(reserved_job_ids) == 1
    assert set(reserved_job_ids).issubset({job.pk for job in jobs})
    assert RepositorySyncJob.objects.filter(worker_pid__isnull=False).count() == 1
