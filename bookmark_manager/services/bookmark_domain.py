"""Transactional bookmark identity, hierarchy, and usage operations.

This module is deliberately independent of HTTP and the real Confluence client. Callers
inject a metadata loader after validating the configured origin and request boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    ConfluencePageNode,
)

type PageIdentity = str | int
type MetadataLoader = Callable[[str], "ConfluencePageSnapshot"]

_PAGE_ID_PATTERN = re.compile(r"^[0-9]+$")
_TITLE_WORD_PATTERN = re.compile(r"[^\w]+", flags=re.UNICODE)
_SIMILAR_TITLE_THRESHOLD = 0.86


class BookmarkDomainError(ValueError):
    """Base exception for rejected bookmark-domain input."""


class InvalidPageIdentity(BookmarkDomainError):
    """Raised when a value is not a positive numeric Confluence Page ID."""


class InvalidPageSnapshot(BookmarkDomainError):
    """Raised before persistence when source metadata is inconsistent or unsafe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfluenceNodeSnapshot:
    """Source-owned metadata for one ancestor node, ordered root to parent."""

    page_id: PageIdentity
    title: str
    url: str
    space_key: str = ""
    sibling_position: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfluencePageSnapshot:
    """Normalized input boundary for one fetched Confluence page."""

    page_id: PageIdentity
    title: str
    url: str
    space_name: str = ""
    space_key: str = ""
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_id: str = ""
    created_by_name: str = ""
    modified_by_id: str = ""
    modified_by_name: str = ""
    author_id: str = ""
    author_name: str = ""
    ancestors: tuple[ConfluenceNodeSnapshot, ...] = ()
    sibling_position: int | None = None
    page_text: str = ""


@dataclass(frozen=True, slots=True)
class BookmarkSaveResult:
    """Outcome used by the HTTP layer to reveal a new or existing bookmark."""

    bookmark: Bookmark
    created: bool
    source_requested: bool
    similar_bookmarks: tuple[Bookmark, ...] = ()
    descendant_count: int = 0
    descendants_created: int = 0


def normalize_page_id(value: PageIdentity) -> str:
    """Return one canonical decimal Page ID representation.

    Confluence Page IDs are identifiers rather than local integers. They are stored as
    strings to avoid database-size assumptions while leading zeroes are removed so all
    supported input forms deduplicate consistently.
    """

    if isinstance(value, bool):
        raise InvalidPageIdentity("A Confluence Page ID must be a positive number.")

    candidate = str(value).strip()
    if not candidate or not _PAGE_ID_PATTERN.fullmatch(candidate):
        raise InvalidPageIdentity("A Confluence Page ID must contain digits only.")

    normalized = candidate.lstrip("0") or "0"
    if normalized == "0":
        raise InvalidPageIdentity("A Confluence Page ID must be greater than zero.")
    if len(normalized) > 64:
        raise InvalidPageIdentity("The Confluence Page ID is too long.")
    return normalized


def save_bookmark_by_page_id(
    page_id: PageIdentity,
    metadata_loader: MetadataLoader,
) -> BookmarkSaveResult:
    """Save a Page ID, avoiding a source request when it already exists locally.

    URL parsing and configured-origin validation belong at the integration boundary.
    Once that boundary has extracted a Page ID, it should call this function so Page ID
    deduplication always happens before the injected loader can contact Confluence.
    """

    normalized_page_id = normalize_page_id(page_id)
    existing = (
        Bookmark.objects.select_related("tree_node").filter(page_id=normalized_page_id).first()
    )
    if existing is not None:
        return BookmarkSaveResult(
            bookmark=existing,
            created=False,
            source_requested=False,
        )

    snapshot = metadata_loader(normalized_page_id)
    if normalize_page_id(snapshot.page_id) != normalized_page_id:
        raise InvalidPageSnapshot(
            "Confluence returned metadata for a different Page ID; nothing was saved."
        )

    result = upsert_bookmark(snapshot)
    return replace(result, source_requested=True)


