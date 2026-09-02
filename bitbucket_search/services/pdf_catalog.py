"""Build and atomically publish the durable PDF inventory for one repository."""

from __future__ import annotations

import errno
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, BinaryIO

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentLifecycle,
    PDFDocumentTimelineBasis,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncPhase,
)
from bitbucket_search.services.filesystem_paths import filesystem_path
from bitbucket_search.services.git_sync import (
    ProgressCallback,
    RepositorySyncError,
    _git_command_operation,
    _git_environment,
    managed_repository_path,
)
from bitbucket_search.services.logging_events import get_logger, log_event, logging_context
from bitbucket_search.services.path_safety import has_disallowed_path_characters

if TYPE_CHECKING:
    from bitbucket_search.services.git_activity import GitActivityBuild

HeartbeatCallback = Callable[[], None]
_PDF_PATHSPEC = "*.[pP][dD][fF]"
_HISTORY_MARKER = b"\0OWL-PDF-HISTORY-COMMIT\0"
_COMMIT_HASH = re.compile(r"^[0-9a-f]{40,64}$")
_HEARTBEAT_SECONDS = 1.0
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_TREE_RECORD_BYTES = 1024 * 1024
_MAX_HISTORY_COMMIT_BYTES = 64 * 1024 * 1024
logger = get_logger("catalog")


@dataclass(frozen=True, slots=True)
class CommitEvidence:
    commit_hash: str
    author_name: str
    committer_name: str
    authored_at: datetime
    committed_at: datetime
    is_shallow_boundary: bool = False


@dataclass(frozen=True, slots=True)
class CatalogPDF:
    filename: str
    relative_path: str
    file_size: int
    git_blob_id: str
    added_evidence: str
    added_commit: CommitEvidence | None
    last_commit: CommitEvidence | None


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    documents: tuple[CatalogPDF, ...]
    history_is_shallow: bool
    policy_signature: tuple[tuple[str, str, int | None], ...] = ()
    activity: GitActivityBuild | None = None


@dataclass(frozen=True, slots=True)
class _TreePDF:
    filename: str
    relative_path: str
    file_size: int
    git_blob_id: str


