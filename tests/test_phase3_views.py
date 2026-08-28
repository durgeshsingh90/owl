from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bookmark_manager import views
from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkImportRun,
    BookmarkRefreshFailure,
    BookmarkRefreshRun,
    BookmarkRefreshStatus,
    BookmarkSource,
    ConfluenceConfiguration,
    ConfluencePageNode,
    SavedBookmarkView,
    Tag,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.configuration import save_ui_configuration
from bookmark_manager.services.secret_store import InMemorySecretStore, get_secret_store

pytestmark = pytest.mark.django_db

SYNTHETIC_ORIGIN = "https://confluence.example.invalid/wiki"
SYNTHETIC_PAT = "synthetic-phase3-ui-pat-never-valid-001"


@pytest.fixture(autouse=True)
def isolated_phase_three_settings(settings):
    settings.CONFLUENCE_BASE_URL = ""
    settings.CONFLUENCE_PAT = ""
    settings.CONFLUENCE_AUTH_MODE = "bearer"
    settings.CONFLUENCE_ACTION_COOLDOWN_SECONDS = 0
    settings.OWL_ALLOW_NON_LOOPBACK = False
    settings.OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS = True


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _node_snapshot(page_id: str, title: str, *, space_key: str = "ENG"):
    return ConfluenceNodeSnapshot(
        page_id=page_id,
        title=title,
        url=f"{SYNTHETIC_ORIGIN}/spaces/{space_key}/pages/{page_id}",
        space_key=space_key,
    )


def _create_bookmark(
    page_id: str = "730003",
    title: str = "PrivateLink route map",
    *,
    ancestors: tuple[ConfluenceNodeSnapshot, ...] | None = None,
    space_name: str = "Architecture Guild",
    space_key: str = "ARC",
) -> Bookmark:
    if ancestors is None:
        ancestors = (
            _node_snapshot("730001", "Knowledge", space_key=space_key),
            _node_snapshot("730002", "Cloud Architecture", space_key=space_key),
        )
    return upsert_bookmark(
        ConfluencePageSnapshot(
            page_id=page_id,
            title=title,
            url=(
                f"{SYNTHETIC_ORIGIN}/spaces/{space_key}/pages/{page_id}/private-link/special-route"
            ),
            space_name=space_name,
            space_key=space_key,
            version=8,
            created_at=datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
            updated_at=datetime(2026, 8, 24, 16, 15, tzinfo=UTC),
            created_by_id="carla-7",
            created_by_name="Carla Creator",
            modified_by_id="morgan-9",
            modified_by_name="Morgan Modifier",
            author_id="alice-42",
            author_name="Alice Author",
            ancestors=ancestors,
        )
    ).bookmark


def _set_bookmark_values(bookmark: Bookmark, **values) -> Bookmark:
    for name, value in values.items():
        setattr(bookmark, name, value)
    bookmark.save(update_fields=tuple(values))
    return bookmark


def _add_tag(bookmark: Bookmark, name: str) -> None:
    tag, _created = Tag.objects.get_or_create_normalized(name)
    bookmark.tags.add(tag)


def _html(response) -> str:
    return response.content.decode("utf-8")


