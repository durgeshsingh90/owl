from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bookmark_manager import views
from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkFolder,
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
from bookmark_manager.services.bookmark_outline import outline_number_map
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
        "Deleted pages",
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


def test_domain_sidebar_shows_global_bookmark_totals_during_filtered_search(loopback_client):
    first = _create_bookmark("810031", "Matching architecture guide", ancestors=())
    second = _create_bookmark("810032", "Other architecture guide", ancestors=())
    third = _create_bookmark("810033", "Support handbook", ancestors=())
    docs = BookmarkCategory.objects.create(
        domain="docs.example.invalid",
        name="Engineering docs",
        description="Architecture and engineering references",
    )
    support = BookmarkCategory.objects.create(
        domain="support.example.invalid",
        name="Support portal",
    )
    Bookmark.objects.filter(pk__in=(first.pk, second.pk)).update(category=docs)
    Bookmark.objects.filter(pk=third.pk).update(category=support)

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"q": "Matching architecture guide"},
    )
    sidebar = _sidebar_html(response)
    totals = {
        category.domain: category.total_bookmarks
        for category in response.context["bookmark_categories"]
    }

    assert response.context["result_count"] == 1
    assert totals == {
        "docs.example.invalid": 2,
        "support.example.invalid": 1,
    }
    assert 'aria-label="2 total bookmarks">2</span>' in sidebar
    assert 'aria-label="1 total bookmark">1</span>' in sidebar
    assert "Architecture and engineering references" in sidebar

    settings_response = loopback_client.get(reverse("bookmark_manager:settings"))
    settings_totals = {
        category.domain: category.total_bookmarks
        for category in settings_response.context["bookmark_categories"]
    }
    assert settings_totals == totals


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
        "deleted_pages": 0,
    }
    sidebar = _sidebar_html(recent_response)
    assert "?min_open=1&amp;sort=recently_opened" in sidebar
    assert "?min_open=1&amp;sort=most_opened" in sidebar
    assert 'data-count="3" aria-current="page" class="is-active">Recently viewed' in sidebar


def test_deleted_pages_sidebar_filters_not_found_references_and_hides_live_open_actions(
    loopback_client,
):
    active = _create_bookmark("811101", "Active Confluence page", ancestors=())
    deleted = _create_bookmark("811102", "Deleted Confluence page", ancestors=())
    _set_bookmark_values(
        deleted,
        availability_status=BookmarkAvailability.NOT_FOUND,
        last_error_code="not_found",
        last_error_message="Confluence could not find this page.",
        page_text="",
    )

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {
            "availability": BookmarkAvailability.NOT_FOUND,
            "sort": "added_newest",
            "selected": deleted.pk,
        },
    )

    assert response.status_code == 200
    assert response.context["active_section"] == "deleted"
    assert response.context["query_result"].bookmarks == (deleted,)
    assert response.context["selected_bookmark"] == deleted
    assert response.context["bookmark_counts"]["deleted_pages"] == 1
    sidebar = _sidebar_html(response)
    assert "?availability=not_found&amp;sort=added_newest" in sidebar
    assert 'data-count="1" aria-current="page" class="is-active">Deleted pages' in sidebar
    html = _html(response)
    assert "Deleted Confluence page" in html
    assert "Not found" in html
    assert f'action="/bookmarks/{deleted.pk}/open/"' not in html
    assert f'action="/bookmarks/{active.pk}/open/"' not in html
    assert "Open in Confluence" not in html


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


def test_folder_rows_render_total_descendant_clicks_for_collapsed_tree(loopback_client):
    first = _create_bookmark("812101", "First child")
    second = _create_bookmark("812102", "Second child")
    _set_bookmark_values(first, open_count=2)
    _set_bookmark_values(second, open_count=9)

    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = _html(response)

    assert 'aria-label="11 total clicks across bookmarked pages in Knowledge"' in html
    assert 'aria-label="11 total clicks across bookmarked pages in Cloud Architecture"' in html
    assert "<strong>11</strong> clicks" in html


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


def test_async_save_success_identifies_domain_and_tree_outline_number(loopback_client):
    response = _async_post(
        loopback_client,
        reverse("bookmark_manager:save"),
        {"q": "https://docs.example.com/runbooks/private-dns"},
    )

    bookmark = Bookmark.objects.select_related("category", "tree_node").get()
    numbers = outline_number_map(ConfluencePageNode.objects.all())
    expected_outline = numbers[bookmark.tree_node_id]
    payload = response.json()

    assert response.status_code == 200
    assert payload["state"] == "success"
    assert payload["domain"] == "docs.example.com"
    assert payload["category"] == "docs.example.com"
    assert payload["title"] == bookmark.title
    assert payload["outline_number"] == expected_outline
    assert f"Added “{bookmark.title}”." in payload["detail"]
    assert f"Saved under docs.example.com as bookmark {expected_outline}." in payload["detail"]
    assert "Category: docs.example.com." in payload["detail"]


def test_existing_bookmark_redirect_status_identifies_title_and_location(loopback_client):
    bookmark = _create_bookmark(title="Existing private DNS runbook")
    category = BookmarkCategory.objects.create(
        domain="confluence.example.invalid",
        name="Architecture knowledge",
    )
    bookmark.category = category
    bookmark.save(update_fields=("category",))

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk, "saved": "existing"},
    )
    selected = response.context["selected_bookmark"]
    status_message = response.context["status_message"]

    assert response.status_code == 200
    assert response.context["bookmark_save_status"] == status_message
    assert status_message in _html(response)
    assert "Existing private DNS runbook" in status_message
    assert "already saved" in status_message
    assert "confluence.example.invalid" in status_message
    assert f"bookmark {selected.outline_number}" in status_message
    assert "Category: Architecture knowledge." in status_message


