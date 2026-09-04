"""Shared local semantic-search service boundaries."""

from semantic_search.services.chunking import (
    SemanticChunkInput,
    chunk_text,
    normalize_semantic_text,
    semantic_chunk_settings,
    validate_chunk_options,
)
from semantic_search.services.sources import (
    SemanticSourceSnapshot,
    SourceUnavailable,
    build_bookmark_source_snapshot,
    current_source_content_hash,
    load_source_snapshot,
)

__all__ = (
    "SemanticChunkInput",
    "SemanticSourceSnapshot",
    "SourceUnavailable",
    "build_bookmark_source_snapshot",
    "chunk_text",
    "current_source_content_hash",
    "load_source_snapshot",
    "normalize_semantic_text",
    "semantic_chunk_settings",
    "validate_chunk_options",
)
