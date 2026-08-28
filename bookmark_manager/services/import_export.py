"""Versioned, secret-free bookmark JSON import and export services.

The import boundary accepts current OWL exports and heterogeneous legacy collections.
Records are normalized before processing so new OWL numbers are deterministic, while
each database write has its own transaction so one malformed record cannot roll back
the valid collection.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from django.db import IntegrityError, transaction
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkActivityType,
    BookmarkAvailability,
    BookmarkImportFailure,
    BookmarkImportRun,
    BookmarkImportStatus,
    ConfluencePageNode,
    Tag,
)
from bookmark_manager.services.bookmark_analytics import record_daily_activity
from bookmark_manager.services.bookmark_application import BookmarkActionError, save_bookmark_input
from bookmark_manager.services.bookmark_domain import InvalidPageIdentity, normalize_page_id
from bookmark_manager.services.bookmark_outline import (
    ensure_outline_position,
    next_outline_position,
)
from bookmark_manager.services.configuration import ConfigurationUnavailable, get_active_profile
from bookmark_manager.services.confluence_validation import PageInputError, parse_page_input
from core.logging import redact_log_text

DOCUMENT_TYPE = "owl.bookmark-export"
EXPORT_SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_NOTE_LENGTH = 250_000
MAX_TAGS_PER_BOOKMARK = 100
MAX_HIERARCHY_DEPTH = 100
MAX_TEXT_IMPORT_URLS = 5_000

_MISSING = object()
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]*$")
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?i)(?:auth|authorization|cookie|credential|key|pass|pat|secret|session|token)"
)
_PAGE_PATH_PATTERNS = (
    re.compile(r"/(?:pages|content)/0*([1-9][0-9]*)(?:/|$)", re.IGNORECASE),
    re.compile(r"/viewpage\.action(?:/|$)", re.IGNORECASE),
)
_TEXT_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class BookmarkImportError(ValueError):
    """Base error for rejected import input."""


class ImportDocumentError(BookmarkImportError):
    """Raised when the JSON document itself is unsafe or unsupported."""


class ImportRecordError(BookmarkImportError):
    """A deliberately sanitized reason for rejecting one record."""


@dataclass(frozen=True, slots=True)
class BookmarkImportResult:
    """Durable outcome of a completed record-by-record import."""

    run: BookmarkImportRun

    @property
    def total_records(self) -> int:
        return self.run.total_records

    @property
    def imported_records(self) -> int:
        return self.run.imported_records

    @property
    def skipped_records(self) -> int:
        return self.run.skipped_records

    @property
    def failed_records(self) -> int:
        return self.run.failed_records

    def sanitized_failure_report(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "record_number": failure.record_number,
                "page_id": failure.page_id,
                "source_url": failure.source_url,
                "reason": failure.reason,
            }
            for failure in self.run.failures.order_by("record_number", "pk")
        )


@dataclass(frozen=True, slots=True)
class _NormalizedNode:
    title: str
    page_id: str | None = None
    url: str = ""
    space_key: str = ""
    sibling_position: int | None = None


@dataclass(frozen=True, slots=True)
class _NormalizedRecord:
    record_number: int
    page_id: str
    title: str
    url: str
    space_name: str
    space_key: str
    version: int
    created_at: datetime | None
    updated_at: datetime | None
    created_by_id: str
    created_by_name: str
    modified_by_id: str
    modified_by_name: str
    author_id: str
    author_name: str
    hierarchy: tuple[_NormalizedNode, ...]
    saved_at: datetime | None
    favorite: bool
    pinned: bool
    notes: str
    notes_updated_at: datetime | None
    tags: tuple[str, ...]
    open_count: int
    first_opened_at: datetime | None
    last_viewed_at: datetime | None
    last_viewed_version: int | None
    last_refresh_attempt_at: datetime | None
    last_refreshed_at: datetime | None
    last_change_detected_at: datetime | None
    availability_status: str
    last_error_code: str
    last_error_message: str
    last_error_at: datetime | None
    legacy_number: str
    canonical_owl_number: int | None


def export_bookmarks_document(*, generated_at: datetime | None = None) -> dict[str, object]:
    """Return a complete versioned bookmark export containing no configuration.

    Only an explicit allowlist of canonical bookmark fields is serialized.  In
    particular, configuration rows, credential-store state, headers, cookies, and
    environment values are never queried.
    """

    generation_time = generated_at or timezone.now()
    if not isinstance(generation_time, datetime) or timezone.is_naive(generation_time):
        raise ValueError("The export generation time must include a timezone.")

    nodes = {node.pk: node for node in ConfluencePageNode.objects.all()}
    bookmarks = list(Bookmark.objects.prefetch_related("tags").order_by("pk"))
    records = [_export_record(bookmark, nodes) for bookmark in bookmarks]
    content: dict[str, object] = {
        "document_type": DOCUMENT_TYPE,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generation_time),
        "record_count": len(records),
        "bookmarks": records,
    }
    content["integrity"] = {
        "algorithm": "sha256",
        "content_sha256": _document_checksum(content),
    }
    return content


def export_bookmarks_json(*, generated_at: datetime | None = None, indent: int = 2) -> str:
    """Serialize a current export as stable UTF-8-compatible JSON text."""

    return json.dumps(
        export_bookmarks_document(generated_at=generated_at),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    )


def import_bookmarks_document(
    payload: bytes | str | Mapping[str, object] | Sequence[object],
    *,
    filename: str = "bookmark-import.json",
    imported_at: datetime | None = None,
    batch_size: int = 100,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> BookmarkImportResult:
    """Safely merge one current or legacy JSON collection into the local database."""

    normalized_batch_size = _bounded_document_integer(
        batch_size,
        field_name="batch size",
        maximum=5_000,
    )
    normalized_max_bytes = _bounded_document_integer(
        max_bytes,
        field_name="size limit",
        maximum=MAX_IMPORT_BYTES,
    )

    import_time = imported_at or timezone.now()
    if not isinstance(import_time, datetime) or timezone.is_naive(import_time):
        raise ImportDocumentError("The import time must include a timezone.")

    document, source_bytes = _decode_document(payload, max_bytes=normalized_max_bytes)
    records, schema_version = _extract_records(document)
    _verify_current_export(document)
    safe_filename = _safe_filename(filename)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    run = BookmarkImportRun.objects.create(
        filename=safe_filename,
        schema_version=schema_version,
        source_sha256=source_sha256,
        total_records=len(records),
        status=BookmarkImportStatus.RUNNING,
        started_at=import_time,
    )

    normalized: list[_NormalizedRecord] = []
    for record_number, raw_record in enumerate(records, start=1):
        source_url = _source_url_from_record(raw_record)
        try:
            normalized.append(_normalize_record(raw_record, record_number=record_number))
        except ImportRecordError as exc:
            _record_failure(
                run,
                record_number,
                "",
                str(exc),
                source_url=source_url,
            )
        except Exception:
            _record_failure(
                run,
                record_number,
                "",
                "The record could not be normalized safely.",
                source_url=source_url,
            )

    if schema_version == str(EXPORT_SCHEMA_VERSION):
        normalized.sort(
            key=lambda record: (
                record.canonical_owl_number is None,
                record.canonical_owl_number or record.record_number,
                record.record_number,
            )
        )
    else:
        normalized.sort(key=_legacy_import_order)

    imported = 0
    skipped = 0
    processed = run.failed_records
    for normalized_record in normalized:
        try:
            with transaction.atomic():
                created = _merge_record(
                    normalized_record,
                    run=run,
                    imported_at=import_time,
                )
        except ImportRecordError as exc:
            _record_failure(
                run,
                normalized_record.record_number,
                normalized_record.page_id,
                str(exc),
                source_url=normalized_record.url,
            )
        except IntegrityError:
            _record_failure(
                run,
                normalized_record.record_number,
                normalized_record.page_id,
                "The record conflicts with existing bookmark data.",
                source_url=normalized_record.url,
            )
        except Exception:
            _record_failure(
                run,
                normalized_record.record_number,
                normalized_record.page_id,
                "The record could not be imported safely.",
                source_url=normalized_record.url,
            )
        else:
            if created:
                imported += 1
            else:
                skipped += 1
        processed += 1
        if processed % normalized_batch_size == 0:
            _update_run_progress(run, processed, imported, skipped)

    _update_run_progress(run, len(records), imported, skipped)
    run.completed_at = timezone.now()
    if run.failed_records:
        run.status = BookmarkImportStatus.COMPLETED_WITH_FAILURES
        run.outcome = (
            f"Imported {imported}, skipped {skipped}, and rejected "
            f"{run.failed_records} of {len(records)} records."
        )
    else:
        run.status = BookmarkImportStatus.COMPLETED
        run.outcome = f"Imported {imported} and skipped {skipped} of {len(records)} records."
    run.save(update_fields=["status", "outcome", "completed_at"])
    return BookmarkImportResult(run=run)


def import_bookmarks_text(
    payload: bytes | str,
    *,
    filename: str = "bookmark-import.txt",
    imported_at: datetime | None = None,
    max_bytes: int = MAX_IMPORT_BYTES,
    bookmark_saver=None,
    profile_loader=None,
) -> BookmarkImportResult:
    """Extract HTTP(S) URLs from text and save each independently.

    Probable Confluence URLs must expose one valid Page ID before any save is
    attempted. A visually truncated Confluence link is recoverable when its Page
    ID remains intact: OWL sends that ID through the normal Confluence save path,
    which retrieves the current canonical URL, metadata, and page text. One
    incomplete or unreachable page is recorded without stopping the remaining
    URLs.
    """

    normalized_max_bytes = _bounded_document_integer(
        max_bytes,
        field_name="size limit",
        maximum=MAX_IMPORT_BYTES,
    )
    source_bytes, text = _decode_text_import(payload, max_bytes=normalized_max_bytes)
    urls = _extract_text_urls(text)
    if not urls:
        raise ImportDocumentError("The text import does not contain an HTTP or HTTPS URL.")
    if len(urls) > MAX_TEXT_IMPORT_URLS:
        raise ImportDocumentError("The text import contains too many URLs.")

    import_time = imported_at or timezone.now()
    if not isinstance(import_time, datetime) or timezone.is_naive(import_time):
        raise ImportDocumentError("The import time must include a timezone.")

    run = BookmarkImportRun.objects.create(
        filename=_safe_filename(filename),
        schema_version="text-urls-v1",
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        total_records=len(urls),
        status=BookmarkImportStatus.RUNNING,
        started_at=import_time,
    )
    saver = bookmark_saver or save_bookmark_input
    load_profile = profile_loader or get_active_profile
    profile_loaded = False
    profile = None
    profile_error = ""
    imported = 0
    skipped = 0

    for record_number, url in enumerate(urls, start=1):
        page_id = ""
        try:
            save_input = url
            truncated = _has_truncation_marker(url)
            if _probably_confluence_url(url):
                if not profile_loaded:
                    profile_loaded = True
                    try:
                        profile = load_profile()
                    except ConfigurationUnavailable:
                        profile_error = (
                            "Connect Confluence before importing Confluence URLs from text."
                        )
                if profile is None:
                    raise ImportRecordError(profile_error)
                # Chat/transcript exports often shorten only the visible page title,
                # leaving a trustworthy Page ID in the path. Strip a trailing visual
                # ellipsis only for identity validation, then save by Page ID so the
                # configured Confluence adapter supplies the authoritative full URL.
                confluence_input = _without_trailing_truncation_marker(url)
                parsed = parse_page_input(confluence_input, profile.origin)
                page_id = parsed.page_id
                if truncated:
                    save_input = page_id
            elif truncated:
                raise ImportRecordError("Incomplete or truncated URL.")
            result = saver(save_input)
        except PageInputError as exc:
            _record_failure(
                run,
                record_number,
                page_id,
                f"Incomplete Confluence URL: {exc}",
                source_url=url,
            )
        except (BookmarkActionError, ImportRecordError) as exc:
            _record_failure(
                run,
                record_number,
                page_id,
                str(exc),
                source_url=url,
            )
        except Exception:
            _record_failure(
                run,
                record_number,
                page_id,
                "The bookmark could not be saved.",
                source_url=url,
            )
        else:
            if result.created:
                imported += 1
            else:
                skipped += 1
        _update_run_progress(run, record_number, imported, skipped)

    run.completed_at = timezone.now()
    if run.failed_records:
        run.status = BookmarkImportStatus.COMPLETED_WITH_FAILURES
        run.outcome = (
            f"Completed {imported + skipped} of {len(urls)} extracted URLs; "
            f"added {imported}, already present {skipped}, and incomplete or failed "
            f"{run.failed_records}."
        )
    else:
        run.status = BookmarkImportStatus.COMPLETED
        run.outcome = (
            f"Completed all {len(urls)} extracted URLs; added {imported} and "
            f"already present {skipped}."
        )
    run.save(update_fields=["status", "outcome", "completed_at"])
    return BookmarkImportResult(run=run)


def _decode_text_import(payload: bytes | str, *, max_bytes: int) -> tuple[bytes, str]:
    if isinstance(payload, str):
        source_bytes = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        source_bytes = payload
    else:
        raise ImportDocumentError("The text import must be a UTF-8 text file.")
    if len(source_bytes) > max_bytes:
        raise ImportDocumentError("The text import exceeds the configured size limit.")
    try:
        return source_bytes, source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportDocumentError("The text import must be valid UTF-8.") from exc


def _extract_text_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _TEXT_URL_PATTERN.finditer(text):
        candidate = html.unescape(match.group(0)).rstrip(",;!?")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while candidate.endswith(closing) and candidate.count(closing) > candidate.count(
                opening
            ):
                candidate = candidate[:-1]
        if candidate.endswith(".") and not candidate.endswith("..."):
            candidate = candidate[:-1]
        if candidate and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _has_truncation_marker(value: str) -> bool:
    return "..." in value or "…" in value


def _without_trailing_truncation_marker(value: str) -> str:
    """Remove only a transcript-style trailing ellipsis from a URL.

    Embedded ellipses remain untouched so a malformed or ambiguous link cannot be
    silently reinterpreted. The configured-origin and Page-ID validation still run
    after this small recovery step.
    """

    candidate = value
    while candidate.endswith("…"):
        candidate = candidate[:-1]
    while candidate.endswith("..."):
        candidate = candidate[:-3]
    return candidate


def _probably_confluence_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "confluence" in value.casefold()
    host = (parts.hostname or "").casefold()
    path = parts.path.casefold()
    return (
        "confluence" in host
        or ("/spaces/" in path and "/pages/" in path)
        or "/content/" in path
        or path.endswith("/viewpage.action")
    )


def _safe_failure_source_url(value: object) -> str:
    """Keep an identifying URL without credentials, fragments, or arbitrary query data."""

    if not isinstance(value, str):
        return ""
    candidate = redact_log_text(value).strip()
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        _validated_port = parts.port
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""

    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"

    # A numeric Page ID identifies legacy viewpage.action links without
    # retaining arbitrary query values that might hold credentials.
    page_ids = parse_qs(parts.query).get("pageId", ())
    safe_query = ""
    if len(page_ids) == 1 and str(page_ids[0]).isdecimal():
        safe_query = urlencode({"pageId": str(page_ids[0])})
    reference = urlunsplit((parts.scheme.casefold(), host, parts.path, safe_query, ""))
    return reference[:2048]


def _source_url_from_record(raw_record: object) -> str:
    if not isinstance(raw_record, Mapping):
        return ""
    source = _mapping(raw_record.get("source", raw_record.get("confluence")))
    raw_url = _pick_many(
        (source, raw_record),
        "url",
        "page_url",
        "pageUrl",
        "link",
        default="",
    )
    return _safe_failure_source_url(raw_url)


def _decode_document(
    payload: bytes | str | Mapping[str, object] | Sequence[object],
    *,
    max_bytes: int,
) -> tuple[Mapping[str, object] | Sequence[object], bytes]:
    if isinstance(payload, bytes):
        source_bytes = payload
        if len(source_bytes) > max_bytes:
            raise ImportDocumentError("The JSON import exceeds the configured size limit.")
        try:
            text = source_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportDocumentError("The import must be valid UTF-8 JSON.") from exc
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ImportDocumentError("The import is not a valid JSON document.") from exc
    elif isinstance(payload, str):
        try:
            source_bytes = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ImportDocumentError("The import must be valid UTF-8 JSON.") from exc
        if len(source_bytes) > max_bytes:
            raise ImportDocumentError("The JSON import exceeds the configured size limit.")
        try:
            document = json.loads(payload)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ImportDocumentError("The import is not a valid JSON document.") from exc
    elif isinstance(payload, Mapping) or _is_non_text_sequence(payload):
        document = payload
        try:
            source_bytes = _canonical_json_bytes(document)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ImportDocumentError("The import contains unsupported JSON values.") from exc
        if len(source_bytes) > max_bytes:
            raise ImportDocumentError("The JSON import exceeds the configured size limit.")
    else:
        raise ImportDocumentError("The import root must be a JSON object or array.")

    if not isinstance(document, Mapping) and not _is_non_text_sequence(document):
        raise ImportDocumentError("The import root must be a JSON object or array.")
    return document, source_bytes


def _extract_records(
    document: Mapping[str, object] | Sequence[object],
) -> tuple[list[object], str]:
    if _is_non_text_sequence(document):
        records = list(document)
        schema_version = "legacy"
    else:
        raw_schema = document.get("schema_version", document.get("version", "legacy"))
        schema_version = _safe_schema_version(raw_schema)
        records_value = _pick(document, "bookmarks", "records", "items", "data", default=_MISSING)
        if records_value is _MISSING and _looks_like_record(document):
            records_value = [document]
            schema_version = "legacy"
        if not _is_non_text_sequence(records_value):
            raise ImportDocumentError("The import must contain a JSON array of bookmarks.")
        records = list(records_value)

    if len(records) > MAX_RECORDS:
        raise ImportDocumentError("The import contains too many bookmark records.")
    return records, schema_version


def _verify_current_export(document: Mapping[str, object] | Sequence[object]) -> None:
    if not isinstance(document, Mapping) or document.get("document_type") != DOCUMENT_TYPE:
        return
    if document.get("schema_version") != EXPORT_SCHEMA_VERSION:
        raise ImportDocumentError("This OWL bookmark export schema is not supported.")
    bookmarks = document.get("bookmarks")
    record_count = document.get("record_count")
    if (
        not _is_non_text_sequence(bookmarks)
        or isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count != len(bookmarks)
    ):
        raise ImportDocumentError("The OWL export record count is inconsistent.")
    integrity = document.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ImportDocumentError("The OWL export integrity information is missing.")
    if integrity.get("algorithm") != "sha256":
        raise ImportDocumentError("The OWL export integrity algorithm is not supported.")
    expected = integrity.get("content_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ImportDocumentError("The OWL export integrity checksum is invalid.")
    unsigned = {key: value for key, value in document.items() if key != "integrity"}
    if not _constant_time_text_equal(expected, _document_checksum(unsigned)):
        raise ImportDocumentError("The OWL export integrity check failed.")


def _normalize_record(raw: object, *, record_number: int) -> _NormalizedRecord:
    if not isinstance(raw, Mapping):
        raise ImportRecordError("The record must be a JSON object.")

    source = _mapping(raw.get("source", raw.get("confluence")))
    personal = _mapping(raw.get("personal"))
    usage = _mapping(raw.get("usage"))
    status = _mapping(raw.get("status"))
    provenance = _mapping(raw.get("provenance"))

    raw_page_id = _pick_many(
        (raw, source),
        "page_id",
        "pageId",
        "pageID",
        "confluence_page_id",
        "confluencePageId",
        default=_MISSING,
    )
    raw_url = _pick_many((source, raw), "url", "page_url", "pageUrl", "link", default="")
    if raw_page_id is _MISSING:
        raw_page_id = _page_id_from_url(raw_url)
    if raw_page_id is _MISSING:
        raw_page_id = _pick(raw, "id", default=_MISSING)
    try:
        page_id = normalize_page_id(raw_page_id)
    except (InvalidPageIdentity, TypeError) as exc:
        raise ImportRecordError("The record has no valid positive Confluence Page ID.") from exc

    title = _required_text(
        _pick_many((source, raw), "title", "page_title", "pageTitle", "name", default=""),
        "title",
        maximum=500,
    )
    url = _safe_url(raw_url, required=False)
    space_name = _optional_text(
        _pick_many((source, raw), "space_name", "spaceName", default=""),
        "space name",
        maximum=255,
    )
    space_key = _optional_text(
        _pick_many((source, raw), "space_key", "spaceKey", "space", default=""),
        "space key",
        maximum=255,
    )
    version = _positive_int(
        _pick_many((source, raw), "version", "page_version", "pageVersion", default=1),
        "version",
    )

    hierarchy_value = _pick_many(
        (raw, source),
        "hierarchy",
        "ancestors",
        "breadcrumb",
        "breadcrumbs",
        "path",
        default=(),
    )
    hierarchy = _normalize_hierarchy(hierarchy_value, space_key=space_key, leaf_page_id=page_id)

    raw_tags = _pick_many((personal, raw), "tags", "tag_names", "tagNames", default=())
    notes = _optional_text(
        _pick_many((personal, raw), "notes", "note", "description", default=""),
        "notes",
        maximum=MAX_NOTE_LENGTH,
    )
    saved_at = _optional_datetime(
        _pick_many(
            (personal, raw),
            "saved_at",
            "savedAt",
            "date_saved",
            "dateAdded",
            "added_at",
            default=None,
        ),
        "saved time",
    )

    availability = _optional_text(
        _pick_many(
            (status, raw), "availability_status", "availability", "status", default="active"
        ),
        "availability status",
        maximum=32,
    ).casefold()
    valid_availability = {choice for choice, _label in BookmarkAvailability.choices}
    if availability not in valid_availability:
        raise ImportRecordError("The record has an unsupported availability status.")

    error_code = _optional_text(
        _pick_many((status, raw), "last_error_code", "error_code", default=""),
        "error code",
        maximum=64,
    )
    if not _SAFE_ERROR_CODE.fullmatch(error_code):
        error_code = "imported_error"
    error_message = _sanitize_error_message(
        _pick_many((status, raw), "last_error_message", "error_message", default="")
    )

    canonical_owl_number = _optional_positive_int(
        _pick(raw, "owl_number", "owlNumber", default=None),
        "OWL number",
    )
    raw_legacy_number = _pick_many(
        (provenance, raw),
        "legacy_number",
        "legacyNumber",
        default=canonical_owl_number or "",
    )
    if raw_legacy_number in (None, "") and canonical_owl_number is not None:
        raw_legacy_number = canonical_owl_number

    return _NormalizedRecord(
        record_number=record_number,
        page_id=page_id,
        title=title,
        url=url,
        space_name=space_name,
        space_key=space_key,
        version=version,
        created_at=_optional_datetime(
            _pick_many((source, raw), "created_at", "createdAt", default=None),
            "created time",
        ),
        updated_at=_optional_datetime(
            _pick_many((source, raw), "updated_at", "updatedAt", "modified_at", default=None),
            "updated time",
        ),
        created_by_id=_person_value(source, raw, "created_by", "created_by_id", "id", 255),
        created_by_name=_person_value(source, raw, "created_by", "created_by_name", "name", 500),
        modified_by_id=_person_value(source, raw, "modified_by", "modified_by_id", "id", 255),
        modified_by_name=_person_value(source, raw, "modified_by", "modified_by_name", "name", 500),
        author_id=_person_value(source, raw, "author", "author_id", "id", 255),
        author_name=_person_value(source, raw, "author", "author_name", "name", 500),
        hierarchy=hierarchy,
        saved_at=saved_at,
        favorite=_boolean(
            _pick_many(
                (personal, raw),
                "favorite",
                "favourite",
                "starred",
                "is_favorite",
                default=False,
            ),
            "favorite",
        ),
        pinned=_boolean(
            _pick_many((personal, raw), "pinned", "pin", "is_pinned", default=False),
            "pin",
        ),
        notes=notes,
        notes_updated_at=_optional_datetime(
            _pick_many((personal, raw), "notes_updated_at", "notesUpdatedAt", default=None),
            "notes updated time",
        ),
        tags=_normalize_tags(raw_tags),
        open_count=_non_negative_int(
            _pick_many((usage, raw), "open_count", "openCount", "view_count", "opens", default=0),
            "open count",
        ),
        first_opened_at=_optional_datetime(
            _pick_many((usage, raw), "first_opened_at", "firstOpenedAt", default=None),
            "first opened time",
        ),
        last_viewed_at=_optional_datetime(
            _pick_many(
                (usage, raw), "last_viewed_at", "lastViewedAt", "last_opened_at", default=None
            ),
            "last viewed time",
        ),
        last_viewed_version=_optional_positive_int(
            _pick_many((usage, raw), "last_viewed_version", "lastViewedVersion", default=None),
            "last viewed version",
        ),
        last_refresh_attempt_at=_optional_datetime(
            _pick_many((status, raw), "last_refresh_attempt_at", default=None),
            "last refresh attempt time",
        ),
        last_refreshed_at=_optional_datetime(
            _pick_many((status, raw), "last_refreshed_at", "refreshed_at", default=None),
            "last refreshed time",
        ),
        last_change_detected_at=_optional_datetime(
            _pick_many((status, raw), "last_change_detected_at", default=None),
            "last change time",
        ),
        availability_status=availability,
        last_error_code=error_code,
        last_error_message=error_message,
        last_error_at=_optional_datetime(
            _pick_many((status, raw), "last_error_at", "error_at", default=None),
            "last error time",
        ),
        legacy_number=_optional_text(raw_legacy_number, "legacy number", maximum=64),
        canonical_owl_number=canonical_owl_number,
    )


def _merge_record(
    record: _NormalizedRecord,
    *,
    run: BookmarkImportRun,
    imported_at: datetime,
) -> bool:
    bookmark = Bookmark.objects.select_for_update().filter(page_id=record.page_id).first()
    if bookmark is not None:
        _fill_existing_personal_blanks(bookmark, record, run)
        return False

    tree_node = _build_hierarchy(record)
    bookmark = Bookmark.objects.create(
        page_id=record.page_id,
        tree_node=tree_node,
        title=record.title,
        url=record.url,
        space_name=record.space_name,
        space_key=record.space_key,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        created_by_id=record.created_by_id,
        created_by_name=record.created_by_name,
        modified_by_id=record.modified_by_id,
        modified_by_name=record.modified_by_name,
        author_id=record.author_id,
        author_name=record.author_name,
        saved_at=record.saved_at or imported_at,
        favorite=record.favorite,
        pinned=record.pinned,
        notes=record.notes,
        notes_updated_at=record.notes_updated_at,
        open_count=record.open_count,
        first_opened_at=record.first_opened_at,
        last_viewed_at=record.last_viewed_at,
        last_viewed_version=record.last_viewed_version,
        last_refresh_attempt_at=record.last_refresh_attempt_at,
        last_refreshed_at=record.last_refreshed_at,
        last_change_detected_at=record.last_change_detected_at,
        availability_status=record.availability_status,
        last_error_code=record.last_error_code,
        last_error_message=record.last_error_message,
        last_error_at=record.last_error_at,
        import_run=run,
        import_record_number=record.record_number,
        legacy_number=record.legacy_number,
    )
    record_daily_activity(
        BookmarkActivityType.ADDED,
        occurred_at=bookmark.saved_at,
    )
    _set_tags(bookmark, record.tags)
    return True


def _fill_existing_personal_blanks(
    bookmark: Bookmark,
    record: _NormalizedRecord,
    run: BookmarkImportRun,
) -> None:
    changed: list[str] = []
    for field_name in (
        "title",
        "url",
        "space_name",
        "space_key",
        "created_at",
        "updated_at",
        "created_by_id",
        "created_by_name",
        "modified_by_id",
        "modified_by_name",
        "author_id",
        "author_name",
        "last_refresh_attempt_at",
        "last_refreshed_at",
        "last_change_detected_at",
        "last_error_code",
        "last_error_message",
        "last_error_at",
    ):
        current = getattr(bookmark, field_name)
        incoming = getattr(record, field_name)
        if current in (None, "") and incoming not in (None, ""):
            setattr(bookmark, field_name, incoming)
            changed.append(field_name)
    if not bookmark.notes and record.notes:
        bookmark.notes = record.notes
        changed.append("notes")
        if record.notes_updated_at is not None:
            bookmark.notes_updated_at = record.notes_updated_at
            changed.append("notes_updated_at")
    if not bookmark.legacy_number and record.legacy_number:
        bookmark.legacy_number = record.legacy_number
        changed.append("legacy_number")
    if bookmark.import_run_id is None:
        bookmark.import_run = run
        bookmark.import_record_number = record.record_number
        changed.extend(("import_run", "import_record_number"))
    if changed:
        bookmark.save(update_fields=tuple(dict.fromkeys(changed)))
    if not bookmark.tags.exists() and record.tags:
        _set_tags(bookmark, record.tags)


def _set_tags(bookmark: Bookmark, names: tuple[str, ...]) -> None:
    if not names:
        return
    tags = [Tag.objects.get_or_create_normalized(name)[0] for name in names]
    bookmark.tags.set(tags)


def _build_hierarchy(record: _NormalizedRecord) -> ConfluencePageNode:
    nodes = list(record.hierarchy)
    if (
        nodes
        and nodes[-1].page_id is None
        and _normalized_title(nodes[-1].title) == _normalized_title(record.title)
    ):
        leaf = nodes[-1]
        nodes[-1] = _NormalizedNode(
            title=record.title,
            page_id=record.page_id,
            url=record.url or leaf.url,
            space_key=record.space_key or leaf.space_key,
            sibling_position=leaf.sibling_position,
        )
    if nodes and nodes[-1].page_id == record.page_id:
        leaf = nodes[-1]
        nodes[-1] = _NormalizedNode(
            title=record.title,
            page_id=record.page_id,
            url=record.url or leaf.url,
            space_key=record.space_key or leaf.space_key,
            sibling_position=leaf.sibling_position,
        )
    else:
        nodes.append(
            _NormalizedNode(
                title=record.title,
                page_id=record.page_id,
                url=record.url,
                space_key=record.space_key,
            )
        )

    parent: ConfluencePageNode | None = None
    path_titles: list[str] = []
    for node_data in nodes:
        path_titles.append(node_data.title)
        provisional_key = _provisional_key(record.space_key, path_titles)
        node = _resolve_hierarchy_node(
            node_data,
            parent=parent,
            provisional_key=provisional_key,
        )
        parent = node
    if parent is None:
        raise ImportRecordError("The record could not build a valid hierarchy.")
    return parent


def _resolve_hierarchy_node(
    data: _NormalizedNode,
    *,
    parent: ConfluencePageNode | None,
    provisional_key: str,
) -> ConfluencePageNode:
    node = None
    if data.page_id is not None:
        node = ConfluencePageNode.objects.select_for_update().filter(page_id=data.page_id).first()
        provisional = (
            ConfluencePageNode.objects.select_for_update()
            .filter(provisional_key=provisional_key)
            .first()
        )
        if node is None and provisional is not None:
            provisional.page_id = data.page_id
            provisional.provisional_key = None
            provisional.title = data.title
            provisional.save(
                update_fields=[
                    "page_id",
                    "provisional_key",
                    "title",
                    "metadata_updated_at",
                ]
            )
            node = provisional
        elif node is not None and provisional is not None and node.pk != provisional.pk:
            _merge_provisional_node(provisional, node)
    else:
        node = (
            ConfluencePageNode.objects.select_for_update()
            .filter(provisional_key=provisional_key)
            .first()
        )

    if node is None:
        return ConfluencePageNode.objects.create(
            page_id=data.page_id,
            provisional_key=None if data.page_id is not None else provisional_key,
            title=data.title,
            url=data.url,
            space_key=data.space_key,
            parent=parent,
            sibling_position=data.sibling_position,
            outline_position=next_outline_position(
                parent_id=parent.pk if parent is not None else None
            ),
        )

    changed: list[str] = []
    for field_name, incoming in (
        ("url", data.url),
        ("space_key", data.space_key),
    ):
        if not getattr(node, field_name) and incoming:
            setattr(node, field_name, incoming)
            changed.append(field_name)
    if node.parent_id is None and parent is not None and node.pk != parent.pk:
        node.parent = parent
        node.outline_position = next_outline_position(parent_id=parent.pk)
        changed.append("parent")
        changed.append("outline_position")
    if node.sibling_position is None and data.sibling_position is not None:
        node.sibling_position = data.sibling_position
        changed.append("sibling_position")
    if changed:
        node.save(update_fields=[*changed, "metadata_updated_at"])
    ensure_outline_position(
        node,
        parent_id=node.parent_id,
    )
    return node


def _merge_provisional_node(
    provisional: ConfluencePageNode,
    canonical: ConfluencePageNode,
) -> None:
    if Bookmark.objects.filter(tree_node=provisional).exists():
        return
    if canonical.parent_id == provisional.pk:
        replacement_position = provisional.outline_position
        # The canonical node replaces this hierarchy-only placeholder. Release the
        # placeholder's slot first so the stable branch number can be retained while
        # the database uniqueness constraint remains active.
        provisional.outline_position = None
        provisional.save(update_fields=["outline_position", "metadata_updated_at"])
        canonical.parent_id = provisional.parent_id
        canonical.outline_position = _available_or_next_outline_position(
            canonical,
            parent_id=provisional.parent_id,
            preferred=replacement_position,
        )
        canonical.save(update_fields=["parent", "outline_position", "metadata_updated_at"])
    for child in provisional.children.select_for_update():
        if child.pk == canonical.pk:
            continue
        child.parent = canonical
        child.outline_position = _available_or_next_outline_position(
            child,
            parent_id=canonical.pk,
        )
        child.save(update_fields=["parent", "outline_position", "metadata_updated_at"])
    if not provisional.children.exists():
        provisional.delete()


def _available_or_next_outline_position(
    node: ConfluencePageNode,
    *,
    parent_id: int | None,
    preferred: int | None = None,
) -> int:
    """Keep a moved node's serial when available; otherwise fill the lowest gap."""

    position = preferred if preferred is not None else node.outline_position
    if position is not None and not (
        ConfluencePageNode.objects.filter(
            parent_id=parent_id,
            outline_position=position,
        )
        .exclude(pk=node.pk)
        .exists()
    ):
        return position
    return next_outline_position(parent_id=parent_id)


