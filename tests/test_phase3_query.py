from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkRecency,
    ConfluencePageNode,
    SavedBookmarkView,
    Tag,
)
from bookmark_manager.services.bookmark_query import (
    BookmarkDateField,
    BookmarkDateFilter,
    BookmarkQuery,
    BookmarkSort,
    DatePreset,
    InvalidBookmarkQuery,
    active_filter_descriptors,
    query_bookmarks,
    sort_requires_flat_mode,
    visible_node_ids_with_ancestors,
)

pytestmark = pytest.mark.django_db


def _node(
    page_id: str,
    title: str,
    *,
    parent: ConfluencePageNode | None = None,
) -> ConfluencePageNode:
    return ConfluencePageNode.objects.create(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.test/pages/{page_id}",
        space_key="ENG",
        parent=parent,
    )


def _bookmark(
    page_id: str,
    title: str,
    *,
    parent: ConfluencePageNode | None = None,
    **values,
) -> Bookmark:
    node = _node(page_id, title, parent=parent)
    defaults = {"space_name": "Engineering", "space_key": "ENG"}
    defaults.update(values)
    defaults.setdefault(
        "url",
        f"https://confluence.example.test/spaces/{defaults['space_key']}/pages/{page_id}/{title}",
    )
    return Bookmark.objects.create(
        page_id=page_id,
        tree_node=node,
        title=title,
        **defaults,
    )


def _tag(bookmark: Bookmark, name: str) -> None:
    tag, _created = Tag.objects.get_or_create_normalized(name)
    bookmark.tags.add(tag)


@pytest.mark.parametrize(
    "search",
    [
        "PrivateLink runbook",
        "99101",
        "#fragment-safe-url",
        "Engineering",
        "ENG",
        "alice-42",
        "Alice Author",
        "Carla Creator",
        "Morgan Modifier",
        "Network Edge",
        "never sent to confluence",
        "Knowledge > Cloud Architecture",
        "Knowledge / Cloud Architecture",
    ],
)
def test_local_search_covers_identity_metadata_people_tags_notes_and_breadcrumb(search):
    root = _node("99001", "Knowledge")
    section = _node("99002", "Cloud Architecture", parent=root)
    target = _bookmark(
        "99101",
        "PrivateLink runbook",
        parent=section,
        url="https://confluence.example.test/spaces/ENG/pages/99101/#fragment-safe-url",
        author_id="alice-42",
        author_name="Alice Author",
        created_by_id="carla-7",
        created_by_name="Carla Creator",
        modified_by_id="morgan-9",
        modified_by_name="Morgan Modifier",
        notes="Local only: never sent to Confluence.",
    )
    _tag(target, "Network Edge")
    _bookmark("99201", "Unrelated page", space_name="Finance", space_key="FIN")

    result = query_bookmarks(BookmarkQuery(search=search))

    assert [bookmark.pk for bookmark in result.bookmarks] == [target.pk]
    assert result.matched_node_ids == {target.tree_node_id}
    assert result.visible_node_ids == {root.pk, section.pk, target.tree_node_id}
    assert result.active_filters[0].key == "search"


def test_numeric_search_also_finds_the_permanent_owl_number():
    target = _bookmark("800001", "Permanent OWL identity")
    _bookmark("800002", "Another page")

    result = query_bookmarks(BookmarkQuery(search=str(target.pk)))
    hash_result = query_bookmarks(BookmarkQuery(search=f"#{target.pk}"))

    assert target in result.bookmarks
    assert hash_result.bookmarks == (target,)


