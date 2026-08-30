"""Safe local actions for registered PDFs in OWL-managed repositories."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from django.conf import settings
from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from bitbucket_search.models import PDFDocument, PDFDocumentLifecycle
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.path_safety import has_disallowed_path_characters
from bitbucket_search.services.pdf_search_query import MAX_SEARCH_PAGE_SIZE
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    repository_checkout_locks,
)

_NATIVE_ACTION_TIMEOUT_SECONDS = 10
logger = logging.getLogger(__name__)


class DocumentActionError(RuntimeError):
    """A safe local-action failure suitable for returning to the UI."""

    def __init__(self, code: str, summary: str) -> None:
        self.code = code
        self.summary = " ".join(summary.split())[:500]
        super().__init__(self.summary)


@dataclass(frozen=True, slots=True)
class BulkDocumentOpenFailure:
    """One sanitized per-document failure from a validated bulk open."""

    document_id: int
    code: str
    summary: str


@dataclass(frozen=True, slots=True)
class BulkDocumentUsageFailure:
    """One PDF the OS opened but whose usage counter could not be recorded."""

    document_id: int
    code: str
    summary: str


@dataclass(frozen=True, slots=True)
class BulkDocumentOpenResult:
    """The complete outcome of one ordered, deduplicated bulk open request."""

    requested_count: int
    opened_documents: tuple[PDFDocument, ...]
    failures: tuple[BulkDocumentOpenFailure, ...]
    usage_failures: tuple[BulkDocumentUsageFailure, ...] = ()

    @property
    def opened_count(self) -> int:
        return len(self.opened_documents)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def usage_failure_count(self) -> int:
        return len(self.usage_failures)


def _validated_relative_pdf_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or has_disallowed_path_characters(value)
    ):
        raise DocumentActionError(
            "invalid_document_path",
            "The registered PDF path is not safe to open.",
        )

    raw_parts = value.split("/")
    relative_path = PurePosixPath(value)
    if (
        relative_path.is_absolute()
        or not raw_parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or PureWindowsPath(value).drive
    ):
        raise DocumentActionError(
            "invalid_document_path",
            "The registered PDF path is not safe to open.",
        )
    return relative_path


def validated_pdf_path(document: PDFDocument) -> Path:
    """Resolve a registered PDF, including a validated private frozen copy."""

    from bitbucket_search.services.pdf_local_policy import frozen_pdf_path

    if document.lifecycle_state != PDFDocumentLifecycle.ACTIVE:
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is no longer available in the managed repository.",
        )
    frozen = frozen_pdf_path(document)
    return frozen if frozen is not None else validated_checkout_pdf_path(document)


def validated_checkout_pdf_path(document: PDFDocument, *, allow_missing: bool = False) -> Path:
    """Return a strictly validated, non-symlink PDF path in its managed checkout."""

    if document.lifecycle_state != PDFDocumentLifecycle.ACTIVE:
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is no longer available in the managed repository.",
        )

    repository = document.repository
    try:
        configured_parent = Path(settings.BITBUCKET_REPOSITORIES_ROOT)
        expected_root = managed_repository_path(repository)
        configured_root = Path(repository.local_path)
        resolved_configured_root = configured_root.resolve(strict=False)
    except (OSError, RepositorySyncError, RuntimeError, TypeError, ValueError) as exc:
        raise DocumentActionError(
            "invalid_repository_checkout",
            "The managed repository checkout is missing or is not valid.",
        ) from exc

    if (
        not repository.local_path
        or configured_parent.is_symlink()
        or configured_parent.absolute() != configured_parent.resolve(strict=False)
        or configured_root.is_symlink()
        or configured_root.absolute() != expected_root
        or resolved_configured_root != expected_root
        or not expected_root.is_dir()
        or (expected_root / ".git").is_symlink()
        or not (expected_root / ".git").is_dir()
    ):
        raise DocumentActionError(
            "invalid_repository_checkout",
            "The managed repository checkout is missing or is not valid.",
        )

    relative_path = _validated_relative_pdf_path(document.relative_path)
    candidate = expected_root.joinpath(*relative_path.parts)

    current = expected_root
    try:
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise DocumentActionError(
                    "invalid_document_path",
                    "The registered PDF path contains a symbolic link.",
                )

        resolved_candidate = candidate.resolve(strict=not allow_missing)
    except DocumentActionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is missing from the managed repository.",
        ) from exc

    if resolved_candidate == expected_root or not resolved_candidate.is_relative_to(expected_root):
        raise DocumentActionError(
            "invalid_document_path",
            "The registered PDF path is outside its managed repository.",
        )
    if resolved_candidate.suffix.casefold() != ".pdf":
        raise DocumentActionError(
            "unsupported_document_type",
            "Only registered PDF files can be opened from this view.",
        )

    try:
        mode = resolved_candidate.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as exc:
        if allow_missing:
            return resolved_candidate
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is missing from the managed repository.",
        ) from exc
    except OSError as exc:
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is missing from the managed repository.",
        ) from exc
    if not stat.S_ISREG(mode):
        raise DocumentActionError(
            "document_unavailable",
            "The registered PDF is not a regular file.",
        )
    return resolved_candidate


def _run_native_action(arguments: list[str], *, action: str) -> None:
    summary = f"OWL could not ask your operating system to {action} this PDF."
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_NATIVE_ACTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocumentActionError("native_action_timeout", summary) from exc
    except OSError as exc:
        raise DocumentActionError("native_action_failed", summary) from exc
    if completed.returncode != 0:
        raise DocumentActionError("native_action_failed", summary)


def _start_file_windows(path: Path, *, action: str) -> None:
    summary = f"OWL could not ask your operating system to {action} this PDF."
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise DocumentActionError("native_action_unavailable", summary)
    try:
        startfile(str(path))
    except OSError as exc:
        raise DocumentActionError("native_action_failed", summary) from exc


def _linux_opener(*, action: str) -> str:
    executable = shutil.which("xdg-open")
    if executable is None:
        raise DocumentActionError(
            "native_action_unavailable",
            f"OWL could not find a desktop application to {action} this PDF.",
        )
    return executable


def open_pdf_native(path: Path) -> None:
    """Ask the host desktop to open a previously validated PDF."""

    path = Path(path)
    if sys.platform == "darwin":
        _run_native_action(["/usr/bin/open", str(path)], action="open")
    elif sys.platform == "win32":
        _start_file_windows(path, action="open")
    elif sys.platform.startswith("linux"):
        _run_native_action([_linux_opener(action="open"), str(path)], action="open")
    else:
        raise DocumentActionError(
            "unsupported_platform",
            "Opening local PDFs is not supported on this operating system.",
        )


def reveal_pdf_in_folder(path: Path) -> None:
    """Ask the host desktop to reveal a previously validated PDF's location."""

    path = Path(path)
    if sys.platform == "darwin":
        _run_native_action(["/usr/bin/open", "-R", str(path)], action="reveal")
    elif sys.platform == "win32":
        _start_file_windows(path.parent, action="reveal")
    elif sys.platform.startswith("linux"):
        _run_native_action(
            [_linux_opener(action="reveal"), str(path.parent)],
            action="reveal",
        )
    else:
        raise DocumentActionError(
            "unsupported_platform",
            "Revealing local PDFs is not supported on this operating system.",
        )