def _sidebar_html(response) -> str:
    match = re.search(
        r'<aside class="app-sidebar bookmark-app-sidebar".*?</aside>',
        _html(response),
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_bookmark_timeline_groups_current_year_by_month_then_older_by_year():
    august = _create_bookmark("810001", "August bookmark", ancestors=())
    july = _create_bookmark("810002", "July bookmark", ancestors=())
    previous_year = _create_bookmark("810003", "Previous year bookmark", ancestors=())
    same_previous_year = _create_bookmark("810005", "Another previous year bookmark", ancestors=())
    older = _create_bookmark("810004", "Older bookmark", ancestors=())
    saved_dates = {
        august.pk: datetime(2026, 8, 20, 10, tzinfo=UTC),
        july.pk: datetime(2026, 7, 10, 10, tzinfo=UTC),
        previous_year.pk: datetime(2025, 12, 1, 10, tzinfo=UTC),
        same_previous_year.pk: datetime(2025, 3, 1, 10, tzinfo=UTC),
        older.pk: datetime(2024, 4, 1, 10, tzinfo=UTC),
    }
    for bookmark in (august, july, previous_year, same_previous_year, older):
        Bookmark.objects.filter(pk=bookmark.pk).update(saved_at=saved_dates[bookmark.pk])
    refreshed = {
        bookmark.pk: Bookmark.objects.get(pk=bookmark.pk)
        for bookmark in (august, july, previous_year, same_previous_year, older)
    }
    bookmarks = [
        refreshed[older.pk],
        refreshed[july.pk],
        refreshed[same_previous_year.pk],
        refreshed[august.pk],
        refreshed[previous_year.pk],
    ]

    groups = views._bookmark_timeline_groups(
        bookmarks,
        at=datetime(2026, 8, 26, 12, tzinfo=UTC),
    )

    assert [group.label for group in groups] == ["August", "July", "2025", "2024"]
    assert [[item.title for item in group.bookmarks] for group in groups] == [
        ["August bookmark"],
        ["July bookmark"],
        ["Previous year bookmark", "Another previous year bookmark"],
        ["Older bookmark"],
    ]

    page = views._bookmark_timeline_page(
        bookmarks,
        page_number=2,
        page_size=2,
        at=datetime(2026, 8, 26, 12, tzinfo=UTC),
    )
    assert (page.number, page.page_count, page.total_count) == (2, 3, 5)
    assert (page.first_item_number, page.last_item_number) == (3, 4)
    assert [item.title for item in page.groups[0].bookmarks] == [
        "Previous year bookmark",
        "Another previous year bookmark",
    ]


def test_bookmark_timeline_uses_local_timezone_at_the_year_boundary():
    january_local = _create_bookmark("810021", "January local bookmark", ancestors=())
    previous_local_year = _create_bookmark("810022", "Previous local year bookmark", ancestors=())
    Bookmark.objects.filter(pk=january_local.pk).update(
        saved_at=datetime(2026, 12, 31, 10, tzinfo=UTC)
    )
    Bookmark.objects.filter(pk=previous_local_year.pk).update(
        saved_at=datetime(2026, 12, 31, 9, tzinfo=UTC)
    )

    with timezone.override("Pacific/Kiritimati"):
        groups = views._bookmark_timeline_groups(
            Bookmark.objects.filter(pk__in=(january_local.pk, previous_local_year.pk)),
            at=datetime(2026, 12, 31, 10, 30, tzinfo=UTC),
        )

    assert [group.label for group in groups] == ["January", "2026"]


def test_slim_sidebar_keeps_browse_links_and_domain_categories_only(loopback_client):
    current_year = timezone.localdate().year
    current = _create_bookmark("810011", "Current year page")
    older = _create_bookmark("810012", "Older page", ancestors=())
    Bookmark.objects.filter(pk=current.pk).update(
        saved_at=datetime(current_year, 7, 15, 10, tzinfo=UTC)
    )
    Bookmark.objects.filter(pk=older.pk).update(
        saved_at=datetime(current_year - 1, 12, 1, 10, tzinfo=UTC)
    )
    category = BookmarkCategory.objects.create(
        domain="confluence.example.invalid",
        name="confluence.example.invalid",
    )
    Bookmark.objects.filter(pk__in=(current.pk, older.pk)).update(category=category)
    parents_before = dict(ConfluencePageNode.objects.values_list("pk", "parent_id"))

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"sort": "title_descending"},
    )
    sidebar = _sidebar_html(response)

    assert response.status_code == 200
    assert response.context["query_result"].flat_mode is True
    assert response.context["bookmark_timeline_page"].total_count == 2
    browse_labels = (
        "All bookmarks",
        "Favorites",
        "Pinned",
        "Recently viewed",
        "Frequently viewed",
        "Never viewed",
    )
    assert all(label in sidebar for label in browse_labels)
    assert [category.name for category in response.context["bookmark_categories"]] == [
        "confluence.example.invalid"
    ]
    assert "confluence.example.invalid" in sidebar
    for removed in (
        "Saved timeline",
        "Filters",
        "Date and sort",
        "Saved views",
        "Import JSON",
        "Export JSON",
        "Confluence settings",
        "System status",
        "Shortcuts:",
    ):
        assert removed not in sidebar
    for removed_hook in (
        'class="bookmark-timeline"',
        'class="app-sidebar__workspace"',
        'id="bookmark-import"',
    ):
        assert removed_hook not in sidebar

    filtered = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"q": "Current year page", "sort": "title_descending"},
    )
    assert filtered.context["bookmark_timeline_page"].total_count == 1
    assert filtered.context["query_result"].bookmarks == (current,)
    assert 'class="bookmark-timeline"' not in _sidebar_html(filtered)

    revealed = loopback_client.get(
        reverse("bookmark_manager:index"),
        {
            "q": "Current year page",
            "timeline": "1",
            "selected": current.pk,
            "located": current.pk,
        },
    )
    revealed_html = _html(revealed)
    assert revealed.context["query_result"].flat_mode is False
    assert revealed.context["selected_bookmark"].pk == current.pk
    assert "data-located-bookmark" in revealed_html
    assert dict(ConfluencePageNode.objects.values_list("pk", "parent_id")) == parents_before


def test_sidebar_shortcuts_filter_and_order_the_expected_bookmarks(loopback_client):
    now = timezone.now()
    favorite = _create_bookmark("811001", "Favorite only", ancestors=())
    pinned = _create_bookmark("811002", "Pinned and frequent", ancestors=())
    recent = _create_bookmark("811003", "Recently viewed", ancestors=())
    never = _create_bookmark("811004", "Never viewed", ancestors=())
    _set_bookmark_values(
        favorite,
        favorite=True,
        open_count=2,
        last_viewed_at=now - timedelta(days=4),
    )
    _set_bookmark_values(
        pinned,
        pinned=True,
        open_count=9,
        last_viewed_at=now - timedelta(days=2),
    )
    _set_bookmark_values(
        recent,
        open_count=1,
        last_viewed_at=now - timedelta(minutes=5),
    )

    favorites_response = loopback_client.get(
        reverse("bookmark_manager:index"), {"favorite": "on", "sort": "added_newest"}
    )
    pinned_response = loopback_client.get(
        reverse("bookmark_manager:index"), {"pinned": "on", "sort": "added_newest"}
    )
    recent_response = loopback_client.get(
        reverse("bookmark_manager:index"), {"min_open": "1", "sort": "recently_opened"}
    )
    frequent_response = loopback_client.get(
        reverse("bookmark_manager:index"), {"min_open": "1", "sort": "most_opened"}
    )
    never_response = loopback_client.get(
        reverse("bookmark_manager:index"), {"max_open": "0", "sort": "added_newest"}
    )

    assert favorites_response.context["query_result"].bookmarks == (favorite,)
    assert pinned_response.context["query_result"].bookmarks == (pinned,)
    assert [item.bookmark for item in recent_response.context["tree_items"]] == [
        recent,
        pinned,
        favorite,
    ]
    assert [item.bookmark for item in frequent_response.context["tree_items"]] == [
        pinned,
        favorite,
        recent,
    ]
    assert never_response.context["query_result"].bookmarks == (never,)
    assert never_response.context["bookmark_counts"] == {
        "all_bookmarks": 4,
        "favorites": 1,
        "pinned": 1,
        "viewed": 3,
        "never_viewed": 1,
    }
    sidebar = _sidebar_html(recent_response)
    assert "?min_open=1&amp;sort=recently_opened" in sidebar
    assert "?min_open=1&amp;sort=most_opened" in sidebar
    assert 'data-count="3" aria-current="page" class="is-active">Recently viewed' in sidebar


