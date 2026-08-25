"""Transactional OWL-owned bookmark organization operations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from bookmark_manager.models import Bookmark, SavedBookmarkView, Tag

_TAG_SEPARATOR = re.compile(r"[,;\n]+")
_MAX_TAGS_PER_BOOKMARK = 30
_MAX_TAG_LENGTH = 100


class BookmarkProductivityError(ValueError):
    """Raised when local organization input is invalid."""


@dataclass(frozen=True, slots=True)
class OrganisationResult:
    bookmark: Bookmark
    tags: tuple[str, ...]


def parse_tag_names(raw_value: str) -> tuple[str, ...]:
    """Return de-duplicated display tags without leaking markup into presentation."""

    names: list[str] = []
    normalized_seen: set[str] = set()
    for candidate in _TAG_SEPARATOR.split(raw_value):
        display_name = " ".join(candidate.split())
        if not display_name:
            continue
        if len(display_name) > _MAX_TAG_LENGTH:
            raise BookmarkProductivityError(
                f"Each tag must contain {_MAX_TAG_LENGTH} characters or fewer."
            )
        normalized = Tag.normalize_name(display_name)
        if not normalized or normalized in normalized_seen:
            continue
        normalized_seen.add(normalized)
        names.append(display_name)

    if len(names) > _MAX_TAGS_PER_BOOKMARK:
        raise BookmarkProductivityError(
            f"A bookmark can have at most {_MAX_TAGS_PER_BOOKMARK} tags."
        )
    return tuple(names)


@transaction.atomic
def update_bookmark_organisation(
    bookmark_or_pk: Bookmark | int,
    *,
    notes: str,
    raw_tags: str,
) -> OrganisationResult:
    """Replace one bookmark's note and tags while leaving source metadata untouched."""

    bookmark_pk = bookmark_or_pk.pk if isinstance(bookmark_or_pk, Bookmark) else bookmark_or_pk
    if bookmark_pk is None:
        raise Bookmark.DoesNotExist("An unsaved bookmark cannot be updated.")

    tag_names = parse_tag_names(raw_tags)
    bookmark = Bookmark.objects.select_for_update().get(pk=bookmark_pk)
    normalized_notes = notes.replace("\r\n", "\n").replace("\r", "\n")
    if bookmark.notes != normalized_notes:
        bookmark.notes = normalized_notes
        bookmark.notes_updated_at = timezone.now()
        bookmark.save(update_fields=("notes", "notes_updated_at"))

    tags = [Tag.objects.get_or_create_normalized(name)[0] for name in tag_names]
    bookmark.tags.set(tags)
    display_tags = tuple(bookmark.tags.order_by("normalized_name").values_list("name", flat=True))
    return OrganisationResult(bookmark=bookmark, tags=display_tags)


@transaction.atomic
def toggle_bookmark_flag(bookmark_or_pk: Bookmark | int, field_name: str) -> Bookmark:
    """Atomically toggle favorite or pin without coupling the independent flags."""

    if field_name not in {"favorite", "pinned"}:
        raise BookmarkProductivityError("Only favorite and pinned can be toggled.")
    bookmark_pk = bookmark_or_pk.pk if isinstance(bookmark_or_pk, Bookmark) else bookmark_or_pk
    if bookmark_pk is None:
        raise Bookmark.DoesNotExist("An unsaved bookmark cannot be updated.")

    bookmark = Bookmark.objects.select_for_update().get(pk=bookmark_pk)
    setattr(bookmark, field_name, not getattr(bookmark, field_name))
    bookmark.save(update_fields=(field_name,))
    return bookmark


@transaction.atomic
def save_bookmark_view(
    *,
    name: str,
    search_text: str,
    filters: dict[str, object],
    sort: str,
    visible_columns: list[str] | None = None,
) -> tuple[SavedBookmarkView, bool]:
    """Create or replace a named view by its case-insensitive local identity."""

    normalized_name = SavedBookmarkView.normalize_name(name)
    if not normalized_name:
        raise BookmarkProductivityError("Enter a saved-view name.")
    defaults = {
        "name": " ".join(name.split()),
        "search_text": search_text,
        "filters": filters,
        "sort": sort,
        "visible_columns": visible_columns or [],
    }
    saved_view, created = SavedBookmarkView.objects.update_or_create(
        normalized_name=normalized_name,
        defaults=defaults,
    )
    return saved_view, created
