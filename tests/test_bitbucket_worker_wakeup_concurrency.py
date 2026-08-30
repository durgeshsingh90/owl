from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services import repository_sync


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
