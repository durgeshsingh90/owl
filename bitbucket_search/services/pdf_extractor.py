"""Bounded, database-free PDF text extraction for an isolated worker process.

The public :func:`extract_pdf` callable is intentionally independent of Django and
accepts every corpus-dependent limit as an argument.  :func:`main` adds a small JSON
stdin/stdout boundary so a supervising worker can apply an OS-level timeout and memory
limit without importing the PDF parser into the web process.

Extracted text is returned only as staging data.  It is never included in exceptions,
diagnostics, or object representations, and this module does not write log records.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

PDF_EXTRACTOR_VERSION = "pypdf-page-text-v1"

_HASH_CHUNK_BYTES = 1024 * 1024
_LFS_POINTER_MAX_BYTES = 8_192
_JSON_REQUEST_MAX_BYTES = 16 * 1024
_LFS_VERSION = b"version https://git-lfs.github.com/spec/v1"
_LFS_OID = re.compile(rb"^oid sha256:[0-9a-f]{64}$", re.MULTILINE)
_LFS_SIZE = re.compile(rb"^size [0-9]+$", re.MULTILINE)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PDFExtractionState(StrEnum):
    """Terminal state returned by one isolated extraction attempt."""

    READY = "ready"
    NO_TEXT = "no_text"
    PARTIAL = "partial"
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    GIT_LFS_POINTER = "git_lfs_pointer"
    DISAPPEARED = "disappeared"
    PERMISSION_DENIED = "permission_denied"
    RESOURCE_LIMIT = "resource_limit"
    CHANGED = "changed_during_extraction"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    UNKNOWN_ERROR = "unknown_error"


class PDFPageState(StrEnum):
    """Text outcome for one one-based PDF page."""

    READY = "ready"
    NO_TEXT = "no_text"
    FAILED = "failed"


DIAGNOSTIC_STAGES = frozenset(
    {"", "fingerprint_before", "fingerprint_after", "load_parser", "read_pdf", "extract_page"}
)
DIAGNOSTIC_REASONS = frozenset(
    {
        "",
        "module_not_found",
        "import_error",
        "permission_error",
        "file_not_found",
        "os_error",
        "memory_error",
        "recursion_error",
        "overflow_error",
        "value_error",
        "type_error",
        "lookup_error",
        "attribute_error",
        "runtime_error",
        "parser_error",
        "dependency_error",
        "unexpected_error",
    }
)


@dataclass(frozen=True, slots=True)
class PDFExtractionDiagnostic:
    """Fixed vocabulary only; never carry parser messages, paths or class names."""

    stage: str = ""
    reason: str = ""
    errno: int | None = None
    winerror: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "errno": self.errno,
            "winerror": self.winerror,
        }


def _diagnostic_for_exception(exc: Exception, stage: str) -> PDFExtractionDiagnostic:
    categories = (
        (ModuleNotFoundError, "module_not_found"),
        (ImportError, "import_error"),
        (PermissionError, "permission_error"),
        (FileNotFoundError, "file_not_found"),
        (OSError, "os_error"),
        (MemoryError, "memory_error"),
        (RecursionError, "recursion_error"),
        (OverflowError, "overflow_error"),
        (ValueError, "value_error"),
        (TypeError, "type_error"),
        (LookupError, "lookup_error"),
        (AttributeError, "attribute_error"),
        (RuntimeError, "runtime_error"),
    )
    reason = next(
        (label for kind, label in categories if isinstance(exc, kind)), "unexpected_error"
    )
    hierarchy_names = {kind.__name__ for kind in type(exc).__mro__}
    if "DependencyError" in hierarchy_names:
        reason = "dependency_error"
    elif hierarchy_names & {"PdfReadError", "PdfStreamError", "EmptyFileError"}:
        reason = "parser_error"

    def numeric_code(name: str) -> int | None:
        value = getattr(exc, name, None) if isinstance(exc, OSError) else None
        return value if type(value) is int and 0 <= value <= 2**31 - 1 else None

    return PDFExtractionDiagnostic(
        stage=stage if stage in DIAGNOSTIC_STAGES else "",
        reason=reason,
        errno=numeric_code("errno"),
        winerror=numeric_code("winerror"),
    )


@dataclass(frozen=True, slots=True)
class ExtractedPDFPage:
    page_number: int
    text: str = field(repr=False)
    character_count: int
    state: PDFPageState
    error_code: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "character_count": self.character_count,
            "state": self.state.value,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class PDFExtractionResult:
    state: PDFExtractionState
    pages: tuple[ExtractedPDFPage, ...] = ()
    page_count: int = 0
    extracted_character_count: int = 0
    source_size_bytes: int = 0
    content_sha256_before: str = ""
    content_sha256_after: str = ""
    error_code: str = ""
    error_summary: str = ""
    extractor_version: str = PDF_EXTRACTOR_VERSION
    diagnostic: PDFExtractionDiagnostic = field(default_factory=PDFExtractionDiagnostic)

    @property
    def publishable(self) -> bool:
        """Whether the staged page rows are safe to publish transactionally."""

        return self.state in {
            PDFExtractionState.READY,
            PDFExtractionState.NO_TEXT,
            PDFExtractionState.PARTIAL,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "pages": [page.to_payload() for page in self.pages],
            "page_count": self.page_count,
            "extracted_character_count": self.extracted_character_count,
            "source_size_bytes": self.source_size_bytes,
            "content_sha256_before": self.content_sha256_before,
            "content_sha256_after": self.content_sha256_after,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "extractor_version": self.extractor_version,
            "publishable": self.publishable,
            "diagnostic": self.diagnostic.to_payload(),
        }


class _PDFPage(Protocol):
    def extract_text(self) -> str | None: ...


class _PDFReader(Protocol):
    is_encrypted: bool
    pages: Sequence[_PDFPage]


type PDFReaderFactory = Callable[[BinaryIO], _PDFReader]


@dataclass(frozen=True, slots=True)
class _Fingerprint:
    sha256: str
    size: int
    prefix: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ExtractionAbort(Exception):
    state: PDFExtractionState
    diagnostic: PDFExtractionDiagnostic = field(default_factory=PDFExtractionDiagnostic)


_FIXED_DIAGNOSTICS: Mapping[PDFExtractionState, tuple[str, str]] = {
    PDFExtractionState.NO_TEXT: (
        "no_text",
        "The PDF contains no machine-readable text. OCR is not enabled.",
    ),
    PDFExtractionState.PARTIAL: (
        "partial_extraction",
        "OWL could not extract text from one or more PDF pages.",
    ),
    PDFExtractionState.ENCRYPTED: (
        "encrypted_pdf",
        "The PDF is encrypted or password-protected.",
    ),
    PDFExtractionState.CORRUPT: (
        "corrupt_pdf",
        "The PDF is invalid or corrupt.",
    ),
    PDFExtractionState.GIT_LFS_POINTER: (
        "git_lfs_pointer",
        "The file is a Git LFS pointer, not a hydrated PDF.",
    ),
    PDFExtractionState.DISAPPEARED: (
        "pdf_disappeared",
        "The PDF disappeared during extraction.",
    ),
    PDFExtractionState.PERMISSION_DENIED: (
        "pdf_permission_denied",
        "OWL does not have permission to read the PDF.",
    ),
    PDFExtractionState.RESOURCE_LIMIT: (
        "pdf_resource_limit",
        "The PDF exceeds a configured extraction resource limit.",
    ),
    PDFExtractionState.CHANGED: (
        "pdf_changed_during_extraction",
        "The PDF changed during extraction and must be retried.",
    ),
    PDFExtractionState.UNKNOWN_ERROR: (
        "pdf_unknown_error",
        "OWL could not extract this PDF safely.",
    ),
    PDFExtractionState.DEPENDENCY_UNAVAILABLE: (
        "pdf_dependency_unavailable",
        "The PDF parser or a required dependency could not load. "
        "Install the project requirements in the Python environment running OWL, restart workers, "
        "then Refresh to retry.",
    ),
}


def _fixed_diagnostic(state: PDFExtractionState) -> tuple[str, str]:
    return _FIXED_DIAGNOSTICS.get(state, ("", ""))


def _result(
    state: PDFExtractionState,
    *,
    pages: tuple[ExtractedPDFPage, ...] = (),
    page_count: int = 0,
    extracted_character_count: int = 0,
    source_size_bytes: int = 0,
    content_sha256_before: str = "",
    content_sha256_after: str = "",
    diagnostic: PDFExtractionDiagnostic | None = None,
) -> PDFExtractionResult:
    error_code, error_summary = _fixed_diagnostic(state)
    return PDFExtractionResult(
        state=state,
        pages=pages,
        page_count=page_count,
        extracted_character_count=extracted_character_count,
        source_size_bytes=source_size_bytes,
        content_sha256_before=content_sha256_before,
        content_sha256_after=content_sha256_after,
        error_code=error_code,
        error_summary=error_summary,
        diagnostic=diagnostic or PDFExtractionDiagnostic(),
    )


def _positive_limit(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _state_for_exception(exc: Exception) -> PDFExtractionState:
    if isinstance(exc, ImportError):
        return PDFExtractionState.DEPENDENCY_UNAVAILABLE
    if isinstance(exc, (MemoryError, RecursionError, OverflowError)):
        return PDFExtractionState.RESOURCE_LIMIT
    if isinstance(exc, OSError):
        if isinstance(exc, FileNotFoundError) or exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return PDFExtractionState.DISAPPEARED
        if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
            return PDFExtractionState.PERMISSION_DENIED
        if exc.errno in {errno.EFBIG, errno.ENOMEM}:
            return PDFExtractionState.RESOURCE_LIMIT

    # pypdf's exception surface has changed names across supported releases.  Use
    # the exception hierarchy names without importing pypdf into the web process.
    hierarchy_names = {item.__name__ for item in type(exc).__mro__}
    if "DependencyError" in hierarchy_names:
        return PDFExtractionState.DEPENDENCY_UNAVAILABLE
    if hierarchy_names & {
        "FileNotDecryptedError",
        "WrongPasswordError",
        "PasswordError",
    }:
        return PDFExtractionState.ENCRYPTED
    if hierarchy_names & {
        "PdfReadError",
        "PdfStreamError",
        "EmptyFileError",
    }:
        return PDFExtractionState.CORRUPT
    return PDFExtractionState.UNKNOWN_ERROR


def _fingerprint_file(path: Path, *, max_file_bytes: int) -> _Fingerprint:
    """Hash one bounded snapshot and retain only enough prefix for LFS detection."""

    initial = path.stat()
    if not stat.S_ISREG(initial.st_mode):
        raise _ExtractionAbort(PDFExtractionState.CORRUPT)
    if initial.st_size > max_file_bytes:
        raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)

    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_file_bytes:
                raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)
            digest.update(chunk)
            if len(prefix) < _LFS_POINTER_MAX_BYTES:
                missing = _LFS_POINTER_MAX_BYTES - len(prefix)
                prefix.extend(chunk[:missing])
    return _Fingerprint(digest.hexdigest(), size, bytes(prefix))


def _validated_parent_fingerprint(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    max_file_bytes: int,
) -> _Fingerprint:
    """Reuse the parent's full hash while retaining local LFS/type checks."""

    if not _SHA256.fullmatch(expected_sha256):
        raise ValueError("expected_content_sha256 must be a SHA-256 digest.")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or expected_size > max_file_bytes
    ):
        raise ValueError("expected_source_size is invalid.")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise _ExtractionAbort(PDFExtractionState.CORRUPT)
    if metadata.st_size != expected_size:
        raise _ExtractionAbort(PDFExtractionState.CHANGED)
    with path.open("rb") as source:
        prefix = source.read(_LFS_POINTER_MAX_BYTES)
    return _Fingerprint(expected_sha256, expected_size, prefix)