@transaction.atomic
def record_successful_open(document_id: int, at: datetime | None = None) -> PDFDocument:
    """Atomically count a successful native open for an active PDF."""

    open_time = at or timezone.now()
    if timezone.is_naive(open_time):
        raise DocumentActionError(
            "invalid_open_time",
            "The PDF open time must include a timezone.",
        )
    date_value = models.Value(open_time, output_field=models.DateTimeField())
    updated = PDFDocument.objects.filter(
        pk=document_id,
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
    ).update(
        open_count=models.F("open_count") + 1,
        first_opened_at=Coalesce("first_opened_at", date_value),
        last_opened_at=open_time,
    )
    if updated != 1:
        raise DocumentActionError(
            "document_unavailable",
            "This PDF is no longer available in the managed repository.",
        )
    return PDFDocument.objects.select_related("repository").get(pk=document_id)


def _registered_document(document_id: int) -> PDFDocument:
    try:
        return PDFDocument.objects.select_related("repository").get(pk=document_id)
    except (PDFDocument.DoesNotExist, TypeError, ValueError, OverflowError) as exc:
        raise DocumentActionError(
            "document_not_found",
            "The requested PDF is not registered in OWL.",
        ) from exc


def _registered_documents(document_ids: Sequence[int]) -> tuple[PDFDocument, ...]:
    """Resolve a bounded ordered selection without trusting caller-provided IDs."""

    if isinstance(document_ids, (str, bytes)) or not isinstance(document_ids, Sequence):
        raise DocumentActionError(
            "invalid_document_selection",
            "Select one or more PDFs from the current results page.",
        )
    if not document_ids:
        raise DocumentActionError(
            "invalid_document_selection",
            "Select one or more PDFs from the current results page.",
        )
    if len(document_ids) > MAX_SEARCH_PAGE_SIZE:
        raise DocumentActionError(
            "too_many_documents",
            f"Open All supports at most {MAX_SEARCH_PAGE_SIZE} PDFs at a time.",
        )

    ordered_ids: list[int] = []
    seen: set[int] = set()
    for document_id in document_ids:
        if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id <= 0:
            raise DocumentActionError(
                "invalid_document_selection",
                "The selected PDF list is not valid.",
            )
        if document_id not in seen:
            seen.add(document_id)
            ordered_ids.append(document_id)

    documents_by_id = PDFDocument.objects.select_related("repository").in_bulk(ordered_ids)
    if len(documents_by_id) != len(ordered_ids):
        raise DocumentActionError(
            "document_not_found",
            "One or more selected PDFs are not registered in OWL.",
        )
    return tuple(documents_by_id[document_id] for document_id in ordered_ids)


