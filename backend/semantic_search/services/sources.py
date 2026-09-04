"""Pure source snapshots consumed by OWL's semantic embedding queue."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from bookmark_manager.models import Bookmark
from semantic_search.models import SemanticSourceType
from semantic_search.services.chunking import (
    SemanticChunkInput,
    chunk_text,
    normalize_semantic_text,
)


class SourceUnavailable(LookupError):
    """Raised when an embedding source no longer exists or has no human text."""


@dataclass(frozen=True, slots=True)
class SemanticSourceSnapshot:
    """Immutable current content and chunks for one local semantic source."""

    source_type: str
    source_id: int
    content_hash: str
    chunks: tuple[SemanticChunkInput, ...]
    character_count: int


def build_bookmark_source_snapshot(bookmark_or_id: Bookmark | int) -> SemanticSourceSnapshot:
    """Build chunks from already-stored bookmark text without network access."""

    bookmark = _bookmark(bookmark_or_id)
    semantic_text = _bookmark_semantic_text(bookmark)
    if not semantic_text:
        raise SourceUnavailable("The bookmark has no stored human-readable text to embed.")
    return SemanticSourceSnapshot(
        source_type=SemanticSourceType.BOOKMARK,
        source_id=bookmark.pk,
        content_hash=hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
        chunks=chunk_text(semantic_text),
        character_count=len(semantic_text),
    )


def load_source_snapshot(source_type: SemanticSourceType | str, source_id: int):
    """Load one typed semantic source snapshot from local stored data only."""

    _source_type(source_type)
    return build_bookmark_source_snapshot(source_id)


def current_source_content_hash(source_type: SemanticSourceType | str, source_id: int) -> str:
    """Return the source hash used to reject stale embedding publications."""

    _source_type(source_type)
    bookmark = _bookmark(source_id)
    semantic_text = _bookmark_semantic_text(bookmark)
    if not semantic_text:
        raise SourceUnavailable("The bookmark has no stored human-readable text to embed.")
    return hashlib.sha256(semantic_text.encode("utf-8")).hexdigest()


def _bookmark(bookmark_or_id: Bookmark | int) -> Bookmark:
    if isinstance(bookmark_or_id, Bookmark):
        if bookmark_or_id.pk is None:
            raise SourceUnavailable("The bookmark is not stored.")
        return bookmark_or_id
    source_id = _source_id(bookmark_or_id)
    try:
        return Bookmark.objects.prefetch_related("tags").get(pk=source_id)
    except Bookmark.DoesNotExist as exc:
        raise SourceUnavailable("The bookmark is no longer available.") from exc


def _bookmark_semantic_text(bookmark: Bookmark) -> str:
    sections: list[str] = []
    title = normalize_semantic_text(bookmark.title)
    page_text = normalize_semantic_text(bookmark.page_text)
    notes = normalize_semantic_text(bookmark.notes)
    tags = sorted(
        {
            normalized
            for tag in bookmark.tags.all()
            if (normalized := normalize_semantic_text(tag.name))
        },
        key=lambda value: (value.casefold(), value),
    )
    if title:
        sections.append(f"Title: {title}")
    if page_text:
        sections.append(f"Page text:\n{page_text}")
    if notes:
        sections.append(f"Notes:\n{notes}")
    if tags:
        sections.append(f"Tags: {', '.join(tags)}")
    return normalize_semantic_text("\n\n".join(sections))


def _source_type(value: SemanticSourceType | str) -> SemanticSourceType:
    try:
        selected_type = SemanticSourceType(value)
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable("The semantic source type is not supported.") from exc
    if selected_type != SemanticSourceType.BOOKMARK:
        raise SourceUnavailable("The semantic source type is not supported.")
    return selected_type


def _source_id(value: object) -> int:
    if isinstance(value, bool):
        raise SourceUnavailable("The semantic source identifier is invalid.")
    try:
        source_id = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable("The semantic source identifier is invalid.") from exc
    if source_id < 1 or str(source_id) != str(value).strip():
        raise SourceUnavailable("The semantic source identifier is invalid.")
    return source_id