def _normalize_hierarchy(
    raw: object,
    *,
    space_key: str,
    leaf_page_id: str,
) -> tuple[_NormalizedNode, ...]:
    if raw in (None, "", (), []):
        return ()
    if isinstance(raw, str):
        if " > " in raw:
            parts = raw.split(" > ")
        elif " / " in raw:
            parts = raw.split(" / ")
        else:
            parts = [raw]
        values: list[object] = [part for part in parts if part.strip()]
    elif _is_non_text_sequence(raw):
        values = list(raw)
    else:
        raise ImportRecordError("The record hierarchy must be a breadcrumb or JSON array.")

    if len(values) > MAX_HIERARCHY_DEPTH:
        raise ImportRecordError("The record hierarchy is too deep.")
    nodes: list[_NormalizedNode] = []
    seen_page_ids: set[str] = set()
    for value in values:
        if isinstance(value, str):
            title = _required_text(value, "hierarchy title", maximum=500)
            node = _NormalizedNode(title=title, space_key=space_key)
        elif isinstance(value, Mapping):
            title = _required_text(
                _pick(value, "title", "name", default=""),
                "hierarchy title",
                maximum=500,
            )
            raw_node_page_id = _pick(value, "page_id", "pageId", "id", default=None)
            try:
                page_id = (
                    normalize_page_id(raw_node_page_id)
                    if raw_node_page_id not in (None, "")
                    else None
                )
            except InvalidPageIdentity as exc:
                raise ImportRecordError("A hierarchy node has an invalid Page ID.") from exc
            node = _NormalizedNode(
                title=title,
                page_id=page_id,
                url=_safe_url(_pick(value, "url", "link", default=""), required=False),
                space_key=_optional_text(
                    _pick(value, "space_key", "spaceKey", default=space_key),
                    "hierarchy space key",
                    maximum=255,
                ),
                sibling_position=_optional_non_negative_int(
                    _pick(value, "sibling_position", "position", default=None),
                    "hierarchy position",
                ),
            )
        else:
            raise ImportRecordError("Every hierarchy entry must be text or a JSON object.")
        if node.page_id is not None:
            if node.page_id in seen_page_ids:
                raise ImportRecordError("The record hierarchy contains a duplicate Page ID.")
            seen_page_ids.add(node.page_id)
        nodes.append(node)

    if leaf_page_id in seen_page_ids and nodes[-1].page_id != leaf_page_id:
        raise ImportRecordError("The bookmark Page ID appears in the middle of its hierarchy.")
    return tuple(nodes)


