from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from bookmark_manager.models import Bookmark, ConfluencePageNode
from semantic_search.models import (
    SemanticChunk,
    SemanticCorpusState,
    SemanticIndex,
    SemanticIndexJobStatus,
    SemanticSourceType,
)
from semantic_search.services.jobs import (
    claim_next_semantic_job,
    execute_semantic_job,
    queue_semantic_source,
)
from semantic_search.services.provider import deserialize_vector, serialize_vector
from semantic_search.services.search import (
    _load_cache,
    clear_semantic_search_cache,
    semantic_search,
)

pytestmark = pytest.mark.django_db


class _MarkerProvider:
    """Deterministic two-dimensional provider for publication and query tests."""

    @property
    def version(self) -> str:
        return "two-stage-test-provider-v1"

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        return np.ascontiguousarray(
            np.vstack([self._vector(text) for text in texts]),
            dtype=np.float32,
        )

    def encode_query(self, text: str) -> np.ndarray:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        lowered = str(text).casefold()
        vector = np.asarray(
            [lowered.count("alpha"), lowered.count("beta")],
            dtype=np.float32,
        )
        if not vector.any():
            vector[1] = 1.0
        return np.ascontiguousarray(vector / np.linalg.norm(vector), dtype=np.float32)


@pytest.fixture(autouse=True)
def _semantic_settings(settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    settings.SEMANTIC_MODEL_VERSION = "two-stage-test-model-v1"
    settings.SEMANTIC_CHUNKER_VERSION = "two-stage-test-chunker-v1"
    settings.SEMANTIC_EMBEDDING_BATCH_SIZE = 64
    settings.SEMANTIC_MAX_WORKERS = 2
    settings.SEMANTIC_SEARCH_TOP_K = 10
    settings.SEMANTIC_RERANK_SOURCE_CANDIDATES = 10
    settings.SEMANTIC_SEARCH_MIN_SCORE = -1.0
    clear_semantic_search_cache()
    yield
    clear_semantic_search_cache()


def _bookmark(key: str, *, title: str = "Stored source", page_text: str = "") -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=f"two-stage-{key}",
        title=title,
        url=f"https://confluence.example.test/pages/two-stage-{key}",
    )
    return Bookmark.objects.create(
        page_id=node.page_id,
        tree_node=node,
        title=title,
        url=node.url,
        page_text=page_text,
    )


def _normalized(*values: float) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    return np.ascontiguousarray(vector / np.linalg.norm(vector), dtype=np.float32)


def _stored_index(
    bookmark: Bookmark,
    *,
    centroid: np.ndarray,
    chunks: Sequence[tuple[int | None, str, np.ndarray]],
) -> SemanticIndex:
    index = SemanticIndex.objects.create(
        source_type=SemanticSourceType.BOOKMARK,
        bookmark=bookmark,
        content_hash=(f"{bookmark.pk:x}" * 64)[:64],
        model_version="two-stage-test-model-v1",
        chunker_version="two-stage-test-chunker-v1",
        dimensions=int(centroid.size),
        centroid_vector=serialize_vector(centroid).value,
        chunk_count=len(chunks),
        character_count=sum(len(text) for _page, text, _vector in chunks),
    )
    SemanticChunk.objects.bulk_create(
        [
            SemanticChunk(
                index=index,
                ordinal=ordinal,
                page_number=page_number,
                chunk_text=text,
                text_hash=(f"{ordinal + 1:x}" * 64)[:64],
                vector=serialize_vector(vector).value,
                character_count=len(text),
            )
            for ordinal, (page_number, text, vector) in enumerate(chunks)
        ]
    )
    SemanticCorpusState.objects.get_or_create(
        source_type=SemanticSourceType.BOOKMARK,
        defaults={"generation": 1},
    )
    return index


