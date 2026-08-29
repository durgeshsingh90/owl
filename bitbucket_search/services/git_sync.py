"""Safe Git clone/refresh operations for OWL-managed repositories."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, RepositorySyncPhase
from bitbucket_search.services.repository_lock import repository_checkout_lock

ProgressCallback = Callable[[str, int, str], None]
HeartbeatCallback = Callable[[], None]
_GIT_PERCENT = re.compile(r"(?<!\d)(\d{1,3})%")
_DOCUMENT_PATTERNS = ("*.[pP][dD][fF]", "*.[vV][sS][dD][xX]")
_GIT_LFS_DOCUMENT_INCLUDE = ",".join(_DOCUMENT_PATTERNS)
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_GIT_LFS_POINTER_VERSION = b"version https://git-lfs.github.com/spec/v1"
_GIT_LFS_POINTER_OID = re.compile(rb"^oid sha256:[0-9a-f]{64}$", re.MULTILINE)
_GIT_LFS_POINTER_SIZE = re.compile(rb"^size [0-9]+$", re.MULTILINE)
_GIT_LFS_POINTER_MAX_BYTES = 8_192


class RepositorySyncError(RuntimeError):
    """A safe, user-actionable repository synchronization failure."""

    def __init__(self, code: str, summary: str, *, blocked_dirty: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = " ".join(summary.split())[:500]
        self.blocked_dirty = blocked_dirty


@dataclass(frozen=True, slots=True)
class DocumentStats:
    pdf_count: int
    vsdx_count: int
    document_bytes: int


@dataclass(frozen=True, slots=True)
class RepositorySyncResult:
    branch: str
    source_commit: str
    result_commit: str
    documents: DocumentStats


def _progress_heartbeat(
    progress_callback: ProgressCallback,
    *,
    phase: str,
    progress: int,
    status_message: str,
) -> HeartbeatCallback:
    return lambda: progress_callback(phase, progress, status_message)


def managed_repository_path(repository: BitbucketRepository) -> Path:
    """Derive the only allowed checkout path from OWL-owned values."""

    root = Path(settings.BITBUCKET_REPOSITORIES_ROOT).resolve()
    safe_name = re.sub(r"[^a-z0-9]+", "-", repository.display_name.casefold()).strip("-")
    safe_name = safe_name[:70] or "repository"
    candidate = (root / f"{repository.pk}-{safe_name}").resolve(strict=False)
    if candidate.parent != root:
        raise RepositorySyncError(
            "invalid_local_path",
            "OWL could not derive a safe local repository folder.",
        )
    return candidate


def _validated_existing_path(repository: BitbucketRepository) -> Path:
    expected = managed_repository_path(repository)
    configured = (
        Path(repository.local_path).resolve(strict=False) if repository.local_path else None
    )
    if configured != expected or not expected.is_dir() or not (expected / ".git").is_dir():
        raise RepositorySyncError(
            "invalid_local_checkout",
            "The managed repository checkout is missing or is not valid. Retry after repairing it.",
        )
    return expected


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_capture(
    arguments: Sequence[str],
    *,
    failure_code: str,
    failure_summary: str,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            close_fds=True,
        )
    except OSError as exc:
        raise RepositorySyncError(failure_code, failure_summary) from exc

    deadline = time.monotonic() + settings.BITBUCKET_GIT_TIMEOUT_SECONDS
    stdout = ""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    list(arguments),
                    settings.BITBUCKET_GIT_TIMEOUT_SECONDS,
                )
            try:
                stdout, _stderr = process.communicate(
                    timeout=min(_HEARTBEAT_INTERVAL_SECONDS, remaining)
                )
            except subprocess.TimeoutExpired:
                if heartbeat_callback is not None:
                    heartbeat_callback()
                continue
            break
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise RepositorySyncError(failure_code, failure_summary) from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise

    if process.returncode != 0:
        raise RepositorySyncError(failure_code, failure_summary)
    return stdout.strip()


def _run_streaming(
    arguments: Sequence[str],
    *,
    phase: str,
    progress_start: int,
    progress_end: int,
    status_message: str,
    progress_callback: ProgressCallback,
    failure_code: str,
    failure_summary: str,
) -> None:
    """Run Git without a shell while emitting progress and silent-period heartbeats."""

    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            close_fds=True,
        )
    except OSError as exc:
        raise RepositorySyncError(failure_code, failure_summary) from exc

    chunks: queue.Queue[str | None] = queue.Queue()

    def read_stderr() -> None:
        assert process.stderr is not None
        try:
            while chunk := process.stderr.read(256):
                chunks.put(chunk)
        finally:
            chunks.put(None)

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    deadline = time.monotonic() + settings.BITBUCKET_GIT_TIMEOUT_SECONDS
    latest_progress = progress_start
    buffer = ""
    stream_finished = False
    progress_callback(phase, latest_progress, status_message)

    try:
        while process.poll() is None or not stream_finished:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise RepositorySyncError(failure_code, failure_summary)
            try:
                chunk = chunks.get(timeout=1)
            except queue.Empty:
                progress_callback(phase, latest_progress, status_message)
                continue
            if chunk is None:
                stream_finished = True
                continue
            buffer = (buffer + chunk)[-4096:]
            percentages = [min(int(match.group(1)), 100) for match in _GIT_PERCENT.finditer(buffer)]
            if percentages:
                observed = max(percentages)
                mapped = progress_start + round((progress_end - progress_start) * observed / 100)
                latest_progress = max(latest_progress, min(mapped, progress_end))
            progress_callback(phase, latest_progress, status_message)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=2)

    if process.returncode != 0:
        raise RepositorySyncError(failure_code, failure_summary)
    progress_callback(phase, progress_end, status_message)


def _git_value(
    repository_path: Path,
    *arguments: str,
    code: str,
    summary: str,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    return _run_capture(
        ("git", "-C", str(repository_path), *arguments),
        failure_code=code,
        failure_summary=summary,
        heartbeat_callback=heartbeat_callback,
    )


def _verify_origin(
    repository: BitbucketRepository,
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> None:
    origin = _git_value(
        repository_path,
        "remote",
        "get-url",
        "origin",
        code="invalid_origin",
        summary="OWL could not verify the repository's origin remote.",
        heartbeat_callback=heartbeat_callback,
    )
    if origin != repository.remote_url:
        raise RepositorySyncError(
            "origin_mismatch",
            "The local checkout points to a different remote. OWL left it unchanged.",
        )


def _branch(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    branch = _git_value(
        repository_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        code="invalid_branch",
        summary="The repository is not on a named branch. OWL left it unchanged.",
        heartbeat_callback=heartbeat_callback,
    )
    _run_capture(
        ("git", "check-ref-format", "--branch", branch),
        failure_code="invalid_branch",
        failure_summary="The repository branch name is not safe to synchronize.",
        heartbeat_callback=heartbeat_callback,
    )
    return branch


def _commit(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    return _git_value(
        repository_path,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        code="invalid_commit",
        summary="OWL could not verify the repository's current commit.",
        heartbeat_callback=heartbeat_callback,
    )


def _is_git_lfs_pointer(candidate: Path, *, size: int) -> bool:
    """Recognize a small canonical Git LFS v1 pointer without reading document data."""

    if size <= 0 or size > _GIT_LFS_POINTER_MAX_BYTES:
        return False
    try:
        content = candidate.read_bytes()
    except OSError:
        return False
    normalized = content.replace(b"\r\n", b"\n")
    return (
        normalized.startswith(_GIT_LFS_POINTER_VERSION + b"\n")
        and _GIT_LFS_POINTER_OID.search(normalized) is not None
        and _GIT_LFS_POINTER_SIZE.search(normalized) is not None
    )


def _scan_documents(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> tuple[DocumentStats, int]:
    """Count materialized documents and unresolved LFS pointers without following symlinks."""

    repository_path = repository_path.resolve()
    pdf_count = 0
    vsdx_count = 0
    document_bytes = 0
    lfs_pointer_count = 0
    last_heartbeat_at = time.monotonic()

    def heartbeat_if_due() -> None:
        nonlocal last_heartbeat_at
        if heartbeat_callback is None:
            return
        observed_at = time.monotonic()
        if observed_at - last_heartbeat_at >= _HEARTBEAT_INTERVAL_SECONDS:
            heartbeat_callback()
            last_heartbeat_at = observed_at

    for directory, directory_names, filenames in os.walk(repository_path, followlinks=False):
        heartbeat_if_due()
        current = Path(directory)
        directory_names[:] = [
            name for name in directory_names if name != ".git" and not (current / name).is_symlink()
        ]
        for filename in filenames:
            heartbeat_if_due()
            candidate = current / filename
            if candidate.is_symlink():
                continue
            suffix = candidate.suffix.casefold()
            if suffix not in {".pdf", ".vsdx"}:
                continue
            try:
                stat = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_git_lfs_pointer(candidate, size=stat.st_size):
                lfs_pointer_count += 1
                continue
            if suffix == ".pdf":
                pdf_count += 1
            else:
                vsdx_count += 1
            document_bytes += stat.st_size
    return DocumentStats(pdf_count, vsdx_count, document_bytes), lfs_pointer_count


def _lfs_pointer_noun(pointer_count: int) -> str:
    return "file" if pointer_count == 1 else "files"


def _hydrate_git_lfs_documents(
    repository_path: Path,
    *,
    pointer_count: int,
    progress_callback: ProgressCallback,
) -> None:
    """Fetch and check out only PDF/VSDX LFS objects for the current branch."""

    noun = _lfs_pointer_noun(pointer_count)
    if shutil.which("git-lfs") is None:
        raise RepositorySyncError(
            "git_lfs_unavailable",
            (
                f"{pointer_count} PDF/VSDX {noun} remains Git LFS pointer content. "
                "Install Git LFS and authenticate it, then select Refresh; OWL will retry "
                "the document-only LFS download."
            ),
        )
    _run_streaming(
        (
            "git",
            "-C",
            str(repository_path),
            "lfs",
            "pull",
            f"--include={_GIT_LFS_DOCUMENT_INCLUDE}",
            "--exclude=",
        ),
        phase=RepositorySyncPhase.DISCOVERING,
        progress_start=94,
        progress_end=97,
        status_message=f"Downloading {pointer_count} PDF/VSDX Git LFS {noun}…",
        progress_callback=progress_callback,
        failure_code="git_lfs_download_failed",
        failure_summary=(
            "Git LFS could not download the PDF/VSDX objects. Check LFS authentication "
            "and object availability, then select Refresh to retry."
        ),
    )


def discover_documents(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> DocumentStats:
    """Materialize remaining document LFS objects, then return safe document totals."""

    documents, pointer_count = _scan_documents(
        repository_path,
        heartbeat_callback=heartbeat_callback,
    )
    if not pointer_count:
        return documents

    if progress_callback is None:

        def progress_callback(_phase: str, _progress: int, _message: str) -> None:
            if heartbeat_callback is not None:
                heartbeat_callback()

    _hydrate_git_lfs_documents(
        repository_path,
        pointer_count=pointer_count,
        progress_callback=progress_callback,
    )
    progress_callback(
        RepositorySyncPhase.DISCOVERING,
        98,
        "Verifying downloaded PDF and VSDX Git LFS files…",
    )
    documents, remaining_pointer_count = _scan_documents(
        repository_path,
        heartbeat_callback=heartbeat_callback,
    )
    if remaining_pointer_count:
        noun = _lfs_pointer_noun(remaining_pointer_count)
        verb = "is" if remaining_pointer_count == 1 else "are"
        raise RepositorySyncError(
            "git_lfs_objects_unavailable",
            (
                f"{remaining_pointer_count} PDF/VSDX {noun} {verb} still Git LFS pointer "
                "content after retrieval. Verify the files are tracked by Git LFS and their "
                "objects exist, then select Refresh to retry."
            ),
        )
    return documents


def _ahead_behind(
    repository_path: Path,
    remote_branch: str,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> tuple[int, int]:
    """Return commits unique to local HEAD and the fetched remote branch."""

    comparison = _git_value(
        repository_path,
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{remote_branch}",
        "--",
        code="history_comparison_failed",
        summary="OWL could not compare local and remote repository history safely.",
        heartbeat_callback=heartbeat_callback,
    )
    try:
        local_only, remote_only = comparison.split()
        return int(local_only), int(remote_only)
    except (TypeError, ValueError) as exc:
        raise RepositorySyncError(
            "history_comparison_failed",
            "OWL could not compare local and remote repository history safely.",
        ) from exc


def _remove_staging_directory(staging_path: Path) -> None:
    temp_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
    resolved = staging_path.resolve(strict=False)
    if resolved.parent == temp_root and resolved.name.startswith("repository-"):
        shutil.rmtree(resolved, ignore_errors=True)


def _clone(
    repository: BitbucketRepository,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    target = managed_repository_path(repository)
    if target.exists():
        raise RepositorySyncError(
            "destination_exists",
            "The managed destination already exists but is not a valid completed checkout.",
        )
    temp_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
    temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = temp_root / f"repository-{repository.pk}-{uuid.uuid4().hex}"
    shallow_since = timezone.now().date() - timedelta(days=365 * settings.BITBUCKET_HISTORY_YEARS)
    try:
        preferred_clone_arguments = (
            "--no-checkout",
            "--single-branch",
            f"--shallow-since={shallow_since.isoformat()}",
            "--progress",
            "--",
            repository.remote_url,
            str(staging),
        )
        fallback_clone_arguments = (
            "--no-checkout",
            "--single-branch",
            "--depth=1",
            "--progress",
            "--",
            repository.remote_url,
            str(staging),
        )
        try:
            _run_streaming(
                ("git", "clone", "--filter=blob:none", *preferred_clone_arguments),
                phase=RepositorySyncPhase.CLONING,
                progress_start=5,
                progress_end=72,
                status_message="Downloading repository history in the background…",
                progress_callback=progress_callback,
                failure_code="clone_failed",
                failure_summary=(
                    "Git could not clone this repository. Check repository access, your SSH "
                    "agent or credential manager, and the configured host."
                ),
            )
        except RepositorySyncError as filtered_error:
            if filtered_error.code != "clone_failed":
                raise
            _remove_staging_directory(staging)
            progress_callback(
                RepositorySyncPhase.CLONING,
                8,
                "The server did not complete an optimized clone; retrying conservatively…",
            )
            _run_streaming(
                ("git", "clone", *fallback_clone_arguments),
                phase=RepositorySyncPhase.CLONING,
                progress_start=8,
                progress_end=72,
                status_message="Downloading a compatible checkout in the background…",
                progress_callback=progress_callback,
                failure_code="clone_failed",
                failure_summary=filtered_error.summary,
            )
        clone_validation_heartbeat = _progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.CLONING,
            progress=72,
            status_message="Validating the downloaded repository…",
        )
        _verify_origin(
            repository,
            staging,
            heartbeat_callback=clone_validation_heartbeat,
        )
        branch = _branch(staging, heartbeat_callback=clone_validation_heartbeat)
        progress_callback(
            RepositorySyncPhase.UPDATING,
            76,
            "Preparing the PDF and VSDX working tree…",
        )
        sparse_checkout_heartbeat = _progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.UPDATING,
            progress=76,
            status_message="Preparing the PDF and VSDX working tree…",
        )
        _run_capture(
            ("git", "-C", str(staging), "sparse-checkout", "init", "--no-cone"),
            failure_code="sparse_checkout_failed",
            failure_summary="Git could not prepare the document-only working tree.",
            heartbeat_callback=sparse_checkout_heartbeat,
        )
        _run_capture(
            (
                "git",
                "-C",
                str(staging),
                "sparse-checkout",
                "set",
                "--no-cone",
                "--",
                *_DOCUMENT_PATTERNS,
            ),
            failure_code="sparse_checkout_failed",
            failure_summary="Git could not select PDF and VSDX files for the working tree.",
            heartbeat_callback=sparse_checkout_heartbeat,
        )
        _run_streaming(
            ("git", "-C", str(staging), "checkout", "-f", "HEAD"),
            phase=RepositorySyncPhase.UPDATING,
            progress_start=78,
            progress_end=90,
            status_message="Downloading PDF and VSDX files…",
            progress_callback=progress_callback,
            failure_code="checkout_failed",
            failure_summary="Git could not materialize the repository's PDF and VSDX files.",
        )
        source_commit = ""
        result_commit = _commit(
            staging,
            heartbeat_callback=_progress_heartbeat(
                progress_callback,
                phase=RepositorySyncPhase.UPDATING,
                progress=90,
                status_message="Validating the PDF and VSDX working tree…",
            ),
        )
        progress_callback(
            RepositorySyncPhase.DISCOVERING,
            93,
            "Counting downloaded PDF and VSDX files…",
        )
        documents = discover_documents(
            staging,
            heartbeat_callback=_progress_heartbeat(
                progress_callback,
                phase=RepositorySyncPhase.DISCOVERING,
                progress=93,
                status_message="Counting downloaded PDF and VSDX files…",
            ),
            progress_callback=progress_callback,
        )
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(staging, target)
        return RepositorySyncResult(
            branch=branch,
            source_commit=source_commit,
            result_commit=result_commit,
            documents=documents,
        )
    except Exception:
        _remove_staging_directory(staging)
        raise


def _refresh(
    repository: BitbucketRepository,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    target = _validated_existing_path(repository)
    validation_heartbeat = _progress_heartbeat(
        progress_callback,
        phase=RepositorySyncPhase.VALIDATING,
        progress=3,
        status_message="Validating the managed repository checkout…",
    )
    _verify_origin(repository, target, heartbeat_callback=validation_heartbeat)
    branch = _branch(target, heartbeat_callback=validation_heartbeat)
    if repository.default_branch and branch != repository.default_branch:
        raise RepositorySyncError(
            "branch_mismatch",
            "The checkout branch changed. OWL left the repository unchanged.",
        )
    dirty = _git_value(
        target,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        code="status_failed",
        summary="OWL could not verify that the repository working tree is clean.",
        heartbeat_callback=validation_heartbeat,
    )
    if dirty:
        raise RepositorySyncError(
            "dirty_working_tree",
            "Local changes were found. OWL did not overwrite them; repair the checkout and retry.",
            blocked_dirty=True,
        )
    source_commit = _commit(target, heartbeat_callback=validation_heartbeat)
    _run_streaming(
        ("git", "-C", str(target), "fetch", "--prune", "--progress", "origin"),
        phase=RepositorySyncPhase.FETCHING,
        progress_start=8,
        progress_end=70,
        status_message="Refreshing repository data in the background…",
        progress_callback=progress_callback,
        failure_code="fetch_failed",
        failure_summary=(
            "Git could not refresh this repository. Check repository access and try again."
        ),
    )
    fetched_validation_heartbeat = _progress_heartbeat(
        progress_callback,
        phase=RepositorySyncPhase.FETCHING,
        progress=70,
        status_message="Validating the refreshed repository data…",
    )
    dirty = _git_value(
        target,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        code="status_failed",
        summary="OWL could not recheck the repository working tree.",
        heartbeat_callback=fetched_validation_heartbeat,
    )
    if dirty:
        raise RepositorySyncError(
            "dirty_working_tree",
            "Local changes appeared during refresh. OWL left them unchanged.",
            blocked_dirty=True,
        )
    remote_branch = f"refs/remotes/origin/{branch}"
    local_only, remote_only = _ahead_behind(
        target,
        remote_branch,
        heartbeat_callback=fetched_validation_heartbeat,
    )
    if local_only:
        if remote_only:
            code = "history_diverged"
            summary = (
                "Local and remote repository history diverged. OWL left the checkout unchanged."
            )
        else:
            code = "local_commits_detected"
            summary = (
                "Local commits not present on the remote were found. "
                "OWL left the checkout unchanged."
            )
        raise RepositorySyncError(code, summary, blocked_dirty=True)
    _run_streaming(
        (
            "git",
            "-C",
            str(target),
            "merge",
            "--ff-only",
            "--no-stat",
            "--progress",
            "--",
            remote_branch,
        ),
        phase=RepositorySyncPhase.UPDATING,
        progress_start=72,
        progress_end=90,
        status_message="Updating the PDF and VSDX working tree…",
        progress_callback=progress_callback,
        failure_code="fast_forward_failed",
        failure_summary=(
            "The repository could not be fast-forwarded safely. OWL left the checkout unchanged."
        ),
    )
    result_commit = _commit(
        target,
        heartbeat_callback=_progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.UPDATING,
            progress=90,
            status_message="Validating the updated working tree…",
        ),
    )
    progress_callback(
        RepositorySyncPhase.DISCOVERING,
        93,
        "Counting downloaded PDF and VSDX files…",
    )
    documents = discover_documents(
        target,
        heartbeat_callback=_progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.DISCOVERING,
            progress=93,
            status_message="Counting downloaded PDF and VSDX files…",
        ),
        progress_callback=progress_callback,
    )
    return RepositorySyncResult(
        branch=branch,
        source_commit=source_commit,
        result_commit=result_commit,
        documents=documents,
    )


def synchronize_repository(
    repository: BitbucketRepository,
    *,
    operation: str,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    """Clone once or safely fast-forward an existing OWL-managed checkout."""

    # Native document actions take this same cross-process lock without
    # waiting. Keeping it for the complete Git operation prevents a checkout
    # path from being replaced after OWL validates it but before the OS opens
    # it. Searches remain available from the last published database index.
    with repository_checkout_lock(repository.pk, blocking=True):
        progress_callback(
            RepositorySyncPhase.VALIDATING,
            2,
            "Validating the repository and private media destination…",
        )
        if operation == "clone":
            return _clone(repository, progress_callback)
        if operation == "refresh":
            return _refresh(repository, progress_callback)
        raise RepositorySyncError("invalid_operation", "OWL received an invalid sync operation.")