def _normalize_tags(raw: object) -> tuple[str, ...]:
    if raw in (None, "", (), []):
        return ()
    if isinstance(raw, str):
        values: Sequence[object] = re.split(r"[,;]", raw)
    elif _is_non_text_sequence(raw):
        values = raw
    else:
        raise ImportRecordError("Tags must be text or a JSON array of text values.")
    if len(values) > MAX_TAGS_PER_BOOKMARK:
        raise ImportRecordError("The record has too many tags.")
    result: list[str] = []
    normalized_seen: set[str] = set()
    for raw_name in values:
        name = _required_text(raw_name, "tag", maximum=100)
        normalized = " ".join(name.casefold().split())
        if normalized not in normalized_seen:
            normalized_seen.add(normalized)
            result.append(" ".join(name.split()))
    return tuple(result)


def _export_record(
    bookmark: Bookmark,
    nodes: Mapping[int, ConfluencePageNode],
) -> dict[str, object]:
    return {
        "owl_number": bookmark.pk,
        "page_id": bookmark.page_id,
        "source": {
            "title": bookmark.title,
            "url": _safe_export_url(bookmark.url),
            "space_name": bookmark.space_name,
            "space_key": bookmark.space_key,
            "version": bookmark.version,
            "created_at": _iso_timestamp(bookmark.created_at),
            "updated_at": _iso_timestamp(bookmark.updated_at),
            "created_by": {"id": bookmark.created_by_id, "name": bookmark.created_by_name},
            "modified_by": {
                "id": bookmark.modified_by_id,
                "name": bookmark.modified_by_name,
            },
            "author": {"id": bookmark.author_id, "name": bookmark.author_name},
        },
        "hierarchy": _export_hierarchy(bookmark.tree_node_id, nodes),
        "personal": {
            "saved_at": _iso_timestamp(bookmark.saved_at),
            "favorite": bookmark.favorite,
            "pinned": bookmark.pinned,
            "notes": bookmark.notes,
            "notes_updated_at": _iso_timestamp(bookmark.notes_updated_at),
            "tags": sorted((tag.name for tag in bookmark.tags.all()), key=str.casefold),
        },
        "usage": {
            "open_count": bookmark.open_count,
            "first_opened_at": _iso_timestamp(bookmark.first_opened_at),
            "last_viewed_at": _iso_timestamp(bookmark.last_viewed_at),
            "last_viewed_version": bookmark.last_viewed_version,
        },
        "status": {
            "availability_status": bookmark.availability_status,
            "last_refresh_attempt_at": _iso_timestamp(bookmark.last_refresh_attempt_at),
            "last_refreshed_at": _iso_timestamp(bookmark.last_refreshed_at),
            "last_change_detected_at": _iso_timestamp(bookmark.last_change_detected_at),
            "last_error_code": bookmark.last_error_code
            if _SAFE_ERROR_CODE.fullmatch(bookmark.last_error_code)
            else "stored_error",
            "last_error_message": _sanitize_error_message(bookmark.last_error_message),
            "last_error_at": _iso_timestamp(bookmark.last_error_at),
        },
        "provenance": {
            "legacy_number": bookmark.legacy_number,
            "import_record_number": bookmark.import_record_number,
            "import_source": _safe_filename(bookmark.import_run.filename)
            if bookmark.import_run_id
            else "",
        },
    }