def _is_git_lfs_pointer(fingerprint: _Fingerprint) -> bool:
    if fingerprint.size <= 0 or fingerprint.size > _LFS_POINTER_MAX_BYTES:
        return False
    normalized = fingerprint.prefix.replace(b"\r\n", b"\n")
    return (
        normalized.startswith(_LFS_VERSION + b"\n")
        and _LFS_OID.search(normalized) is not None
        and _LFS_SIZE.search(normalized) is not None
    )


def normalize_pdf_text(value: str) -> str:
    """NFKC-normalize text and collapse untrusted whitespace/control runs."""

    normalized = unicodedata.normalize("NFKC", value)
    safe_characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character.isspace() or category in {"Cc", "Cf", "Cs"}:
            safe_characters.append(" ")
        else:
            safe_characters.append(character)
    return " ".join("".join(safe_characters).split())


def _default_reader_factory(source: BinaryIO) -> _PDFReader:
    # Lazy import is deliberate: callers can inspect/queue extraction work without
    # loading pypdf, and the supervising process is the parser isolation boundary.
    try:
        from pypdf import PdfReader
    except Exception as exc:
        # An import/startup failure affects the installation, not the PDF bytes.
        # Distinguish it even if a binary dependency raises OSError/RuntimeError.
        raise _ExtractionAbort(
            PDFExtractionState.RESOURCE_LIMIT
            if isinstance(exc, (MemoryError, RecursionError, OverflowError))
            else PDFExtractionState.DEPENDENCY_UNAVAILABLE,
            _diagnostic_for_exception(exc, "load_parser"),
        ) from None

    return PdfReader(source, strict=False)