def test_filtered_tree_renders_unmatched_bookmarked_ancestor_as_folder_context(loopback_client):
    parent = _create_bookmark("812001", "Parent page", ancestors=())
    child = _create_bookmark(
        "812002",
        "Favorite child",
        ancestors=(_node_snapshot(parent.page_id, parent.title, space_key="ARC"),),
    )
    _set_bookmark_values(child, favorite=True)

    response = loopback_client.get(
        reverse("bookmark_manager:index"), {"favorite": "on", "sort": "added_newest"}
    )
    root = response.context["tree_items"][0]

    assert response.context["query_result"].bookmarks == (child,)
    assert root.node.pk == parent.tree_node_id
    assert root.bookmark is None
    assert root.children[0].bookmark == child
    html = _html(response)
    assert html.count('data-bookmark-id="') >= 1
    assert f'data-bookmark-id="{parent.pk}"' not in html
    assert f'data-bookmark-id="{child.pk}"' in html


def test_bookmark_timeline_context_paginates_without_rendering_sidebar_timeline(
    loopback_client,
):
    observation_time = timezone.now()
    for offset in range(101):
        bookmark = _create_bookmark(
            str(820000 + offset),
            f"Paged timeline bookmark {offset:03d}",
            ancestors=(),
        )
        Bookmark.objects.filter(pk=bookmark.pk).update(
            saved_at=observation_time - timedelta(days=offset)
        )

    first_response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"q": "Paged timeline", "sort": "title_descending"},
    )
    first_page = first_response.context["bookmark_timeline_page"]

    assert (first_page.number, first_page.page_count, first_page.total_count) == (1, 2, 101)
    assert (first_page.first_item_number, first_page.last_item_number) == (1, 100)
    assert first_page.has_next is True
    assert 'class="bookmark-timeline"' not in _sidebar_html(first_response)

    second_response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"q": "Paged timeline", "sort": "title_descending", "timeline_page": "2"},
    )
    second_page = second_response.context["bookmark_timeline_page"]

    assert (second_page.number, second_page.first_item_number, second_page.last_item_number) == (
        2,
        101,
        101,
    )
    assert second_page.has_previous is True
    assert second_page.has_next is False
    assert 'class="bookmark-timeline"' not in _sidebar_html(second_response)


def _async_post(client, path: str, data=None):
    return client.post(
        path,
        data or {},
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def _legacy_record(page_id: str, title: str, saved_at: str) -> dict[str, object]:
    return {
        "pageId": page_id,
        "pageTitle": title,
        "pageUrl": f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/{page_id}",
        "spaceKey": "ENG",
        "savedAt": saved_at,
        "breadcrumb": f"Engineering > Network > {title}",
    }


def test_notes_and_tags_ajax_are_local_searchable_and_html_escaped(loopback_client):
    bookmark = _create_bookmark()
    unsafe_note = '<script>alert("personal-note")</script> local edge checklist'
    unsafe_tag = '<img src=x onerror="tag()">'

    response = _async_post(
        loopback_client,
        reverse("bookmark_manager:organise", args=(bookmark.pk,)),
        {"notes": unsafe_note, "tags": f"{unsafe_tag}, Network Edge, network edge"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "state": "success",
        "label": "Personal details saved",
        "detail": f"Notes and tags saved for bookmark #{bookmark.pk}",
        "bookmark_id": bookmark.pk,
        "favorite": False,
        "pinned": False,
        "notes": unsafe_note,
        "tags": [unsafe_tag, "Network Edge"],
    }
    bookmark.refresh_from_db()
    assert bookmark.notes == unsafe_note
    assert list(bookmark.tags.values_list("name", flat=True)) == [unsafe_tag, "Network Edge"]

    page = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk},
    )
    html = _html(page)
    assert response.status_code == 200
    assert '<script>alert("personal-note")</script>' not in html
    assert '<img src=x onerror="tag()">' not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=" in html
    assert "data-notes-display" in html
    assert "data-tags-display" in html

    note_search = loopback_client.get(
        reverse("bookmark_manager:index"), {"q": "local edge checklist"}
    )
    tag_search = loopback_client.get(reverse("bookmark_manager:index"), {"q": "Network Edge"})
    assert note_search.context["query_result"].bookmarks == (bookmark,)
    assert tag_search.context["query_result"].bookmarks == (bookmark,)


