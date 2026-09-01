"""Memory-bounded two-stage cosine search over local semantic embeddings."""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from semantic_search.models import (
    SemanticChunk,
    SemanticCorpusState,
    SemanticIndex,
    SemanticSourceType,
)
from semantic_search.services.logging_events import get_logger, log_event
from semantic_search.services.provider import (
    EmbeddingProvider,
    EmbeddingProviderError,
    deserialize_vector,
    get_embedding_provider,
)

logger = get_logger("search")


@dataclass(frozen=True, slots=True)
class SemanticSearchMatch:
    source_id: int
    score: float
    page_number: int | None
    snippet: str


@dataclass(frozen=True, slots=True)
class _SourceVectorCache:
    generation: int
    model_version: str
    chunker_version: str
    dimensions: int
    index_ids: np.ndarray
    source_ids: np.ndarray
    vectors: np.ndarray


_cache: dict[str, _SourceVectorCache] = {}
_cache_lock = threading.Lock()


def clear_semantic_search_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _generation(source_type: SemanticSourceType) -> int:
    value = (
        SemanticCorpusState.objects.filter(source_type=source_type)
        .values_list("generation", flat=True)
        .first()
    )
    return int(value or 0)


def _empty_cache(generation: int) -> _SourceVectorCache:
    return _SourceVectorCache(
        generation=generation,
        model_version=str(settings.SEMANTIC_MODEL_VERSION),
        chunker_version=str(settings.SEMANTIC_CHUNKER_VERSION),
        dimensions=0,
        index_ids=np.empty(0, dtype=np.int64),
        source_ids=np.empty(0, dtype=np.int64),
        vectors=np.empty((0, 0), dtype=np.float32),
    )


def _cache_is_current(cache: _SourceVectorCache | None, generation: int) -> bool:
    return bool(
        cache is not None
        and cache.generation == generation
        and cache.model_version == str(settings.SEMANTIC_MODEL_VERSION)
        and cache.chunker_version == str(settings.SEMANTIC_CHUNKER_VERSION)
    )


def _load_cache(source_type: SemanticSourceType) -> _SourceVectorCache:
    """Cache one centroid per source; page/chunk vectors stay in SQLite until reranking."""

    generation = _generation(source_type)
    cached = _cache.get(source_type)
    if _cache_is_current(cached, generation):
        assert cached is not None
        return cached

    with _cache_lock:
        cached = _cache.get(source_type)
        if _cache_is_current(cached, generation):
            assert cached is not None
            return cached
        rows = tuple(
            SemanticIndex.objects.filter(
                source_type=source_type,
                model_version=str(settings.SEMANTIC_MODEL_VERSION),
                chunker_version=str(settings.SEMANTIC_CHUNKER_VERSION),
            )
            .order_by("id")
            .values_list(
                "id",
                "pdf_revision_id",
                "bookmark_id",
                "dimensions",
                "centroid_vector",
            )
        )
        if not rows:
            loaded = _empty_cache(generation)
            _cache[source_type] = loaded
            return loaded

        dimension_counts = Counter(
            int(row_dimensions)
            for _index_id, _pdf_revision_id, _bookmark_id, row_dimensions, raw_vector in rows
            if int(row_dimensions) > 0 and len(raw_vector) == int(row_dimensions) * 4
        )
        if not dimension_counts:
            loaded = _empty_cache(generation)
            _cache[source_type] = loaded
            return loaded
        # A malformed or obsolete row must not choose the shape of the entire
        # in-process matrix. A published model has one dimension, so use the
        # dominant structurally valid cohort and quarantine outliers below.
        dimensions = max(
            dimension_counts,
            key=lambda candidate: (dimension_counts[candidate], candidate),
        )
        index_ids = np.empty(len(rows), dtype=np.int64)
        source_ids = np.empty(len(rows), dtype=np.int64)
        vectors = np.empty((len(rows), dimensions), dtype=np.float32)
        valid_count = 0
        for index_id, pdf_revision_id, bookmark_id, row_dimensions, raw_vector in rows:
            source_id = pdf_revision_id or bookmark_id
            if source_id is None or int(row_dimensions) != dimensions:
                continue
            try:
                vector = deserialize_vector(raw_vector, dimensions=dimensions)
            except EmbeddingProviderError as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "semantic_centroid_decode_failed",
                    error=exc,
                    source_type=source_type,
                    source_id=source_id,
                )
                continue
            index_ids[valid_count] = int(index_id)
            source_ids[valid_count] = int(source_id)
            vectors[valid_count] = vector
            valid_count += 1

        if not valid_count:
            loaded = _empty_cache(generation)
        else:
            loaded = _SourceVectorCache(
                generation=generation,
                model_version=str(settings.SEMANTIC_MODEL_VERSION),
                chunker_version=str(settings.SEMANTIC_CHUNKER_VERSION),
                dimensions=dimensions,
                index_ids=index_ids[:valid_count],
                source_ids=source_ids[:valid_count],
                vectors=vectors[:valid_count],
            )
        _cache[source_type] = loaded
        return loaded


