from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pytest
from django.conf import settings as django_settings

from bitbucket_search.models import (
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bookmark_manager.models import Bookmark, ConfluencePageNode
from semantic_search.models import (
    SemanticChunk,
    SemanticCorpusState,
    SemanticIndex,
    SemanticIndexJob,
    SemanticIndexJobStatus,
    SemanticSourceType,
)
from semantic_search.services.jobs import (
    claim_next_semantic_job,
    execute_semantic_job,
    queue_semantic_source,
    sweep_semantic_index_queue,
)
from semantic_search.services.provider import (
    EmbeddingProviderError,
    deserialize_vector,
)
from semantic_search.services.search import (
    clear_semantic_search_cache,
    semantic_search,
)
from semantic_search.services.sources import current_source_content_hash

pytestmark = pytest.mark.django_db


class DeterministicEmbeddingProvider:
    """Small local fake whose vectors depend only on synthetic marker words."""

    def __init__(
        self,
        *,
        fail: bool = False,
        after_documents: Callable[[], None] | None = None,
    ) -> None:
        self.fail = fail
        self.after_documents = after_documents
        self.document_calls: list[tuple[str, ...]] = []
        self.query_calls: list[str] = []

    @property
    def version(self) -> str:
        return "deterministic-test-provider-v1"

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        normalized = tuple(str(text) for text in texts)
        self.document_calls.append(normalized)
        if self.fail:
            raise EmbeddingProviderError("Synthetic embedding provider failure.")
        if self.after_documents is not None:
            callback, self.after_documents = self.after_documents, None
            callback()
        return np.ascontiguousarray(
            np.vstack([self._vector(text) for text in normalized]),
            dtype=np.float32,
        )

    def encode_query(self, text: str) -> np.ndarray:
        self.query_calls.append(text)
        if self.fail:
            raise EmbeddingProviderError("Synthetic query provider failure.")
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        lowered = str(text).casefold()
        vector = np.asarray(
            [
                lowered.count("alpha"),
                lowered.count("beta"),
                lowered.count("gamma"),
            ],
            dtype=np.float32,
        )
        if not vector.any():
            vector[2] = 1.0
        return np.ascontiguousarray(vector / np.linalg.norm(vector), dtype=np.float32)


@pytest.fixture(autouse=True)
def semantic_test_configuration(settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    settings.SEMANTIC_MODEL_VERSION = "deterministic-test-model-v1"
    settings.SEMANTIC_CHUNKER_VERSION = "deterministic-test-chunker-v1"
    settings.SEMANTIC_EMBEDDING_BATCH_SIZE = 64
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 1_800
    settings.SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 180
    settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES = 2
    settings.SEMANTIC_JOB_RETRY_SECONDS = 0
    settings.SEMANTIC_SEARCH_MIN_SCORE = -1.0
    clear_semantic_search_cache()
    yield
    clear_semantic_search_cache()


def _bookmark(
    key: str,
    *,
    title: str = "alpha architecture",
    page_text: str = "",
    notes: str = "",
) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=f"semantic-{key}",
        title=title,
        url=f"https://confluence.example.test/pages/semantic-{key}",
    )
    return Bookmark.objects.create(
        page_id=node.page_id,
        tree_node=node,
        title=title,
        url=node.url,
        page_text=page_text,
        notes=notes,
    )


def _pdf_revision(key: str, *pages: str) -> PDFTextRevision:
    digest = (key * 64)[:64]
    revision = PDFTextRevision.objects.create(
        content_sha256=digest,
        extractor_version="semantic-test-extractor-v1",
        source_byte_size=sum(len(page.encode()) for page in pages),
        state=PDFTextRevisionState.READY,
        page_count=len(pages),
        extracted_character_count=sum(len(page) for page in pages),
    )
    PDFTextPage.objects.bulk_create(
        [
            PDFTextPage(
                revision=revision,
                page_number=page_number,
                extracted_text=text,
                character_count=len(text),
                extraction_state=(
                    PDFPageExtractionState.READY if text.strip() else PDFPageExtractionState.NO_TEXT
                ),
            )
            for page_number, text in enumerate(pages, start=1)
        ]
    )
    return revision


def _run_queued_job(job_id: int, provider: DeterministicEmbeddingProvider):
    claimed = claim_next_semantic_job()
    assert claimed is not None
    assert claimed.pk == job_id
    assert claimed.status == SemanticIndexJobStatus.RUNNING
    assert claimed.started_at is not None
    assert claimed.heartbeat_at is not None
    assert claimed.worker_pid is not None
    completed = execute_semantic_job(claimed.pk, provider=provider)
    assert completed is not None
    return completed


def _seed_index(
    *,
    bookmark: Bookmark | None = None,
    pdf_revision: PDFTextRevision | None = None,
    create_corpus_state: bool = True,
) -> SemanticIndex:
    assert (bookmark is None) != (pdf_revision is None)
    source_type = (
        SemanticSourceType.BOOKMARK if bookmark is not None else SemanticSourceType.PDF_REVISION
    )
    if create_corpus_state:
        SemanticCorpusState.objects.get_or_create(source_type=source_type)
    return SemanticIndex.objects.create(
        source_type=source_type,
        bookmark=bookmark,
        pdf_revision=pdf_revision,
        content_hash="0" * 64,
        model_version=str(django_settings.SEMANTIC_MODEL_VERSION),
        chunker_version=str(django_settings.SEMANTIC_CHUNKER_VERSION),
        dimensions=3,
        centroid_vector=np.asarray((0.0, 0.0, 1.0), dtype="<f4").tobytes(),
    )


def _publish_bookmark(
    bookmark: Bookmark,
    provider: DeterministicEmbeddingProvider,
) -> SemanticIndex:
    if not SemanticIndex.objects.filter(bookmark=bookmark).exists():
        _seed_index(bookmark=bookmark)
    job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert job_id is not None
    completed = _run_queued_job(job_id, provider)
    assert completed.status == SemanticIndexJobStatus.SUCCEEDED
    return SemanticIndex.objects.get(bookmark=bookmark)


def test_queue_is_idempotent_for_pdf_revision_and_bookmark():
    revision = _pdf_revision("a", "alpha page", "beta page")
    bookmark = _bookmark("queue", page_text="alpha bookmark body")

    pdf_job_id = queue_semantic_source(SemanticSourceType.PDF_REVISION, revision.pk)
    bookmark_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)

    assert pdf_job_id is not None
    assert bookmark_job_id is not None
    assert queue_semantic_source(SemanticSourceType.PDF_REVISION, revision.pk) is None
    assert queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk) is None
    assert SemanticIndexJob.objects.count() == 2

    pdf_job = SemanticIndexJob.objects.get(pk=pdf_job_id)
    bookmark_job = SemanticIndexJob.objects.get(pk=bookmark_job_id)
    assert pdf_job.source_type == SemanticSourceType.PDF_REVISION
    assert pdf_job.pdf_revision_id == revision.pk
    assert pdf_job.bookmark_id is None
    assert pdf_job.target_content_hash == current_source_content_hash(
        SemanticSourceType.PDF_REVISION,
        revision.pk,
    )
    assert bookmark_job.source_type == SemanticSourceType.BOOKMARK
    assert bookmark_job.bookmark_id == bookmark.pk
    assert bookmark_job.pdf_revision_id is None
    assert bookmark_job.target_content_hash == current_source_content_hash(
        SemanticSourceType.BOOKMARK,
        bookmark.pk,
    )