def test_favorite_and_pin_ajax_actions_are_independent(loopback_client):
    bookmark = _create_bookmark()

    favorite = _async_post(
        loopback_client,
        reverse("bookmark_manager:favorite", args=(bookmark.pk,)),
    )
    assert favorite.status_code == 200
    assert favorite.json()["favorite"] is True
    assert favorite.json()["pinned"] is False

    pin = _async_post(
        loopback_client,
        reverse("bookmark_manager:pin", args=(bookmark.pk,)),
    )
    assert pin.status_code == 200
    assert pin.json()["favorite"] is True
    assert pin.json()["pinned"] is True

    unfavorite = _async_post(
        loopback_client,
        reverse("bookmark_manager:favorite", args=(bookmark.pk,)),
    )
    assert unfavorite.json()["favorite"] is False
    assert unfavorite.json()["pinned"] is True
    bookmark.refresh_from_db()
    assert bookmark.favorite is False
    assert bookmark.pinned is True


@pytest.mark.parametrize(
    "search",
    [
        "PrivateLink route map",
        "730003",
        "special-route",
        "Architecture Guild",
        "ARC",
        "alice-42",
        "Alice Author",
        "Carla Creator",
        "Morgan Modifier",
        "Network Edge",
        "edge gateway checklist",
        "Knowledge > Cloud Architecture",
    ],
)
def test_http_search_covers_every_local_bookmark_scope(loopback_client, search):
    target = _create_bookmark()
    _set_bookmark_values(target, notes="Local edge gateway checklist")
    _add_tag(target, "Network Edge")
    decoy = _create_bookmark(
        "740003",
        "Unrelated finance page",
        ancestors=(),
        space_name="Finance",
        space_key="FIN",
    )
    _set_bookmark_values(
        decoy,
        url=f"{SYNTHETIC_ORIGIN}/spaces/FIN/pages/740003/finance-only",
        author_id="finance-author",
        author_name="Finance Author",
        created_by_id="finance-creator",
        created_by_name="Finance Creator",
        modified_by_id="finance-modifier",
        modified_by_name="Finance Modifier",
    )

    response = loopback_client.get(reverse("bookmark_manager:index"), {"q": search})

    assert response.status_code == 200
    assert response.context["query_result"].bookmarks == (target,)
    assert response.context["selected_bookmark"] == target
    assert response.context["result_count"] == 1
    assert "Filtered tree" in _html(response)


def test_tree_opens_titles_shows_usage_and_keeps_quick_note_in_details(loopback_client):
    bookmark = _create_bookmark()
    _set_bookmark_values(bookmark, open_count=2, notes="Review the routing notes")

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk},
    )
    html = _html(response)

    assert response.status_code == 200
    assert f'class="tree-open-form" method="post" action="/bookmarks/{bookmark.pk}/open/"' in html
    assert f'aria-label="Open bookmark 1.1.1, {bookmark.title}"' in html
    assert "Open link ↗" not in html
    assert 'class="tree-quick-note"' not in html
    assert "<strong>2</strong> opens" in html
    assert "Added " in html
    assert "Written " in html
    assert "Updated " in html
    assert ">Quick notes <small>Stored locally and never sent to Confluence</small>" in html
    assert 'id="bookmark-organisation-form"' in html
    assert 'form="bookmark-organisation-form"' in html
    assert "Review the routing notes" in html


def test_refresh_failure_status_renders_wrapped_url_reason_and_dismiss_control(
    loopback_client,
):
    bookmark = _create_bookmark()
    run = BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        total_bookmarks=1,
        processed_bookmarks=1,
        succeeded_bookmarks=0,
        failed_bookmarks=1,
        completed_at=timezone.now(),
    )
    BookmarkRefreshFailure.objects.create(
        refresh_run=run,
        bookmark=bookmark,
        page_id=bookmark.page_id,
        url=bookmark.url,
        error_code="upstream_unavailable",
        reason="Confluence did not return this page after 3 attempts.",
        attempt_count=3,
    )

    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = _html(response)

    assert response.status_code == 200
    assert "data-refresh-result" in html
    assert "data-refresh-failure-list" in html
    assert 'aria-label="Dismiss refresh issues"' in html
    assert bookmark.url in html
    assert "Confluence did not return this page after 3 attempts." in html