def _extract_pages(
    path: Path,
    *,
    max_pages: int,
    max_page_characters: int,
    max_characters: int,
    reader_factory: PDFReaderFactory,
) -> tuple[tuple[ExtractedPDFPage, ...], int, int, bool, PDFExtractionDiagnostic | None]:
    pages: list[ExtractedPDFPage] = []
    normalized_character_count = 0
    raw_character_count = 0
    had_page_failure = False
    first_page_diagnostic = None

    with path.open("rb") as source:
        reader = reader_factory(source)
        if bool(reader.is_encrypted):
            raise _ExtractionAbort(PDFExtractionState.ENCRYPTED)
        page_count = len(reader.pages)
        if page_count > max_pages:
            raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)

        for page_number, page in enumerate(reader.pages, start=1):
            try:
                raw_text = page.extract_text()
                if raw_text is None:
                    raw_text = ""
                if not isinstance(raw_text, str):
                    raise TypeError("The PDF parser returned non-text page content.")
                if len(raw_text) > max_page_characters:
                    raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)
                raw_character_count += len(raw_text)
                if raw_character_count > max_characters:
                    raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)
                text = normalize_pdf_text(raw_text)
                if len(text) > max_page_characters:
                    raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)
                normalized_character_count += len(text)
                if normalized_character_count > max_characters:
                    raise _ExtractionAbort(PDFExtractionState.RESOURCE_LIMIT)
            except _ExtractionAbort:
                raise
            except Exception as exc:
                failure_state = _state_for_exception(exc)
                if failure_state in {
                    PDFExtractionState.ENCRYPTED,
                    PDFExtractionState.DISAPPEARED,
                    PDFExtractionState.PERMISSION_DENIED,
                    PDFExtractionState.RESOURCE_LIMIT,
                    PDFExtractionState.DEPENDENCY_UNAVAILABLE,
                }:
                    raise _ExtractionAbort(
                        failure_state, _diagnostic_for_exception(exc, "extract_page")
                    ) from None
                had_page_failure = True
                first_page_diagnostic = first_page_diagnostic or _diagnostic_for_exception(
                    exc, "extract_page"
                )
                pages.append(
                    ExtractedPDFPage(
                        page_number=page_number,
                        text="",
                        character_count=0,
                        state=PDFPageState.FAILED,
                        error_code=(
                            "corrupt_page"
                            if failure_state == PDFExtractionState.CORRUPT
                            else "page_extraction_failed"
                        ),
                    )
                )
                continue

            page_state = PDFPageState.READY if text else PDFPageState.NO_TEXT
            pages.append(
                ExtractedPDFPage(
                    page_number=page_number,
                    text=text,
                    character_count=len(text),
                    state=page_state,
                )
            )

    return (
        tuple(pages),
        page_count,
        normalized_character_count,
        had_page_failure,
        first_page_diagnostic,
    )