@transaction.atomic
def upsert_bookmark(
    snapshot: ConfluencePageSnapshot,
    *,
    observed_at: datetime | None = None,
    record_refresh: bool = False,
) -> BookmarkSaveResult:
    """Atomically create or source-update one bookmark and its complete hierarchy.

    Ancestors must be supplied root-first. Existing OWL-owned state is intentionally
    excluded from the update map. Setting ``record_refresh`` records the source snapshot
    as a successful refresh; an initial save normally leaves refresh dates empty.
    """

    normalized = _normalize_snapshot(snapshot)
    observation_time = observed_at or timezone.now()
    _require_aware_datetime(observation_time, "observed_at")

    parent: ConfluencePageNode | None = None
    previous_parent_ids: list[int] = []
    for ancestor in normalized.ancestors:
        node, previous_parent_id = _upsert_node(ancestor, parent=parent)
        if previous_parent_id is not None and previous_parent_id != node.parent_id:
            previous_parent_ids.append(previous_parent_id)
        parent = node

    page_node_snapshot = ConfluenceNodeSnapshot(
        page_id=normalized.page_id,
        title=normalized.title,
        url=normalized.url,
        space_key=normalized.space_key,
        sibling_position=normalized.sibling_position,
    )
    page_node, previous_parent_id = _upsert_node(page_node_snapshot, parent=parent)
    if previous_parent_id is not None and previous_parent_id != page_node.parent_id:
        previous_parent_ids.append(previous_parent_id)

    page_id = normalize_page_id(normalized.page_id)
    bookmark = Bookmark.objects.select_for_update().filter(page_id=page_id).first()
    created = bookmark is None
    if bookmark is None:
        bookmark = Bookmark(page_id=page_id, tree_node=page_node)

    previous_version = None if created else bookmark.version
    source_values = _bookmark_source_values(normalized)
    source_values["tree_node"] = page_node
    source_values["availability_status"] = BookmarkAvailability.ACTIVE
    source_values["last_error_code"] = ""
    source_values["last_error_message"] = ""
    source_values["last_error_at"] = None

    if record_refresh:
        source_values["last_refresh_attempt_at"] = observation_time
        source_values["last_refreshed_at"] = observation_time
        if previous_version is not None and previous_version != normalized.version:
            source_values["last_change_detected_at"] = observation_time

    for field_name, value in source_values.items():
        setattr(bookmark, field_name, value)

    if created:
        bookmark.save(force_insert=True)
    else:
        bookmark.save(update_fields=tuple(source_values))

    for old_parent_id in reversed(tuple(dict.fromkeys(previous_parent_ids))):
        old_parent = ConfluencePageNode.objects.filter(pk=old_parent_id).first()
        if old_parent is not None:
            _prune_empty_hierarchy_branch(old_parent)

    similar = find_similar_title_bookmarks(
        normalized.title,
        exclude_page_id=page_id,
    )
    return BookmarkSaveResult(
        bookmark=bookmark,
        created=created,
        source_requested=False,
        similar_bookmarks=similar,
    )


@transaction.atomic
def record_successful_open(
    bookmark_or_pk: Bookmark | int,
    *,
    opened_at: datetime | None = None,
) -> Bookmark:
    """Atomically record an open only after the caller successfully initiated it."""

    bookmark_pk = bookmark_or_pk.pk if isinstance(bookmark_or_pk, Bookmark) else bookmark_or_pk
    if bookmark_pk is None:
        raise Bookmark.DoesNotExist("An unsaved bookmark cannot be opened.")

    open_time = opened_at or timezone.now()
    _require_aware_datetime(open_time, "opened_at")
    date_value = models.Value(open_time, output_field=models.DateTimeField())

    updated = Bookmark.objects.filter(pk=bookmark_pk).update(
        open_count=models.F("open_count") + 1,
        first_opened_at=Coalesce("first_opened_at", date_value),
        last_viewed_at=open_time,
        last_viewed_version=models.F("version"),
    )
    if updated != 1:
        raise Bookmark.DoesNotExist(f"Bookmark {bookmark_pk} does not exist.")
    return Bookmark.objects.select_related("tree_node").get(pk=bookmark_pk)


