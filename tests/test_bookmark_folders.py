from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from bookmark_manager.models import Bookmark, BookmarkFolder, ConfluencePageNode
from bookmark_manager.services.bookmark_folders import (
    BookmarkFolderError,
    create_bookmark_folder,
    move_bookmark_to_folder,
    move_bookmarks_to_folder,
    unfile_bookmark,
    unfile_bookmarks,
)
from bookmark_manager.services.bookmark_outline import next_outline_position

pytestmark = pytest.mark.django_db


def make_bookmark(page_id: str, *, parent: ConfluencePageNode | None = None) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=page_id,
        title=f"Page {page_id}",
        url=f"https://confluence.example.invalid/pages/{page_id}",
        space_key="ENG",
        parent=parent,
        outline_position=next_outline_position(parent_id=parent.pk if parent else None),
    )
    return Bookmark.objects.create(
        page_id=page_id,
        tree_node=node,
        title=node.title,
        url=node.url,
        space_key="ENG",
    )


def test_folder_name_is_normalized_and_case_insensitively_unique():
    folder = BookmarkFolder.objects.create(name="  Platform   Guides ")

    assert folder.name == "Platform Guides"
    assert folder.normalized_name == "platform guides"
    assert folder.public_id is not None

    with pytest.raises(ValidationError, match="cannot be empty"):
        BookmarkFolder.objects.create(name=" \t ")

    with pytest.raises(IntegrityError), transaction.atomic():
        BookmarkFolder.objects.create(name="PLATFORM GUIDES")


def test_move_one_or_many_preserves_canonical_parent_and_outline_positions():
    source_parent = ConfluencePageNode.objects.create(
        page_id="100",
        title="Source parent",
        url="https://confluence.example.invalid/pages/100",
        space_key="ENG",
        outline_position=4,
    )
    first = make_bookmark("101", parent=source_parent)
    second_node = ConfluencePageNode.objects.create(
        page_id="102",
        title="Page 102",
        url="https://confluence.example.invalid/pages/102",
        space_key="ENG",
        parent=source_parent,
        outline_position=2,
    )
    second = Bookmark.objects.create(
        page_id="102",
        tree_node=second_node,
        title=second_node.title,
        url=second_node.url,
        space_key="ENG",
    )
    folder = create_bookmark_folder(name="Architecture")
    canonical_before = {
        bookmark.pk: (
            bookmark.tree_node_id,
            bookmark.tree_node.parent_id,
            bookmark.tree_node.outline_position,
        )
        for bookmark in (first, second)
    }

    one_result = move_bookmark_to_folder(first, folder)
    many_result = move_bookmarks_to_folder([first.pk, second, first.pk], folder.pk)

    first.refresh_from_db()
    second.refresh_from_db()
    assert one_result.updated_count == 1
    assert many_result.bookmark_ids == (first.pk, second.pk)
    assert many_result.updated_count == 1
    assert first.manual_folder == folder
    assert second.manual_folder == folder
    assert {
        bookmark.pk: (
            bookmark.tree_node_id,
            bookmark.tree_node.parent_id,
            bookmark.tree_node.outline_position,
        )
        for bookmark in (first, second)
    } == canonical_before


def test_unfile_is_selective_idempotent_and_folder_deletion_is_protected():
    first = make_bookmark("201")
    second = make_bookmark("202")
    folder = create_bookmark_folder(name="Reviews")
    move_bookmarks_to_folder([first, second], folder)

    result = unfile_bookmark(first)
    repeated = unfile_bookmarks([first.pk])

    first.refresh_from_db()
    second.refresh_from_db()
    assert result.updated_count == 1
    assert repeated.updated_count == 0
    assert first.manual_folder is None
    assert second.manual_folder == folder
    with pytest.raises(ProtectedError):
        folder.delete()


def test_multi_move_validates_the_whole_selection_before_updating_any_bookmark():
    bookmark = make_bookmark("301")
    folder = create_bookmark_folder(name="Operations")

    with pytest.raises(Bookmark.DoesNotExist, match="999999"):
        move_bookmarks_to_folder([bookmark.pk, 999999], folder)

    bookmark.refresh_from_db()
    assert bookmark.manual_folder is None

    with pytest.raises(BookmarkFolderError, match="Select at least one"):
        move_bookmarks_to_folder([], folder)

    with pytest.raises(BookmarkFolder.DoesNotExist):
        move_bookmark_to_folder(bookmark, 999999)