def test_first_publication_creates_an_index_without_preexisting_rows():
    bookmark = _bookmark("first-publication", page_text="alpha first publication")
    job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert job_id is not None

    completed = _run_queued_job(job_id, DeterministicEmbeddingProvider())

    assert completed.status == SemanticIndexJobStatus.SUCCEEDED
    assert SemanticIndex.objects.filter(bookmark=bookmark, chunk_count__gt=0).exists()


def test_claim_publishes_pdf_chunks_and_advances_corpus_generation(settings):
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 28
    settings.SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 4
    revision = _pdf_revision(
        "b",
        "alpha architecture has several bounded words",
        "beta controls belong on the second page",
    )
    provider = DeterministicEmbeddingProvider()
    _seed_index(pdf_revision=revision)
    job_id = queue_semantic_source(SemanticSourceType.PDF_REVISION, revision.pk)
    assert job_id is not None

    completed = _run_queued_job(job_id, provider)

    assert completed.status == SemanticIndexJobStatus.SUCCEEDED
    assert completed.completed_at is not None
    assert completed.worker_pid is None
    index = SemanticIndex.objects.get(pdf_revision=revision)
    chunks = list(index.chunks.order_by("ordinal"))
    assert index.chunk_count == len(chunks) > 2
    assert index.character_count == revision.extracted_character_count
    assert index.dimensions == 3
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.page_number for chunk in chunks} == {1, 2}
    assert len(provider.document_calls) == 1
    for chunk in chunks:
        np.testing.assert_allclose(
            deserialize_vector(chunk.vector, dimensions=index.dimensions),
            provider._vector(chunk.chunk_text),
        )
    assert (
        SemanticCorpusState.objects.get(source_type=SemanticSourceType.PDF_REVISION).generation == 1
    )


