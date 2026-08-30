"""Index available Git commit and folder activity during background synchronization."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import BinaryIO

from django.db import transaction

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    GitCommitFolder,
    InvalidPeopleName,
    canonical_people_name,
)
from bitbucket_search.services.git_sync import RepositorySyncError
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_catalog import (
    _COMMIT_HASH,
    CommitEvidence,
    HeartbeatCallback,
    _decode_timestamp,
    _iter_delimited,
    _run_binary_spooled,
)

logger = get_logger("activity")
_MARKER = b"OWL-GIT-ACTIVITY-COMMIT"
_STATUS = re.compile(rb"[ADMTUXB][0-9]*\Z")
_MAX_TOKEN_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GitActivityCommit:
    evidence: CommitEvidence
    folders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitActivityBuild:
    result_commit: str
    commits: tuple[GitActivityCommit, ...]


def _history_error() -> RepositorySyncError:
    return RepositorySyncError(
        "git_activity_failed",
        "OWL could not safely read repository commit activity. Refresh to retry.",
    )


def _person_name(raw_name: bytes, *, fallback: str) -> str:
    try:
        return canonical_people_name(raw_name.decode("utf-8", errors="replace").strip()[:200])
    except InvalidPeopleName:
        return fallback


def _folder_path(raw_path: bytes) -> str:
    """Return a lossless, SQL-safe display path even for Git's byte filenames."""

    parts = raw_path.split(b"/")
    if not raw_path or any(part in {b"", b".", b".."} for part in parts):
        raise _history_error()
    decoded = b"/".join(parts[:-1]).decode("utf-8", errors="surrogateescape")
    characters: list[str] = []
    for character in decoded:
        codepoint = ord(character)
        if character == "\\":
            characters.append("\\\\")
        elif 0xDC80 <= codepoint <= 0xDCFF:
            characters.append(f"\\x{codepoint - 0xDC00:02x}")
        elif unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            if codepoint <= 0x7F:
                characters.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                characters.append(f"\\u{codepoint:04x}")
            else:
                characters.append(f"\\U{codepoint:08x}")
        else:
            characters.append(character)
    folder = "".join(characters)
    if len(folder) > 2048:
        raise _history_error()
    return folder


def _parse_activity(
    output: BinaryIO,
    *,
    shallow_boundaries: frozenset[str],
    heartbeat_callback: HeartbeatCallback,
) -> Iterator[GitActivityCommit]:
    """Parse NUL tokens by state, so a marker-looking filename stays a filename."""

    tokens = iter(_iter_delimited(output, b"\0", maximum_record_bytes=_MAX_TOKEN_BYTES))
    current: CommitEvidence | None = None
    folders: set[str] = set()
    for token_number, raw_token in enumerate(tokens):
        if token_number % 500 == 0:
            heartbeat_callback()
        token = raw_token.lstrip(b"\n")
        if not token:
            continue
        if token == _MARKER:
            if current is not None:
                yield GitActivityCommit(current, tuple(sorted(folders)))
            heartbeat_callback()
            try:
                raw_hash, authored_at, author, committed_at, committer = (
                    next(tokens) for _ in range(5)
                )
                commit_hash = raw_hash.decode("ascii")
            except (StopIteration, RuntimeError, UnicodeDecodeError) as exc:
                raise _history_error() from exc
            if not _COMMIT_HASH.fullmatch(commit_hash):
                raise _history_error()
            current = CommitEvidence(
                commit_hash=commit_hash,
                author_name=_person_name(author, fallback="Unknown Git author"),
                committer_name=_person_name(committer, fallback="Unknown Git committer"),
                authored_at=_decode_timestamp(authored_at),
                committed_at=_decode_timestamp(committed_at),
                is_shallow_boundary=commit_hash in shallow_boundaries,
            )
            folders = set()
        else:
            if current is None or not _STATUS.fullmatch(token):
                raise _history_error()
            try:
                path = next(tokens)
            except StopIteration as exc:
                raise _history_error() from exc
            folder = _folder_path(path)
            # Git treats a shallow boundary as a root, which would falsely claim
            # every existing folder changed in that commit. Its parent diff is unknown.
            if not current.is_shallow_boundary:
                folders.add(folder)
    if current is not None:
        yield GitActivityCommit(current, tuple(sorted(folders)))


