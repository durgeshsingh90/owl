from __future__ import annotations

import pytest
from django.db import transaction

from bookmark_manager.models import Bookmark, ConfluencePageNode, Tag
from semantic_search.models import SemanticIndex, SemanticSourceType

pytestmark = pytest.mark.django_db


def _bookmark(key: str) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=f"semantic-signal-{key}",
        title=f"Bookmark {key}",
        url=f"https://confluence.example.test/pages/semantic-signal-{key}",
    )
    return Bookmark.objects.create(
        page_id=node.page_id,
        tree_node=node,
        title=node.title,
        url=node.url,
    )


def _tag(name: str) -> Tag:
    return Tag.objects.create(name=name, normalized_name=Tag.normalize_name(name))


def _semantic_index(bookmark: Bookmark) -> SemanticIndex:
    return SemanticIndex.objects.create(
        source_type=SemanticSourceType.BOOKMARK,
        bookmark=bookmark,
        content_hash="0" * 64,
        model_version="semantic-signal-test-model-v1",
        chunker_version="semantic-signal-test-chunker-v1",
        dimensions=3,
        centroid_vector=b"\x00" * 12,
    )


def test_tag_rename_and_delete_queue_every_attached_bookmark(
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    from semantic_search import signals

    bookmarks = (_bookmark("tag-a"), _bookmark("tag-b"))
    tag = _tag("Architecture")
    tag.bookmarks.add(*bookmarks)
    queued: list[tuple[str, int]] = []
    monkeypatch.setattr(
        signals,
        "_queue_safely",
        lambda source_type, source_id: queued.append((source_type, source_id)),
    )

    with django_capture_on_commit_callbacks(execute=True):
        tag.name = "Platform Architecture"
        tag.save(update_fields=("name",))

    assert queued == [
        (SemanticSourceType.BOOKMARK, bookmarks[0].pk),
        (SemanticSourceType.BOOKMARK, bookmarks[1].pk),
    ]

    queued.clear()
    with django_capture_on_commit_callbacks(execute=True):
        tag.delete()

    assert queued == [
        (SemanticSourceType.BOOKMARK, bookmarks[0].pk),
        (SemanticSourceType.BOOKMARK, bookmarks[1].pk),
    ]


def test_reverse_tag_relationship_changes_queue_every_affected_bookmark(
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    from semantic_search import signals

    bookmarks = (_bookmark("reverse-a"), _bookmark("reverse-b"))
    tag = _tag("Security")
    queued: list[tuple[str, int]] = []
    monkeypatch.setattr(
        signals,
        "_queue_safely",
        lambda source_type, source_id: queued.append((source_type, source_id)),
    )

    with django_capture_on_commit_callbacks(execute=True):
        tag.bookmarks.add(*bookmarks)
    assert queued == [
        (SemanticSourceType.BOOKMARK, bookmarks[0].pk),
        (SemanticSourceType.BOOKMARK, bookmarks[1].pk),
    ]

    queued.clear()
    with django_capture_on_commit_callbacks(execute=True):
        tag.bookmarks.remove(bookmarks[0])
    assert queued == [(SemanticSourceType.BOOKMARK, bookmarks[0].pk)]

    queued.clear()
    with django_capture_on_commit_callbacks(execute=True):
        tag.bookmarks.clear()
    assert queued == [(SemanticSourceType.BOOKMARK, bookmarks[1].pk)]


def test_semantic_index_deletions_coalesce_generation_bumps_inside_one_transaction(
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    from semantic_search import signals
    from semantic_search.services import search

    indexes = (_semantic_index(_bookmark("delete-a")), _semantic_index(_bookmark("delete-b")))
    bumped: list[str] = []
    cache_clears: list[None] = []
    monkeypatch.setattr(
        signals,
        "bump_semantic_corpus_generation",
        lambda source_type: bumped.append(source_type),
    )
    monkeypatch.setattr(
        search,
        "clear_semantic_search_cache",
        lambda: cache_clears.append(None),
    )

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        indexes[0].delete()
        indexes[1].delete()

    assert bumped == [SemanticSourceType.BOOKMARK]
    assert len(cache_clears) == 2