def find_similar_title_bookmarks(
    title: str,
    *,
    exclude_page_id: PageIdentity | None = None,
) -> tuple[Bookmark, ...]:
    """Find non-blocking similarity candidates without conflating their identity."""

    normalized_title = _normalize_title(title)
    if not normalized_title:
        return ()

    queryset = Bookmark.objects.only("id", "page_id", "title")
    if exclude_page_id is not None:
        queryset = queryset.exclude(page_id=normalize_page_id(exclude_page_id))

    matches = []
    for bookmark in queryset.iterator(chunk_size=500):
        candidate = _normalize_title(bookmark.title)
        if not candidate:
            continue
        if (
            candidate == normalized_title
            or SequenceMatcher(
                None,
                normalized_title,
                candidate,
                autojunk=False,
            ).ratio()
            >= _SIMILAR_TITLE_THRESHOLD
        ):
            matches.append(bookmark)
    return tuple(matches)


def _normalize_snapshot(snapshot: ConfluencePageSnapshot) -> ConfluencePageSnapshot:
    page_id = normalize_page_id(snapshot.page_id)
    title = _required_text(snapshot.title, "title", maximum=500)
    url = _safe_source_url(snapshot.url, "url", required=True)
    version = _positive_integer(snapshot.version, "version")
    sibling_position = _optional_non_negative_integer(
        snapshot.sibling_position,
        "sibling_position",
    )
    _require_optional_aware_datetime(snapshot.created_at, "created_at")
    _require_optional_aware_datetime(snapshot.updated_at, "updated_at")

    ancestors: list[ConfluenceNodeSnapshot] = []
    ancestor_ids: set[str] = set()
    for raw_ancestor in snapshot.ancestors:
        ancestor_id = normalize_page_id(raw_ancestor.page_id)
        if ancestor_id == page_id or ancestor_id in ancestor_ids:
            raise InvalidPageSnapshot(
                "The Confluence ancestor chain contains a cycle or duplicate Page ID."
            )
        ancestor_ids.add(ancestor_id)
        ancestors.append(
            ConfluenceNodeSnapshot(
                page_id=ancestor_id,
                title=_required_text(raw_ancestor.title, "ancestor title", maximum=500),
                url=_safe_source_url(raw_ancestor.url, "ancestor url", required=False),
                space_key=_optional_text(
                    raw_ancestor.space_key,
                    "ancestor space key",
                    maximum=255,
                ),
                sibling_position=_optional_non_negative_integer(
                    raw_ancestor.sibling_position,
                    "ancestor sibling_position",
                ),
            )
        )

    return replace(
        snapshot,
        page_id=page_id,
        title=title,
        url=url,
        space_name=_optional_text(snapshot.space_name, "space_name", maximum=255),
        space_key=_optional_text(snapshot.space_key, "space_key", maximum=255),
        version=version,
        created_by_id=_optional_text(
            snapshot.created_by_id,
            "created_by_id",
            maximum=255,
        ),
        created_by_name=_optional_text(
            snapshot.created_by_name,
            "created_by_name",
            maximum=500,
        ),
        modified_by_id=_optional_text(
            snapshot.modified_by_id,
            "modified_by_id",
            maximum=255,
        ),
        modified_by_name=_optional_text(
            snapshot.modified_by_name,
            "modified_by_name",
            maximum=500,
        ),
        author_id=_optional_text(snapshot.author_id, "author_id", maximum=255),
        author_name=_optional_text(
            snapshot.author_name,
            "author_name",
            maximum=500,
        ),
        page_text=_optional_text(snapshot.page_text, "page_text", maximum=8_388_608),
        ancestors=tuple(ancestors),
        sibling_position=sibling_position,
    )


