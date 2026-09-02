"""Repository-level refresh controls and recoverable, exact-target local removal."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Q

from bitbucket_search.models import (
    BitbucketRepository,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFTextRevision,
    RepositoryRemovalRecovery,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.filesystem_paths import filesystem_path
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import cancel_repository_pdf_extractions
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    pdf_extraction_claim_lock,
    repository_checkout_lock,
)
from bitbucket_search.services.repository_sync import reserve_repository_write
from bookmark_manager.models import Notification, NotificationKind

logger = get_logger("lifecycle")
_IS_WINDOWS = os.name == "nt"
_ACTIVE_SYNC_STATES = (
    RepositorySyncState.QUEUED,
    RepositorySyncState.CLONING,
    RepositorySyncState.FETCHING,
    RepositorySyncState.UPDATING,
)
_REMOVAL_CHECKOUT_POLL_SECONDS = 0.05


class RepositoryLifecycleError(RuntimeError):
    """A safe user-facing repository action rejection or failure."""

    def __init__(self, code: str, summary: str) -> None:
        self.code = code
        self.summary = summary
        super().__init__(summary)


@dataclass(frozen=True, slots=True)
class RepositoryRemovalResult:
    repository_id: int
    display_name: str


def _validate_id(repository_id: int) -> None:
    if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id <= 0:
        raise RepositoryLifecycleError("repository_unavailable", "This repository is unavailable.")


def set_repository_refresh_excluded(repository_id: int, *, excluded: bool) -> BitbucketRepository:
    """Change future bulk/scheduled work without cancelling already queued jobs."""

    _validate_id(repository_id)
    if not isinstance(excluded, bool):
        raise RepositoryLifecycleError("invalid_refresh_policy", "Choose a valid refresh setting.")
    try:
        with transaction.atomic():
            reserve_repository_write()
            repository = BitbucketRepository.objects.select_for_update().get(pk=repository_id)
            if RepositoryRemovalRecovery.objects.filter(repository_id=repository_id).exists():
                raise RepositoryLifecycleError(
                    "repository_removal_pending",
                    "Repository removal is incomplete. Use Retry removal before changing its refresh setting.",
                )
            repository.exclude_from_refresh = excluded
            repository.save(update_fields=("exclude_from_refresh", "updated_at"))
    except BitbucketRepository.DoesNotExist as error:
        raise RepositoryLifecycleError(
            "repository_unavailable", "This repository is unavailable."
        ) from error
    except DatabaseError as error:
        log_event(
            logger,
            logging.ERROR,
            "repository_refresh_policy_failed",
            error=error,
            repository_id=repository_id,
        )
        raise RepositoryLifecycleError(
            "repository_policy_failed",
            "OWL could not save the repository refresh setting. Try again.",
        ) from error
    log_event(
        logger, logging.INFO, "repository_refresh_policy_changed", repository_id=repository_id
    )
    return repository


def _invalid_path() -> RepositoryLifecycleError:
    return RepositoryLifecycleError(
        "invalid_repository_path",
        "OWL refused to remove an unsafe repository storage path. No unrelated folders were touched.",
    )


def _is_link(path: Path) -> bool:
    native = filesystem_path(path)
    return native.is_symlink() or native.is_junction()


def _safe_root(raw: object) -> Path:
    """Require canonical configured roots, checking every existing ancestor."""

    root = Path(raw)
    if not root.is_absolute() or root != root.resolve(strict=False) or root == Path(root.anchor):
        raise _invalid_path()
    for candidate in (*reversed(root.parents), root):
        if _is_link(candidate):
            raise _invalid_path()
        native = filesystem_path(candidate)
        if native.exists() and not native.is_dir():
            raise _invalid_path()
    return root


def _roots() -> dict[str, Path]:
    media = _safe_root(settings.MEDIA_ROOT)
    return {
        "checkout": _safe_root(settings.BITBUCKET_REPOSITORIES_ROOT),
        "snapshots": _safe_root(media / "bitbucket" / "excluded"),
        "staging": _safe_root(settings.BITBUCKET_TEMP_ROOT),
    }


def _checkout_name(repository_id: int, display_name: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", display_name.casefold()).strip("-")[:70] or "repository"
    return f"{repository_id}-{name}"


def _safe_tree(path: Path) -> bool:
    """Never descend through a symlink/junction, including inside a Git tree."""

    if _is_link(path):
        raise _invalid_path()
    native = filesystem_path(path)
    if not native.exists():
        return False
    if not native.is_dir():
        raise _invalid_path()

    def fail(error: OSError) -> None:
        raise error

    for directory, names, filenames in os.walk(native, followlinks=False, onerror=fail):
        for name in (*names, *filenames):
            candidate = Path(directory) / name
            if _is_link(candidate):
                raise _invalid_path()
    return True


def _ensure_git_idle(repository: BitbucketRepository) -> None:
    if (
        repository.sync_state in _ACTIVE_SYNC_STATES
        or repository.sync_jobs.filter(
            status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
        ).exists()
    ):
        raise RepositoryLifecycleError(
            "repository_busy",
            "Wait for this repository's background refresh to finish before removing it.",
        )


def _ensure_pdf_stopped(repository: BitbucketRepository) -> None:
    if PDFExtractionJob.objects.filter(
        document__repository=repository,
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
    ).exists():
        raise RepositoryLifecycleError(
            "repository_removal_pending",
            "OWL is still stopping this repository's PDF indexing. Use Retry removal.",
        )


@contextmanager
def _bounded_repository_checkout_lock(repository_id: int) -> Iterator[None]:
    """Wait for revoked PDF readers, bounded by the Django deletion setting."""

    wait_seconds = max(
        0.0,
        float(getattr(settings, "BITBUCKET_REPOSITORY_REMOVAL_WAIT_SECONDS", 120)),
    )
    deadline = time.monotonic() + wait_seconds
    acquired = None
    while acquired is None:
        candidate = repository_checkout_lock(repository_id, blocking=False)
        try:
            candidate.__enter__()
        except RepositoryCheckoutBusy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_REMOVAL_CHECKOUT_POLL_SECONDS, remaining))
        else:
            acquired = candidate
    try:
        yield
    finally:
        acquired.__exit__(None, None, None)


def _manifest(repository: BitbucketRepository) -> list[dict[str, str]]:
    roots = _roots()
    checkout_name = _checkout_name(repository.pk, repository.display_name)
    checkout = roots["checkout"] / checkout_name
    if repository.local_path and Path(repository.local_path) != checkout:
        raise _invalid_path()
    candidates = [("checkout", checkout_name), ("snapshots", str(repository.pk))]
    staging = roots["staging"]
    if staging.exists():
        pattern = re.compile(rf"repository-{repository.pk}-[0-9a-f]{{32}}")
        candidates.extend(
            ("staging", child.name) for child in staging.iterdir() if pattern.fullmatch(child.name)
        )
    token = uuid.uuid4().hex
    return [
        {"kind": kind, "name": name, "token": token}
        for kind, name in candidates
        if _safe_tree(roots[kind] / name)
    ]


def _manifest_paths(recovery: RepositoryRemovalRecovery) -> tuple[tuple[Path, Path], ...]:
    """Resolve only tightly whitelisted names; never accept stored absolute paths."""

    roots = _roots()
    entries = recovery.quarantine_manifest
    if not isinstance(entries, list):
        raise _invalid_path()
    paths = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"kind", "name", "token"}:
            raise _invalid_path()
        kind, name, token = entry["kind"], entry["name"], entry["token"]
        if not all(isinstance(value, str) for value in (kind, name, token)):
            raise _invalid_path()
        if kind not in roots or re.fullmatch(r"[0-9a-f]{32}", token) is None:
            raise _invalid_path()
        expected = {
            "checkout": name == _checkout_name(recovery.repository_id, recovery.display_name),
            "snapshots": name == str(recovery.repository_id),
            "staging": re.fullmatch(rf"repository-{recovery.repository_id}-[0-9a-f]{{32}}", name)
            is not None,
        }
        if not expected[kind] or (kind, name) in seen:
            raise _invalid_path()
        seen.add((kind, name))
        original = roots[kind] / name
        quarantine = roots[kind] / f".owl-removal-{recovery.repository_id}-{token}-{name}"
        if _is_link(original) or _is_link(quarantine):
            raise _invalid_path()
        paths.append((original, quarantine))
    return tuple(paths)


def _restore(recovery: RepositoryRemovalRecovery) -> None:
    for original, quarantine in reversed(_manifest_paths(recovery)):
        if _safe_tree(quarantine):
            if filesystem_path(original).exists():
                raise _invalid_path()
            os.replace(filesystem_path(quarantine), filesystem_path(original))


def _remove_tree(path: Path) -> None:
    if not _safe_tree(path):
        return

    native_root = filesystem_path(path)

    def remove_readonly(function, filename, error):
        candidate = Path(filename)
        if (
            not _IS_WINDOWS
            or not isinstance(error, PermissionError)
            or function not in {os.unlink, os.remove, os.rmdir}
            or _is_link(candidate)
            or not candidate.resolve(strict=False).is_relative_to(native_root)
        ):
            raise error
        metadata = candidate.stat(follow_symlinks=False)
        if metadata.st_mode & stat.S_IWRITE:
            raise error
        os.chmod(candidate, stat.S_IMODE(metadata.st_mode) | stat.S_IWRITE)
        function(filename)

    shutil.rmtree(native_root, onexc=remove_readonly)


def _purge(recovery: RepositoryRemovalRecovery) -> None:
    try:
        if BitbucketRepository.objects.filter(pk=recovery.repository_id).exists():
            raise _invalid_path()
        for _original, quarantine in _manifest_paths(recovery):
            _remove_tree(quarantine)
        recovery.delete()
    except Exception as error:
        raise RepositoryLifecycleError(
            "repository_cleanup_incomplete",
            "Repository database records were removed, but local cleanup is incomplete. "
            "Use Retry removal to finish deleting its retained local data.",
        ) from error


def _delete_database_records(repository: BitbucketRepository) -> None:
    repository_id = repository.pk
    repository.delete()
    # Historical revisions have no repository owner. Collect only unreferenced
    # cache entries (including obsolete versions); shared searchable text stays.
    for revision in PDFTextRevision.objects.select_for_update().filter(documents__isnull=True):
        if not revision.documents.exists():
            revision.delete()
    Notification.objects.filter(kind=NotificationKind.BITBUCKET_REFRESH).filter(
        Q(event_key=f"bitbucket-connection:{repository_id}")
        | Q(event_key__startswith=f"bitbucket-refresh:{repository_id}:")
    ).delete()


def _prepare_removal_intent(repository_id: int) -> RepositoryRemovalRecovery:
    """Publish a durable queue barrier before revoking this repository's PDF leases."""

    completed_recovery = RepositoryRemovalRecovery.objects.filter(
        repository_id=repository_id,
        database_deleted=True,
    ).first()
    if completed_recovery is not None:
        return completed_recovery

    with pdf_extraction_claim_lock(), transaction.atomic(durable=True):
        reserve_repository_write()
        try:
            repository = BitbucketRepository.objects.select_for_update().get(pk=repository_id)
        except BitbucketRepository.DoesNotExist as error:
            raise RepositoryLifecycleError(
                "repository_unavailable",
                "This repository has already been removed or is unavailable.",
            ) from error
        # Git has no repository-scoped stop contract yet. Reject it before
        # publishing removal intent. PDF leases are revoked after this commit.
        _ensure_git_idle(repository)
        recovery = (
            RepositoryRemovalRecovery.objects.select_for_update()
            .filter(repository_id=repository_id)
            .first()
        )
        if recovery is None:
            recovery = RepositoryRemovalRecovery.objects.create(
                repository_id=repository_id,
                display_name=repository.display_name,
                quarantine_manifest=[],
            )
    return recovery