def test_enter_on_a_complete_unmatched_url_uses_the_add_bookmark_submitter(
    loopback_client,
    settings,
):
    url = "https://docs.example.com/runbooks/new-private-dns"
    response = loopback_client.get(reverse("bookmark_manager:index"), {"q": url})
    html = _html(response)
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()
    enter_handler = script.split(
        'bookmarkSearch?.addEventListener("keydown", (event) => {',
        1,
    )[1].split(
        'bookmarkSearch?.addEventListener("input", (event) => {',
        1,
    )[0]

    assert response.status_code == 200
    assert response.context["url_search_active"] is True
    assert response.context["url_identity_match_count"] == 0
    assert f'data-completed-search="{url}"' in html
    assert 'data-url-search-complete="true"' in html
    assert 'data-url-match-count="0"' in html
    assert 'formaction="/bookmarks/save/"' in html
    assert "No saved bookmark matches this Page ID or URL. Press Enter to add it." in html
    assert 'event.key !== "Enter"' in enter_handler
    assert "event.preventDefault();" in enter_handler
    assert 'bookmarkUnifiedForm.dataset.urlSearchComplete === "true"' in enter_handler
    assert "if (matchCount > 0)" in enter_handler
    assert "bookmarkUnifiedForm.requestSubmit(bookmarkSaveButton);" in enter_handler
    assert enter_handler.index("if (matchCount > 0)") < enter_handler.index(
        "bookmarkUnifiedForm.requestSubmit(bookmarkSaveButton);"
    )


def test_complete_unmatched_url_search_restores_focus_and_caret(
    loopback_client,
    settings,
):
    url = "https://docs.example.com/runbooks/new-private-dns"
    response = loopback_client.get(reverse("bookmark_manager:index"), {"q": url})
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()
    focus_handler = script.split(
        "const refocusUnmatchedUrlSearch = () => {",
        1,
    )[1].split(
        "refocusUnmatchedUrlSearch();",
        1,
    )[0]

    assert response.status_code == 200
    assert response.context["url_search_active"] is True
    assert response.context["url_identity_match_count"] == 0
    assert 'bookmarkUnifiedForm.dataset.urlSearchComplete !== "true"' in focus_handler
    assert "matchCount !== 0" in focus_handler
    assert "!isCompleteHttpUrl(bookmarkSearch.value)" in focus_handler
    assert "bookmarkSearch.focus({ preventScroll: true });" in focus_handler
    assert "bookmarkSearch.setSelectionRange(caretPosition, caretPosition);" in focus_handler


def test_add_bookmark_button_shows_busy_success_and_restored_states(
    loopback_client,
    settings,
):
    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = _html(response)
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()
    stylesheet = Path(
        settings.BASE_DIR,
        "static",
        "bookmark_manager",
        "bookmarks.css",
    ).read_text()
    save_handler = script.split(
        'bookmarkUnifiedForm?.addEventListener("submit", async (event) => {',
        1,
    )[1].split(
        'document.querySelectorAll("[data-bookmark-import-form]")',
        1,
    )[0]
    submit_helper = script.split("const submitLocalForm = async (", 1)[1].split(
        'settingsForm?.addEventListener("submit"',
        1,
    )[0]
    added_rule = stylesheet.split(
        ".bookmark-manager-shell .bookmark-unified-form .button--primary.is-added {",
        1,
    )[1].split("}", 1)[0]

    assert response.status_code == 200
    assert "data-bookmark-save" in html
    assert ">Add bookmark</button>" in html
    assert re.search(
        r'<p[^>]*role="status"[^>]*hidden[^>]*data-bookmark-save-result[^>]*></p>',
        html,
    )
    assert re.search(r'submitter\.textContent = "Adding(?: bookmark)?…";', save_handler)
    assert 'busyButton?.setAttribute("aria-busy", "true")' in submit_helper
    assert "busyButton.disabled = true" in submit_helper
    assert "busyButton.disabled = false" in submit_helper
    assert 'busyButton?.removeAttribute("aria-busy")' in submit_helper
    assert "submitter," in save_handler
    assert save_handler.index("const defaultSaveLabel") < save_handler.index(
        'submitter.textContent = "Adding'
    )
    assert save_handler.index('submitter.textContent = "Adding') < save_handler.index(
        "await submitLocalForm("
    )
    assert 'submitter.classList.add("is-added")' in save_handler
    assert 'submitter.textContent = "✓ Added"' in save_handler
    assert 'submitter.setAttribute("aria-label", "Bookmark added")' in save_handler
    assert "submitter.textContent = defaultSaveLabel" in save_handler
    assert 'submitter.removeAttribute("aria-label")' in save_handler
    assert "let redirectPending = false;" in save_handler
    assert "new URL(payload.redirect, window.location.href).origin" in save_handler
    assert "submitter.disabled = redirectPending;" in save_handler
    assert "if (!redirectPending)" in save_handler
    assert "bookmarkSaveInFlight = false;" in save_handler
    assert "let redirectAccepted = false;" in submit_helper
    assert "if (!redirectAccepted)" in submit_helper
    assert "window.setTimeout(() =>" in save_handler
    assert "background: var(--teal);" in added_rule


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


def test_page_details_exposes_every_existing_tag_as_escaped_autocomplete_data(loopback_client):
    selected = _create_bookmark()
    another = _create_bookmark(page_id="730004", title="Another saved page")
    _add_tag(selected, "Network Edge")
    _add_tag(another, "Architecture")
    Tag.objects.get_or_create_normalized("<Existing & reusable>")

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": selected.pk},
    )
    html = _html(response)

    assert response.status_code == 200
    assert set(response.context["tag_suggestions"]) == {
        "Network Edge",
        "Architecture",
        "<Existing & reusable>",
    }
    assert 'role="combobox"' in html
    assert 'aria-autocomplete="list"' in html
    assert 'aria-haspopup="listbox"' in html
    assert 'aria-controls="bookmark-tag-suggestion-list"' in html
    assert 'id="bookmark-tag-suggestion-list"' in html
    assert 'role="listbox"' in html
    encoded = re.search(
        r'<script id="bookmark-tag-suggestions-data" type="application/json">(.*?)</script>',
        html,
    )
    assert encoded is not None
    assert set(json.loads(encoded.group(1))) == {
        "Network Edge",
        "Architecture",
        "<Existing & reusable>",
    }
    assert "\\u003CExisting \\u0026 reusable\\u003E" in encoded.group(1)