def _export_hierarchy(
    leaf_id: int,
    nodes: Mapping[int, ConfluencePageNode],
) -> list[dict[str, object]]:
    chain: list[ConfluencePageNode] = []
    seen: set[int] = set()
    node_id: int | None = leaf_id
    while node_id is not None:
        if node_id in seen or node_id not in nodes:
            break
        seen.add(node_id)
        node = nodes[node_id]
        chain.append(node)
        node_id = node.parent_id
    chain.reverse()
    return [
        {
            "page_id": node.page_id,
            "provisional_key": node.provisional_key,
            "title": node.title,
            "url": _safe_export_url(node.url),
            "space_key": node.space_key,
            "sibling_position": node.sibling_position,
        }
        for node in chain
    ]


def _legacy_import_order(record: _NormalizedRecord) -> tuple[datetime, int]:
    latest = datetime.max.replace(tzinfo=UTC)
    return (record.saved_at.astimezone(UTC) if record.saved_at else latest, record.record_number)


def _record_failure(
    run: BookmarkImportRun,
    record_number: int,
    page_id: str,
    reason: str,
    *,
    source_url: object = "",
) -> None:
    BookmarkImportFailure.objects.update_or_create(
        import_run=run,
        record_number=record_number,
        defaults={
            "page_id": page_id if page_id.isdecimal() and len(page_id) <= 64 else "",
            "source_url": _safe_failure_source_url(source_url),
            "reason": _sanitize_failure_reason(reason),
        },
    )
    run.failed_records = run.failures.count()