def test_combined_filters_use_and_between_groups_and_all_selected_tags():
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    root = _node("10000", "Root")
    target = _bookmark(
        "10001",
        "Target",
        parent=root,
        favorite=True,
        pinned=True,
        author_name="Alice",
        saved_at=now - timedelta(days=45),
        created_at=now - timedelta(days=200),
        updated_at=now - timedelta(days=2),
        version=8,
        last_viewed_version=7,
        last_viewed_at=now - timedelta(days=10),
        last_refreshed_at=now - timedelta(days=1),
        open_count=5,
    )
    _tag(target, "AWS")
    _tag(target, "Private Link")
    missing_tag = _bookmark(
        "10002",
        "Has only one tag",
        parent=root,
        favorite=True,
        pinned=True,
        author_name="Alice",
        saved_at=now - timedelta(days=45),
        updated_at=now - timedelta(days=2),
        version=8,
        last_viewed_version=7,
        last_viewed_at=now - timedelta(days=10),
        open_count=5,
    )
    _tag(missing_tag, "AWS")
    broken = _bookmark(
        "10003",
        "Broken match",
        parent=root,
        favorite=True,
        pinned=True,
        author_name="Alice",
        saved_at=now - timedelta(days=45),
        updated_at=now - timedelta(days=2),
        version=8,
        last_viewed_version=7,
        last_viewed_at=now - timedelta(days=10),
        open_count=5,
        availability_status=BookmarkAvailability.ACCESS_DENIED,
    )
    _tag(broken, "AWS")
    _tag(broken, "Private Link")

    query = BookmarkQuery(
        favorite=True,
        pinned=True,
        tags=("aws", "PRIVATE   LINK"),
        people=("Alice",),
        spaces=("ENG",),
        availability=(BookmarkAvailability.ACTIVE,),
        recency=(BookmarkRecency.UPDATED,),
        changed_since_viewed=True,
        dates=(BookmarkDateFilter(BookmarkDateField.UPDATED, DatePreset.LAST_7_DAYS),),
        open_count_min=4,
        open_count_max=6,
        recently_changed_days=7,
        broken=False,
    )
    result = query_bookmarks(query, at=now)

    assert result.bookmarks == (target,)
    assert result.visible_node_ids == {root.pk, target.tree_node_id}
    assert result.active_filter_count == 13
    assert {descriptor.key for descriptor in result.active_filters} >= {
        "favorite",
        "pinned",
        "tag",
        "person",
        "space",
        "availability",
        "recency",
        "changed_since_viewed",
        "date:updated",
        "open_count",
        "recently_changed",
        "broken",
    }
    assert result.counts.matching == 1
    assert result.counts.all_bookmarks == 3
    assert result.counts.by_recency == {BookmarkRecency.UPDATED: 1}
    assert result.counts.by_tag == {"AWS": 1, "Private Link": 1}


def test_recency_boundaries_and_availability_are_independent_dimensions():
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    new_but_denied = _bookmark(
        "20001",
        "New but inaccessible",
        saved_at=now - timedelta(days=30),
        updated_at=now - timedelta(days=1),
        availability_status=BookmarkAvailability.ACCESS_DENIED,
    )
    updated = _bookmark(
        "20002",
        "Updated active",
        saved_at=now - timedelta(days=31),
        updated_at=now - timedelta(days=30),
    )
    denied_old = _bookmark(
        "20003",
        "Denied old",
        saved_at=now - timedelta(days=31),
        updated_at=now - timedelta(days=1),
        availability_status=BookmarkAvailability.ACCESS_DENIED,
    )
    normal = _bookmark(
        "20004",
        "Normal",
        saved_at=now - timedelta(days=31),
        updated_at=now - timedelta(days=31),
    )

    new_result = query_bookmarks(BookmarkQuery(recency=(BookmarkRecency.NEW,)), at=now)
    updated_result = query_bookmarks(BookmarkQuery(recency=(BookmarkRecency.UPDATED,)), at=now)
    normal_result = query_bookmarks(BookmarkQuery(recency=(BookmarkRecency.NORMAL,)), at=now)
    broken_result = query_bookmarks(BookmarkQuery(broken=True), at=now)

    assert new_result.bookmarks == (new_but_denied,)
    assert updated_result.bookmarks == (updated,)
    assert set(normal_result.bookmarks) == {denied_old, normal}
    assert set(broken_result.bookmarks) == {new_but_denied, denied_old}