def test_tag_autocomplete_is_comma_aware_and_keeps_free_form_tags(settings):
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()

    for hook in (
        'document.querySelector("[data-tag-autocomplete]")',
        'value.lastIndexOf(",", cursor - 1)',
        'value.indexOf(",", cursor)',
        "candidate.includes(query)",
        ".slice(0, 8)",
        'event.key === "ArrowDown"',
        'event.key === "ArrowUp"',
        'event.key === "Enter"',
        'event.key === "Escape"',
        "chooseTagSuggestion",
    ):
        assert hook in script


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
    assert '<button class="tree-node-link" type="submit"' in html
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


def test_tree_folder_open_counts_roll_up_all_descendant_bookmarks_without_extra_queries(
    django_assert_num_queries,
):
    root = _node_snapshot("741001", "Shared knowledge", space_key="ENG")
    section = _node_snapshot("741002", "Architecture", space_key="ENG")
    first = _create_bookmark(
        page_id="741003",
        title="Routing guide",
        ancestors=(root, section),
        space_key="ENG",
    )
    second = _create_bookmark(
        page_id="741004",
        title="Security guide",
        ancestors=(root, section),
        space_key="ENG",
    )
    _set_bookmark_values(first, open_count=2)
    _set_bookmark_values(second, open_count=7)

    # Only one leaf is visible, but a folder's count describes its complete local
    # subtree rather than changing when a search hides another saved descendant.
    query_result = views.query_bookmarks(views.BookmarkQuery(search="Routing guide"))
    with django_assert_num_queries(1):
        roots, _outline_numbers = views._tree_items(
            query_result,
            selected_pk=None,
            located_pk=None,
        )

    root_item = next(item for item in roots if item.node.page_id == root.page_id)
    section_item = next(item for item in root_item.children if item.node.page_id == section.page_id)
    leaf_item = next(item for item in section_item.children if item.bookmark == first)

    assert root_item.subtree_open_count == 9
    assert section_item.subtree_open_count == 9
    assert leaf_item.subtree_open_count == 2


def test_tree_title_is_the_only_external_open_hit_target(loopback_client, settings):
    bookmark = _create_bookmark()

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk},
    )
    html = _html(response)
    stylesheet = Path(
        settings.BASE_DIR,
        "static",
        "bookmark_manager",
        "bookmarks.css",
    ).read_text()
    open_form_rule = stylesheet.split(
        ".bookmark-manager-shell .tree-open-form {",
        1,
    )[1].split("}", 1)[0]
    title_button_rule = stylesheet.split(
        ".bookmark-manager-shell .tree-node-link {",
        1,
    )[1].split("}", 1)[0]

    assert f'data-tree-row\n        data-details-url="?selected={bookmark.pk}' in html
    assert f'action="/bookmarks/{bookmark.pk}/open/" target="_blank"' in html
    assert '<button class="tree-node-link" type="submit"' in html
    assert "width: fit-content;" in open_form_rule
    assert "max-width: 100%;" in open_form_rule
    assert "justify-self: start;" in open_form_rule
    assert "display: inline-block;" in title_button_rule
    assert "width: auto;" in title_button_rule
    assert "max-width: 100%;" in title_button_rule


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


def test_historical_refresh_notification_target_keeps_its_failure_details_visible(
    loopback_client,
):
    bookmark = _create_bookmark()
    historical = BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        total_bookmarks=1,
        processed_bookmarks=1,
        failed_bookmarks=1,
        completed_at=timezone.now() - timedelta(hours=3),
    )
    failure = BookmarkRefreshFailure.objects.create(
        refresh_run=historical,
        bookmark=bookmark,
        page_id=bookmark.page_id,
        url=bookmark.url,
        error_code="unreachable",
        reason="Confluence was temporarily unreachable.",
        attempt_count=3,
    )
    BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED,
        total_bookmarks=1,
        processed_bookmarks=1,
        succeeded_bookmarks=1,
        completed_at=timezone.now(),
    )

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"refresh_run": historical.pk},
    )
    html = _html(response)

    assert response.status_code == 200
    assert 'data-history-locked="true"' in html
    assert failure.url in html
    assert failure.reason in html


@pytest.mark.parametrize(
    ("status", "expected_label"),
    (
        (BookmarkRefreshStatus.SUCCEEDED, "Confluence refreshed"),
        (BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS, "Confluence refreshed"),
        (BookmarkRefreshStatus.FAILED, "Refresh needs attention"),
        (BookmarkRefreshStatus.INTERRUPTED, "Refresh needs attention"),
    ),
)
def test_terminal_refresh_status_on_initial_load_is_inactive(
    loopback_client,
    status,
    expected_label,
):
    succeeded = int(status == BookmarkRefreshStatus.SUCCEEDED)
    BookmarkRefreshRun.objects.create(
        status=status,
        total_bookmarks=1,
        processed_bookmarks=1,
        succeeded_bookmarks=succeeded,
        failed_bookmarks=1 - succeeded,
        completed_at=timezone.now(),
    )

    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = _html(response)

    assert response.status_code == 200
    assert "data-global-refresh\n                data-status-url=" in html
    assert 'data-active="false"' in html
    assert f'data-status="{status}"' in html
    assert expected_label in html


