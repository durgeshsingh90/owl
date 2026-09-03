from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bitbucket_search.services import git_sync
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    pdf_search_index_repair_lock,
    repository_checkout_lock,
    repository_worker_wakeup_lock,
    resident_worker_supervisor_lock,
)


def test_checkout_lock_rejects_a_second_nonblocking_process_gate(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"

    with (
        repository_checkout_lock(17, blocking=True),
        pytest.raises(RepositoryCheckoutBusy),
        repository_checkout_lock(17, blocking=False),
    ):
        pass

    with repository_checkout_lock(17, blocking=False):
        pass


def test_worker_wakeup_lock_serializes_callers(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"
    contender_started = threading.Event()
    contender_acquired = threading.Event()

    def contend_for_lock():
        contender_started.set()
        with repository_worker_wakeup_lock():
            contender_acquired.set()

    contender = threading.Thread(target=contend_for_lock, daemon=True)
    with repository_worker_wakeup_lock():
        contender.start()
        assert contender_started.wait(timeout=1)
        assert not contender_acquired.wait(timeout=0.1)

    contender.join(timeout=1)
    assert not contender.is_alive()
    assert contender_acquired.is_set()


def test_only_one_resident_supervisor_can_own_a_data_root(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"

    with (
        resident_worker_supervisor_lock(),
        pytest.raises(RepositoryCheckoutBusy),
        resident_worker_supervisor_lock(),
    ):
        pass

    with resident_worker_supervisor_lock():
        pass


def test_pdf_search_index_repair_lock_serializes_callers(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "bitbucket" / "tmp"
    contender_started = threading.Event()
    contender_acquired = threading.Event()

    def contend_for_lock():
        contender_started.set()
        with pdf_search_index_repair_lock():
            contender_acquired.set()

    contender = threading.Thread(target=contend_for_lock, daemon=True)
    with pdf_search_index_repair_lock():
        contender.start()
        assert contender_started.wait(timeout=1)
        assert not contender_acquired.wait(timeout=0.1)

    contender.join(timeout=1)
    assert not contender.is_alive()
    assert contender_acquired.is_set()


def test_repository_synchronization_holds_checkout_lock(monkeypatch):
    repository = SimpleNamespace(pk=23)
    result = git_sync.RepositorySyncResult(
        branch="main",
        source_commit="a" * 40,
        result_commit="b" * 40,
        documents=git_sync.DocumentStats(pdf_count=2, vsdx_count=1, document_bytes=1234),
    )
    events: list[object] = []

    @contextmanager
    def lock(repository_id: int, *, blocking: bool):
        events.append(("lock", repository_id, blocking))
        yield
        events.append("unlock")

    def refresh(received_repository, progress_callback):
        events.append(("refresh", received_repository.pk))
        return result

    monkeypatch.setattr(git_sync, "repository_checkout_lock", lock)
    monkeypatch.setattr(git_sync, "_refresh", refresh)

    actual = git_sync.synchronize_repository(
        repository,
        operation="refresh",
        progress_callback=lambda *progress: events.append(("progress", *progress)),
    )

    assert actual is result
    assert events[0] == ("lock", 23, True)
    assert events[-2:] == [("refresh", 23), "unlock"]