def test_first_generation_invalidates_an_empty_search_cache():
    provider = DeterministicEmbeddingProvider()
    bookmark = _bookmark("first-generation", title="alpha newly indexed")
    _seed_index(bookmark=bookmark, create_corpus_state=False)
    assert (
        semantic_search(
            SemanticSourceType.BOOKMARK,
            "alpha",
            provider=provider,
        )
        == ()
    )
    job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert job_id is not None

    completed = _run_queued_job(job_id, provider)

    assert completed.status == SemanticIndexJobStatus.SUCCEEDED
    assert SemanticCorpusState.objects.get(source_type=SemanticSourceType.BOOKMARK).generation == 1
    assert (
        semantic_search(
            SemanticSourceType.BOOKMARK,
            "alpha",
            provider=provider,
        )[0].source_id
        == bookmark.pk
    )


def test_changed_bookmark_signal_queues_and_atomically_replaces_current_index(
    django_capture_on_commit_callbacks,
):
    provider = DeterministicEmbeddingProvider()
    bookmark = _bookmark("changed", page_text="alpha original body")
    original_index = _publish_bookmark(bookmark, provider)
    original_index_id = original_index.pk
    original_hash = original_index.content_hash
    original_chunks = tuple(original_index.chunks.values_list("chunk_text", flat=True))
    initial_generation = SemanticCorpusState.objects.get(
        source_type=SemanticSourceType.BOOKMARK
    ).generation

    with django_capture_on_commit_callbacks(execute=True):
        bookmark.notes = "beta replacement note"
        bookmark.save(update_fields=("notes",))

    changed_job = SemanticIndexJob.objects.get(
        bookmark=bookmark,
        status=SemanticIndexJobStatus.QUEUED,
    )
    assert changed_job.target_content_hash != original_hash
    unchanged = SemanticIndex.objects.get(pk=original_index_id)
    assert unchanged.content_hash == original_hash
    assert tuple(unchanged.chunks.values_list("chunk_text", flat=True)) == original_chunks

    completed = _run_queued_job(changed_job.pk, provider)

    assert completed.status == SemanticIndexJobStatus.SUCCEEDED
    replacement = SemanticIndex.objects.get(bookmark=bookmark)
    assert replacement.pk == original_index_id
    assert replacement.content_hash == changed_job.target_content_hash
    assert replacement.content_hash != original_hash
    assert tuple(replacement.chunks.values_list("chunk_text", flat=True)) != original_chunks
    assert (
        SemanticCorpusState.objects.get(source_type=SemanticSourceType.BOOKMARK).generation
        == initial_generation + 1
    )


def test_source_deletion_cascades_index_jobs_and_chunks_and_invalidates_search_cache():
    provider = DeterministicEmbeddingProvider()
    bookmark = _bookmark("delete", title="alpha disposable source")
    index = _publish_bookmark(bookmark, provider)
    queued_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk, force=True)
    assert queued_id is not None
    index_id = index.pk
    job_ids = tuple(bookmark.semantic_index_jobs.values_list("pk", flat=True))
    chunk_ids = tuple(index.chunks.values_list("pk", flat=True))
    assert (
        semantic_search(
            SemanticSourceType.BOOKMARK,
            "alpha",
            provider=provider,
        )[0].source_id
        == bookmark.pk
    )

    bookmark.delete()

    assert not SemanticIndex.objects.filter(pk=index_id).exists()
    assert not SemanticIndexJob.objects.filter(pk__in=job_ids).exists()
    assert not SemanticChunk.objects.filter(pk__in=chunk_ids).exists()
    assert (
        semantic_search(
            SemanticSourceType.BOOKMARK,
            "alpha",
            provider=provider,
        )
        == ()
    )


def test_provider_failure_retries_then_fails_without_replacing_old_index(settings):
    settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES = 1
    settings.SEMANTIC_JOB_RETRY_SECONDS = 0
    bookmark = _bookmark("retry", page_text="alpha published body")
    old_index = _publish_bookmark(bookmark, DeterministicEmbeddingProvider())
    old_hash = old_index.content_hash
    old_chunk_rows = tuple(old_index.chunks.values_list("ordinal", "chunk_text", "vector"))
    Bookmark.objects.filter(pk=bookmark.pk).update(page_text="beta unpublished body")
    job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert job_id is not None
    failing_provider = DeterministicEmbeddingProvider(fail=True)

    first_attempt = _run_queued_job(job_id, failing_provider)

    assert first_attempt.status == SemanticIndexJobStatus.QUEUED
    assert first_attempt.retry_count == 1
    assert first_attempt.next_attempt_at is not None
    assert first_attempt.completed_at is None

    final_attempt = _run_queued_job(job_id, failing_provider)

    assert final_attempt.status == SemanticIndexJobStatus.FAILED
    assert final_attempt.retry_count == 1
    assert final_attempt.error_code == "embedding_provider_unavailable"
    assert final_attempt.completed_at is not None
    current_index = SemanticIndex.objects.get(bookmark=bookmark)
    assert current_index.pk == old_index.pk
    assert current_index.content_hash == old_hash
    assert (
        tuple(current_index.chunks.values_list("ordinal", "chunk_text", "vector")) == old_chunk_rows
    )