def test_completed_refresh_timestamp_shows_both_local_date_and_time(loopback_client):
    completed_at = datetime(2026, 8, 28, 18, 37, tzinfo=UTC)
    BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED,
        total_bookmarks=1,
        processed_bookmarks=1,
        succeeded_bookmarks=1,
        failed_bookmarks=0,
        completed_at=completed_at,
    )

    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = _html(response)
    expected_display = timezone.localtime(completed_at).strftime("%d %b %Y, %H:%M:%S %Z")

    assert response.status_code == 200
    assert f'<time datetime="{completed_at.isoformat()}">{expected_display}</time>' in html
    assert response.context["refresh_status"]["last_completed_display"]
    assert re.search(
        r"Last completed <time datetime=\"[^\"]+\">"
        r"\d{2} \w{3} \d{4}, \d{2}:\d{2}:\d{2} \w+</time>",
        html,
    )


def test_global_refresh_javascript_schedules_one_reload_for_every_terminal_outcome(settings):
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()
    refresh_script = script.split(
        'const globalRefresh = document.querySelector("[data-global-refresh]");',
        1,
    )[1].split("    const safeStorage = {", 1)[0]
    submit_script = refresh_script.split(
        'globalRefresh?.addEventListener("submit", async (event) => {',
        1,
    )[1]

    # An active run rendered by the server and a run started on this page both
    # become reload candidates. A terminal result rendered on the initial page
    # does not, because data-active is false and renderGlobalRefresh is not called.
    assert (
        'let globalRefreshReloadPending = globalRefresh?.dataset.active === "true";'
        in refresh_script
    )
    assert submit_script.index("globalRefreshReloadPending = true;") < submit_script.index(
        "renderGlobalRefresh(payload.refresh);"
    )
    assert "runId !== globalRefreshObservedRunId" in refresh_script
    assert 'window.addEventListener("owl:refresh-status"' in refresh_script

    # Success, partial success, and final failures all finish the background run
    # and therefore must refresh the bookmark names, text, dates, and status UI.
    for terminal_status in (
        '"succeeded"',
        '"succeeded_with_errors"',
        '"failed"',
        '"interrupted"',
    ):
        assert (
            terminal_status
            in refresh_script.split(
                "const globalRefreshTerminalStatuses = new Set([",
                1,
            )[1].split("]);", 1)[0]
        )
    assert "globalRefreshTerminalStatuses.has(status)" in refresh_script

    # Polling can return the same terminal snapshot more than once, so the reload
    # helper and the pending flag both guard the navigation as a one-shot action.
    assert "if (globalRefreshReloadScheduled)" in refresh_script
    assert "globalRefreshReloadScheduled = true;" in refresh_script
    assert "globalRefreshReloadPending = false;" in refresh_script
    assert refresh_script.count("reloadAfterGlobalRefresh();") == 1
    assert refresh_script.count("window.location.reload()") == 1


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
    people_header = html.split(
        '<aside class="bookmark-pane bookmark-pane--people"',
        1,
    )[1].split('<div class="people-search"', 1)[0]

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
    assert "data-people-total-count" in people_header
    assert 'aria-label="3 people total"' in people_header
    assert re.search(r"data-people-total-count[^>]*>\s*3\s*<", people_header)
    assert "data-people-search-input" in html
    assert 'data-person-name="Alice Author"' in html
    assert "data-people-no-results" in html
    assert "data-people-filter-form" in html
    assert 'name="person" value="Alice Author" data-person-select' in html

    filtered = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"person": "Alice Author", "sort": "updated_newest"},
    )

    assert set(filtered.context["query_result"].bookmarks) == {first, second}
    assert web not in filtered.context["query_result"].bookmarks
    assert filtered.context["active_contact"] == "Alice Author"
    assert filtered.context["active_contacts"] == ("Alice Author",)
    filtered_html = _html(filtered)
    filtered_header = filtered_html.split(
        '<aside class="bookmark-pane bookmark-pane--people"',
        1,
    )[1].split('<div class="people-search"', 1)[0]
    assert 'aria-label="3 people total"' in filtered_header
    assert re.search(r"data-people-total-count[^>]*>\s*3\s*<", filtered_header)
    assert filtered_html.count("data-people-entry") == 3
    assert 'value="Alice Author" data-person-select checked' in filtered_html


def test_people_filter_accepts_multiple_people_and_matches_any_confluence_role(
    loopback_client,
):
    alice_page = _create_bookmark("731001", "Alice architecture page", ancestors=())
    zoe_page = _create_bookmark("731002", "Zoe operations page", ancestors=())
    unrelated_page = _create_bookmark("731003", "Unrelated platform page", ancestors=())
    web_collision = _create_bookmark("731004", "Alice web page", ancestors=())
    _set_bookmark_values(
        zoe_page,
        author_id="dana-1",
        author_name="Dana Writer",
        created_by_id="dana-1",
        created_by_name="Dana Writer",
        modified_by_id="zoe-2",
        modified_by_name="Zoe Editor",
    )
    _set_bookmark_values(
        unrelated_page,
        author_id="uma-3",
        author_name="Uma Writer",
        created_by_id="uma-3",
        created_by_name="Uma Writer",
        modified_by_id="victor-4",
        modified_by_name="Victor Editor",
    )
    _set_bookmark_values(
        web_collision,
        source_type=BookmarkSource.WEB,
        author_name="Alice Author",
        created_by_name="Alice Author",
        modified_by_name="Zoe Editor",
    )
    path = (
        reverse("bookmark_manager:index")
        + "?"
        + urlencode(
            [
                ("q", "page"),
                ("person", "Alice Author"),
                ("person", "Zoe Editor"),
                ("sort", "updated_newest"),
            ]
        )
    )

    response = loopback_client.get(path)

    assert response.status_code == 200
    assert response.context["bookmark_query"].people == (
        "Alice Author",
        "Zoe Editor",
    )
    assert response.context["active_contacts"] == ("Alice Author", "Zoe Editor")
    assert set(response.context["query_result"].bookmarks) == {alice_page, zoe_page}
    assert unrelated_page not in response.context["query_result"].bookmarks
    assert web_collision not in response.context["query_result"].bookmarks
    assert [
        active.value for active in response.context["active_filters"] if active.key == "person"
    ] == ["Alice Author", "Zoe Editor"]
    assert parse_qs(urlparse(response.context["people_filter_clear_href"]).query) == {
        "people_filter": ["1"],
        "q": ["page"],
        "sort": ["updated_newest"],
    }
    html = _html(response)
    assert html.count("data-person-select checked") == 2
    assert html.count('type="hidden" name="person"') == 2
    assert "2 selected" in html
    assert 'name="q" value="page"' in html


