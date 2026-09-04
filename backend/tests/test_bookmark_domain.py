from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    ConfluencePageNode,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    InvalidPageIdentity,
    InvalidPageSnapshot,
    normalize_page_id,
    record_successful_open,
    save_bookmark_by_page_id,
    upsert_bookmark,
)

pytestmark = pytest.mark.django_db


def node_snapshot(
    page_id: str,
    title: str,
    *,
    space_key: str = "ENG",
    sibling_position: int | None = None,
) -> ConfluenceNodeSnapshot:
    return ConfluenceNodeSnapshot(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.invalid/wiki/pages/{page_id}",
        space_key=space_key,
        sibling_position=sibling_position,
    )


def page_snapshot(
    page_id: str = "300",
    title: str = "Private DNS Architecture",
    *,
    version: int = 7,
    ancestors: tuple[ConfluenceNodeSnapshot, ...] | None = None,
) -> ConfluencePageSnapshot:
    if ancestors is None:
        ancestors = (
            node_snapshot("100", "Engineering", sibling_position=0),
            node_snapshot("200", "Networking", sibling_position=3),
        )
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
        space_name="Engineering",
        space_key="ENG",
        version=version,
        created_at=datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 8, 24, 16, 15, tzinfo=UTC),
        created_by_id="person-creator",
        created_by_name="Synthetic Creator",
        modified_by_id="person-modifier",
        modified_by_name="Synthetic Modifier",
        author_id="person-author",
        author_name="Synthetic Author",
        ancestors=ancestors,
        sibling_position=4,
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("123", "123"), (" 00123 ", "123"), (987654321, "987654321")],
)
def test_page_id_normalization_has_one_stable_decimal_identity(raw_value, expected):
    assert normalize_page_id(raw_value) == expected


@pytest.mark.parametrize(
    "raw_value",
    ["", "abc", "12.3", "-1", "0", 0, True, "1/2", "9" * 65],
)
def test_invalid_page_ids_are_rejected(raw_value):
    with pytest.raises(InvalidPageIdentity):
        normalize_page_id(raw_value)


def test_bmk_001_new_save_builds_real_hierarchy_and_assigns_only_one_owl_number():
    loader_calls = []

    def loader(page_id: str) -> ConfluencePageSnapshot:
        loader_calls.append(page_id)
        return page_snapshot(page_id)

    result = save_bookmark_by_page_id("00300", loader)

    assert result.created is True
    assert result.source_requested is True
    assert loader_calls == ["300"]
    assert result.bookmark.pk == 1
    assert result.bookmark.page_id == "300"
    assert result.bookmark.open_count == 0
    assert result.bookmark.first_opened_at is None
    assert result.bookmark.last_viewed_at is None
    assert result.bookmark.last_viewed_version is None

    root = ConfluencePageNode.objects.get(page_id="100")
    section = ConfluencePageNode.objects.get(page_id="200")
    page = ConfluencePageNode.objects.get(page_id="300")
    assert root.parent is None
    assert section.parent == root
    assert page.parent == section
    assert result.bookmark.tree_node == page
    assert Bookmark.objects.filter(tree_node__in=(root, section)).count() == 0


def test_bmk_002_existing_page_id_returns_before_the_loader_is_called():
    first = save_bookmark_by_page_id("300", lambda _page_id: page_snapshot())

    def forbidden_loader(_page_id: str) -> ConfluencePageSnapshot:
        raise AssertionError("A duplicate must not make a Confluence request")

    duplicate = save_bookmark_by_page_id("000300", forbidden_loader)

    assert duplicate.created is False
    assert duplicate.source_requested is False
    assert duplicate.bookmark.pk == first.bookmark.pk
    assert Bookmark.objects.count() == 1


def test_bmk_003_similar_titles_with_distinct_ids_remain_distinct():
    first = upsert_bookmark(page_snapshot("300", "Private DNS Architecture"))
    second = upsert_bookmark(page_snapshot("301", "Private DNS Architecture Guide"))

    assert first.bookmark.pk != second.bookmark.pk
    assert first.bookmark.page_id != second.bookmark.page_id
    assert [bookmark.pk for bookmark in second.similar_bookmarks] == [first.bookmark.pk]
    assert Bookmark.objects.count() == 2