def _run_binary_capture(
    arguments: Sequence[str],
    *,
    heartbeat_callback: HeartbeatCallback,
    failure_code: str,
    failure_summary: str,
) -> bytes:
    operation = _git_command_operation(arguments)
    log_event(logger, logging.DEBUG, "catalog_git_command_started", operation=operation)
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            close_fds=True,
        )
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "catalog_git_spawn_failed",
            error=exc,
            error_code=failure_code,
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary) from exc

    deadline = time.monotonic() + settings.BITBUCKET_GIT_TIMEOUT_SECONDS
    output = b""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    list(arguments),
                    settings.BITBUCKET_GIT_TIMEOUT_SECONDS,
                )
            try:
                output, _error = process.communicate(timeout=min(_HEARTBEAT_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                heartbeat_callback()
                continue
            break
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        process.communicate()
        log_event(
            logger,
            logging.ERROR,
            "catalog_git_timeout",
            error=exc,
            error_code=failure_code,
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary) from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise

    if process.returncode != 0:
        log_event(
            logger,
            logging.ERROR,
            "catalog_git_command_failed",
            error_code=failure_code,
            return_code=process.returncode,
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary)
    log_event(logger, logging.DEBUG, "catalog_git_command_completed", operation=operation)
    return output


@contextmanager
def _run_binary_spooled(
    arguments: Sequence[str],
    *,
    heartbeat_callback: HeartbeatCallback,
    failure_code: str,
    failure_summary: str,
) -> Iterator[BinaryIO]:
    """Spool potentially large Git output to disk while keeping the lease alive."""

    operation = _git_command_operation(arguments)
    log_event(logger, logging.DEBUG, "catalog_git_command_started", operation=operation)
    spool_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
    try:
        spool_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_file = tempfile.TemporaryFile(  # noqa: SIM115 - closed by the yielding manager.
            mode="w+b",
            prefix="pdf-catalog-",
            dir=spool_root,
        )
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "catalog_spool_creation_failed",
            error=exc,
            error_code=failure_code,
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary) from exc

    with output_file as output:
        try:
            process = subprocess.Popen(
                list(arguments),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=_git_environment(),
                close_fds=True,
            )
        except OSError as exc:
            log_event(
                logger,
                logging.ERROR,
                "catalog_git_spawn_failed",
                error=exc,
                error_code=failure_code,
                operation=operation,
            )
            raise RepositorySyncError(failure_code, failure_summary) from exc

        deadline = time.monotonic() + settings.BITBUCKET_GIT_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        list(arguments),
                        settings.BITBUCKET_GIT_TIMEOUT_SECONDS,
                    )
                try:
                    process.wait(timeout=min(_HEARTBEAT_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    heartbeat_callback()
                    continue
                break
        except subprocess.TimeoutExpired as exc:
            if process.poll() is None:
                process.kill()
            process.wait()
            log_event(
                logger,
                logging.ERROR,
                "catalog_git_timeout",
                error=exc,
                error_code=failure_code,
                operation=operation,
            )
            raise RepositorySyncError(failure_code, failure_summary) from exc
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise

        if process.returncode != 0:
            log_event(
                logger,
                logging.ERROR,
                "catalog_git_command_failed",
                error_code=failure_code,
                return_code=process.returncode,
                operation=operation,
            )
            raise RepositorySyncError(failure_code, failure_summary)
        output.seek(0)
        log_event(logger, logging.DEBUG, "catalog_git_command_completed", operation=operation)
        yield output


def _iter_delimited(
    source: BinaryIO,
    delimiter: bytes,
    *,
    maximum_record_bytes: int,
) -> Iterator[bytes]:
    """Read delimiter-framed binary records without loading all output into memory."""

    buffer = bytearray()
    while chunk := source.read(_STREAM_CHUNK_BYTES):
        buffer.extend(chunk)
        while (separator_at := buffer.find(delimiter)) >= 0:
            if separator_at > maximum_record_bytes:
                raise RepositorySyncError(
                    "git_output_too_large",
                    "One Git metadata record was too large for OWL to process safely.",
                )
            yield bytes(buffer[:separator_at])
            del buffer[: separator_at + len(delimiter)]
        if len(buffer) > maximum_record_bytes:
            raise RepositorySyncError(
                "git_output_too_large",
                "One Git metadata record was too large for OWL to process safely.",
            )
    if buffer:
        if len(buffer) > maximum_record_bytes:
            raise RepositorySyncError(
                "git_output_too_large",
                "One Git metadata record was too large for OWL to process safely.",
            )
        yield bytes(buffer)


def _validated_relative_path(raw_path: str) -> PurePosixPath:
    relative = PurePosixPath(raw_path)
    if (
        not raw_path
        or "\\" in raw_path
        or has_disallowed_path_characters(raw_path)
        or relative.is_absolute()
        or PureWindowsPath(raw_path).drive
        or not relative.parts
        or any(part in {"", ".", ".."} for part in raw_path.split("/"))
        or relative.suffix.casefold() != ".pdf"
    ):
        raise RepositorySyncError(
            "invalid_pdf_path",
            "Git returned a PDF path that OWL could not validate safely.",
        )
    return relative


def _pdf_file_access_error(relative_path: str, error: OSError) -> RepositorySyncError:
    """Expose a validated relative filename and OS category, never raw error text."""

    filename = relative_path
    if len(filename) > 220:
        filename = f"{filename[:100]}…{filename[-100:]}"
    os_error = error.errno
    windows_error = getattr(error, "winerror", None)
    codes = []
    if isinstance(os_error, int):
        codes.append(f"errno {os_error}")
    if isinstance(windows_error, int):
        codes.append(f"Windows error {windows_error}")
    detail = f" ({', '.join(codes)})" if codes else ""
    if os_error == errno.ENAMETOOLONG or windows_error == 206:
        return RepositorySyncError(
            "pdf_path_too_long",
            f'The local path for tracked PDF "{filename}" is too long{detail}. '
            "Use a shorter OWL media path or check operating-system and Git long-path support.",
        )
    if (
        isinstance(error, PermissionError)
        or os_error in {errno.EACCES, errno.EPERM}
        or (windows_error in {5, 32, 33})
    ):
        return RepositorySyncError(
            "pdf_file_access_denied",
            f'OWL cannot access tracked PDF "{filename}"{detail}. '
            "Check file permissions or another program locking the file, then retry.",
        )
    if (
        isinstance(error, FileNotFoundError)
        or os_error in {errno.ENOENT, errno.ENOTDIR}
        or (windows_error in {2, 3})
    ):
        return RepositorySyncError(
            "missing_pdf_file",
            f'Tracked PDF "{filename}" is missing from the managed checkout{detail}. '
            "Refresh to restore the document checkout.",
        )
    return RepositorySyncError(
        "pdf_file_read_failed",
        f'OWL could not read tracked PDF "{filename}"{detail}. Check storage access and retry.',
    )


def _parse_tree_pdfs(
    repository_path: Path,
    records: Iterator[bytes],
    *,
    ignored_paths: frozenset[str] = frozenset(),
) -> tuple[_TreePDF, ...]:
    repository_root = filesystem_path(repository_path.resolve())
    documents: list[_TreePDF] = []
    seen_paths: set[str] = set()
    for raw_record in records:
        if not raw_record:
            continue
        metadata, separator, raw_path = raw_record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RepositorySyncError(
                "pdf_catalog_failed",
                "OWL could not read the repository's PDF inventory safely.",
            )
        raw_mode, raw_blob_id, raw_stage = fields
        # A PDF-looking path may be a gitlink (uninitialized submodule), sparse
        # directory, or symlink. Only regular Git blobs are local PDF documents.
        if raw_stage != b"0" or raw_mode not in {b"100644", b"100755"}:
            continue
        relative_text = os.fsdecode(raw_path)
        relative = _validated_relative_path(relative_text)
        normalized = relative.as_posix()
        if normalized in ignored_paths:
            continue
        if normalized in seen_paths:
            raise RepositorySyncError(
                "duplicate_pdf_path",
                "Git returned the same normalized PDF path more than once.",
            )
        candidate = repository_root.joinpath(*relative.parts)
        current = repository_root
        for part in relative.parts:
            current /= part
            if current.is_symlink() or current.is_junction():
                raise RepositorySyncError(
                    "unsafe_pdf_path",
                    "A tracked PDF resolves through a symbolic link or junction. "
                    "OWL did not catalog it.",
                )
        try:
            resolved = candidate.resolve(strict=True)
            stat = resolved.stat(follow_symlinks=False)
        except OSError as exc:
            log_event(logger, logging.ERROR, "catalog_pdf_access_failed", error=exc)
            raise _pdf_file_access_error(normalized, exc) from exc
        if not resolved.is_relative_to(repository_root) or not resolved.is_file():
            raise RepositorySyncError(
                "unsafe_pdf_path",
                "A tracked PDF resolves outside its managed repository. OWL did not catalog it.",
            )
        blob_id = raw_blob_id.decode("ascii", errors="strict")
        if not _COMMIT_HASH.fullmatch(blob_id):
            raise RepositorySyncError(
                "invalid_pdf_blob",
                "Git returned an invalid PDF blob identity.",
            )
        seen_paths.add(normalized)
        documents.append(
            _TreePDF(
                filename=relative.name,
                relative_path=normalized,
                file_size=stat.st_size,
                git_blob_id=blob_id,
            )
        )
    return tuple(sorted(documents, key=lambda item: item.relative_path.casefold()))


def _read_current_pdfs(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback,
    ignored_paths: frozenset[str] = frozenset(),
) -> tuple[_TreePDF, ...]:
    with _run_binary_spooled(
        (
            "git",
            "-C",
            str(repository_path),
            "ls-files",
            "-z",
            "--stage",
            "--",
            _PDF_PATHSPEC,
        ),
        heartbeat_callback=heartbeat_callback,
        failure_code="pdf_catalog_failed",
        failure_summary="OWL could not enumerate PDFs at the synchronized commit.",
    ) as output:
        return _parse_tree_pdfs(
            repository_path,
            _iter_delimited(
                output,
                b"\0",
                maximum_record_bytes=_MAX_TREE_RECORD_BYTES,
            ),
            ignored_paths=ignored_paths,
        )


def _shallow_boundary_hashes(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback,
) -> frozenset[str]:
    shallow_state = _run_binary_capture(
        ("git", "-C", str(repository_path), "rev-parse", "--is-shallow-repository"),
        heartbeat_callback=heartbeat_callback,
        failure_code="history_state_failed",
        failure_summary="OWL could not determine the available Git history coverage.",
    ).strip()
    if shallow_state != b"true":
        return frozenset()
    shallow_path_raw = _run_binary_capture(
        ("git", "-C", str(repository_path), "rev-parse", "--git-path", "shallow"),
        heartbeat_callback=heartbeat_callback,
        failure_code="history_state_failed",
        failure_summary="OWL could not determine the available Git history boundary.",
    ).strip()
    shallow_path = Path(os.fsdecode(shallow_path_raw))
    if not shallow_path.is_absolute():
        shallow_path = repository_path / shallow_path
    try:
        lines = shallow_path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RepositorySyncError(
            "history_state_failed",
            "OWL could not read the available Git history boundary.",
        ) from exc
    hashes = frozenset(line.strip() for line in lines if _COMMIT_HASH.fullmatch(line.strip()))
    if not hashes:
        raise RepositorySyncError(
            "history_state_failed",
            "OWL found a shallow repository without a valid history boundary.",
        )
    return hashes


def _decode_timestamp(raw_value: bytes) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw_value.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepositorySyncError(
            "git_history_failed",
            "OWL could not parse a Git history timestamp safely.",
        ) from exc
    if timezone.is_naive(parsed):
        raise RepositorySyncError(
            "git_history_failed",
            "Git returned a history timestamp without a timezone.",
        )
    return parsed


def _parse_history_block(
    block: bytes,
    *,
    shallow_boundaries: frozenset[str],
) -> tuple[CommitEvidence, tuple[tuple[str, ...], ...]]:
    fields = block.split(b"\0")
    if len(fields) < 6:
        raise RepositorySyncError(
            "git_history_failed",
            "OWL could not parse the available Git history safely.",
        )
    commit_hash = fields[0].decode("ascii", errors="strict")
    if not _COMMIT_HASH.fullmatch(commit_hash):
        raise RepositorySyncError(
            "git_history_failed",
            "Git returned an invalid commit identity while cataloguing PDFs.",
        )
    commit = CommitEvidence(
        commit_hash=commit_hash,
        authored_at=_decode_timestamp(fields[1]),
        author_name=os.fsdecode(fields[2]).strip()[:200] or "Unknown Git author",
        committed_at=_decode_timestamp(fields[3]),
        committer_name=os.fsdecode(fields[4]).strip()[:200] or "Unknown Git committer",
        is_shallow_boundary=commit_hash in shallow_boundaries,
    )
    tokens = list(fields[5:])
    changes: list[tuple[str, ...]] = []
    index = 0
    while index < len(tokens):
        status = os.fsdecode(tokens[index]).lstrip("\n")
        index += 1
        if not status:
            continue
        code = status[0]
        if code == "R":
            if index + 1 >= len(tokens):
                raise RepositorySyncError(
                    "git_history_failed",
                    "OWL could not parse a PDF rename in Git history.",
                )
            old_path = os.fsdecode(tokens[index])
            new_path = os.fsdecode(tokens[index + 1])
            index += 2
            changes.append((status, old_path, new_path))
        elif code in {"A", "M"}:
            if index >= len(tokens):
                raise RepositorySyncError(
                    "git_history_failed",
                    "OWL could not parse a PDF change in Git history.",
                )
            path = os.fsdecode(tokens[index])
            index += 1
            changes.append((status, path))
    return commit, tuple(changes)


def _history_blocks(output: BinaryIO) -> Iterator[bytes]:
    for block in _iter_delimited(
        output,
        _HISTORY_MARKER,
        maximum_record_bytes=_MAX_HISTORY_COMMIT_BYTES,
    ):
        if block:
            yield block


def _read_history_evidence(
    repository_path: Path,
    current_paths: set[str],
    shallow_boundaries: frozenset[str],
    *,
    heartbeat_callback: HeartbeatCallback,
) -> tuple[
    dict[str, CommitEvidence],
    dict[str, CommitEvidence],
    dict[str, str],
]:
    arguments = [
        "git",
        "-C",
        str(repository_path),
        "log",
        "--date-order",
        "--use-mailmap",
        "--format=%x00OWL-PDF-HISTORY-COMMIT%x00%H%x00%aI%x00%aN%x00%cI%x00%cN%x00",
        "--name-status",
        "-z",
        "--find-renames=100%",
        "--diff-filter=AMR",
    ]
    if not shallow_boundaries:
        arguments.append("--root")
    arguments.extend(("HEAD", "--", _PDF_PATHSPEC))
    lineage = {path: path for path in current_paths}
    added: dict[str, CommitEvidence] = {}
    last_changed: dict[str, CommitEvidence] = {}
    evidence: dict[str, str] = {}
    with _run_binary_spooled(
        arguments,
        heartbeat_callback=heartbeat_callback,
        failure_code="git_history_failed",
        failure_summary="OWL could not read available PDF history from Git.",
    ) as output:
        for block in _history_blocks(output):
            heartbeat_callback()
            commit, changes = _parse_history_block(
                block,
                shallow_boundaries=shallow_boundaries,
            )
            for change in changes:
                status = change[0]
                if status.startswith("R"):
                    old_path, new_path = change[1], change[2]
                    current_path = lineage.pop(new_path, None)
                    if current_path is None:
                        continue
                    last_changed.setdefault(current_path, commit)
                    if PurePosixPath(old_path).suffix.casefold() == ".pdf":
                        lineage[old_path] = current_path
                    else:
                        if commit.is_shallow_boundary:
                            evidence[current_path] = (
                                PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY
                            )
                        else:
                            evidence[current_path] = PDFDocumentAddedEvidence.CONFIRMED
                            added[current_path] = commit
                    continue

                path = change[1]
                current_path = lineage.get(path)
                if current_path is None:
                    continue
                last_changed.setdefault(current_path, commit)
                if status.startswith("A"):
                    lineage.pop(path, None)
                    if commit.is_shallow_boundary:
                        evidence[current_path] = PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY
                    else:
                        evidence[current_path] = PDFDocumentAddedEvidence.CONFIRMED
                        added[current_path] = commit

            if not lineage:
                break

    unresolved_evidence = (
        PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY
        if shallow_boundaries
        else PDFDocumentAddedEvidence.NOT_FOUND
    )
    for current_path in lineage.values():
        evidence[current_path] = unresolved_evidence
    return added, last_changed, evidence


def _repository_policy_signature(
    repository: BitbucketRepository,
) -> tuple[tuple[str, str, int | None], ...]:
    return tuple(
        PDFLocalPolicy.objects.filter(repository=repository)
        .order_by("relative_path")
        .values_list("relative_path", "state", "document_id")
    )


def build_repository_pdf_catalog(
    repository: BitbucketRepository,
    *,
    result_commit: str,
    progress_callback: ProgressCallback,
) -> CatalogBuild:
    """Inspect one completed checkout without issuing per-PDF Git commands."""

    started_at = time.monotonic()
    log_event(logger, logging.DEBUG, "catalog_build_started", repository_id=repository.pk)
    try:
        with logging_context(repository_id=repository.pk):
            catalog = _build_repository_pdf_catalog(
                repository, result_commit=result_commit, progress_callback=progress_callback
            )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "catalog_build_failed",
            error=exc,
            repository_id=repository.pk,
            error_code=exc.code if isinstance(exc, RepositorySyncError) else "pdf_catalog_failed",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "catalog_build_completed",
        repository_id=repository.pk,
        pdf_count=len(catalog.documents),
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )
    return catalog


def _build_repository_pdf_catalog(
    repository: BitbucketRepository,
    *,
    result_commit: str,
    progress_callback: ProgressCallback,
) -> CatalogBuild:
    repository_path = managed_repository_path(repository)

    def heartbeat() -> None:
        progress_callback(
            RepositorySyncPhase.DISCOVERING,
            98,
            "Cataloguing synchronized PDFs…",
        )

    progress_callback(
        RepositorySyncPhase.DISCOVERING,
        98,
        "Cataloguing synchronized PDFs…",
    )
    policy_signature = _repository_policy_signature(repository)
    ignored_paths = frozenset(
        path
        for path, state, _document_id in policy_signature
        if state in {PDFLocalPolicyState.EXCLUDED, PDFLocalPolicyState.DELETED}
    )
    log_event(
        logger,
        logging.DEBUG,
        "catalog_policy_selection",
        repository_id=repository.pk,
        skipped_count=len(ignored_paths),
        count=len(policy_signature),
    )
    tree_documents = _read_current_pdfs(
        repository_path,
        heartbeat_callback=heartbeat,
        ignored_paths=ignored_paths,
    )
    shallow_boundaries = _shallow_boundary_hashes(
        repository_path,
        heartbeat_callback=heartbeat,
    )

    can_reuse_history = (
        repository.metadata_indexed_commit == result_commit
        and repository.history_is_shallow == bool(shallow_boundaries)
        and not any(state == PDFLocalPolicyState.RESUMING for _, state, _ in policy_signature)
        and repository.pdf_documents.filter(lifecycle_state=PDFDocumentLifecycle.ACTIVE).exists()
    )
    if can_reuse_history:
        log_event(
            logger,
            logging.DEBUG,
            "catalog_history_reused",
            repository_id=repository.pk,
            pdf_count=len(tree_documents),
        )
        added: dict[str, CommitEvidence] = {}
        last_changed: dict[str, CommitEvidence] = {}
        evidence = {
            item.relative_path: PDFDocumentAddedEvidence.NOT_FOUND for item in tree_documents
        }
    else:
        log_event(
            logger,
            logging.DEBUG,
            "catalog_history_read_started",
            repository_id=repository.pk,
            pdf_count=len(tree_documents),
        )
        progress_callback(
            RepositorySyncPhase.DISCOVERING,
            99,
            "Reading available Git history for PDF dates and authors…",
        )
        added, last_changed, evidence = _read_history_evidence(
            repository_path,
            {item.relative_path for item in tree_documents},
            shallow_boundaries,
            heartbeat_callback=lambda: progress_callback(
                RepositorySyncPhase.DISCOVERING,
                99,
                "Reading available Git history for PDF dates and authors…",
            ),
        )

    documents = tuple(
        CatalogPDF(
            filename=item.filename,
            relative_path=item.relative_path,
            file_size=item.file_size,
            git_blob_id=item.git_blob_id,
            added_evidence=evidence.get(
                item.relative_path,
                PDFDocumentAddedEvidence.NOT_FOUND,
            ),
            added_commit=added.get(item.relative_path),
            last_commit=last_changed.get(item.relative_path),
        )
        for item in tree_documents
    )
    from bitbucket_search.services.git_activity import build_repository_git_activity

    activity = build_repository_git_activity(
        repository,
        repository_path=repository_path,
        result_commit=result_commit,
        shallow_boundaries=shallow_boundaries,
        heartbeat_callback=lambda: progress_callback(
            RepositorySyncPhase.DISCOVERING,
            99,
            "Reading available Git commit and folder activity…",
        ),
    )
    return CatalogBuild(
        documents=documents,
        history_is_shallow=bool(shallow_boundaries),
        policy_signature=policy_signature,
        activity=activity,
    )


def _commit_map(
    repository: BitbucketRepository,
    catalog: CatalogBuild,
) -> dict[str, GitCommit]:
    evidence_by_hash: dict[str, CommitEvidence] = {}
    for document in catalog.documents:
        for evidence in (document.added_commit, document.last_commit):
            if evidence is not None:
                evidence_by_hash[evidence.commit_hash] = evidence
    if not evidence_by_hash:
        return {}

    existing = {
        item.commit_hash: item
        for item in GitCommit.objects.filter(
            repository=repository,
            commit_hash__in=evidence_by_hash,
        )
    }
    GitCommit.objects.bulk_create(
        [
            GitCommit(
                repository=repository,
                commit_hash=commit_hash,
                author_name=evidence.author_name,
                committer_name=evidence.committer_name,
                authored_at=evidence.authored_at,
                committed_at=evidence.committed_at,
                is_shallow_boundary=evidence.is_shallow_boundary,
            )
            for commit_hash, evidence in evidence_by_hash.items()
            if commit_hash not in existing
        ],
        batch_size=500,
        ignore_conflicts=True,
    )
    return {
        item.commit_hash: item
        for item in GitCommit.objects.filter(
            repository=repository,
            commit_hash__in=evidence_by_hash,
        )
    }


def publish_repository_pdf_catalog(
    repository: BitbucketRepository,
    catalog: CatalogBuild,
    *,
    result_commit: str,
    observed_at: datetime,
) -> None:
    """Switch one repository's active PDF inventory while preserving OWL usage."""

    started_at = time.monotonic()
    log_event(
        logger,
        logging.DEBUG,
        "catalog_publication_started",
        repository_id=repository.pk,
        pdf_count=len(catalog.documents),
    )
    try:
        with logging_context(repository_id=repository.pk):
            _publish_repository_pdf_catalog(
                repository, catalog, result_commit=result_commit, observed_at=observed_at
            )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "catalog_publication_failed",
            error=exc,
            repository_id=repository.pk,
            error_code=exc.code
            if isinstance(exc, RepositorySyncError)
            else "pdf_catalog_publish_failed",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise
    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    transaction.on_commit(
        lambda: log_event(
            logger,
            logging.INFO,
            "catalog_publication_completed",
            repository_id=repository.pk,
            pdf_count=len(catalog.documents),
            elapsed_ms=elapsed_ms,
        )
    )


@transaction.atomic
def _publish_repository_pdf_catalog(
    repository: BitbucketRepository,
    catalog: CatalogBuild,
    *,
    result_commit: str,
    observed_at: datetime,
) -> None:
    policy_signature = _repository_policy_signature(repository)
    if policy_signature != catalog.policy_signature:
        raise RepositorySyncError(
            "document_policy_changed",
            "A local PDF policy changed while the catalogue was being built. Refresh to retry.",
        )
    if catalog.activity is not None:
        from bitbucket_search.services.git_activity import publish_repository_git_activity

        publish_repository_git_activity(
            repository, catalog.activity, result_commit=result_commit, observed_at=observed_at
        )
    frozen_paths = {
        path
        for path, state, _document_id in policy_signature
        if state == PDFLocalPolicyState.EXCLUDED
    }
    blocked_paths = frozen_paths | {
        path
        for path, state, _document_id in policy_signature
        if state == PDFLocalPolicyState.DELETED
    }
    commits = _commit_map(repository, catalog)
    existing = {
        document.relative_path: document
        for document in PDFDocument.objects.filter(repository=repository)
    }
    seen_paths: set[str] = set()
    creates: list[PDFDocument] = []
    updates: list[PDFDocument] = []
    future_limit = observed_at + timedelta(days=1)

    for item in catalog.documents:
        if item.relative_path in blocked_paths:
            continue
        seen_paths.add(item.relative_path)
        document = existing.get(item.relative_path)
        added_commit = (
            commits.get(item.added_commit.commit_hash) if item.added_commit is not None else None
        )
        last_commit = (
            commits.get(item.last_commit.commit_hash) if item.last_commit is not None else None
        )
        confirmed_timeline = added_commit is not None and added_commit.committed_at <= future_limit

        if document is None:
            timeline_at = added_commit.committed_at if confirmed_timeline else observed_at
            creates.append(
                PDFDocument(
                    repository=repository,
                    filename=item.filename,
                    relative_path=item.relative_path,
                    file_size=item.file_size,
                    git_blob_id=item.git_blob_id,
                    lifecycle_state=PDFDocumentLifecycle.ACTIVE,
                    discovered_at=observed_at,
                    last_seen_at=observed_at,
                    last_seen_commit=result_commit,
                    added_evidence=item.added_evidence,
                    added_commit=added_commit,
                    last_commit=last_commit,
                    timeline_at=timeline_at,
                    timeline_basis=(
                        PDFDocumentTimelineBasis.GIT_ADDED
                        if confirmed_timeline
                        else PDFDocumentTimelineBasis.OWL_DISCOVERED
                    ),
                )
            )
            continue

        document.filename = item.filename
        document.file_size = item.file_size
        document.git_blob_id = item.git_blob_id
        document.lifecycle_state = PDFDocumentLifecycle.ACTIVE
        document.last_seen_at = observed_at
        document.removed_at = None
        document.last_seen_commit = result_commit
        document.last_commit = last_commit or document.last_commit
        if item.added_evidence == PDFDocumentAddedEvidence.CONFIRMED and added_commit is not None:
            document.added_evidence = item.added_evidence
            document.added_commit = added_commit
            if confirmed_timeline:
                document.timeline_at = added_commit.committed_at
                document.timeline_basis = PDFDocumentTimelineBasis.GIT_ADDED
        elif document.added_commit_id is None:
            document.added_evidence = item.added_evidence
            document.timeline_at = document.discovered_at
            document.timeline_basis = PDFDocumentTimelineBasis.OWL_DISCOVERED
        updates.append(document)

    removed = [
        document
        for path, document in existing.items()
        if path not in seen_paths
        and path not in frozen_paths
        and document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    ]
    for document in removed:
        document.lifecycle_state = PDFDocumentLifecycle.REMOVED
        document.removed_at = observed_at

    PDFDocument.objects.bulk_create(creates, batch_size=500)
    if updates:
        PDFDocument.objects.bulk_update(
            updates,
            fields=(
                "filename",
                "file_size",
                "git_blob_id",
                "lifecycle_state",
                "last_seen_at",
                "removed_at",
                "last_seen_commit",
                "added_evidence",
                "added_commit",
                "last_commit",
                "timeline_at",
                "timeline_basis",
            ),
            batch_size=500,
        )
    if removed:
        PDFDocument.objects.bulk_update(
            removed,
            fields=("lifecycle_state", "removed_at"),
            batch_size=500,
        )
    resumed_paths = tuple(
        path
        for path, state, _document_id in policy_signature
        if state == PDFLocalPolicyState.RESUMING
    )
    if resumed_paths:
        from bitbucket_search.services.pdf_local_policy import complete_resumed_policies

        complete_resumed_policies(repository.pk, resumed_paths)
    log_event(
        logger,
        logging.DEBUG,
        "catalog_publication_counts",
        repository_id=repository.pk,
        count=len(creates),
        indexed_count=len(updates),
        removed_count=len(removed),
        skipped_count=len(blocked_paths),
    )