def test_known_exited_semantic_worker_is_requeued_before_lease_expiry(settings):
    settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES = 2
    bookmark = _bookmark("exited-worker", page_text="alpha queued body")
    job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    claimed = claim_next_semantic_job()
    assert claimed.pk == job_id
    SemanticIndexJob.objects.filter(pk=job_id).update(worker_pid=24680)

    sweep_semantic_index_queue(interrupt_worker_pids=(24680,))

    claimed.refresh_from_db()
    assert claimed.status == SemanticIndexJobStatus.QUEUED
    assert claimed.retry_count == 1
    assert claimed.worker_pid is None
    assert claimed.error_code == "worker_interrupted"


def test_stale_source_publication_is_cancelled_and_current_revision_is_requeued(
    django_capture_on_commit_callbacks,
):
    bookmark = _bookmark("stale", page_text="alpha published body")
    old_index = _publish_bookmark(bookmark, DeterministicEmbeddingProvider())
    old_hash = old_index.content_hash
    Bookmark.objects.filter(pk=bookmark.pk).update(page_text="beta requested body")
    stale_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    assert stale_job_id is not None

    def change_source_during_embedding() -> None:
        Bookmark.objects.filter(pk=bookmark.pk).update(page_text="gamma newest body")

    provider = DeterministicEmbeddingProvider(after_documents=change_source_during_embedding)
    with django_capture_on_commit_callbacks(execute=True):
        stale_result = _run_queued_job(stale_job_id, provider)

    assert stale_result.status == SemanticIndexJobStatus.CANCELLED
    assert stale_result.error_code == "source_changed"
    assert stale_result.worker_pid is None
    assert stale_result.heartbeat_at is None
    assert SemanticIndex.objects.get(bookmark=bookmark).content_hash == old_hash
    replacement = SemanticIndexJob.objects.get(
        bookmark=bookmark,
        status=SemanticIndexJobStatus.QUEUED,
    )
    assert replacement.pk != stale_job_id
    assert replacement.target_content_hash == current_source_content_hash(
        SemanticSourceType.BOOKMARK,
        bookmark.pk,
    )


def test_semantic_search_ranks_one_match_per_source_filters_ids_and_reloads_changed_corpus():
    provider = DeterministicEmbeddingProvider()
    strongest = _bookmark("rank-a", title="alpha alpha beta")
    middle = _bookmark("rank-b", title="alpha beta beta")
    weakest = _bookmark("rank-c", title="beta")
    for bookmark in (strongest, middle, weakest):
        _publish_bookmark(bookmark, provider)

    initial = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        minimum_score=-1.0,
        limit=3,
    )

    assert [match.source_id for match in initial] == [strongest.pk, middle.pk, weakest.pk]
    assert [match.score for match in initial] == sorted(
        (match.score for match in initial),
        reverse=True,
    )
    assert len({match.source_id for match in initial}) == len(initial)
    allowed = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        allowed_source_ids={middle.pk, weakest.pk},
        minimum_score=-1.0,
        limit=3,
    )
    assert [match.source_id for match in allowed] == [middle.pk, weakest.pk]
    assert (
        semantic_search(
            SemanticSourceType.BOOKMARK,
            "alpha",
            provider=provider,
            allowed_source_ids=set(),
        )
        == ()
    )

    Bookmark.objects.filter(pk=weakest.pk).update(title="alpha")
    replacement_job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, weakest.pk)
    assert replacement_job_id is not None
    replacement = _run_queued_job(replacement_job_id, provider)
    assert replacement.status == SemanticIndexJobStatus.SUCCEEDED

    reloaded = semantic_search(
        SemanticSourceType.BOOKMARK,
        "alpha",
        provider=provider,
        minimum_score=-1.0,
        limit=3,
    )
    assert reloaded[0].source_id == weakest.pk
    assert reloaded[0].score > reloaded[1].score