def test_bmk_004_shared_ancestor_page_ids_create_one_shared_branch():
    first = upsert_bookmark(page_snapshot("300", "Private DNS Architecture"))
    second = upsert_bookmark(page_snapshot("301", "Network Firewall Architecture"))

    assert first.bookmark.tree_node.parent_id == second.bookmark.tree_node.parent_id
    assert ConfluencePageNode.objects.filter(page_id="100").count() == 1
    assert ConfluencePageNode.objects.filter(page_id="200").count() == 1
    assert ConfluencePageNode.objects.count() == 4
    assert Bookmark.objects.count() == 2


def test_bmk_005_rename_updates_source_fields_but_preserves_owl_owned_state():
    original = upsert_bookmark(page_snapshot()).bookmark
    saved_at = original.saved_at
    tree_node_id = original.tree_node_id
    original.favorite = True
    original.pinned = True
    original.notes = "Keep this local note exactly."
    original.open_count = 9
    original.save(update_fields=["favorite", "pinned", "notes", "open_count"])

    renamed_snapshot = replace(
        page_snapshot(),
        title="Private DNS Architecture — Renamed",
        url="https://confluence.example.invalid/wiki/spaces/ENG/pages/300/renamed",
        version=8,
    )
    renamed = upsert_bookmark(renamed_snapshot).bookmark

    assert renamed.pk == original.pk
    assert renamed.tree_node_id == tree_node_id
    assert renamed.saved_at == saved_at
    assert renamed.title == "Private DNS Architecture — Renamed"
    assert renamed.tree_node.title == "Private DNS Architecture — Renamed"
    assert renamed.url.endswith("/renamed")
    assert renamed.version == 8
    assert renamed.favorite is True
    assert renamed.pinned is True
    assert renamed.notes == "Keep this local note exactly."
    assert renamed.open_count == 9


def test_bmk_005_move_relocates_same_node_and_prunes_only_stale_empty_branch():
    original = upsert_bookmark(page_snapshot()).bookmark
    original_pk = original.pk
    page_node_pk = original.tree_node_id
    old_section_pk = ConfluencePageNode.objects.get(page_id="200").pk

    moved_ancestors = (
        node_snapshot("100", "Engineering", sibling_position=0),
        node_snapshot("250", "Platform Networking", sibling_position=5),
    )
    moved = upsert_bookmark(
        page_snapshot(ancestors=moved_ancestors),
        record_refresh=True,
    ).bookmark

    assert moved.pk == original_pk
    assert moved.tree_node_id == page_node_pk
    assert moved.tree_node.parent.page_id == "250"
    assert not ConfluencePageNode.objects.filter(pk=old_section_pk).exists()
    assert ConfluencePageNode.objects.filter(page_id="100").exists()
    assert ConfluencePageNode.objects.filter(page_id="300").count() == 1


def test_move_keeps_an_old_branch_when_another_bookmark_still_uses_it():
    upsert_bookmark(page_snapshot("300", "Private DNS Architecture"))
    other = upsert_bookmark(page_snapshot("301", "Firewall Architecture")).bookmark

    moved_ancestors = (
        node_snapshot("100", "Engineering"),
        node_snapshot("250", "Platform Networking"),
    )
    upsert_bookmark(page_snapshot("300", ancestors=moved_ancestors))

    old_section = ConfluencePageNode.objects.get(page_id="200")
    other.refresh_from_db()
    assert other.tree_node.parent == old_section
    assert old_section.children.count() == 1


def test_parent_deletion_is_protected_while_the_tree_is_populated():
    upsert_bookmark(page_snapshot())
    root = ConfluencePageNode.objects.get(page_id="100")

    with pytest.raises(ProtectedError):
        root.delete()


def test_loader_page_id_mismatch_creates_no_partial_local_records():
    with pytest.raises(InvalidPageSnapshot, match="different Page ID"):
        save_bookmark_by_page_id("300", lambda _page_id: page_snapshot("999"))

    assert Bookmark.objects.count() == 0
    assert ConfluencePageNode.objects.count() == 0