def _remove_locked(repository_id: int) -> RepositoryRemovalResult:
    recovery = RepositoryRemovalRecovery.objects.filter(repository_id=repository_id).first()
    if recovery is not None and recovery.database_deleted:
        result = RepositoryRemovalResult(repository_id, recovery.display_name)
        _purge(recovery)
        return result
    if recovery is None:
        raise RepositoryLifecycleError(
            "repository_removal_pending",
            "OWL could not find this repository's removal journal. Retry removal.",
        )

    # A prior attempt may have moved only part of the manifest. Restore it while
    # keeping the same journal continuously visible to every Git/PDF queue.
    if recovery.quarantine_manifest:
        _restore(recovery)

    try:
        initial = BitbucketRepository.objects.get(pk=repository_id)
    except BitbucketRepository.DoesNotExist as error:
        raise RepositoryLifecycleError(
            "repository_unavailable", "This repository has already been removed or is unavailable."
        ) from error
    _ensure_git_idle(initial)
    _ensure_pdf_stopped(initial)
    # Walking a huge checkout must not hold SQLite's writer or the global PDF
    # publication gate. The exclusive per-repository lock keeps its files still.
    manifest = _manifest(initial)

    # Update the already-committed journal before any filesystem move. It must
    # remain visible continuously from cancellation through the final purge.
    with pdf_extraction_claim_lock(), transaction.atomic(durable=True):
        reserve_repository_write()
        try:
            repository = BitbucketRepository.objects.select_for_update().get(pk=repository_id)
        except BitbucketRepository.DoesNotExist as error:
            raise RepositoryLifecycleError(
                "repository_unavailable",
                "This repository has already been removed or is unavailable.",
            ) from error
        _ensure_git_idle(repository)
        _ensure_pdf_stopped(repository)
        if (repository.display_name, repository.local_path) != (
            initial.display_name,
            initial.local_path,
        ):
            raise _invalid_path()
        recovery = RepositoryRemovalRecovery.objects.select_for_update().get(
            repository_id=repository_id
        )
        recovery.display_name = repository.display_name
        recovery.quarantine_manifest = manifest
        recovery.save(update_fields=("display_name", "quarantine_manifest"))
    result = RepositoryRemovalResult(repository_id, repository.display_name)
    try:
        with pdf_extraction_claim_lock(), transaction.atomic(durable=True):
            reserve_repository_write()
            repository = BitbucketRepository.objects.select_for_update().get(pk=repository_id)
            _ensure_git_idle(repository)
            _ensure_pdf_stopped(repository)
            for original, quarantine in _manifest_paths(recovery):
                if not filesystem_path(original).is_dir() or filesystem_path(quarantine).exists():
                    raise _invalid_path()
                os.replace(filesystem_path(original), filesystem_path(quarantine))
            _delete_database_records(repository)
            recovery.database_deleted = True
            recovery.save(update_fields=("database_deleted",))
    except Exception:
        # The rollback leaves database_deleted=False. Restore what moved, but
        # retain the journal as a queue barrier for an exact retry.
        _restore(recovery)
        raise
    _purge(recovery)
    return result


