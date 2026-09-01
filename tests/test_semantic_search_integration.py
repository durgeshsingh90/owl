from __future__ import annotations

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bitbucket_search.services import pdf_search
from bitbucket_search.services.pdf_search_query import (
    PDFSearchFilters,
    PDFSearchQuery,
    PDFSearchScope,
)
from bookmark_manager.models import Bookmark, ConfluencePageNode, Tag
from bookmark_manager.services import bookmark_query
from bookmark_manager.services.bookmark_query import BookmarkQuery
from semantic_search.models import SemanticSourceType
from semantic_search.services.search import SemanticSearchMatch

pytestmark = pytest.mark.django_db


def _bookmark(
    page_id: str,
    title: str,
    *,
    space_key: str = "ENG",
    **values,
) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.test/pages/{page_id}",
        space_key=space_key,
    )
    return Bookmark.objects.create(
        page_id=page_id,
        tree_node=node,
        title=title,
        space_name=space_key,
        space_key=space_key,
        url=f"https://confluence.example.test/spaces/{space_key}/pages/{page_id}",
        **values,
    )


def _tag(bookmark: Bookmark, name: str) -> None:
    tag, _created = Tag.objects.get_or_create_normalized(name)
    bookmark.tags.add(tag)


def _repository(name: str, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name.casefold()}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name.casefold()}.git",
        enabled=enabled,
    )


def _indexed_document(
    repository: BitbucketRepository,
    *,
    filename: str,
    path: str,
    pages: tuple[str, ...],
    digest: str,
    committer_name: str,
) -> PDFDocument:
    revision = PDFTextRevision.objects.create(
        content_sha256=(digest * 64)[:64],
        extractor_version="semantic-integration-test-v1",
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
                extraction_state=PDFPageExtractionState.READY,
            )
            for page_number, text in enumerate(pages, start=1)
        ]
    )
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash=(digest * 40)[:40],
        author_name=committer_name,
        committer_name=committer_name,
        authored_at=timezone.now(),
        committed_at=timezone.now(),
    )
    git_blob_id = (digest * 40)[:40]
    return PDFDocument.objects.create(
        repository=repository,
        filename=filename,
        relative_path=path,
        git_blob_id=git_blob_id,
        last_commit=commit,
        indexed_revision=revision,
        indexed_git_blob_id=git_blob_id,
        index_state=PDFIndexState.READY,
        page_count=len(pages),
        extracted_character_count=sum(len(page) for page in pages),
    )


def test_bookmark_exact_multiword_search_remains_primary(monkeypatch):
    target = _bookmark(
        "semantic-bookmark-1",
        "Alpha architecture",
        page_text="Contains omega guidance.",
    )

    def semantic_must_not_run(*args, **kwargs):
        pytest.fail("Semantic fallback ran despite an exact stored-text match")

    monkeypatch.setattr(bookmark_query, "semantic_search", semantic_must_not_run)

    result = bookmark_query.query_bookmarks(BookmarkQuery(search="alpha omega"))

    assert result.bookmarks == (target,)
    assert result.semantic_fallback_used is False


def test_bookmark_zero_exact_uses_semantics_after_applying_filters(monkeypatch):
    target = _bookmark(
        "semantic-bookmark-2",
        "Selected page",
        favorite=True,
        author_name="Alice",
    )
    _tag(target, "Architecture")
    excluded_by_favorite = _bookmark(
        "semantic-bookmark-3",
        "Not a favorite",
        favorite=False,
        author_name="Alice",
    )
    _tag(excluded_by_favorite, "Architecture")
    excluded_by_space = _bookmark(
        "semantic-bookmark-4",
        "Wrong space",
        space_key="FIN",
        favorite=True,
        author_name="Alice",
    )
    _tag(excluded_by_space, "Architecture")

    def fake_semantic_search(source_type, query, *, allowed_source_ids):
        assert source_type is SemanticSourceType.BOOKMARK
        assert query == "continuity blueprint"
        assert allowed_source_ids == frozenset({target.pk})
        return (
            SemanticSearchMatch(
                source_id=target.pk,
                score=0.82,
                page_number=None,
                snippet="Selected page related content",
            ),
        )

    monkeypatch.setattr(bookmark_query, "semantic_search", fake_semantic_search)

    result = bookmark_query.query_bookmarks(
        BookmarkQuery(
            search="continuity blueprint",
            favorite=True,
            tags=("Architecture",),
            spaces=("ENG",),
        )
    )

    assert result.bookmarks == (target,)
    assert result.semantic_fallback_used is True