def test_cyclic_or_duplicate_ancestor_chain_rolls_back_completely():
    invalid = page_snapshot(
        "300",
        ancestors=(
            node_snapshot("100", "Engineering"),
            node_snapshot("100", "Engineering duplicate"),
        ),
    )

    with pytest.raises(InvalidPageSnapshot, match="cycle or duplicate"):
        upsert_bookmark(invalid)

    assert Bookmark.objects.count() == 0
    assert ConfluencePageNode.objects.count() == 0


def test_credential_bearing_source_url_is_rejected_before_any_write():
    credential_bearing_url = (
        "https://" + "synthetic-user" + ":" + "synthetic-password"
        "@confluence.example.invalid/wiki/pages/300"
    )
    invalid = replace(
        page_snapshot(),
        url=credential_bearing_url,
    )

    with pytest.raises(InvalidPageSnapshot, match="safe absolute URL"):
        upsert_bookmark(invalid)

    assert Bookmark.objects.count() == 0
    assert ConfluencePageNode.objects.count() == 0


def test_refresh_snapshot_restores_active_state_and_records_version_change():
    bookmark = upsert_bookmark(page_snapshot(version=7)).bookmark
    bookmark.availability_status = BookmarkAvailability.REFRESH_ERROR
    bookmark.last_error_code = "synthetic-timeout"
    bookmark.last_error_message = "The synthetic source timed out."
    bookmark.save(
        update_fields=[
            "availability_status",
            "last_error_code",
            "last_error_message",
        ]
    )
    observed_at = datetime(2026, 8, 25, 10, 5, tzinfo=UTC)

    refreshed = upsert_bookmark(
        page_snapshot(version=8),
        observed_at=observed_at,
        record_refresh=True,
    ).bookmark

    assert refreshed.availability_status == BookmarkAvailability.ACTIVE
    assert refreshed.last_error_code == ""
    assert refreshed.last_error_message == ""
    assert refreshed.last_refresh_attempt_at == observed_at
    assert refreshed.last_refreshed_at == observed_at
    assert refreshed.last_change_detected_at == observed_at


def test_successful_open_tracking_is_atomic_and_keeps_the_first_open_time():
    bookmark = upsert_bookmark(page_snapshot(version=7)).bookmark
    first_time = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    second_time = first_time + timedelta(hours=2)

    first_open = record_successful_open(bookmark, opened_at=first_time)
    second_open = record_successful_open(bookmark.pk, opened_at=second_time)

    assert first_open.open_count == 1
    assert second_open.open_count == 2
    assert second_open.first_opened_at == first_time
    assert second_open.last_viewed_at == second_time
    assert second_open.last_viewed_version == 7
    assert second_open.changed_since_viewed is False

    upsert_bookmark(page_snapshot(version=8))
    second_open.refresh_from_db()
    assert second_open.changed_since_viewed is True


def test_naive_usage_timestamp_is_rejected_without_changing_usage():
    bookmark = upsert_bookmark(page_snapshot()).bookmark

    with pytest.raises(InvalidPageSnapshot, match="timezone"):
        record_successful_open(bookmark, opened_at=datetime(2026, 8, 25, 9, 0))

    bookmark.refresh_from_db()
    assert bookmark.open_count == 0
    assert bookmark.first_opened_at is None


def test_owl_numbers_are_not_renumbered_or_reused_after_delete():
    first = upsert_bookmark(page_snapshot("300", "First Page")).bookmark
    second = upsert_bookmark(page_snapshot("301", "Second Page")).bookmark
    first_number = first.pk
    second_number = second.pk
    second.delete()

    third = upsert_bookmark(page_snapshot("302", "Third Page")).bookmark

    first.refresh_from_db()
    assert first.pk == first_number
    assert third.pk > second_number
    assert not Bookmark.objects.filter(pk=second_number).exists()


def test_database_constraints_prevent_duplicate_identity_and_tree_link():
    bookmark = upsert_bookmark(page_snapshot()).bookmark

    with pytest.raises(IntegrityError), transaction.atomic():
        Bookmark.objects.create(
            page_id=bookmark.page_id,
            tree_node=bookmark.tree_node,
            title="Duplicate",
            url=bookmark.url,
        )


def test_hierarchy_node_must_have_a_source_or_provisional_identity():
    with pytest.raises(IntegrityError), transaction.atomic():
        ConfluencePageNode.objects.create(title="Identity-free node")