def _upsert_node(
    snapshot: ConfluenceNodeSnapshot,
    *,
    parent: ConfluencePageNode | None,
) -> tuple[ConfluencePageNode, int | None]:
    page_id = normalize_page_id(snapshot.page_id)
    node = ConfluencePageNode.objects.select_for_update().filter(page_id=page_id).first()
    previous_parent_id = None if node is None else node.parent_id
    values = {
        "title": snapshot.title,
        "url": snapshot.url,
        "space_key": snapshot.space_key,
        "parent": parent,
        "sibling_position": snapshot.sibling_position,
    }
    if node is None:
        node = ConfluencePageNode.objects.create(page_id=page_id, **values)
        return node, None

    changed_fields: list[str] = []
    for field_name, value in values.items():
        current_value = node.parent if field_name == "parent" else getattr(node, field_name)
        if current_value != value:
            setattr(node, field_name, value)
            changed_fields.append(field_name)
    if changed_fields:
        node.save(update_fields=[*changed_fields, "metadata_updated_at"])
    return node, previous_parent_id


def _bookmark_source_values(snapshot: ConfluencePageSnapshot) -> dict[str, object]:
    return {
        "title": snapshot.title,
        "url": snapshot.url,
        "space_name": snapshot.space_name,
        "space_key": snapshot.space_key,
        "version": snapshot.version,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "created_by_id": snapshot.created_by_id,
        "created_by_name": snapshot.created_by_name,
        "modified_by_id": snapshot.modified_by_id,
        "modified_by_name": snapshot.modified_by_name,
        "author_id": snapshot.author_id,
        "author_name": snapshot.author_name,
        "page_text": snapshot.page_text,
    }


def _prune_empty_hierarchy_branch(node: ConfluencePageNode) -> None:
    """Remove only hierarchy-only leaves, stopping at shared or bookmarked nodes."""

    current: ConfluencePageNode | None = node
    while current is not None:
        if current.children.exists() or Bookmark.objects.filter(tree_node=current).exists():
            return
        parent_id = current.parent_id
        current.delete()
        current = (
            ConfluencePageNode.objects.filter(pk=parent_id).first()
            if parent_id is not None
            else None
        )


def _normalize_title(value: str) -> str:
    words = _TITLE_WORD_PATTERN.sub(" ", str(value).casefold()).split()
    return " ".join(words)


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    candidate = str(value).strip()
    if not candidate:
        raise InvalidPageSnapshot(f"Confluence {field_name} is required.")
    if len(candidate) > maximum:
        raise InvalidPageSnapshot(f"Confluence {field_name} is too long.")
    return candidate


def _optional_text(value: object, field_name: str, *, maximum: int) -> str:
    if value is None:
        return ""
    candidate = str(value).strip()
    if len(candidate) > maximum:
        raise InvalidPageSnapshot(f"Confluence {field_name} is too long.")
    return candidate


def _safe_source_url(value: object, field_name: str, *, required: bool) -> str:
    candidate = str(value).strip()
    if not candidate and not required:
        return ""
    if not candidate:
        raise InvalidPageSnapshot(f"Confluence {field_name} is required.")
    if len(candidate) > 2048:
        raise InvalidPageSnapshot(f"Confluence {field_name} is too long.")

    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        _validated_port = parts.port
    except ValueError as exc:
        raise InvalidPageSnapshot(f"Confluence {field_name} is not a safe absolute URL.") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise InvalidPageSnapshot(f"Confluence {field_name} is not a safe absolute URL.")
    return candidate


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a positive number.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a positive number.") from exc
    if normalized < 1:
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a positive number.")
    if isinstance(value, float) and not value.is_integer():
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a whole number.")
    return normalized


def _optional_non_negative_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidPageSnapshot(f"Confluence {field_name} cannot be negative.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a number.") from exc
    if normalized < 0:
        raise InvalidPageSnapshot(f"Confluence {field_name} cannot be negative.")
    if isinstance(value, float) and not value.is_integer():
        raise InvalidPageSnapshot(f"Confluence {field_name} must be a whole number.")
    return normalized


def _require_optional_aware_datetime(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware_datetime(value, field_name)


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if timezone.is_naive(value):
        raise InvalidPageSnapshot(f"Confluence {field_name} must include a timezone.")


def iter_ancestor_page_ids(snapshot: ConfluencePageSnapshot) -> Iterable[str]:
    """Expose normalized ancestor identity order for tree/reveal consumers."""

    normalized = _normalize_snapshot(snapshot)
    return tuple(normalize_page_id(item.page_id) for item in normalized.ancestors)