def test_people_filter_explicitly_overrides_people_from_a_saved_view(loopback_client):
    alice_page = _create_bookmark("731011", "Alice saved-view page", ancestors=())
    zoe_page = _create_bookmark("731012", "Zoe saved-view page", ancestors=())
    _set_bookmark_values(
        zoe_page,
        author_name="Zoe Editor",
        created_by_name="Zoe Editor",
        modified_by_name="Zoe Editor",
    )
    saved_view = SavedBookmarkView.objects.create(
        name="Alice pages",
        filters={"people": ["Alice Author"]},
        sort="added_newest",
    )
    path = (
        reverse("bookmark_manager:index")
        + "?"
        + urlencode(
            [
                ("saved_view", saved_view.pk),
                ("people_filter", "1"),
                ("person", "Zoe Editor"),
            ]
        )
    )

    response = loopback_client.get(path)

    assert response.status_code == 200
    assert response.context["bookmark_query"].people == ("Zoe Editor",)
    assert response.context["query_result"].bookmarks == (zoe_page,)
    assert alice_page not in response.context["query_result"].bookmarks


def test_people_header_always_shows_zero_total_with_empty_rail(loopback_client):
    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"q": "no saved bookmark can match this search"},
    )
    html = _html(response)
    people_header = html.split(
        '<aside class="bookmark-pane bookmark-pane--people"',
        1,
    )[1].split('<div class="people-search"', 1)[0]

    assert response.status_code == 200
    assert response.context["confluence_contacts"] == ()
    assert "data-people-total-count" in people_header
    assert 'aria-label="0 people total"' in people_header
    assert re.search(r"data-people-total-count[^>]*>\s*0\s*<", people_header)
    assert "data-people-search-toggle\n                        disabled" in people_header
    assert "data-people-entry" not in html
    assert "No Confluence people yet" in html


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


def test_sort_icons_preserve_filters_and_selection_and_toggle_the_active_direction(
    loopback_client,
):
    target = _create_bookmark()
    _set_bookmark_values(target, favorite=True, open_count=9)
    category = BookmarkCategory.objects.create(
        domain="architecture.example.invalid",
        name="Architecture",
    )
    _set_bookmark_values(target, category=category)

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {
            "q": target.title,
            "favorite": "on",
            "category": target.category_id,
            "selected": target.pk,
            "located": target.pk,
            "sort": "most_opened",
            "saved": "new",
        },
    )

    assert response.status_code == 200
    controls = {control.key: control for control in response.context["sort_controls"]}
    open_count = controls["open_count"]
    assert open_count.active is True
    assert open_count.direction == "descending"
    next_open_query = parse_qs(urlparse(open_count.href).query)
    assert next_open_query["sort"] == ["least_opened"]
    assert next_open_query["q"] == [target.title]
    assert next_open_query["favorite"] == ["on"]
    assert next_open_query["category"] == [str(target.category_id)]
    assert next_open_query["selected"] == [str(target.pk)]
    assert next_open_query["located"] == [str(target.pk)]
    assert "saved" not in next_open_query

    updated = controls["updated"]
    assert updated.active is False
    assert updated.direction == "inactive"
    assert parse_qs(urlparse(updated.href).query)["sort"] == ["updated_newest"]

    html = _html(response)
    assert 'type="hidden" name="sort" value="most_opened"' in html
    assert 'data-sort-key="open_count"' in html
    assert 'data-sort-direction="descending"' in html
    assert 'aria-label="Sort by Open count, lowest first"' in html
    assert 'aria-current="true"' in html

    toggled = loopback_client.get(open_count.href)
    assert toggled.context["bookmark_query"].sort == "least_opened"
    assert toggled.context["selected_bookmark"] == target
    toggled_control = {control.key: control for control in toggled.context["sort_controls"]}[
        "open_count"
    ]
    assert toggled_control.direction == "ascending"
    assert parse_qs(urlparse(toggled_control.href).query)["sort"] == ["most_opened"]


@pytest.mark.parametrize(
    ("key", "descending", "ascending"),
    (
        ("open_count", "most_opened", "least_opened"),
        ("updated", "updated_newest", "updated_oldest"),
        ("created", "created_newest", "created_oldest"),
        ("saved", "added_newest", "added_oldest"),
    ),
)
def test_each_sort_icon_is_a_two_state_get_toggle(
    loopback_client,
    key,
    descending,
    ascending,
):
    descending_response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"sort": descending},
    )
    descending_control = {
        control.key: control for control in descending_response.context["sort_controls"]
    }[key]
    assert descending_control.active is True
    assert descending_control.direction == "descending"
    assert parse_qs(urlparse(descending_control.href).query)["sort"] == [ascending]

    ascending_response = loopback_client.get(descending_control.href)
    ascending_control = {
        control.key: control for control in ascending_response.context["sort_controls"]
    }[key]
    assert ascending_control.active is True
    assert ascending_control.direction == "ascending"
    assert parse_qs(urlparse(ascending_control.href).query)["sort"] == [descending]