def test_source_cache_holds_one_centroid_per_source_not_every_chunk():
    first = _bookmark("cache-first")
    second = _bookmark("cache-second")
    chunk_vector = _normalized(1.0, 0.0)
    for bookmark, centroid in (
        (first, _normalized(1.0, 0.0)),
        (second, _normalized(0.0, 1.0)),
    ):
        _stored_index(
            bookmark,
            centroid=centroid,
            chunks=tuple((None, f"stored chunk {ordinal}", chunk_vector) for ordinal in range(40)),
        )

    assert SemanticChunk.objects.count() == 80

    cache = _load_cache(SemanticSourceType.BOOKMARK)

    assert cache.vectors.shape == (2, 2)
    assert cache.vectors.dtype == np.float32
    assert cache.index_ids.size == cache.source_ids.size == 2
    assert set(cache.source_ids.tolist()) == {first.pk, second.pk}
    assert cache.vectors.nbytes == 2 * 2 * np.dtype(np.float32).itemsize


def test_corrupted_first_centroid_dimension_does_not_hide_the_valid_corpus():
    corrupted = _bookmark("corrupted-first")
    SemanticIndex.objects.create(
        source_type=SemanticSourceType.BOOKMARK,
        bookmark=corrupted,
        content_hash="f" * 64,
        model_version="two-stage-test-model-v1",
        chunker_version="two-stage-test-chunker-v1",
        dimensions=3,
        centroid_vector=serialize_vector(_normalized(1.0, 0.0, 0.0)).value,
    )
    valid_sources = (_bookmark("valid-first"), _bookmark("valid-second"))
    for bookmark in valid_sources:
        _stored_index(
            bookmark,
            centroid=_normalized(1.0, 0.0),
            chunks=((None, "alpha valid passage", _normalized(1.0, 0.0)),),
        )

    cache = _load_cache(SemanticSourceType.BOOKMARK)
    matches = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=_MarkerProvider(),
        allowed_source_ids={valid_sources[0].pk},
        limit=1,
        minimum_score=-1.0,
    )

    assert cache.dimensions == 2
    assert set(cache.source_ids.tolist()) == {bookmark.pk for bookmark in valid_sources}
    assert [match.source_id for match in matches] == [valid_sources[0].pk]


def test_allowed_sources_are_filtered_before_the_global_centroid_window(settings):
    settings.SEMANTIC_RERANK_SOURCE_CANDIDATES = 2
    provider = _MarkerProvider()
    query_facing = _normalized(1.0, 0.0)
    for ordinal in range(3):
        disallowed = _bookmark(f"global-{ordinal}")
        _stored_index(
            disallowed,
            centroid=query_facing,
            chunks=((None, f"globally strong {ordinal}", query_facing),),
        )

    allowed = _bookmark("allowed-low-centroid")
    allowed_index = _stored_index(
        allowed,
        centroid=_normalized(0.05, 0.95),
        chunks=(
            (2, "alpha is the exact related passage", query_facing),
            (3, "counterbalancing passage", _normalized(-0.99, 0.14)),
        ),
    )

    global_matches = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        limit=1,
        minimum_score=-1.0,
    )
    filtered_matches = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        allowed_source_ids={allowed.pk},
        limit=1,
        minimum_score=-1.0,
    )

    assert global_matches[0].source_id != allowed.pk
    assert filtered_matches[0].source_id == allowed.pk
    assert filtered_matches[0].page_number == 2
    assert filtered_matches[0].score == pytest.approx(1.0)
    assert allowed_index.pk not in {
        SemanticIndex.objects.get(bookmark_id=match.source_id).pk for match in global_matches
    }


def test_chunk_reranking_returns_the_best_page_and_bounded_snippet():
    provider = _MarkerProvider()
    bookmark = _bookmark("best-page")
    long_best_text = "  alpha   decisive passage  " + ("supporting detail " * 30)
    _stored_index(
        bookmark,
        centroid=_normalized(0.7, 0.7),
        chunks=(
            (1, "beta-only passage from the wrong page", _normalized(0.0, 1.0)),
            (9, long_best_text, _normalized(1.0, 0.0)),
        ),
    )

    match = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        limit=1,
        minimum_score=-1.0,
    )[0]

    assert match.source_id == bookmark.pk
    assert match.page_number == 9
    assert match.score == pytest.approx(1.0)
    assert match.snippet.startswith("alpha decisive passage supporting detail")
    assert match.snippet.endswith("…")
    assert len(match.snippet) <= 320
    assert "wrong page" not in match.snippet


