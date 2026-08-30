"""Cross-process checkout locks for repository readers and synchronizers."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

from django.conf import settings


class RepositoryCheckoutBusy(RuntimeError):
    """The checkout is currently owned by another OWL operation."""


def _lock_path(repository_id: int) -> Path:
    lock_root = Path(settings.BITBUCKET_TEMP_ROOT) / "checkout-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / f"repository-{repository_id}.lock"


def _worker_wakeup_lock_path() -> Path:
    lock_root = Path(settings.BITBUCKET_TEMP_ROOT) / "service-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / "repository-worker-wakeup-reservations.lock"


def _acquire(handle, *, blocking: bool) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
                return
            except OSError as exc:
                if not blocking:
                    raise RepositoryCheckoutBusy from exc
                time.sleep(0.1)

    else:
        import fcntl

        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise RepositoryCheckoutBusy from exc


def _release(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_file_lock(lock_path: Path, *, blocking: bool) -> Iterator[None]:
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            # Windows permissions are governed by the containing private data
            # directory; chmod is best-effort there.
            if os.name != "nt":
                raise

        _acquire(handle, blocking=blocking)
        try:
            yield
        finally:
            _release(handle)


@contextmanager
def repository_checkout_lock(
    repository_id: int,
    *,
    blocking: bool,
) -> Iterator[None]:
    """Hold OWL's exclusive mutation/read gate for one managed checkout."""

    if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id <= 0:
        raise ValueError("repository_id must be a positive integer")

    with _exclusive_file_lock(_lock_path(repository_id), blocking=blocking):
        yield


@contextmanager
def repository_worker_wakeup_lock() -> Iterator[None]:
    """Serialize queued-worker launch reservations across OWL web processes."""

    with _exclusive_file_lock(_worker_wakeup_lock_path(), blocking=True):
        yield


@contextmanager
def repository_checkout_locks(
    repository_ids: Sequence[int],
    *,
    blocking: bool,
) -> Iterator[None]:
    """Acquire multiple checkout locks in stable order to avoid deadlocks."""

    ordered_ids = sorted(set(repository_ids))
    with ExitStack() as stack:
        for repository_id in ordered_ids:
            stack.enter_context(repository_checkout_lock(repository_id, blocking=blocking))
        yield