@pytest.mark.parametrize(
    ("sort", "expected_titles"),
    (
        ("most_opened", ["Bravo", "Charlie", "Alpha"]),
        ("least_opened", ["Alpha", "Charlie", "Bravo"]),
        ("updated_newest", ["Bravo", "Alpha", "Charlie"]),
        ("updated_oldest", ["Charlie", "Alpha", "Bravo"]),
        ("created_newest", ["Alpha", "Charlie", "Bravo"]),
        ("created_oldest", ["Bravo", "Charlie", "Alpha"]),
        ("added_newest", ["Charlie", "Bravo", "Alpha"]),
        ("added_oldest", ["Alpha", "Bravo", "Charlie"]),
    ),
)
def test_sort_icons_reorder_rendered_bookmark_branches(sort, expected_titles):
    now = timezone.now()
    alpha = _create_bookmark("813001", "Alpha", ancestors=())
    bravo = _create_bookmark("813002", "Bravo", ancestors=())
    charlie = _create_bookmark("813003", "Charlie", ancestors=())
    _set_bookmark_values(
        alpha,
        open_count=1,
        saved_at=now - timedelta(days=3),
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=2),
    )
    _set_bookmark_values(
        bravo,
        open_count=3,
        saved_at=now - timedelta(days=2),
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(days=1),
    )
    _set_bookmark_values(
        charlie,
        open_count=2,
        saved_at=now - timedelta(days=1),
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=3),
    )

    query_result = views.query_bookmarks(views.BookmarkQuery(sort=sort))
    tree_items, _outline_numbers = views._tree_items(
        query_result,
        selected_pk=None,
        located_pk=None,
    )

    assert [item.node.title for item in tree_items] == expected_titles


def test_explicit_sort_link_overrides_only_the_saved_view_sort(loopback_client):
    target = _create_bookmark()
    _set_bookmark_values(target, favorite=True, open_count=9)
    saved = SavedBookmarkView.objects.create(
        name="Favorite architecture",
        search_text=target.title,
        filters={"favorite": True},
        sort="most_opened",
    )

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {
            "saved_view": saved.pk,
            "sort": "least_opened",
            "selected": target.pk,
            "located": target.pk,
        },
    )

    assert response.status_code == 200
    assert response.context["active_saved_view"] == saved
    assert response.context["bookmark_query"].sort == "least_opened"
    assert response.context["bookmark_query"].favorite is True
    assert response.context["bookmark_query"].search == target.title
    assert response.context["query_result"].bookmarks == (target,)
    assert response.context["selected_bookmark"] == target
    open_count = {control.key: control for control in response.context["sort_controls"]}[
        "open_count"
    ]
    next_query = parse_qs(urlparse(open_count.href).query)
    assert next_query["saved_view"] == [str(saved.pk)]
    assert next_query["sort"] == ["most_opened"]


def test_personal_folder_create_move_render_and_unfile_preserve_confluence_hierarchy(
    loopback_client,
):
    first = _create_bookmark("811001", "General platform guide")
    second = _create_bookmark("811002", "General operations guide")
    all_nodes = list(
        ConfluencePageNode.objects.order_by(
            "parent_id", "outline_position", "sibling_position", "title", "id"
        )
    )
    original_numbers = outline_number_map(all_nodes)
    canonical_before = {
        bookmark.pk: (
            bookmark.tree_node_id,
            bookmark.tree_node.parent_id,
            bookmark.tree_node.outline_position,
        )
        for bookmark in (first, second)
    }

    created = _async_post(
        loopback_client,
        reverse("bookmark_manager:folder_create"),
        {
            "name": "General",
            "return_to": "/bookmarks/?sort=most_opened",
        },
    )

    assert created.status_code == 200
    assert created.json()["state"] == "success"
    assert "sort=most_opened" in created.json()["redirect"]
    folder = BookmarkFolder.objects.get(normalized_name="general")
    empty_folder_html = _html(loopback_client.get(reverse("bookmark_manager:index")))
    assert "Drop bookmarks here" in empty_folder_html
    assert empty_folder_html.count(f'data-folder-drop-target="{folder.pk}"') == 2

    moved = _async_post(
        loopback_client,
        reverse("bookmark_manager:folder_move"),
        {
            "bookmark_ids": [str(first.pk), str(second.pk)],
            "folder_id": str(folder.pk),
            "return_to": "/bookmarks/?sort=most_opened",
        },
    )

    assert moved.status_code == 200
    assert moved.json()["updated_count"] == 2
    assert moved.json()["detail"] == "Moved 2 bookmarks to personal folder “General”."
    first.refresh_from_db()
    second.refresh_from_db()
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

    page = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": first.pk, "sort": "most_opened"},
    )

    assert page.status_code == 200
    folder_item = page.context["manual_folder_items"][0]
    assert folder_item.folder == folder
    assert {item.bookmark for item in folder_item.items} == {first, second}
    assert {item.bookmark.pk: item.outline_number for item in folder_item.items} == {
        first.pk: original_numbers[first.tree_node_id],
        second.pk: original_numbers[second.tree_node_id],
    }
    canonical_bookmark_ids: set[int] = set()

    def collect_canonical_bookmarks(items):
        for item in items:
            if item.bookmark is not None:
                canonical_bookmark_ids.add(item.bookmark.pk)
            collect_canonical_bookmarks(item.children)

    collect_canonical_bookmarks(page.context["tree_items"])
    assert first.pk not in canonical_bookmark_ids
    assert second.pk not in canonical_bookmark_ids
    html = _html(page)
    assert "data-folder-create-toggle" in html
    assert f'data-folder-drop-target="{folder.pk}"' in html
    assert "data-bookmark-drag" in html
    assert f'data-bookmark-id="{first.pk}"' in html
    assert "data-folder-move-form" in html
    assert "Confluence hierarchy (no personal folder)" in html

    unfiled = _async_post(
        loopback_client,
        reverse("bookmark_manager:folder_move"),
        {
            "bookmark_ids": [str(first.pk), str(second.pk)],
            "folder_id": "",
            "return_to": "/bookmarks/",
        },
    )

    assert unfiled.status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.manual_folder is None
    assert second.manual_folder is None


