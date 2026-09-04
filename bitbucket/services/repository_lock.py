"""Cross-process checkout locks for repository readers and synchronizers."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

from django.conf import settings


class RepositoryCheckoutBusy(RuntimeError):
    """The checkout is currently owned by another OWL operation."""


def _lock_path(repository_id: int) -> Path:
    lock_root = Path(settings.BITBUCKET_APP_TEMP_ROOT) / "checkout-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / f"repository-{repository_id}.lock"


def _worker_wakeup_lock_path() -> Path:
    lock_root = Path(settings.BITBUCKET_APP_TEMP_ROOT) / "service-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / "repository-worker-wakeup-reservations.lock"


def _resident_supervisor_lock_path() -> Path:
    lock_root = Path(settings.BITBUCKET_APP_TEMP_ROOT) / "service-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / "resident-worker-supervisor.lock"


def _pdf_search_index_repair_lock_path() -> Path:
    lock_root = Path(settings.BITBUCKET_APP_TEMP_ROOT) / "service-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / "pdf-search-index-repair.lock"


def _windows_lock(handle, *, blocking: bool, shared: bool, release: bool = False) -> None:
    """Use real shared byte-range locks; CRT LK_RLCK is still exclusive."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    overlapped = Overlapped()
    native_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
    if release:
        operation = kernel.UnlockFileEx
        operation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        )
        arguments = (native_handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        operation = kernel.LockFileEx
        operation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        )
        flags = (0 if shared else 0x02) | (0 if blocking else 0x01)
        arguments = (native_handle, flags, 0, 1, 0, ctypes.byref(overlapped))
    operation.restype = wintypes.BOOL
    if not operation(*arguments):
        code = ctypes.get_last_error()
        if not release and not blocking and code in (32, 33):
            raise RepositoryCheckoutBusy
        raise ctypes.WinError(code)


def _acquire(handle, *, blocking: bool, shared: bool = False) -> None:
    if os.name == "nt":
        _windows_lock(handle, blocking=blocking, shared=shared)
    else:
        import fcntl

        flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise RepositoryCheckoutBusy from exc


def _release(handle) -> None:
    if os.name == "nt":
        _windows_lock(handle, blocking=False, shared=False, release=True)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _file_lock(lock_path: Path, *, blocking: bool, shared: bool = False) -> Iterator[None]:
    with lock_path.open("a+b") as handle:
        # Both flock and LockFileEx support empty lock files. Avoid writing a
        # placeholder byte: another shared reader may already own that byte.
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            # Windows permissions are governed by the containing private data
            # directory; chmod is best-effort there.
            if os.name != "nt":
                raise

        _acquire(handle, blocking=blocking, shared=shared)
        try:
            yield
        finally:
            _release(handle)


@contextmanager
def repository_checkout_lock(
    repository_id: int,
    *,
    blocking: bool,
    shared: bool = False,
) -> Iterator[None]:
    """Allow concurrent PDF readers while excluding Git and local mutations."""

    if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id <= 0:
        raise ValueError("repository_id must be a positive integer")

    with _file_lock(_lock_path(repository_id), blocking=blocking, shared=shared):
        yield


@contextmanager
def repository_worker_wakeup_lock() -> Iterator[None]:
    """Serialize queued-worker launch reservations across OWL web processes."""

    with _file_lock(_worker_wakeup_lock_path(), blocking=True):
        yield


@contextmanager
def resident_worker_supervisor_lock() -> Iterator[None]:
    """Allow only one ``run_owl`` process to own this data root's worker pool."""

    with _file_lock(_resident_supervisor_lock_path(), blocking=False):
        yield


@contextmanager
def pdf_search_index_repair_lock() -> Iterator[None]:
    """Serialize reconstruction of OWL's derived PDF FTS schema."""

    with _file_lock(_pdf_search_index_repair_lock_path(), blocking=True):
        yield


@contextmanager
def pdf_extraction_claim_lock() -> Iterator[None]:
    """Serialize short PDF claim/publication writes, not the parsing work."""

    lock_path = _worker_wakeup_lock_path().with_name("pdf-extraction-claim.lock")
    with _file_lock(lock_path, blocking=True):
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