def test_confluence_people_column_counts_unique_pages_and_filters_by_name(loopback_client):
    first = _create_bookmark()
    second = _create_bookmark("730004", "Second architecture page", ancestors=())
    _set_bookmark_values(
        second,
        author_name="Alice Author",
        created_by_name="Alice Author",
        modified_by_name="Morgan Modifier",
    )
    web = _create_bookmark("730005", "Ordinary web bookmark", ancestors=())
    _set_bookmark_values(
        web,
        source_type=BookmarkSource.WEB,
        author_name="Web Writer",
        created_by_name="Web Writer",
        modified_by_name="Web Editor",
    )

    response = loopback_client.get(reverse("bookmark_manager:index"))
    contacts = {contact.name: contact for contact in response.context["confluence_contacts"]}
    html = _html(response)

    assert contacts["Alice Author"].page_count == 2
    assert contacts["Alice Author"].written_count == 2
    assert contacts["Alice Author"].updated_count == 0
    assert contacts["Morgan Modifier"].page_count == 2
    assert contacts["Morgan Modifier"].updated_count == 2
    assert contacts["Carla Creator"].page_count == 1
    assert "Web Writer" not in contacts
    assert "Web Editor" not in contacts
    assert 'class="bookmark-pane bookmark-pane--people"' in html
    assert 'aria-label="Search people"' in html
    assert 'aria-controls="people-search-panel"' in html
    assert "data-people-search-input" in html
    assert 'data-person-name="Alice Author"' in html
    assert "data-people-no-results" in html
    assert "?person=Alice%20Author&amp;sort=updated_newest" in html

    filtered = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"person": "Alice Author", "sort": "updated_newest"},
    )

    assert set(filtered.context["query_result"].bookmarks) == {first, second}
    assert web not in filtered.context["query_result"].bookmarks
    assert filtered.context["active_contact"] == "Alice Author"


def test_combined_http_filters_and_flat_sort_preserve_the_tree(loopback_client):
    now = timezone.now()
    target = _create_bookmark()
    _set_bookmark_values(
        target,
        favorite=True,
        pinned=True,
        saved_at=now - timedelta(days=45),
        updated_at=now - timedelta(days=2),
        last_viewed_at=now - timedelta(days=10),
        last_viewed_version=7,
        open_count=5,
    )
    _add_tag(target, "AWS")
    _add_tag(target, "Private Link")
    only_one_tag = _create_bookmark(
        "730004",
        "Only one tag",
        ancestors=(
            _node_snapshot("730001", "Knowledge", space_key="ARC"),
            _node_snapshot("730002", "Cloud Architecture", space_key="ARC"),
        ),
    )
    _set_bookmark_values(
        only_one_tag,
        favorite=True,
        pinned=True,
        saved_at=now - timedelta(days=45),
        updated_at=now - timedelta(days=2),
        last_viewed_at=now - timedelta(days=10),
        last_viewed_version=7,
        open_count=5,
    )
    _add_tag(only_one_tag, "AWS")
    parents_before = dict(ConfluencePageNode.objects.values_list("pk", "parent_id"))

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {
            "favorite": "on",
            "pinned": "on",
            "tags": ["aws", "private link"],
            "person": "Alice Author",
            "space": "ARC",
            "availability": [BookmarkAvailability.ACTIVE],
            "recency": ["updated"],
            "changed": "on",
            "date_field": "updated",
            "date_preset": "last_7_days",
            "min_open": "4",
            "max_open": "6",
            "sort": "most_opened",
        },
    )

    result = response.context["query_result"]
    html = _html(response)
    assert response.status_code == 200
    assert result.bookmarks == (target,)
    assert result.flat_mode is True
    assert result.active_filter_count == 11
    assert "Bookmark tree" in html
    assert "Sorted matches in hierarchy" in html
    assert 'role="tree" aria-label="Bookmark hierarchy"' in html
    assert "Knowledge" in html
    assert "Cloud Architecture" in html
    assert "PrivateLink route map" in html
    assert "flat-bookmark-list" not in html
    assert "Selected tags use ALL" not in _sidebar_html(response)
    assert dict(ConfluencePageNode.objects.values_list("pk", "parent_id")) == parents_before


def test_saved_view_ajax_round_trip_restores_validated_query_not_tree_state(loopback_client):
    target = _create_bookmark()
    _set_bookmark_values(target, favorite=True, open_count=9)
    raw_state = urlencode(
        {
            "q": target.title,
            "favorite": "on",
            "sort": "most_opened",
            "selected": target.pk,
            "located": target.pk,
        }
    )

    response = _async_post(
        loopback_client,
        reverse("bookmark_manager:view_save"),
        {"name": "Daily architecture", "query_string": raw_state},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "success"
    assert payload["label"] == "View saved"
    saved = SavedBookmarkView.objects.get(normalized_name="daily architecture")
    assert saved.search_text == target.title
    assert saved.sort == "most_opened"
    assert saved.filters["favorite"] is True
    assert "selected" not in saved.filters
    assert "located" not in saved.filters

    restored = loopback_client.get(payload["redirect"])
    assert restored.status_code == 200
    assert restored.context["active_saved_view"] == saved
    assert restored.context["query_result"].bookmarks == (target,)
    assert restored.context["query_result"].flat_mode is True
    assert saved in restored.context["saved_views"]
    assert saved.name not in _sidebar_html(restored)


def test_export_response_is_sensitive_attachment_and_excludes_credentials(
    loopback_client,
    settings,
):
    marker = "synthetic-phase3-export-secret-canary"
    settings.CONFLUENCE_PAT = marker
    ConfluenceConfiguration.objects.create(
        base_url=SYNTHETIC_ORIGIN,
        last_error_message=f"Authorization: Bearer {marker}",
    )
    bookmark = _create_bookmark()
    _set_bookmark_values(bookmark, notes="A safe local export note")
    _add_tag(bookmark, "Backup")

    response = loopback_client.get(reverse("bookmark_manager:export"))
    rendered = response.content.decode("utf-8")
    document = json.loads(rendered)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json; charset=utf-8"
    assert re.fullmatch(
        r'attachment; filename="owl-bookmarks-\d{4}-\d{2}-\d{2}\.json"',
        response["Content-Disposition"],
    )
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert document["document_type"] == "owl.bookmark-export"
    assert document["record_count"] == 1
    assert document["bookmarks"][0]["personal"]["notes"] == "A safe local export note"
    assert marker not in rendered
    assert "personal_access_token" not in rendered
    assert "authorization" not in rendered.casefold()


def test_import_http_journey_returns_to_settings_and_renders_partial_result(
    loopback_client,
):
    records = [
        _legacy_record("810001", "First imported page", "2025-08-20T09:30:00Z"),
        "malformed sibling that must not abort valid records",
        _legacy_record("810002", "Second imported page", "2026-08-20T09:30:00Z"),
    ]
    upload = SimpleUploadedFile(
        "customer-bookmarks.json",
        json.dumps(records).encode("utf-8"),
        content_type="application/json",
    )

    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"import_file": upload, "return_to": "settings"},
    )

    assert response.status_code == 302
    run = BookmarkImportRun.objects.get()
    assert response["Location"] == (f"{reverse('bookmark_manager:settings')}?import_run={run.pk}")
    assert run.imported_records == 2
    assert run.failed_records == 1
    assert Bookmark.objects.filter(page_id__in=("810001", "810002")).count() == 2

    result_page = loopback_client.get(response["Location"])
    html = _html(result_page)
    assert result_page.status_code == 200
    assert "Import completed with failures" in html
    assert "Imported 2, skipped 0, and rejected 1 of 3 records." in html
    assert "<dt>Imported</dt><dd>2</dd>" in html
    assert "<dt>Rejected</dt><dd>1</dd>" in html
    assert "Record 2" in html
    assert "The record must be a JSON object." in html
    assert "data-import-result" in html
    assert 'aria-label="Dismiss import result"' in html
    assert "data-dismiss-import-result" in html
    assert 'class="operation-failure-list"' in html