def test_personal_folder_endpoint_rejects_duplicate_names_and_partial_moves(loopback_client):
    bookmark = _create_bookmark("812001", "Folder validation page")
    folder = BookmarkFolder.objects.create(name="Architecture")

    duplicate = _async_post(
        loopback_client,
        reverse("bookmark_manager:folder_create"),
        {"name": "  ARCHITECTURE  "},
    )
    invalid_move = _async_post(
        loopback_client,
        reverse("bookmark_manager:folder_move"),
        {
            "bookmark_ids": [str(bookmark.pk), "999999"],
            "folder_id": str(folder.pk),
        },
    )

    assert duplicate.status_code == 400
    assert duplicate.json()["state"] == "invalid_folder"
    assert invalid_move.status_code == 400
    bookmark.refresh_from_db()
    assert bookmark.manual_folder is None


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

    response = loopback_client.post(reverse("bookmark_manager:export"))
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
    destination = urlparse(response["Location"])
    assert destination.path == reverse("bookmark_manager:settings")
    assert parse_qs(destination.query) == {
        "section": ["bookmark-data"],
        "import_run": [str(run.pk)],
    }
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
    assert "Failed URL:</strong> Unavailable for this record." in html
    assert "data-import-result" in html
    assert 'aria-label="Dismiss import result"' in html
    assert "data-dismiss-import-result" in html
    assert 'class="operation-failure-list"' in html


def test_import_database_contention_returns_controlled_retry_response(loopback_client, monkeypatch):
    monkeypatch.setattr(
        views,
        "import_bookmarks_document",
        Mock(side_effect=OperationalError("database is locked")),
    )
    upload = SimpleUploadedFile("bookmarks.json", b"[]", content_type="application/json")

    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"import_file": upload, "return_to": "settings"},
    )

    assert response.status_code == 503
    assert "Bookmark import paused" in _html(response)
    assert "local database is busy" in _html(response)


def test_json_import_result_identifies_failed_record_with_sanitized_source_url(
    loopback_client,
):
    upload = SimpleUploadedFile(
        "failed-bookmark.json",
        json.dumps(
            [
                {
                    "page_id": "810099",
                    "title": "Unsafe source URL",
                    "url": (
                        "https://"
                        "username:password@confluence.example.invalid/"
                        "wiki/pages/810099?token=private-token#private-fragment"
                    ),
                }
            ]
        ).encode("utf-8"),
        content_type="application/json",
    )

    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"import_file": upload, "return_to": "settings"},
    )

    assert response.status_code == 302
    run = BookmarkImportRun.objects.get()
    failure = run.failures.get()
    assert failure.source_url == ("https://confluence.example.invalid/wiki/pages/810099")

    html = _html(loopback_client.get(response["Location"]))
    assert "Failed URL:" in html
    assert "<code>https://confluence.example.invalid/wiki/pages/810099</code>" in html
    assert "username" not in html
    assert "username:password@" not in html
    assert "private-token" not in html
    assert "private-fragment" not in html


def test_browser_style_async_json_import_passes_csrf_and_redirects_to_result():
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = csrf_client.get(reverse("bookmark_manager:settings"), {"section": "bookmark-data"})
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
    destination = urlparse(response["Location"])
    assert destination.path == reverse("bookmark_manager:settings")
    assert parse_qs(destination.query)["section"] == ["bookmark-data"]
    assert parse_qs(destination.query)["import_run"]
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
    page = csrf_client.get(reverse("bookmark_manager:settings"), {"section": "bookmark-data"})
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
    assert "<code>https://example.com/guides/incomplete...</code>" in html
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
    assert response.context["selected_settings_section"] == "bookmark-data"
    assert "Bookmark data" in html
    assert "Import bookmarks" in html
    assert "Choose a valid UTF-8 .json or .txt file" in html
    assert "This field is required." in html
    assert 'name="return_to" value="settings"' in html
    assert "data-settings-autofocus" in html
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
        reverse("bookmark_manager:export"),
        reverse("bookmark_manager:delete", args=(bookmark.pk,)),
        reverse("bookmark_manager:delete_selected"),
        reverse("bookmark_manager:open_parent", args=(bookmark.pk,)),
        reverse("bookmark_manager:folder_create"),
        reverse("bookmark_manager:folder_move"),
    )

    for path in post_only:
        assert loopback_client.get(path).status_code == 405


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
        (reverse("bookmark_manager:export"), {}),
        (reverse("bookmark_manager:delete", args=(bookmark.pk,)), {"confirm": "delete"}),
        (
            reverse("bookmark_manager:delete_selected"),
            {"confirm": "delete-selected", "bookmark_ids": [bookmark.pk]},
        ),
        (reverse("bookmark_manager:open_parent", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:folder_create"), {"name": "CSRF folder"}),
        (
            reverse("bookmark_manager:folder_move"),
            {"bookmark_ids": [bookmark.pk], "folder_id": ""},
        ),
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
        (reverse("bookmark_manager:export"), {}),
        (reverse("bookmark_manager:delete", args=(bookmark.pk,)), {"confirm": "delete"}),
        (
            reverse("bookmark_manager:delete_selected"),
            {"confirm": "delete-selected", "bookmark_ids": [bookmark.pk]},
        ),
        (reverse("bookmark_manager:open_parent", args=(bookmark.pk,)), {}),
        (reverse("bookmark_manager:folder_create"), {"name": "Remote folder"}),
        (
            reverse("bookmark_manager:folder_move"),
            {"bookmark_ids": [bookmark.pk], "folder_id": ""},
        ),
    )

    for path, data in post_actions:
        assert remote_client.post(path, data).status_code == 403


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


def test_selected_live_bookmarks_expose_only_safe_local_open_endpoints(loopback_client):
    store = get_secret_store()
    assert isinstance(store, InMemorySecretStore)
    configured = save_ui_configuration(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=SYNTHETIC_PAT,
        secret_store=store,
    )
    assert configured.success is True
    first = _create_bookmark("840001", "First live page", ancestors=())
    second = _create_bookmark("840002", "Second live page", ancestors=())
    deleted = _create_bookmark("840003", "Deleted page", ancestors=())
    _set_bookmark_values(deleted, availability_status=BookmarkAvailability.NOT_FOUND)

    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": first.pk},
    )
    html = _html(response)

    def checkbox(bookmark: Bookmark) -> str:
        match = re.search(
            rf'<input\b(?=[^>]*\bvalue="{bookmark.pk}")[^>]*>',
            html,
            re.DOTALL,
        )
        assert match is not None
        return match.group(0)

    for bookmark in (first, second):
        element = checkbox(bookmark)
        endpoint = reverse("bookmark_manager:open", args=(bookmark.pk,))
        assert f'data-open-url="{endpoint}"' in element
        assert bookmark.url not in element

        opened = _async_post(loopback_client, endpoint)
        assert opened.status_code == 200
        assert opened.json()["url"] == bookmark.url

    assert "data-open-url" not in checkbox(deleted)
    assert "data-open-selected" in html
    assert "data-open-selected-button" in html
    assert "data-details-open-form" in html
    assert "data-details-open-button" in html
    assert Bookmark.objects.get(pk=first.pk).open_count == 1
    assert Bookmark.objects.get(pk=second.pk).open_count == 1
    assert Bookmark.objects.get(pk=deleted.pk).open_count == 0