def test_successful_republication_replaces_centroid_and_chunks_together():
    provider = _MarkerProvider()
    bookmark = _bookmark("replacement", page_text="alpha original body")
    first_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert first_job_id is not None
    first_claim = claim_next_semantic_job()
    assert first_claim is not None and first_claim.pk == first_job_id
    assert execute_semantic_job(first_claim.pk, provider=provider).status == (
        SemanticIndexJobStatus.SUCCEEDED
    )
    original = SemanticIndex.objects.get(bookmark=bookmark)
    original_index_id = original.pk
    original_centroid = bytes(original.centroid_vector)
    original_chunk_ids = tuple(original.chunks.values_list("pk", flat=True))
    original_chunk_vectors = tuple(original.chunks.values_list("vector", flat=True))

    Bookmark.objects.filter(pk=bookmark.pk).update(page_text="beta replacement body")
    replacement_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert replacement_job_id is not None
    replacement_claim = claim_next_semantic_job()
    assert replacement_claim is not None and replacement_claim.pk == replacement_job_id
    assert execute_semantic_job(replacement_claim.pk, provider=provider).status == (
        SemanticIndexJobStatus.SUCCEEDED
    )

    replacement = SemanticIndex.objects.get(bookmark=bookmark)
    replacement_centroid = bytes(replacement.centroid_vector)
    replacement_chunk_vectors = tuple(replacement.chunks.values_list("vector", flat=True))
    assert replacement.pk == original_index_id
    assert replacement_centroid != original_centroid
    assert replacement_chunk_vectors != original_chunk_vectors
    assert not SemanticChunk.objects.filter(pk__in=original_chunk_ids).exists()
    np.testing.assert_allclose(
        deserialize_vector(replacement.centroid_vector, dimensions=2),
        _normalized(0.0, 1.0),
    )
    assert replacement.chunk_count == replacement.chunks.count() > 0


def test_failed_chunk_replacement_rolls_back_the_new_centroid_and_old_chunk_deletion(monkeypatch):
    provider = _MarkerProvider()
    bookmark = _bookmark("replacement-rollback", page_text="alpha published body")
    first_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert first_job_id is not None
    first_claim = claim_next_semantic_job()
    assert first_claim is not None and first_claim.pk == first_job_id
    assert execute_semantic_job(first_claim.pk, provider=provider).status == (
        SemanticIndexJobStatus.SUCCEEDED
    )
    original = SemanticIndex.objects.get(bookmark=bookmark)
    original_values = (
        original.content_hash,
        bytes(original.centroid_vector),
        original.chunk_count,
        tuple(original.chunks.values_list("pk", "chunk_text", "vector")),
    )

    Bookmark.objects.filter(pk=bookmark.pk).update(page_text="beta unpublished body")
    replacement_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert replacement_job_id is not None
    replacement_claim = claim_next_semantic_job()
    assert replacement_claim is not None and replacement_claim.pk == replacement_job_id

    def fail_chunk_insert(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic chunk publication failure")

    monkeypatch.setattr(SemanticChunk.objects, "bulk_create", fail_chunk_insert)
    failed = execute_semantic_job(replacement_claim.pk, provider=provider)

    assert failed is not None
    assert failed.status == SemanticIndexJobStatus.QUEUED
    preserved = SemanticIndex.objects.get(bookmark=bookmark)
    assert (
        preserved.content_hash,
        bytes(preserved.centroid_vector),
        preserved.chunk_count,
        tuple(preserved.chunks.values_list("pk", "chunk_text", "vector")),
    ) == original_values