def remove_repository(repository_id: int, *, confirmed: bool = False) -> RepositoryRemovalResult:
    """Remove only this repository's local files/data; never contact remote Git."""

    _validate_id(repository_id)
    if confirmed is not True:
        raise RepositoryLifecycleError(
            "repository_delete_confirmation_required",
            "Confirm removal of this repository, its downloaded files, and its indexed database data.",
        )
    try:
        # Validate roots before the lock helper creates its private lock file.
        roots = _roots()
        _safe_root(roots["staging"] / "checkout-locks")
        _safe_root(roots["staging"] / "service-locks")
        lockfile = roots["staging"] / "checkout-locks" / f"repository-{repository_id}.lock"
        claimfile = roots["staging"] / "service-locks" / "pdf-extraction-claim.lock"
        if _is_link(lockfile) or _is_link(claimfile):
            raise _invalid_path()
        recovery = _prepare_removal_intent(repository_id)
        if not recovery.database_deleted:
            cancel_repository_pdf_extractions(repository_id)
        with _bounded_repository_checkout_lock(repository_id):
            result = _remove_locked(repository_id)
    except RepositoryCheckoutBusy as error:
        raise RepositoryLifecycleError(
            "repository_removal_pending",
            "PDF indexing was stopped, but OWL is still waiting for the repository folder "
            "to be released. Use Retry removal.",
        ) from error
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "repository_removal_failed",
            error=error,
            repository_id=repository_id,
        )
        if isinstance(error, RepositoryLifecycleError):
            raise
        raise RepositoryLifecycleError(
            "repository_removal_failed",
            "OWL could not finish removing this repository. Its recovery data was retained; try again.",
        ) from error
    log_event(logger, logging.INFO, "repository_removed", repository_id=repository_id)
    return result
