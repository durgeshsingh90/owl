"""Build a lightweight PDF/VSDX catalogue from Git metadata."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from bitbucket.models import Contributor, Document, DocumentKind, Repository

_DOCUMENT_PATHSPECS = (":(icase,glob)**/*.pdf", ":(icase,glob)**/*.vsdx")
_PDF_PATHSPEC = ":(icase,glob)**/*.pdf"
_RECORD_SEPARATOR = b"\x1e"
_LOG_FORMAT = "format:%x1e%H%x00%an%x00%ae%x00%aI%x00"


@dataclass(frozen=True, slots=True)
class CommitPaths:
    commit_id: str
    author: str
    email: str
    committed_at: datetime | None
    paths: tuple[str, ...]


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def parse_log_records(raw_output: bytes) -> tuple[CommitPaths, ...]:
    records: list[CommitPaths] = []
    for raw_record in raw_output.split(_RECORD_SEPARATOR)[1:]:
        fields = raw_record.split(b"\x00")
        if len(fields) < 5:
            continue
        commit_id = _decode(fields[0]).strip()
        author = _decode(fields[1]).strip()[:255]
        email = _decode(fields[2]).strip()[:320]
        try:
            committed_at = datetime.fromisoformat(_decode(fields[3]).strip())
            if timezone.is_naive(committed_at):
                committed_at = timezone.make_aware(committed_at)
        except ValueError:
            committed_at = None
        paths: list[str] = []
        for index, raw_path in enumerate(fields[4:]):
            if index == 0 and raw_path.startswith(b"\n"):
                raw_path = raw_path[1:]
            if raw_path:
                paths.append(_decode(raw_path))
        if commit_id:
            records.append(CommitPaths(commit_id, author, email, committed_at, tuple(paths)))
    return tuple(records)


def _run_git(checkout: Path, arguments: Iterable[str]) -> bytes:
    timeout = int(getattr(settings, "BITBUCKET_APP_GIT_TIMEOUT_SECONDS", 3600))
    completed = subprocess.run(
        ("git", "-C", str(checkout), *arguments),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = _decode(completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or "Git could not read repository history.")
    return completed.stdout


def _current_paths(run_git: Callable[[Iterable[str]], bytes]) -> dict[str, DocumentKind]:
    paths: dict[str, DocumentKind] = {}
    for raw_path in run_git(("ls-files", "-z")).split(b"\x00"):
        if not raw_path:
            continue
        path = _decode(raw_path)
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix == ".pdf":
            paths[path] = DocumentKind.PDF
        elif suffix == ".vsdx":
            paths[path] = DocumentKind.VSDX
    return paths


def refresh_catalog(
    repository: Repository,
    checkout: Path,
    *,
    runner: Callable[[Iterable[str]], bytes] | None = None,
) -> tuple[int, int]:
    """Replace current inventory and contributor totals while preserving open counts."""

    run_git = runner or (lambda arguments: _run_git(checkout, arguments))
    current = _current_paths(run_git)
    additions: dict[str, CommitPaths] = {}
    addition_records = parse_log_records(
        run_git(
            (
                "log",
                "--reverse",
                "--no-renames",
                "--diff-filter=A",
                f"--format={_LOG_FORMAT}",
                "--name-only",
                "-z",
                "HEAD",
                "--",
                *_DOCUMENT_PATHSPECS,
            )
        )
    )
    for record in addition_records:
        for path in record.paths:
            additions.setdefault(path, record)

    contributions: dict[str, dict[str, object]] = {}
    pdf_records = parse_log_records(
        run_git(
            (
                "log",
                f"--format={_LOG_FORMAT}",
                "--name-only",
                "-z",
                "HEAD",
                "--",
                _PDF_PATHSPEC,
            )
        )
    )
    for record in pdf_records:
        if not any(path.casefold().endswith(".pdf") for path in record.paths):
            continue
        identity = (record.email or record.author).strip().casefold()
        if not identity:
            identity = f"unknown:{record.commit_id}"
        item = contributions.setdefault(
            identity,
            {
                "name": record.author or "Unknown contributor",
                "email": record.email,
                "count": 0,
                "last": record.committed_at,
            },
        )
        item["count"] = int(item["count"]) + 1
        if record.committed_at and (item["last"] is None or record.committed_at > item["last"]):
            item["last"] = record.committed_at

    with transaction.atomic():
        existing = {
            document.relative_path: document
            for document in Document.objects.filter(repository=repository)
        }
        creates: list[Document] = []
        updates: list[Document] = []
        for relative_path, kind in current.items():
            metadata = additions.get(relative_path)
            document = existing.pop(relative_path, None)
            if document is None:
                creates.append(
                    Document(
                        repository=repository,
                        kind=kind,
                        relative_path=relative_path,
                        filename=PurePosixPath(relative_path).name[:500],
                        added_at=metadata.committed_at if metadata else None,
                        added_by=metadata.author if metadata else "",
                        added_by_email=metadata.email if metadata else "",
                        commit_id=metadata.commit_id if metadata else "",
                    )
                )
                continue
            document.kind = kind
            document.filename = PurePosixPath(relative_path).name[:500]
            if metadata:
                document.added_at = metadata.committed_at
                document.added_by = metadata.author
                document.added_by_email = metadata.email
                document.commit_id = metadata.commit_id
            updates.append(document)
        if existing:
            Document.objects.filter(pk__in=[item.pk for item in existing.values()]).delete()
        Document.objects.bulk_create(creates, batch_size=500)
        if updates:
            Document.objects.bulk_update(
                updates,
                ("kind", "filename", "added_at", "added_by", "added_by_email", "commit_id"),
                batch_size=500,
            )
        Contributor.objects.filter(repository=repository).delete()
        Contributor.objects.bulk_create(
            [
                Contributor(
                    repository=repository,
                    identity_key=identity[:600],
                    name=str(item["name"])[:255],
                    email=str(item["email"])[:320],
                    pdf_commit_count=int(item["count"]),
                    last_pdf_commit_at=item["last"],
                )
                for identity, item in contributions.items()
            ],
            batch_size=500,
        )
        pdf_count = sum(kind == DocumentKind.PDF for kind in current.values())
        vsdx_count = sum(kind == DocumentKind.VSDX for kind in current.values())
        Repository.objects.filter(pk=repository.pk).update(
            pdf_count=pdf_count,
            vsdx_count=vsdx_count,
        )
    return pdf_count, vsdx_count