def semantic_search(
    source_type: SemanticSourceType | str,
    query: str,
    *,
    allowed_source_ids: set[int] | frozenset[int] | None = None,
    limit: int | None = None,
    minimum_score: float | None = None,
    provider: EmbeddingProvider | None = None,
) -> tuple[SemanticSearchMatch, ...]:
    """Rank source centroids in memory, then rerank only candidate chunks from SQLite."""

    if not settings.SEMANTIC_SEARCH_ENABLED or not str(query or "").strip():
        return ()
    selected_type = SemanticSourceType(source_type)
    selected_limit = max(1, int(limit or settings.SEMANTIC_SEARCH_TOP_K))
    threshold = (
        float(settings.SEMANTIC_SEARCH_MIN_SCORE) if minimum_score is None else float(minimum_score)
    )
    cache = _load_cache(selected_type)
    if cache.vectors.shape[0] == 0:
        return ()
    query_vector = (provider or get_embedding_provider()).encode_query(query)
    if query_vector.size != cache.dimensions:
        raise EmbeddingProviderError(
            "The semantic query model does not match the published vector dimensions."
        )

    candidate_rows = _candidate_source_rows(
        cache,
        query_vector,
        allowed_source_ids=allowed_source_ids,
        limit=max(selected_limit, int(settings.SEMANTIC_RERANK_SOURCE_CANDIDATES)),
    )
    if candidate_rows.size == 0:
        return ()
    return _rerank_candidate_chunks(
        cache,
        candidate_rows,
        query_vector,
        limit=selected_limit,
        minimum_score=threshold,
    )


def _candidate_source_rows(
    cache: _SourceVectorCache,
    query_vector: np.ndarray,
    *,
    allowed_source_ids: set[int] | frozenset[int] | None,
    limit: int,
) -> np.ndarray:
    """Select centroid candidates after filtering, avoiding global-window false negatives."""

    if allowed_source_ids is not None:
        allowed = frozenset(int(source_id) for source_id in allowed_source_ids)
        if not allowed:
            return np.empty(0, dtype=np.int64)
        rows = np.fromiter(
            (row for row, source_id in enumerate(cache.source_ids) if int(source_id) in allowed),
            dtype=np.int64,
        )
    else:
        rows = np.arange(cache.source_ids.size, dtype=np.int64)
    if rows.size == 0:
        return rows

    scores = cache.vectors[rows] @ query_vector
    candidate_count = min(int(rows.size), max(1, limit))
    if candidate_count == rows.size:
        ranked_positions = np.argsort(scores)[::-1]
    else:
        partition = np.argpartition(scores, -candidate_count)[-candidate_count:]
        ranked_positions = partition[np.argsort(scores[partition])[::-1]]
    return rows[ranked_positions]


def _rerank_candidate_chunks(
    cache: _SourceVectorCache,
    candidate_rows: np.ndarray,
    query_vector: np.ndarray,
    *,
    limit: int,
    minimum_score: float,
) -> tuple[SemanticSearchMatch, ...]:
    index_to_source = {
        int(cache.index_ids[int(row)]): int(cache.source_ids[int(row)]) for row in candidate_rows
    }
    best: dict[int, SemanticSearchMatch] = {}
    chunk_rows = (
        SemanticChunk.objects.filter(index_id__in=tuple(index_to_source))
        .order_by("index_id", "ordinal")
        .values_list("index_id", "page_number", "chunk_text", "vector")
        .iterator(chunk_size=128)
    )
    for index_id, page_number, chunk_text, raw_vector in chunk_rows:
        source_id = index_to_source.get(int(index_id))
        if source_id is None:
            continue
        try:
            vector = deserialize_vector(raw_vector, dimensions=cache.dimensions)
        except EmbeddingProviderError as exc:
            log_event(
                logger,
                logging.ERROR,
                "semantic_vector_decode_failed",
                error=exc,
                source_id=source_id,
            )
            continue
        score = float(vector @ query_vector)
        current = best.get(source_id)
        if current is None or score > current.score:
            best[source_id] = SemanticSearchMatch(
                source_id=source_id,
                score=score,
                page_number=page_number,
                snippet=_bounded_snippet(chunk_text),
            )
    ranked = sorted(
        (match for match in best.values() if match.score >= minimum_score),
        key=lambda match: (-match.score, match.source_id),
    )
    return tuple(ranked[:limit])


def _bounded_snippet(value: object, *, limit: int = 320) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"
