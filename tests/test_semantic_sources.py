import hashlib

import pytest

from bitbucket_search.models import (
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bookmark_manager.models import Bookmark, ConfluencePageNode
from semantic_search.models import SemanticSourceType
from semantic_search.services.chunking import chunk_text, normalize_semantic_text
from semantic_search.services.sources import (
    SourceUnavailable,
    build_bookmark_source_snapshot,
    build_pdf_source_snapshot,
    current_source_content_hash,
    load_source_snapshot,
)

pytestmark = pytest.mark.django_db


def _revision(*, digest: str = "a" * 64, extractor: str = "test-extractor-v1"):
    return PDFTextRevision.objects.create(
        content_sha256=digest,
        extractor_version=extractor,
        source_byte_size=100,
        state=PDFTextRevisionState.READY,
        page_count=3,
        extracted_character_count=100,
    )


def _page(revision, page_number: int, text: str):
    return PDFTextPage.objects.create(
        revision=revision,
        page_number=page_number,
        extracted_text=text,
        character_count=len(text),
        extraction_state=(
            PDFPageExtractionState.READY if text.strip() else PDFPageExtractionState.NO_TEXT
        ),
    )


def _bookmark(*, title="Architecture guide", page_text="", notes=""):
    node = ConfluencePageNode.objects.create(
        page_id="998877",
        title=title,
        url="https://confluence.example.test/pages/998877",
    )
    return Bookmark.objects.create(
        page_id="998877",
        tree_node=node,
        title=title,
        url=node.url,
        page_text=page_text,
        notes=notes,
    )


def test_normalization_and_paragraph_word_overlap_are_deterministic(settings):
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 34
    settings.SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 9
    source = "  Fullwidth Ａ\r\nline\twith spaces\r\n\r\n\r\nSecond paragraph has words.  "

    normalized = normalize_semantic_text(source)
    first = chunk_text(source, page_number=4)
    second = chunk_text(normalized, page_number=4)

    assert normalized == "Fullwidth A\nline with spaces\n\nSecond paragraph has words."
    assert first == second
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert all(chunk.page_number == 4 for chunk in first)
    assert all(chunk.chunk_text and len(chunk.chunk_text) <= 34 for chunk in first)
    assert all(
        chunk.text_hash == hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest()
        for chunk in first
    )
    assert len(first) > 1
    assert set(first[0].chunk_text.split()) & set(first[1].chunk_text.split())


@pytest.mark.parametrize(
    ("max_chars", "overlap"),
    ((0, 0), (10, -1), (10, 10), (True, 0), (10, False)),
)
def test_chunk_configuration_rejects_non_progressing_values(max_chars, overlap):
    with pytest.raises(ValueError):
        chunk_text("human text", max_chars=max_chars, overlap=overlap)


def test_pdf_snapshot_orders_pages_skips_blanks_and_preserves_page_locators(settings):
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 24
    settings.SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 5
    revision = _revision()
    _page(revision, 3, "Third page semantic text")
    _page(revision, 1, "First page has enough words for chunks")
    _page(revision, 2, " \r\n\t ")

    snapshot = build_pdf_source_snapshot(revision.pk)

    assert snapshot.source_type == SemanticSourceType.PDF_REVISION
    assert snapshot.source_id == revision.pk
    assert [chunk.ordinal for chunk in snapshot.chunks] == list(range(len(snapshot.chunks)))
    assert [chunk.page_number for chunk in snapshot.chunks] == sorted(
        chunk.page_number for chunk in snapshot.chunks
    )
    assert {chunk.page_number for chunk in snapshot.chunks} == {1, 3}
    assert snapshot.character_count == len("First page has enough words for chunks") + len(
        "Third page semantic text"
    )
    assert snapshot.content_hash == current_source_content_hash(
        SemanticSourceType.PDF_REVISION,
        revision.pk,
    )


def test_pdf_content_hash_uses_immutable_identity_not_page_loading(django_assert_num_queries):
    revision = _revision(digest="b" * 64, extractor="extractor-v2")
    _page(revision, 1, "Stored page text")

    with django_assert_num_queries(1):
        first = current_source_content_hash(SemanticSourceType.PDF_REVISION, revision.pk)
    _page(revision, 2, "Unexpected extra text")
    second = current_source_content_hash(SemanticSourceType.PDF_REVISION, revision.pk)

    assert first == second
    expected_identity = "\0".join(
        ("owl-semantic-source-v1", "pdf_revision", "b" * 64, "extractor-v2")
    )
    assert first == hashlib.sha256(expected_identity.encode()).hexdigest()
    assert first != current_source_content_hash(
        SemanticSourceType.PDF_REVISION,
        _revision(digest="b" * 64, extractor="extractor-v3").pk,
    )


def test_bookmark_snapshot_labels_only_stored_human_text_and_never_url(settings):
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 2_000
    settings.SEMANTIC_CHUNK_OVERLAP_CHARACTERS = 100
    bookmark = _bookmark(
        title="Network Ａrchitecture",
        page_text="Stored\r\npage body",
        notes="My local note",
    )
    bookmark.tags.create(name="Zero Trust", normalized_name="zero trust")
    bookmark.tags.create(name="Cloud", normalized_name="cloud")

    snapshot = build_bookmark_source_snapshot(bookmark.pk)
    combined = "\n".join(chunk.chunk_text for chunk in snapshot.chunks)

    assert snapshot.source_type == SemanticSourceType.BOOKMARK
    assert "Title: Network Architecture" in combined
    assert "Page text:\nStored\npage body" in combined
    assert "Notes:\nMy local note" in combined
    assert "Tags: Cloud, Zero Trust" in combined
    assert bookmark.url not in combined
    assert snapshot.character_count == len(snapshot.chunks[0].chunk_text)
    assert (
        snapshot.content_hash == hashlib.sha256(snapshot.chunks[0].chunk_text.encode()).hexdigest()
    )


def test_bookmark_without_page_text_still_embeds_title_notes_and_tags(settings):
    settings.SEMANTIC_CHUNK_MAX_CHARACTERS = 2_000
    bookmark = _bookmark(title="Saved webpage", notes="Review after VPN login")
    bookmark.tags.create(name="Internal", normalized_name="internal")

    snapshot = load_source_snapshot(SemanticSourceType.BOOKMARK, bookmark.pk)
    text = snapshot.chunks[0].chunk_text

    assert text == "Title: Saved webpage\n\nNotes:\nReview after VPN login\n\nTags: Internal"
    assert "Page text:" not in text


def test_bookmark_snapshot_uses_the_prefetched_tag_objects(django_assert_num_queries):
    bookmark = _bookmark(title="Prefetched bookmark")
    bookmark.tags.create(name="Architecture", normalized_name="architecture")
    bookmark.tags.create(name="Cloud", normalized_name="cloud")

    with django_assert_num_queries(2):
        snapshot = build_bookmark_source_snapshot(bookmark.pk)

    assert snapshot.chunks[0].chunk_text.endswith("Tags: Architecture, Cloud")


def test_bookmark_hash_changes_with_notes_and_tags_but_is_order_stable():
    bookmark = _bookmark(page_text="Stable stored body", notes="First note")
    first_hash = current_source_content_hash(SemanticSourceType.BOOKMARK, bookmark.pk)

    bookmark.notes = "Second note"
    bookmark.save(update_fields=("notes",))
    notes_hash = current_source_content_hash(SemanticSourceType.BOOKMARK, bookmark.pk)
    bookmark.tags.create(name="Zulu", normalized_name="zulu")
    bookmark.tags.create(name="Alpha", normalized_name="alpha")
    tags_hash = current_source_content_hash(SemanticSourceType.BOOKMARK, bookmark.pk)

    assert first_hash != notes_hash != tags_hash
    assert (
        build_bookmark_source_snapshot(bookmark.pk)
        .chunks[-1]
        .chunk_text.endswith("Tags: Alpha, Zulu")
    )


def test_deleted_sources_are_unavailable():
    revision = _revision()
    bookmark = _bookmark()
    revision_id = revision.pk
    bookmark_id = bookmark.pk
    revision.delete()
    bookmark.delete()

    with pytest.raises(SourceUnavailable):
        load_source_snapshot(SemanticSourceType.PDF_REVISION, revision_id)
    with pytest.raises(SourceUnavailable):
        current_source_content_hash(SemanticSourceType.BOOKMARK, bookmark_id)