def build_repository_git_activity(
    repository: BitbucketRepository,
    *,
    repository_path: Path,
    result_commit: str,
    shallow_boundaries: frozenset[str],
    heartbeat_callback: HeartbeatCallback,
) -> GitActivityBuild | None:
    """Read all locally reachable HEAD commits; None reuses published evidence."""

    current_commits = GitCommit.objects.filter(repository=repository, in_activity_history=True)
    if (
        repository.activity_indexed_commit == result_commit
        and repository.activity_indexed_at is not None
        and current_commits.filter(commit_hash=result_commit).exists()
        and set(
            current_commits.filter(is_shallow_boundary=True).values_list("commit_hash", flat=True)
        )
        == shallow_boundaries
    ):
        log_event(logger, logging.DEBUG, "git_activity_reused", repository_id=repository.pk)
        return None

    started_at = time.monotonic()
    log_event(logger, logging.DEBUG, "git_activity_build_started", repository_id=repository.pk)
    try:
        if not _COMMIT_HASH.fullmatch(result_commit):
            raise _history_error()
        with _run_binary_spooled(
            (
                "git",
                "-C",
                str(repository_path),
                "log",
                "--date-order",
                "--use-mailmap",
                "--format=%x00OWL-GIT-ACTIVITY-COMMIT%x00%H%x00%aI%x00%aN%x00%cI%x00%cN%x00",
                "--name-status",
                "-z",
                "--no-renames",
                "--diff-merges=first-parent",
                "--root",
                result_commit,
                "--",
            ),
            heartbeat_callback=heartbeat_callback,
            failure_code="git_activity_failed",
            failure_summary="OWL could not read repository commit activity from Git.",
        ) as output:
            commits = tuple(
                _parse_activity(
                    output,
                    shallow_boundaries=shallow_boundaries,
                    heartbeat_callback=heartbeat_callback,
                )
            )
        hashes = {item.evidence.commit_hash for item in commits}
        if result_commit not in hashes or len(hashes) != len(commits):
            raise _history_error()
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_activity_build_failed",
            repository_id=repository.pk,
            error=exc,
            error_code=exc.code if isinstance(exc, RepositorySyncError) else "git_activity_failed",
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "git_activity_build_completed",
        repository_id=repository.pk,
        count=len(commits),
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )
    return GitActivityBuild(result_commit, commits)


@transaction.atomic
def publish_repository_git_activity(
    repository: BitbucketRepository,
    activity: GitActivityBuild,
    *,
    result_commit: str,
    observed_at: datetime,
) -> None:
    """Replace the available history atomically without deleting PDF commit references."""

    if activity.result_commit != result_commit:
        raise _history_error()
    existing = {item.commit_hash: item for item in GitCommit.objects.filter(repository=repository)}
    creates: list[GitCommit] = []
    updates: list[GitCommit] = []
    for item in activity.commits:
        evidence = item.evidence
        commit = existing.get(evidence.commit_hash)
        if commit is None:
            commit = GitCommit(repository=repository, commit_hash=evidence.commit_hash)
            creates.append(commit)
            existing[evidence.commit_hash] = commit
        else:
            updates.append(commit)
        commit.author_name = evidence.author_name
        commit.committer_name = evidence.committer_name
        commit.authored_at = evidence.authored_at
        commit.committed_at = evidence.committed_at
        commit.is_shallow_boundary = evidence.is_shallow_boundary
        commit.in_activity_history = True

    GitCommit.objects.filter(repository=repository).update(in_activity_history=False)
    GitCommit.objects.bulk_create(creates, batch_size=500)
    if updates:
        GitCommit.objects.bulk_update(
            updates,
            fields=(
                "author_name",
                "committer_name",
                "authored_at",
                "committed_at",
                "is_shallow_boundary",
                "in_activity_history",
            ),
            batch_size=500,
        )
    GitCommitFolder.objects.filter(commit__repository=repository).delete()
    folders = (
        GitCommitFolder(commit=existing[item.evidence.commit_hash], folder_path=folder)
        for item in activity.commits
        for folder in item.folders
    )
    while batch := list(islice(folders, 500)):
        GitCommitFolder.objects.bulk_create(batch, batch_size=500)
    BitbucketRepository.objects.filter(pk=repository.pk).update(
        activity_indexed_commit=result_commit,
        activity_indexed_at=observed_at,
    )
    transaction.on_commit(
        lambda: log_event(
            logger,
            logging.INFO,
            "git_activity_published",
            repository_id=repository.pk,
            count=len(activity.commits),
        )
    )