def test_browser_style_async_json_import_passes_csrf_and_redirects_to_result():
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = csrf_client.get(reverse("bookmark_manager:settings"))
    token = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        _html(page),
    )
    assert token is not None
    upload = SimpleUploadedFile(
        "browser-import.json",
        json.dumps(
            [_legacy_record("810003", "Browser imported page", "2026-08-20T09:30:00Z")]
        ).encode("utf-8"),
        content_type="application/json",
    )

    response = csrf_client.post(
        reverse("bookmark_manager:import"),
        {
            "csrfmiddlewaretoken": token.group("token"),
            "import_file": upload,
            "return_to": "settings",
        },
        HTTP_ORIGIN="http://127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 302
    assert response["Location"].startswith(f"{reverse('bookmark_manager:settings')}?import_run=")
    assert Bookmark.objects.filter(page_id="810003", title="Browser imported page").exists()


def test_async_import_validation_returns_panel_feedback(loopback_client):
    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"return_to": "settings"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {
        "state": "invalid",
        "label": "Import not started",
        "detail": "Choose a valid UTF-8 .json or .txt file within the configured size limit.",
    }


def test_null_origin_import_remains_rejected_even_with_a_valid_csrf_token():
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = csrf_client.get(reverse("bookmark_manager:settings"))
    token = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        _html(page),
    )
    assert token is not None

    response = csrf_client.post(
        reverse("bookmark_manager:import"),
        {"csrfmiddlewaretoken": token.group("token"), "return_to": "settings"},
        HTTP_ORIGIN="null",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 403


def test_text_import_http_journey_adds_valid_urls_and_reports_incomplete_ones(
    loopback_client,
):
    upload = SimpleUploadedFile(
        "meeting-chat.txt",
        (
            b"Useful link https://example.com/guides/complete\n"
            b"Truncated link https://example.com/guides/incomplete...\n"
        ),
        content_type="text/plain",
    )

    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"import_file": upload, "return_to": "settings"},
    )

    assert response.status_code == 302
    run = BookmarkImportRun.objects.get()
    assert run.schema_version == "text-urls-v1"
    assert run.total_records == 2
    assert run.imported_records == 1
    assert run.failed_records == 1
    assert Bookmark.objects.filter(canonical_url="https://example.com/guides/complete").exists()

    result_page = loopback_client.get(response["Location"])
    html = _html(result_page)
    assert "Completed 1 of 2 extracted URLs" in html
    assert "incomplete or failed 1" in html
    assert "Review incomplete or failed URLs" in html
    assert "example.com/guides/incomplete..." in html
    assert "Incomplete or truncated URL" in html


def test_invalid_import_returning_to_settings_renders_field_and_error_feedback(
    loopback_client,
):
    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"return_to": "settings"},
    )
    html = _html(response)

    assert response.status_code == 400
    assert response.context["import_form"].errors["import_file"]
    assert "Bookmark data" in html
    assert "Import bookmarks" in html
    assert "Choose a valid UTF-8 .json or .txt file" in html
    assert "This field is required." in html
    assert 'name="return_to" value="settings"' in html
    assert "Advanced settings" not in html


