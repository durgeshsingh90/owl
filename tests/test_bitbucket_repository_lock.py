from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bitbucket_search.services import git_sync
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    repository_checkout_lock,
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


def test_repository_synchronization_holds_checkout_lock(monkeypatch):
    repository = SimpleNamespace(pk=23)
    result = object()
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
