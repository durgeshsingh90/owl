from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction

from bookmark_manager.models import Bookmark, ConfluencePageNode
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.bookmark_outline import (
    next_outline_position,
    outline_number_map,
)
from bookmark_manager.services.deletion import delete_local_bookmark
from bookmark_manager.services.import_export import import_bookmarks_document
from bookmark_manager.services.web_bookmarks import save_web_bookmark

pytestmark = pytest.mark.django_db


def _node_snapshot(page_id: str, title: str) -> ConfluenceNodeSnapshot:
    return ConfluenceNodeSnapshot(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.invalid/wiki/pages/{page_id}",
        space_key="ENG",
    )


def _bookmark(
    page_id: str,
    title: str,
    *,
    ancestors: tuple[ConfluenceNodeSnapshot, ...] = (),
) -> Bookmark:
    return upsert_bookmark(
        ConfluencePageSnapshot(
            page_id=page_id,
            title=title,
            url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
            space_name="Engineering",
            space_key="ENG",
            version=1,
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
            updated_at=datetime(2026, 8, 28, tzinfo=UTC),
            ancestors=ancestors,
        )
    ).bookmark


def test_allocator_returns_lowest_available_root_and_child_positions():
    first_root = ConfluencePageNode.objects.create(
        page_id="10001",
        title="First root",
        outline_position=1,
    )
    ConfluencePageNode.objects.create(
        page_id="10003",
        title="Third root",
        outline_position=3,
    )
    ConfluencePageNode.objects.create(
        page_id="10101",
        title="First child",
        parent=first_root,
        outline_position=1,
    )
    ConfluencePageNode.objects.create(
        page_id="10103",
        title="Third child",
        parent=first_root,
        outline_position=3,
    )

    assert next_outline_position(parent_id=None) == 2
    assert next_outline_position(parent_id=first_root.pk) == 2


def test_deleting_a_top_level_bookmark_reuses_gap_without_renumbering_siblings():
    first = _bookmark("11001", "First root")
    second = _bookmark("11002", "Second root")
    assert (first.tree_node.outline_position, second.tree_node.outline_position) == (1, 2)

    delete_local_bookmark(first, confirmed=True)
    replacement = _bookmark("11003", "Replacement root")
    second.tree_node.refresh_from_db()

    assert replacement.tree_node.outline_position == 1
    assert second.tree_node.outline_position == 2
    numbers = outline_number_map(ConfluencePageNode.objects.all())
    assert numbers[replacement.tree_node_id] == "1"
    assert numbers[second.tree_node_id] == "2"


def test_deleting_a_nested_bookmark_reuses_lowest_sibling_gap_only():
    root = _node_snapshot("12000", "Engineering")
    first = _bookmark("12001", "First child", ancestors=(root,))
    second = _bookmark("12002", "Second child", ancestors=(root,))
    parent_id = first.tree_node.parent_id
    assert parent_id == second.tree_node.parent_id
    assert (first.tree_node.outline_position, second.tree_node.outline_position) == (1, 2)

    delete_local_bookmark(first, confirmed=True)
    replacement = _bookmark("12003", "Replacement child", ancestors=(root,))
    second.tree_node.refresh_from_db()

    assert replacement.tree_node.parent_id == parent_id
    assert replacement.tree_node.outline_position == 1
    assert second.tree_node.outline_position == 2
    numbers = outline_number_map(ConfluencePageNode.objects.all())
    assert numbers[replacement.tree_node_id] == "1.1"
    assert numbers[second.tree_node_id] == "1.2"


def test_web_bookmark_creation_reuses_root_and_nested_gaps():
    first = save_web_bookmark("https://docs.example.org/first").bookmark
    second = save_web_bookmark("https://docs.example.org/second").bookmark
    other_domain = save_web_bookmark("https://support.example.net/start").bookmark

    delete_local_bookmark(first, confirmed=True)
    replacement_child = save_web_bookmark("https://docs.example.org/replacement").bookmark
    second.tree_node.refresh_from_db()
    assert replacement_child.tree_node.outline_position == 1
    assert second.tree_node.outline_position == 2

    delete_local_bookmark(second, confirmed=True)
    delete_local_bookmark(replacement_child, confirmed=True)
    replacement_root = save_web_bookmark("https://new.example.com/start").bookmark
    other_domain.tree_node.parent.refresh_from_db()

    assert replacement_root.tree_node.parent.outline_position == 1
    assert other_domain.tree_node.parent.outline_position == 2


def test_import_creation_path_reuses_a_deleted_top_level_gap():
    deleted = _bookmark("13001", "Deleted root")
    existing = _bookmark("13002", "Existing root")
    delete_local_bookmark(deleted, confirmed=True)

    result = import_bookmarks_document(
        [
            {
                "pageId": "13003",
                "pageTitle": "Imported page",
                "pageUrl": ("https://confluence.example.invalid/wiki/spaces/ENG/pages/13003"),
                "spaceKey": "ENG",
                "savedAt": "2026-08-28T09:00:00Z",
                "breadcrumb": "Imported root > Imported page",
            }
        ],
        filename="outline-gap.json",
    )

    imported = Bookmark.objects.get(page_id="13003")
    imported_root = imported.tree_node.parent
    existing.tree_node.refresh_from_db()
    assert result.imported_records == 1
    assert imported_root.outline_position == 1
    assert imported.tree_node.outline_position == 1
    assert existing.tree_node.outline_position == 2


def test_database_constraints_are_the_final_concurrent_duplicate_guard():
    root = ConfluencePageNode.objects.create(
        page_id="14001",
        title="Root one",
        outline_position=1,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ConfluencePageNode.objects.create(
            page_id="14002",
            title="Competing root",
            outline_position=1,
        )

    ConfluencePageNode.objects.create(
        page_id="14101",
        title="Child one",
        parent=root,
        outline_position=1,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ConfluencePageNode.objects.create(
            page_id="14102",
            title="Competing child",
            parent=root,
            outline_position=1,
        )

    assert ConfluencePageNode.objects.filter(parent__isnull=True, outline_position=1).count() == 1
    assert ConfluencePageNode.objects.filter(parent=root, outline_position=1).count() == 1