def _update_run_progress(
    run: BookmarkImportRun,
    processed: int,
    imported: int,
    skipped: int,
) -> None:
    run.processed_records = processed
    run.imported_records = imported
    run.skipped_records = skipped
    run.failed_records = run.failures.count()
    run.save(
        update_fields=[
            "processed_records",
            "imported_records",
            "skipped_records",
            "failed_records",
        ]
    )


def _safe_filename(value: object) -> str:
    candidate = str(value).replace("\\", "/")
    candidate = PurePosixPath(candidate).name
    candidate = "".join(character for character in candidate if character.isprintable()).strip()
    candidate = redact_log_text(candidate)[:255]
    return candidate or "bookmark-import.json"


def _safe_schema_version(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ImportDocumentError("The import schema version is invalid.")
    candidate = str(value).strip()
    if not candidate or len(candidate) > 32 or not re.fullmatch(r"[A-Za-z0-9_.-]+", candidate):
        raise ImportDocumentError("The import schema version is invalid.")
    return candidate


def _bounded_document_integer(value: object, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ImportDocumentError(f"The import {field_name} is outside the safe range.")
    return value


def _looks_like_record(document: Mapping[str, object]) -> bool:
    return any(
        key in document
        for key in ("page_id", "pageId", "pageID", "confluence_page_id", "url", "title")
    )


def _page_id_from_url(value: object) -> object:
    if not isinstance(value, str) or not value.strip():
        return _MISSING
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return _MISSING
    for pattern in _PAGE_PATH_PATTERNS:
        match = pattern.search(parts.path)
        if match and match.lastindex:
            return match.group(1)
        if match:
            query_value = parse_qs(parts.query).get("pageId", [None])[0]
            if query_value:
                return query_value
    return _MISSING


def _person_value(
    source: Mapping[str, object],
    raw: Mapping[str, object],
    group_name: str,
    flat_name: str,
    nested_name: str,
    maximum: int,
) -> str:
    group = _mapping(source.get(group_name))
    value = _pick_many((group, source, raw), nested_name, flat_name, default="")
    return _optional_text(value, flat_name.replace("_", " "), maximum=maximum)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _pick(mapping: Mapping[str, object], *names: str, default: object) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _pick_many(
    mappings: Sequence[Mapping[str, object]],
    *names: str,
    default: object,
) -> object:
    for mapping in mappings:
        value = _pick(mapping, *names, default=_MISSING)
        if value is not _MISSING:
            return value
    return default


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ImportRecordError(f"The record {field_name} must be text.")
    candidate = str(value).strip()
    if not candidate:
        raise ImportRecordError(f"The record {field_name} is required.")
    if len(candidate) > maximum:
        raise ImportRecordError(f"The record {field_name} is too long.")
    return candidate


def _optional_text(value: object, field_name: str, *, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ImportRecordError(f"The record {field_name} must be text.")
    candidate = str(value).strip()
    if len(candidate) > maximum:
        raise ImportRecordError(f"The record {field_name} is too long.")
    return candidate


def _safe_url(value: object, *, required: bool) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or len(value.strip()) > 2048:
        raise ImportRecordError("The record URL is invalid.")
    candidate = value.strip()
    try:
        parts = urlsplit(candidate)
        _validated_port = parts.port
    except ValueError as exc:
        raise ImportRecordError("The record URL is invalid.") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ImportRecordError("The record URL is not a safe absolute URL.")
    if any(_SENSITIVE_QUERY_KEY.search(key) for key, _value in parse_qsl(parts.query)):
        raise ImportRecordError("The record URL contains credential-shaped query data.")
    return candidate


def _safe_export_url(value: object) -> str:
    """Omit malformed/userinfo URLs and redact credential-shaped query values."""

    if not isinstance(value, str) or not value:
        return ""
    try:
        parts = urlsplit(value)
        _validated_port = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        return ""
    safe_query = urlencode(
        [
            (key, query_value)
            for key, query_value in parse_qsl(parts.query, keep_blank_values=True)
            if not _SENSITIVE_QUERY_KEY.search(key)
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, ""))


def _boolean(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "y", "1", "starred", "pinned"}:
            return True
        if normalized in {"false", "no", "n", "0", "", "unstarred", "unpinned"}:
            return False
    raise ImportRecordError(f"The record {field_name} value must be true or false.")


def _positive_int(value: object, field_name: str) -> int:
    normalized = _optional_positive_int(value, field_name)
    if normalized is None:
        raise ImportRecordError(f"The record {field_name} must be a positive whole number.")
    return normalized


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ImportRecordError(f"The record {field_name} must be a positive whole number.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ImportRecordError(
            f"The record {field_name} must be a positive whole number."
        ) from exc
    if normalized < 1 or (isinstance(value, float) and not value.is_integer()):
        raise ImportRecordError(f"The record {field_name} must be a positive whole number.")
    return normalized


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ImportRecordError(f"The record {field_name} cannot be negative.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ImportRecordError(f"The record {field_name} must be a whole number.") from exc
    if normalized < 0 or (isinstance(value, float) and not value.is_integer()):
        raise ImportRecordError(f"The record {field_name} cannot be negative.")
    return normalized


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _non_negative_int(value, field_name)


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ImportRecordError(f"The record {field_name} is invalid.") from exc
    elif isinstance(value, str):
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            parsed = _parse_legacy_datetime(candidate, field_name)
    else:
        raise ImportRecordError(f"The record {field_name} is invalid.")
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_legacy_datetime(value: str, field_name: str) -> datetime:
    for format_string in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, format_string).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ImportRecordError(f"The record {field_name} is invalid.")


def _sanitize_error_message(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return "Imported diagnostic details were omitted."
    return redact_log_text(str(value)).replace("\r", " ").replace("\n", " ")[:255]


def _sanitize_failure_reason(value: str) -> str:
    candidate = redact_log_text(value).replace("\r", " ").replace("\n", " ").strip()
    return candidate[:255] or "The record was rejected safely."


def _provisional_key(space_key: str, titles: Sequence[str]) -> str:
    material = "\0".join((space_key.casefold(), *(" ".join(t.casefold().split()) for t in titles)))
    return f"import:v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _normalized_title(value: str) -> str:
    return " ".join(value.casefold().split())


def _iso_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if timezone.is_naive(value):
        raise ValueError("Exported timestamps must include a timezone.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _document_checksum(document: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _constant_time_text_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _is_non_text_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