def test_bookmark_semantic_fallback_preserves_relevance_order(monkeypatch):
    strongest = _bookmark("semantic-bookmark-ranked-1", "Older strongest result")
    weaker = _bookmark("semantic-bookmark-ranked-2", "Newer weaker result")

    def fake_semantic_search(source_type, query, *, allowed_source_ids):
        assert source_type is SemanticSourceType.BOOKMARK
        assert query == "resilient service design"
        assert allowed_source_ids == frozenset({strongest.pk, weaker.pk})
        return (
            SemanticSearchMatch(
                source_id=strongest.pk,
                score=0.94,
                page_number=None,
                snippet="Strongest related content",
            ),
            SemanticSearchMatch(
                source_id=weaker.pk,
                score=0.71,
                page_number=None,
                snippet="Weaker related content",
            ),
        )

    monkeypatch.setattr(bookmark_query, "semantic_search", fake_semantic_search)

    result = bookmark_query.query_bookmarks(BookmarkQuery(search="resilient service design"))

    assert result.semantic_fallback_used is True
    assert result.bookmarks == (strongest, weaker)


def test_bookmark_semantic_failure_returns_an_ordinary_empty_result(monkeypatch):
    _bookmark("semantic-bookmark-5", "Stored page")

    def failed_semantic_search(*args, **kwargs):
        raise RuntimeError("deterministic semantic provider failure")

    monkeypatch.setattr(bookmark_query, "semantic_search", failed_semantic_search)

    result = bookmark_query.query_bookmarks(BookmarkQuery(search="no exact result"))

    assert result.bookmarks == ()
    assert result.semantic_fallback_used is False