def extract_pdf(
    path: str | os.PathLike[str],
    *,
    max_file_bytes: int,
    max_pages: int,
    max_characters: int,
    max_page_characters: int | None = None,
    reader_factory: PDFReaderFactory | None = None,
    expected_content_sha256: str | None = None,
    expected_source_size: int | None = None,
) -> PDFExtractionResult:
    """Extract one PDF into immutable staging rows without touching a database.

    The caller must validate that ``path`` belongs to a managed repository before
    starting the isolated process.  OS-level time and memory limits remain the
    supervising worker's responsibility; this boundary enforces file, page, and
    character limits and converts parser/IO failures to content-free state codes.
    """

    file_limit = _positive_limit(max_file_bytes, "max_file_bytes")
    page_limit = _positive_limit(max_pages, "max_pages")
    character_limit = _positive_limit(max_characters, "max_characters")
    page_character_limit = _positive_limit(
        max_page_characters if max_page_characters is not None else max_characters,
        "max_page_characters",
    )
    candidate = Path(path)

    if (expected_content_sha256 is None) is not (expected_source_size is None):
        raise ValueError("Expected fingerprint fields must be supplied together.")
    if expected_content_sha256 is not None and (
        not isinstance(expected_content_sha256, str)
        or not _SHA256.fullmatch(expected_content_sha256)
    ):
        raise ValueError("expected_content_sha256 must be a SHA-256 digest.")
    if expected_source_size is not None and (
        isinstance(expected_source_size, bool)
        or not isinstance(expected_source_size, int)
        or expected_source_size < 0
        or expected_source_size > file_limit
    ):
        raise ValueError("expected_source_size is invalid.")
    try:
        before = (
            _validated_parent_fingerprint(
                candidate,
                expected_sha256=expected_content_sha256,
                expected_size=expected_source_size,
                max_file_bytes=file_limit,
            )
            if expected_content_sha256 is not None and expected_source_size is not None
            else _fingerprint_file(candidate, max_file_bytes=file_limit)
        )
    except _ExtractionAbort as exc:
        return _result(exc.state, diagnostic=exc.diagnostic)
    except Exception as exc:
        return _result(
            _state_for_exception(exc),
            diagnostic=_diagnostic_for_exception(exc, "fingerprint_before"),
        )

    if _is_git_lfs_pointer(before):
        try:
            after = _fingerprint_file(candidate, max_file_bytes=file_limit)
        except _ExtractionAbort as exc:
            return _result(
                exc.state,
                source_size_bytes=before.size,
                content_sha256_before=before.sha256,
                diagnostic=exc.diagnostic,
            )
        except Exception as exc:
            return _result(
                _state_for_exception(exc),
                source_size_bytes=before.size,
                content_sha256_before=before.sha256,
                diagnostic=_diagnostic_for_exception(exc, "fingerprint_after"),
            )
        if before.sha256 != after.sha256 or before.size != after.size:
            return _result(
                PDFExtractionState.CHANGED,
                source_size_bytes=before.size,
                content_sha256_before=before.sha256,
                content_sha256_after=after.sha256,
            )
        return _result(
            PDFExtractionState.GIT_LFS_POINTER,
            source_size_bytes=before.size,
            content_sha256_before=before.sha256,
            content_sha256_after=after.sha256,
        )

    extracted_pages: tuple[ExtractedPDFPage, ...] = ()
    page_count = 0
    character_count = 0
    had_page_failure = False
    terminal_state: PDFExtractionState | None = None
    diagnostic = None
    try:
        extracted_pages, page_count, character_count, had_page_failure, diagnostic = _extract_pages(
            candidate,
            max_pages=page_limit,
            max_page_characters=page_character_limit,
            max_characters=character_limit,
            reader_factory=reader_factory or _default_reader_factory,
        )
    except _ExtractionAbort as exc:
        terminal_state = exc.state
        diagnostic = exc.diagnostic
    except Exception as exc:
        terminal_state = _state_for_exception(exc)
        diagnostic = _diagnostic_for_exception(exc, "read_pdf")

    try:
        after = _fingerprint_file(candidate, max_file_bytes=file_limit)
    except _ExtractionAbort as exc:
        return _result(
            exc.state,
            source_size_bytes=before.size,
            content_sha256_before=before.sha256,
            diagnostic=exc.diagnostic,
        )
    except Exception as exc:
        return _result(
            _state_for_exception(exc),
            source_size_bytes=before.size,
            content_sha256_before=before.sha256,
            diagnostic=_diagnostic_for_exception(exc, "fingerprint_after"),
        )

    if before.sha256 != after.sha256 or before.size != after.size:
        return _result(
            PDFExtractionState.CHANGED,
            source_size_bytes=before.size,
            content_sha256_before=before.sha256,
            content_sha256_after=after.sha256,
            diagnostic=diagnostic,
        )
    if terminal_state is not None:
        return _result(
            terminal_state,
            source_size_bytes=before.size,
            content_sha256_before=before.sha256,
            content_sha256_after=after.sha256,
            diagnostic=diagnostic,
        )

    state = (
        PDFExtractionState.PARTIAL
        if had_page_failure
        else PDFExtractionState.READY
        if character_count
        else PDFExtractionState.NO_TEXT
    )
    return _result(
        state,
        pages=extracted_pages,
        page_count=page_count,
        extracted_character_count=character_count,
        source_size_bytes=before.size,
        content_sha256_before=before.sha256,
        content_sha256_after=after.sha256,
        diagnostic=diagnostic,
    )