def test_date_presets_custom_ranges_and_open_count_are_inclusive():
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    inside = _bookmark(
        "30001",
        "Inside",
        saved_at=datetime(2025, 6, 15, 9, tzinfo=UTC),
        created_at=datetime(2025, 3, 10, 9, tzinfo=UTC),
        open_count=3,
    )
    _bookmark(
        "30002",
        "Outside",
        saved_at=datetime(2026, 1, 10, 9, tzinfo=UTC),
        created_at=datetime(2024, 12, 31, 23, tzinfo=UTC),
        open_count=4,
    )

    last_year = query_bookmarks(
        BookmarkQuery(dates=(BookmarkDateFilter(BookmarkDateField.ADDED, DatePreset.LAST_YEAR),)),
        at=now,
    )
    custom = query_bookmarks(
        BookmarkQuery(
            dates=(
                BookmarkDateFilter(
                    BookmarkDateField.CREATED,
                    DatePreset.CUSTOM_RANGE,
                    start=date(2025, 3, 10),
                    end=date(2025, 3, 10),
                ),
            ),
            open_count_min=3,
            open_count_max=3,
        ),
        at=now,
    )

    assert last_year.bookmarks == (inside,)
    assert custom.bookmarks == (inside,)


def test_saved_view_filter_state_round_trips_without_transient_tree_state():
    original = BookmarkQuery(
        search="private link",
        favorite=True,
        tags=("Cloud",),
        recency=(BookmarkRecency.NEW,),
        dates=(
            BookmarkDateFilter(
                BookmarkDateField.UPDATED,
                DatePreset.CUSTOM_RANGE,
                start=date(2026, 1, 1),
                end=date(2026, 8, 25),
            ),
        ),
        sort=BookmarkSort.MOST_OPENED,
    )
    view = SavedBookmarkView.objects.create(
        name="Cloud priorities",
        search_text=original.search,
        filters=original.to_filter_dict(),
        sort=original.sort,
        visible_columns=["status", "updated", "tags"],
    )

    restored = BookmarkQuery.from_saved_view(view)

    assert restored == original
    assert "selected" not in view.filters
    assert "expanded" not in view.filters
    with pytest.raises(InvalidBookmarkQuery, match="Unknown saved bookmark filter"):
        BookmarkQuery.from_filter_dict({"selection": 123})


def test_active_any_time_date_is_canonicalized_to_no_filter():
    query = BookmarkQuery(
        dates=(BookmarkDateFilter(BookmarkDateField.CREATED, DatePreset.ANY_TIME),)
    )

    assert query.dates == ()
    assert active_filter_descriptors(query) == ()