def test_page_details_copy_controls_use_compact_icons_and_one_second_tick_feedback(
    loopback_client,
    settings,
):
    bookmark = _create_bookmark()

    response = loopback_client.get(reverse("bookmark_manager:index"), {"selected": bookmark.pk})
    html = _html(response)
    script = Path(settings.BASE_DIR, "static", "bookmark_manager", "bookmarks.js").read_text()

    assert response.status_code == 200
    assert f'data-copy-value="{bookmark.url}"' in html
    assert 'class="copy-url-control"' in html
    assert 'aria-label="Copy URL"' in html
    assert 'title="Copy URL"' in html
    assert 'data-copy-success="URL copied"' in html
    assert 'data-copy-default-label="Copy URL"' in html
    assert 'data-copy-default-label="Copy Page ID"' in html
    assert 'data-copy-default-label="Copy breadcrumb"' in html
    assert html.count('class="copy-url-control"') == 3
    assert html.count("copy-url-control__icon--success") == 3
    assert "data-copy-feedback" not in html
    assert html.count('data-copy-default-label="Copy URL"') == 1
    assert "const copyFeedbackTimers = new WeakMap()" in script
    assert "if (!button.dataset.copyDefaultLabel)" in script
    assert "window.setTimeout(() => resetCopyFeedback(button), 1000)" in script


def test_every_page_detail_copy_icon_keeps_an_accessible_label(loopback_client):
    bookmark = _create_bookmark()

    response = loopback_client.get(reverse("bookmark_manager:index"), {"selected": bookmark.pk})
    html = _html(response)

    for label in ("Copy URL", "Copy Page ID", "Copy breadcrumb"):
        match = re.search(
            rf'<button\b(?=[^>]*\bdata-copy-value=)(?=[^>]*\baria-label="{label}")[^>]*>',
            html,
            re.DOTALL,
        )
        assert match is not None, f"Missing accessible label for {label}"
        assert f'title="{label}"' in match.group(0)


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
    assert f'data-open-url="/bookmarks/{bookmark.pk}/open/"' in html
    assert "tree-details-link" not in html
    assert ">Page details</a>" not in html
    assert "data-tree-expand-all" in html
    assert "data-tree-collapse-all" in html
    assert "data-tree-select-all" in html
    assert 'aria-label="Select all bookmarks shown in the tree"' in html
    open_selected_button = re.search(
        r"<button\b(?=[^>]*\bdata-open-selected-button)(?=[^>]*\bdisabled)[^>]*>",
        html,
        re.DOTALL,
    )
    assert open_selected_button is not None
    assert 'aria-label="Open selected bookmarks"' in open_selected_button.group(0)
    assert 'title="Select bookmarks to open"' in open_selected_button.group(0)
    assert 'id="bulk-delete-bookmarks"' in html
    assert "data-delete-selected" in html
    assert 'data-delete-locked="true"' in html
    assert "🔒" in html
    assert "bookmarks.js?v=connection-health-v1" in html
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
    assert 'class="copy-url-control"' in html
    assert 'aria-label="Copy URL"' in html
    assert 'data-copy-success="URL copied"' in html
    assert 'data-copy-default-label="Copy URL"' in html
    assert 'data-copy-default-label="Copy Page ID"' in html
    assert 'data-copy-default-label="Copy breadcrumb"' in html
    assert "data-copy-feedback" not in html
    assert "data-details-open-form" in html
    assert "data-details-open-button" in html
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
        'selectAllCheckbox?.addEventListener("change"',
        "selectAllCheckbox.indeterminate = count > 0",
        "treeChecks.forEach((checkbox) => {",
        "[data-bookmark-import-form]",
        "Extracting URLs, checking Confluence Page IDs, and retrieving pages",
        "Retrieving the Confluence page, hierarchy, and searchable text",
        "bookmarkUnifiedForm.dataset.urlSearchComplete",
        "bookmarkUnifiedForm.requestSubmit(bookmarkSaveButton)",
        "window.clearTimeout(searchTimer)",
        "element.textContent = payload.notes",
        "data-external-open-form",
        'document.querySelector("[data-open-selected-button]")',
        'openSelectedButton?.addEventListener("click"',
        "selectedOpenRequests(checkedBookmarks)",
        "selectedCount - requests.length",
        'form.matches("[data-details-open-form]")',
        "selectedBookmarkChecks()",
        'window.open("about:blank", "_blank")',
        "openedWindows[index].location.replace(payload.url)",
        "if (!button.dataset.copyDefaultLabel)",
        "window.setTimeout(() => resetCopyFeedback(button), 1000)",
        'submitter.textContent = "✓ Added"',
        'submitter.classList.add("is-added")',
    ):
        assert hook in script