@contextmanager
def _locked_documents(
    documents: Sequence[PDFDocument],
) -> Iterator[tuple[PDFDocument, ...]]:
    """Re-read registered documents while their checkouts cannot be mutated."""

    document_ids = tuple(document.pk for document in documents)
    repository_ids = tuple(document.repository_id for document in documents)
    try:
        with repository_checkout_locks(repository_ids, blocking=False):
            yield _registered_documents(document_ids)
    except RepositoryCheckoutBusy as exc:
        raise DocumentActionError(
            "repository_refresh_in_progress",
            "A selected repository is refreshing. Retry when its background update finishes.",
        ) from exc


def open_registered_pdf(document_id: int) -> PDFDocument:
    """Validate and open one registered PDF, then atomically record the success."""

    registered = _registered_document(document_id)
    with _locked_documents((registered,)) as documents:
        document = documents[0]
        path = validated_pdf_path(document)
        open_pdf_native(path)
        return record_successful_open(document.pk)


def open_registered_pdfs(document_ids: Sequence[int]) -> BulkDocumentOpenResult:
    """Validate a complete selection, then open and count each successful dispatch.

    Every managed path is resolved before any operating-system action occurs. Once
    validation succeeds, one native failure does not prevent the remaining valid
    PDFs from opening, and only successful native dispatches increment usage.
    """

    registered = _registered_documents(document_ids)
    with _locked_documents(registered) as documents:
        validated_documents = tuple(
            (document, validated_pdf_path(document)) for document in documents
        )
        opened: list[PDFDocument] = []
        failures: list[BulkDocumentOpenFailure] = []
        usage_failures: list[BulkDocumentUsageFailure] = []
        for document, path in validated_documents:
            try:
                open_pdf_native(path)
            except DocumentActionError as exc:
                failures.append(
                    BulkDocumentOpenFailure(
                        document_id=document.pk,
                        code=exc.code,
                        summary=exc.summary,
                    )
                )
                continue
            try:
                opened.append(record_successful_open(document.pk))
            except DocumentActionError as exc:
                # The OS already accepted this open. Preserve that accurate
                # outcome, continue the batch, and report the separate usage
                # bookkeeping failure instead of claiming the open failed.
                opened.append(document)
                usage_failures.append(
                    BulkDocumentUsageFailure(
                        document_id=document.pk,
                        code=exc.code,
                        summary=exc.summary,
                    )
                )
            except Exception:
                logger.exception(
                    "A successful native PDF open could not be recorded",
                    extra={"document_id": document.pk},
                )
                opened.append(document)
                usage_failures.append(
                    BulkDocumentUsageFailure(
                        document_id=document.pk,
                        code="usage_record_failed",
                        summary="This PDF opened, but OWL could not record its usage.",
                    )
                )

    return BulkDocumentOpenResult(
        requested_count=len(documents),
        opened_documents=tuple(opened),
        failures=tuple(failures),
        usage_failures=tuple(usage_failures),
    )


def reveal_registered_pdf(document_id: int) -> PDFDocument:
    """Validate and reveal one registered PDF without changing its open count."""

    registered = _registered_document(document_id)
    with _locked_documents((registered,)) as documents:
        document = documents[0]
        path = validated_pdf_path(document)
        reveal_pdf_in_folder(path)
        return document