@pytest.mark.parametrize(
    ("sort", "expected_titles"),
    [
        (BookmarkSort.ADDED_NEWEST, ["Charlie", "Alpha", "Bravo"]),
        (BookmarkSort.ADDED_OLDEST, ["Bravo", "Alpha", "Charlie"]),
        (BookmarkSort.UPDATED_NEWEST, ["Charlie", "Alpha", "Bravo"]),
        (BookmarkSort.UPDATED_OLDEST, ["Bravo", "Alpha", "Charlie"]),
        (BookmarkSort.CREATED_NEWEST, ["Charlie", "Alpha", "Bravo"]),
        (BookmarkSort.CREATED_OLDEST, ["Bravo", "Alpha", "Charlie"]),
        (BookmarkSort.TITLE_ASCENDING, ["Alpha", "Bravo", "Charlie"]),
        (BookmarkSort.TITLE_DESCENDING, ["Charlie", "Bravo", "Alpha"]),
        (BookmarkSort.AUTHOR_ASCENDING, ["Alpha", "Bravo", "Charlie"]),
        (BookmarkSort.FAVORITES_FIRST, ["Alpha", "Charlie", "Bravo"]),
        (BookmarkSort.PINNED_FIRST, ["Bravo", "Charlie", "Alpha"]),
        (BookmarkSort.MOST_OPENED, ["Charlie", "Alpha", "Bravo"]),
        (BookmarkSort.LEAST_OPENED, ["Bravo", "Alpha", "Charlie"]),
        (BookmarkSort.RECENTLY_OPENED, ["Charlie", "Alpha", "Bravo"]),
        (BookmarkSort.RECENTLY_REFRESHED, ["Charlie", "Alpha", "Bravo"]),
    ],
)
def test_declared_sorts_are_deterministic_and_never_mutate_hierarchy(sort, expected_titles):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    root = _node("40000", "Root")
    _bookmark(
        "40001",
        "Alpha",
        parent=root,
        author_name="Alice",
        favorite=True,
        saved_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        created_at=now - timedelta(days=2),
        open_count=2,
        last_viewed_at=now - timedelta(days=2),
        last_refreshed_at=now - timedelta(days=2),
    )
    _bookmark(
        "40002",
        "Bravo",
        parent=root,
        author_name="Bob",
        pinned=True,
        saved_at=now - timedelta(days=3),
        updated_at=now - timedelta(days=3),
        created_at=now - timedelta(days=3),
        open_count=1,
        last_viewed_at=None,
        last_refreshed_at=None,
    )
    _bookmark(
        "40003",
        "Charlie",
        parent=root,
        author_name="Charlie",
        saved_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
        created_at=now - timedelta(days=1),
        open_count=3,
        last_viewed_at=now - timedelta(days=1),
        last_refreshed_at=now - timedelta(days=1),
    )
    parents_before = dict(ConfluencePageNode.objects.values_list("pk", "parent_id"))

    result = query_bookmarks(BookmarkQuery(sort=sort), at=now)

    assert [bookmark.title for bookmark in result.bookmarks] == expected_titles
    assert result.flat_mode is (sort != BookmarkSort.ADDED_NEWEST)
    assert dict(ConfluencePageNode.objects.values_list("pk", "parent_id")) == parents_before


def test_least_recently_opened_places_never_viewed_first():
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    never = _bookmark("50001", "Never")
    oldest = _bookmark("50002", "Oldest", last_viewed_at=now - timedelta(days=30))
    newest = _bookmark("50003", "Newest", last_viewed_at=now - timedelta(days=1))

    result = query_bookmarks(BookmarkQuery(sort=BookmarkSort.LEAST_RECENTLY_OPENED), at=now)

    assert result.bookmarks == (never, oldest, newest)


def test_visible_node_context_is_cycle_safe_and_ignores_unknown_ids():
    root = _node("60000", "Root")
    section = _node("60001", "Section", parent=root)
    leaf = _node("60002", "Leaf", parent=section)
    unrelated = _node("60003", "Unrelated")

    visible = visible_node_ids_with_ancestors((leaf.pk, 999999, -1))

    assert visible == {root.pk, section.pk, leaf.pk}
    assert unrelated.pk not in visible


def test_invalid_query_values_fail_closed():
    with pytest.raises(InvalidBookmarkQuery, match="Minimum open count"):
        BookmarkQuery(open_count_min=5, open_count_max=4)
    with pytest.raises(InvalidBookmarkQuery, match="7 or 30"):
        BookmarkQuery(recently_changed_days=14)
    with pytest.raises(InvalidBookmarkQuery, match="timezone"):
        BookmarkDateFilter(
            BookmarkDateField.CREATED,
            DatePreset.CUSTOM_RANGE,
            start=datetime(2026, 1, 1),
        )
    with pytest.raises(InvalidBookmarkQuery, match="Unknown sort"):
        sort_requires_flat_mode("not-a-sort")
