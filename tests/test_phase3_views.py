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

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkImportRun,
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
    assert "Sorted results" in html
    assert "Flat view with paths" in html
    assert "Knowledge › Cloud Architecture › PrivateLink route map" in html
    assert "Selected tags use ALL" in html
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
    html = _html(restored)
    assert restored.status_code == 200
    assert restored.context["active_saved_view"] == saved
    assert restored.context["query_result"].bookmarks == (target,)
    assert restored.context["query_result"].flat_mode is True
    assert f'?saved_view={saved.pk}" aria-current="true"' in html


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


def test_import_http_journey_continues_and_renders_partial_result(loopback_client):
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
        {"import_file": upload},
    )

    assert response.status_code == 302
    run = BookmarkImportRun.objects.get()
    assert f"import_run={run.pk}" in response["Location"]
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
    assert 'role="tree" aria-label="Confluence page hierarchy" data-bookmark-tree' in html
    assert 'role="treeitem"' in html
    assert 'aria-level="1"' in html
    assert 'aria-level="2"' in html
    assert 'aria-level="3"' in html
    assert 'aria-selected="true"' in html
    assert 'aria-expanded="true"' in html
    assert "data-located-bookmark" in html
    assert "data-tree-toggle" in html
    assert "data-tree-expand-all" in html
    assert "data-tree-collapse-all" in html
    assert "data-productivity-form" in html
    assert "data-favorite-form" in html
    assert "data-pin-form" in html
    assert 'aria-pressed="true"' in html
    assert 'data-copy-value="730003"' in html
    assert 'data-confirm-message="Delete this bookmark from OWL?' in html
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert "<time datetime=" in html and 'tabindex="0"' in html
    assert "Shortcuts:" in html
    for key in ("/", "E", "F", "P"):
        assert f"<kbd>{key}</kbd>" in html
    assert "Enter opens details" in html
    assert html.count('name="csrfmiddlewaretoken"') >= 8
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
        "element.textContent = payload.notes",
        "data-external-open-form",
    ):
        assert hook in script
