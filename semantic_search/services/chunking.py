"""Deterministic normalization and bounded text chunks for semantic indexing."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULT_SEMANTIC_CHUNK_MAX_CHARACTERS = 1_200
DEFAULT_SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 200


@dataclass(frozen=True, slots=True)
class SemanticChunkInput:
    """One immutable, source-located input to the embedding provider."""

    ordinal: int
    page_number: int | None
    chunk_text: str
    text_hash: str
    character_count: int


def normalize_semantic_text(value: object) -> str:
    """Return stable NFKC text with platform-independent paragraph newlines.

    Horizontal whitespace is collapsed within a line, line endings are normalized to
    ``\n``, and multiple blank lines become one paragraph separator. This keeps the
    content hash independent of operating-system newlines without flattening paragraph
    boundaries that are useful to the chunker.
    """

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = (
        normalized.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\v", "\n")
        .replace("\f", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )

    lines: list[str] = []
    blank_pending = False
    for raw_line in normalized.split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            if lines:
                blank_pending = True
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    return "\n".join(lines).strip()


def semantic_chunk_settings() -> tuple[int, int]:
    """Return validated configured chunk size and overlap with safe defaults."""

    max_characters = _setting(
        "SEMANTIC_CHUNK_MAX_CHARACTERS",
        DEFAULT_SEMANTIC_CHUNK_MAX_CHARACTERS,
    )
    overlap_characters = _optional_setting("SEMANTIC_CHUNK_OVERLAP_CHARACTERS")
    if overlap_characters is None:
        overlap_characters = (
            min(DEFAULT_SEMANTIC_CHUNK_OVERLAP_CHARACTERS, max_characters - 1)
            if isinstance(max_characters, int)
            and not isinstance(max_characters, bool)
            and max_characters > 0
            else DEFAULT_SEMANTIC_CHUNK_OVERLAP_CHARACTERS
        )
    return validate_chunk_options(max_characters, overlap_characters)


def validate_chunk_options(max_chars: int, overlap: int) -> tuple[int, int]:
    """Validate a bounded, progressing chunk configuration."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("Semantic chunk size must be a positive integer.")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("Semantic chunk overlap must be zero or greater.")
    if overlap >= max_chars:
        raise ValueError("Semantic chunk overlap must be smaller than the chunk size.")
    return max_chars, overlap


def chunk_text(
    value: object,
    *,
    page_number: int | None = None,
    ordinal_start: int = 0,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> tuple[SemanticChunkInput, ...]:
    """Split normalized text at paragraphs or words, with deterministic overlap."""

    if isinstance(ordinal_start, bool) or not isinstance(ordinal_start, int) or ordinal_start < 0:
        raise ValueError("Semantic chunk ordinals must start at zero or greater.")
    if page_number is not None and (
        isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1
    ):
        raise ValueError("Semantic PDF page numbers must be positive integers.")

    configured_max, configured_overlap = semantic_chunk_settings()
    selected_max = configured_max if max_chars is None else max_chars
    selected_overlap = configured_overlap if overlap is None else overlap
    selected_max, selected_overlap = validate_chunk_options(selected_max, selected_overlap)

    normalized = normalize_semantic_text(value)
    if not normalized:
        return ()

    chunks: list[SemanticChunkInput] = []
    start = 0
    text_length = len(normalized)
    while start < text_length:
        start = _next_content_index(normalized, start)
        if start >= text_length:
            break

        limit = min(start + selected_max, text_length)
        end = (
            text_length
            if limit == text_length
            else _preferred_chunk_end(normalized, start=start, limit=limit)
        )
        if end <= start:
            end = min(start + selected_max, text_length)

        chunk_value = normalized[start:end].strip()
        if chunk_value:
            chunks.append(
                SemanticChunkInput(
                    ordinal=ordinal_start + len(chunks),
                    page_number=page_number,
                    chunk_text=chunk_value,
                    text_hash=hashlib.sha256(chunk_value.encode("utf-8")).hexdigest(),
                    character_count=len(chunk_value),
                )
            )
        if end >= text_length:
            break

        next_start = _overlap_start(
            normalized,
            current_start=start,
            end=end,
            overlap=selected_overlap,
        )
        start = next_start if next_start > start else start + 1

    return tuple(chunks)


def _setting(name: str, default: int) -> int:
    try:
        return getattr(settings, name, default)
    except ImproperlyConfigured:
        return default


def _optional_setting(name: str) -> int | None:
    try:
        return getattr(settings, name, None)
    except ImproperlyConfigured:
        return None


def _next_content_index(value: str, start: int) -> int:
    while start < len(value) and value[start].isspace():
        start += 1
    return start


def _preferred_chunk_end(value: str, *, start: int, limit: int) -> int:
    """Prefer a late paragraph boundary, then a late line/word boundary."""

    minimum = start + max(1, (limit - start) // 2)
    for separator in ("\n\n", "\n", " "):
        boundary = value.rfind(separator, minimum, limit + 1)
        if boundary > start:
            return boundary

    # A short paragraph near the beginning is still better than splitting a word.
    for separator in ("\n\n", "\n", " "):
        boundary = value.rfind(separator, start + 1, limit + 1)
        if boundary > start:
            return boundary
    return limit


def _overlap_start(value: str, *, current_start: int, end: int, overlap: int) -> int:
    if overlap == 0:
        return _next_content_index(value, end)

    target = max(current_start + 1, end - overlap)
    # Include a complete word when the extra overlap does not return to the current
    # chunk's beginning. Hard-split tokens still progress through the fallback target.
    word_boundary = value.rfind(" ", current_start + 1, target + 1)
    line_boundary = value.rfind("\n", current_start + 1, target + 1)
    boundary = max(word_boundary, line_boundary)
    candidate = boundary + 1 if boundary >= current_start + 1 else target
    return _next_content_index(value, candidate)