def test_confirmed_ajax_delete_preserves_shared_hierarchy_and_sibling(loopback_client):
    shared = (
        _node_snapshot("820001", "Engineering"),
        _node_snapshot("820002", "Network"),
    )
    first = _create_bookmark("820003", "First shared page", ancestors=shared)
    second = _create_bookmark("820004", "Second shared page", ancestors=shared)
    root_id = ConfluencePageNode.objects.get(page_id="820001").pk
    parent_id = ConfluencePageNode.objects.get(page_id="820002").pk
    first_leaf_id = first.tree_node_id

    rejected = loopback_client.post(
        reverse("bookmark_manager:delete", args=(first.pk,)),
        {"confirm": "not-confirmed"},
    )
    assert rejected.status_code == 400
    assert Bookmark.objects.filter(pk=first.pk).exists()

    deleted = _async_post(
        loopback_client,
        reverse("bookmark_manager:delete", args=(first.pk,)),
        {"confirm": "delete"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["detail"] == "Removed from OWL. Confluence was not changed."
    assert not Bookmark.objects.filter(pk=first.pk).exists()
    assert not ConfluencePageNode.objects.filter(pk=first_leaf_id).exists()
    assert Bookmark.objects.filter(pk=second.pk).exists()
    assert ConfluencePageNode.objects.filter(pk__in=(root_id, parent_id)).count() == 2


def test_bulk_delete_removes_selected_parent_and_child_bookmarks(loopback_client):
    parent = _create_bookmark("830001", "Selected parent", ancestors=())
    child = _create_bookmark(
        "830002",
        "Selected child",
        ancestors=(_node_snapshot("830001", "Selected parent", space_key="ARC"),),
    )
    untouched = _create_bookmark("830003", "Untouched bookmark", ancestors=())
    parent_node_id = parent.tree_node_id
    child_node_id = child.tree_node_id

    rejected = loopback_client.post(
        reverse("bookmark_manager:delete_selected"),
        {"confirm": "not-confirmed", "bookmark_ids": [parent.pk, child.pk]},
    )
    assert rejected.status_code == 400
    assert Bookmark.objects.filter(pk__in=(parent.pk, child.pk)).count() == 2

    deleted = _async_post(
        loopback_client,
        reverse("bookmark_manager:delete_selected"),
        {
            "confirm": "delete-selected",
            "bookmark_ids": [str(parent.pk), str(child.pk)],
        },
    )

    assert deleted.status_code == 200
    assert deleted.json()["deleted_count"] == 2
    assert deleted.json()["detail"] == ("Deleted 2 bookmarks from OWL. Confluence was not changed.")
    assert not Bookmark.objects.filter(pk__in=(parent.pk, child.pk)).exists()
    assert not ConfluencePageNode.objects.filter(pk__in=(parent_node_id, child_node_id)).exists()
    assert Bookmark.objects.filter(pk=untouched.pk).exists()


def test_phase_three_routes_enforce_declared_http_methods(loopback_client):
    bookmark = _create_bookmark()
    saved_view = SavedBookmarkView.objects.create(name="Protected view")
    post_only = (
        reverse("bookmark_manager:organise", args=(bookmark.pk,)),
        reverse("bookmark_manager:favorite", args=(bookmark.pk,)),
        reverse("bookmark_manager:pin", args=(bookmark.pk,)),
        reverse("bookmark_manager:view_save"),
        reverse("bookmark_manager:view_delete", args=(saved_view.pk,)),
        reverse("bookmark_manager:import"),
        reverse("bookmark_manager:delete", args=(bookmark.pk,)),
        reverse("bookmark_manager:delete_selected"),
        reverse("bookmark_manager:open_parent", args=(bookmark.pk,)),
    )

    for path in post_only:
        assert loopback_client.get(path).status_code == 405
    assert loopback_client.post(reverse("bookmark_manager:export")).status_code == 405


def test_every_phase_three_state_change_requires_csrf():
    bookmark = _create_bookmark()
    saved_view = SavedBookmarkView.objects.create(name="CSRF view")
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    actions = (
        (reverse("bookmark_manager:organise", args=(bookmark.pk,)), {"notes": "x"}),
        (reverse("bookmark_manager:favorite", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:pin", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:view_save"), {"name": "x"}),
        (
            reverse("bookmark_manager:view_delete", args=(saved_view.pk,)),
            {"confirm": "delete"},
        ),
        (reverse("bookmark_manager:import"), {}),
        (reverse("bookmark_manager:delete", args=(bookmark.pk,)), {"confirm": "delete"}),
        (
            reverse("bookmark_manager:delete_selected"),
            {"confirm": "delete-selected", "bookmark_ids": [bookmark.pk]},
        ),
        (reverse("bookmark_manager:open_parent", args=(bookmark.pk,)), {}),
    )

    for path, data in actions:
        assert csrf_client.post(path, data).status_code == 403


def test_phase_three_actions_and_export_are_loopback_only():
    bookmark = _create_bookmark()
    saved_view = SavedBookmarkView.objects.create(name="Local view")
    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.44")
    post_actions = (
        (reverse("bookmark_manager:organise", args=(bookmark.pk,)), {"notes": "x"}),
        (reverse("bookmark_manager:favorite", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:pin", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:view_save"), {"name": "x"}),
        (
            reverse("bookmark_manager:view_delete", args=(saved_view.pk,)),
            {"confirm": "delete"},
        ),
        (reverse("bookmark_manager:import"), {}),
        (reverse("bookmark_manager:delete", args=(bookmark.pk,)), {"confirm": "delete"}),
        (
            reverse("bookmark_manager:delete_selected"),
            {"confirm": "delete-selected", "bookmark_ids": [bookmark.pk]},
        ),
        (reverse("bookmark_manager:open_parent", args=(bookmark.pk,)), {}),
    )

    for path, data in post_actions:
        assert remote_client.post(path, data).status_code == 403
    assert remote_client.get(reverse("bookmark_manager:export")).status_code == 403


def test_async_open_returns_only_a_validated_target_and_tracks_usage(loopback_client):
    store = get_secret_store()
    assert isinstance(store, InMemorySecretStore)
    configured = save_ui_configuration(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=SYNTHETIC_PAT,
        secret_store=store,
    )
    assert configured.success is True
    bookmark = _create_bookmark()

    response = _async_post(
        loopback_client,
        reverse("bookmark_manager:open", args=(bookmark.pk,)),
    )

    assert response.status_code == 200
    assert response.json()["url"] == bookmark.url
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    bookmark.refresh_from_db()
    assert bookmark.open_count == 1
    assert bookmark.first_opened_at is not None
    assert bookmark.last_viewed_version == bookmark.version


def test_phase_three_tree_renders_keyboard_aria_and_safe_dom_hooks(loopback_client, settings):
    store = get_secret_store()
    assert isinstance(store, InMemorySecretStore)
    configured = save_ui_configuration(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=SYNTHETIC_PAT,
        secret_store=store,
    )
    assert configured.success is True
    bookmark = _create_bookmark()
    _set_bookmark_values(
        bookmark,
        favorite=True,
        pinned=True,
        notes="Keyboard-accessible personal note",
    )
    _add_tag(bookmark, "Accessible")

    response = loopback_client.get(
        reverse("bookmark_manager:index"), {"selected": bookmark.pk, "located": bookmark.pk}
    )
    html = _html(response)

    assert response.status_code == 200
    assert 'role="tree" aria-label="Bookmark hierarchy" data-bookmark-tree' in html
    assert 'role="treeitem"' in html
    assert 'aria-level="1"' in html
    assert 'aria-level="2"' in html
    assert 'aria-level="3"' in html
    assert 'aria-selected="true"' in html
    assert 'aria-expanded="true"' in html
    assert "data-located-bookmark" in html
    assert "data-tree-toggle" in html
    assert "data-tree-check" in html
    assert f'data-details-url="?selected={bookmark.pk}' in html
    assert "tree-details-link" not in html
    assert ">Page details</a>" not in html
    assert "data-tree-expand-all" in html
    assert "data-tree-collapse-all" in html
    assert 'id="bulk-delete-bookmarks"' in html
    assert "data-delete-selected" in html
    assert 'data-delete-locked="true"' in html
    assert "🔒" in html
    assert "bookmarks.js?v=workspace-ui-v11" in html
    assert f'name="bookmark_ids" value="{bookmark.pk}"' in html
    assert 'form="bulk-delete-bookmarks"' in html
    assert "data-productivity-form" in html
    assert "data-favorite-form" in html
    assert "data-pin-form" in html
    assert 'aria-pressed="true"' in html
    assert 'data-copy-value="730003"' in html
    assert 'data-confirm-message="Delete this bookmark from OWL?' in html
    assert "bookmark-connection-summary" not in html
    assert html.index("organisation-form--primary") < html.index("detail-actions")
    assert html.index("organisation-form--primary") < html.index("detail-full-url")
    assert html.index("detail-full-url") < html.index("detail-actions")
    assert f"<code>{bookmark.url}</code>" in html
    assert f'data-copy-value="{bookmark.url}"' in html
    assert bookmark.url in html
    assert "<time datetime=" in html and 'tabindex="0"' in html
    sidebar = _sidebar_html(response)
    assert "Shortcuts:" not in sidebar
    assert "Enter opens details" not in sidebar
    assert html.count('name="csrfmiddlewaretoken"') >= 6
    element_ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(element_ids) == len(set(element_ids))

    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()
    for hook in (
        'event.key === "/"',
        'event.key === "ArrowDown"',
        'event.key === "ArrowUp"',
        'event.key === "ArrowRight"',
        'event.key === "ArrowLeft"',
        'event.key === "Enter"',
        'shortcut === "e"',
        'shortcut === "f"',
        'shortcut === "p"',
        "owl.bookmark-manager.tree-expansion.v1",
        "owl.bookmark-manager.selection.v1",
        "owl.bookmark-manager.tree-scroll.v1",
        'treeRoot?.addEventListener("click"',
        "showTreeRowDetails(row)",
        'event.target.closest("button, input, label, a, form, select, textarea")',
        "keyTarget instanceof HTMLButtonElement",
        "keyTarget instanceof HTMLAnchorElement",
        'removeQueryParameter("import_run")',
        "renderRefreshFailures(refresh.failures || [])",
        'deleteSelectedButton.dataset.deleteLocked = "false"',
        'label.textContent = "Click again to delete"',
        "window.setTimeout(lockDeleteSelected, 10000)",
        "[data-bookmark-import-form]",
        "Extracting URLs, checking Confluence Page IDs, and retrieving pages",
        "Retrieving the Confluence page, hierarchy, and searchable text",
        "bookmarkUnifiedForm.dataset.urlSearchComplete",
        "bookmarkUnifiedForm.requestSubmit(bookmarkSaveButton)",
        "window.clearTimeout(searchTimer)",
        "element.textContent = payload.notes",
        "data-external-open-form",
    ):
        assert hook in script