def extract_pdf_request(
    payload: Mapping[str, object],
    *,
    reader_factory: PDFReaderFactory | None = None,
) -> dict[str, object]:
    """Validate one JSON-compatible subprocess request and return its result payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("The extraction request must be a JSON object.")
    required = {"path", "max_file_bytes", "max_pages", "max_characters"}
    optional = {"max_page_characters", "expected_content_sha256", "expected_source_size"}
    supplied = set(payload)
    if not required <= supplied or not supplied - required <= optional:
        raise ValueError("The extraction request has unsupported or missing fields.")
    expected_fields = {"expected_content_sha256", "expected_source_size"}
    if bool(supplied & expected_fields) and not expected_fields <= supplied:
        raise ValueError("The extraction request expected fingerprint is incomplete.")
    raw_path = payload["path"]
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096 or "\x00" in raw_path:
        raise ValueError("The extraction path is invalid.")
    result = extract_pdf(
        raw_path,
        max_file_bytes=_positive_limit(payload["max_file_bytes"], "max_file_bytes"),
        max_pages=_positive_limit(payload["max_pages"], "max_pages"),
        max_characters=_positive_limit(payload["max_characters"], "max_characters"),
        max_page_characters=(
            _positive_limit(payload["max_page_characters"], "max_page_characters")
            if "max_page_characters" in payload
            else None
        ),
        reader_factory=reader_factory,
        expected_content_sha256=payload.get("expected_content_sha256"),
        expected_source_size=payload.get("expected_source_size"),
    )
    return result.to_payload()


def _invalid_request_payload() -> dict[str, object]:
    result = _result(PDFExtractionState.UNKNOWN_ERROR).to_payload()
    result["error_code"] = "invalid_extraction_request"
    result["error_summary"] = "The isolated PDF extraction request is invalid."
    return result


def main() -> int:
    """Read one bounded JSON request from stdin and write one JSON result to stdout."""

    try:
        raw_request = sys.stdin.buffer.read(_JSON_REQUEST_MAX_BYTES + 1)
        if len(raw_request) > _JSON_REQUEST_MAX_BYTES:
            raise ValueError("Request too large")
        payload = json.loads(raw_request.decode("utf-8"))
        result = extract_pdf_request(payload)
    except Exception:
        # The subprocess boundary must never emit a traceback, source path, parser
        # message, or extracted text in response to malformed control input.
        result = _invalid_request_payload()
        exit_code = 2
    else:
        exit_code = 0
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