def test_pdf_exact_fts_search_remains_primary(monkeypatch):
    repository = _repository("Exact PDF repository")
    target = _indexed_document(
        repository,
        filename="Exact.pdf",
        path="docs/Exact.pdf",
        pages=("The stored runbook documents aurora failover.",),
        digest="a",
        committer_name="Alice",
    )

    def semantic_must_not_run(*args, **kwargs):
        pytest.fail("Semantic fallback ran despite an exact FTS result")

    monkeypatch.setattr(pdf_search, "semantic_search", semantic_must_not_run)

    result = pdf_search.search_documents(
        PDFSearchQuery(chips=("aurora failover",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert [hit.document for hit in result.results] == [target]
    assert result.semantic_fallback_used is False


def test_pdf_zero_exact_uses_semantics_with_repository_and_committer_filters(monkeypatch):
    selected_repository = _repository("Selected PDF repository")
    other_repository = _repository("Other PDF repository")
    target = _indexed_document(
        selected_repository,
        filename="Target.pdf",
        path="docs/Target.pdf",
        pages=("Horizontal queues coordinate work.", "Workers survive node loss."),
        digest="b",
        committer_name="Alice Smith",
    )
    _indexed_document(
        selected_repository,
        filename="Wrong committer.pdf",
        path="docs/Wrong committer.pdf",
        pages=("Another document.",),
        digest="c",
        committer_name="Bob Jones",
    )
    _indexed_document(
        other_repository,
        filename="Wrong repository.pdf",
        path="docs/Wrong repository.pdf",
        pages=("Another document.",),
        digest="d",
        committer_name="Alice Smith",
    )

    def fake_semantic_search(source_type, query, *, allowed_source_ids):
        assert source_type is SemanticSourceType.PDF_REVISION
        assert query == "failure tolerant processing"
        assert allowed_source_ids == frozenset({target.indexed_revision_id})
        return (
            SemanticSearchMatch(
                source_id=target.indexed_revision_id,
                score=0.91,
                page_number=2,
                snippet="Workers survive node loss.",
            ),
        )

    monkeypatch.setattr(pdf_search, "semantic_search", fake_semantic_search)

    result = pdf_search.search_documents(
        PDFSearchQuery(
            chips=("failure tolerant processing",),
            scopes=(PDFSearchScope.CONTENT,),
            filters=PDFSearchFilters(
                repository_ids=(selected_repository.pk,),
                committer_names=("alice smith",),
            ),
        )
    )

    assert result.semantic_fallback_used is True
    assert result.total == 1
    assert len(result.results) == 1
    hit = result.results[0]
    assert hit.document == target
    assert hit.score == pytest.approx(0.91)
    assert hit.best_page_number == 2
    assert hit.explanations[0].scopes == ("Related PDF content",)
    assert hit.explanations[0].page_numbers == (2,)
    assert "".join(part.text for part in hit.snippet) == "Workers survive node loss."


def test_pdf_semantic_failure_returns_an_ordinary_empty_result(monkeypatch):
    repository = _repository("Failed semantic PDF repository")
    _indexed_document(
        repository,
        filename="Stored.pdf",
        path="docs/Stored.pdf",
        pages=("Stored content.",),
        digest="e",
        committer_name="Alice",
    )

    def failed_semantic_search(*args, **kwargs):
        raise RuntimeError("deterministic semantic provider failure")

    monkeypatch.setattr(pdf_search, "semantic_search", failed_semantic_search)

    result = pdf_search.search_documents(
        PDFSearchQuery(chips=("no exact result",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert result.total == 0
    assert result.results == ()
    assert result.semantic_fallback_used is False


def test_pdf_semantic_fallback_paginates_one_hundred_and_caps_the_ranked_corpus(monkeypatch):
    repository = _repository("Paginated semantic PDF repository")
    revisions = PDFTextRevision.objects.bulk_create(
        [
            PDFTextRevision(
                content_sha256=f"{number:064x}",
                extractor_version="semantic-pagination-test-v1",
                source_byte_size=100,
                state=PDFTextRevisionState.READY,
                page_count=1,
                extracted_character_count=20,
            )
            for number in range(1, 261)
        ]
    )
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Semantic guide {number:03d}.pdf",
                relative_path=f"docs/semantic-guide-{number:03d}.pdf",
                git_blob_id=f"{number:040x}",
                indexed_revision=revision,
                indexed_git_blob_id=f"{number:040x}",
                index_state=PDFIndexState.READY,
                page_count=1,
                extracted_character_count=20,
            )
            for number, revision in enumerate(revisions, start=1)
        ],
        batch_size=100,
    )

    ranked_revisions = tuple(revisions[:250])

    def fake_semantic_search(source_type, query, *, allowed_source_ids):
        assert source_type is SemanticSourceType.PDF_REVISION
        assert query == "meaning based discovery"
        assert allowed_source_ids == frozenset(revision.pk for revision in revisions)
        return tuple(
            SemanticSearchMatch(
                source_id=revision.pk,
                score=1.0 - (position / 1_000),
                page_number=position + 1,
                snippet=f"Related content {position + 1}",
            )
            for position, revision in enumerate(ranked_revisions)
        )

    monkeypatch.setattr(pdf_search, "semantic_search", fake_semantic_search)

    result = pdf_search.search_documents(
        PDFSearchQuery(
            chips=("meaning based discovery",),
            scopes=(PDFSearchScope.CONTENT,),
            page=3,
            page_size=100,
        )
    )

    assert result.semantic_fallback_used is True
    assert result.total == 250
    assert result.page_count == 3
    assert len(result.results) == 50
    assert [hit.document.indexed_revision_id for hit in result.results] == [
        revision.pk for revision in ranked_revisions[200:250]
    ]
    assert result.results[0].best_page_number == 201
    assert result.results[-1].best_page_number == 250
