"""Contracts between Django data services and the shared Vite React frontend."""

import pytest
from django.urls import reverse

from bookmark_manager.models import Bookmark, BookmarkCategory, ConfluencePageNode

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    return client


def _bookmark():
    category = BookmarkCategory.objects.create(
        domain="docs.example.test",
        name="Engineering docs",
    )
    node = ConfluencePageNode.objects.create(
        page_id="react-1001",
        title="Availability sign-off",
        url="https://docs.example.test/pages/react-1001",
        space_key="ADR",
        outline_position=1,
    )
    return Bookmark.objects.create(
        page_id="react-1001",
        tree_node=node,
        title=node.title,
        url=node.url,
        space_name="Architecture",
        space_key="ADR",
        category=category,
        author_name="Alex Engineer",
        created_by_name="Alex Engineer",
        modified_by_name="Morgan Editor",
        open_count=4,
    )


def test_home_and_bookmark_pages_are_thin_react_mounts(loopback_client):
    home = loopback_client.get(reverse("core:dashboard")).content.decode()
    bookmarks = loopback_client.get(reverse("bookmark_manager:index")).content.decode()
    settings = loopback_client.get(reverse("bookmark_manager:settings")).content.decode()

    assert 'id="home-root" data-workspace-url="/home/workspace/"' in home
    assert 'id="bookmarks-root" data-workspace-url="/bookmarks/workspace/"' in bookmarks
    assert "owl-frontend.css" in home and "owl-frontend.js" in home
    assert "owl-frontend.css" in bookmarks and "owl-frontend.js" in bookmarks
    assert 'id="settings-root" data-workspace-url="/bookmarks/settings/workspace/"' in settings
    assert "owl-frontend.css" in settings and "owl-frontend.js" in settings
    assert "knowledge-app-card" not in home
    assert "bookmark-tree-row" not in bookmarks


def test_settings_workspace_is_secret_free_and_supports_react_sections(loopback_client):
    response = loopback_client.get(
        reverse("bookmark_manager:settings_workspace"),
        {"section": "overview"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selectedSection"] == "overview"
    assert [item["key"] for item in payload["sections"]] == [
        "overview",
        "confluence",
        "bookmark-data",
    ]
    assert payload["configuration"]["baseUrl"] == ""
    assert "personal_access_token" not in response.content.decode()
    assert "token" not in payload["configuration"]
    assert payload["urls"]["confluenceTest"] == "/bookmarks/settings/test/"


def test_bookmark_workspace_serializes_hierarchy_people_and_safe_actions(loopback_client):
    bookmark = _bookmark()
    response = loopback_client.get(
        reverse("bookmark_manager:workspace"),
        {"selected": bookmark.pk},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["totalBookmarks"] == 1
    assert payload["flatItems"][0]["title"] == "Availability sign-off"
    assert payload["flatItems"][0]["openUrl"] == f"/bookmarks/{bookmark.pk}/open/"
    assert payload["flatItems"][0]["url"] == bookmark.url
    assert payload["selectedBookmark"]["author"] == "Alex Engineer"
    assert payload["tree"][0]["bookmark"]["id"] == bookmark.pk
    assert payload["timelinePagination"]["previousUrl"] is None
    assert payload["timelinePagination"]["nextUrl"] is None
    assert [person["name"] for person in payload["people"]] == [
        "Alex Engineer",
        "Morgan Editor",
    ]
    assert payload["csrfToken"]
    assert "no-store" in response.headers["Cache-Control"]


def test_home_workspace_exposes_dashboard_data_and_post_only_open_urls(loopback_client):
    bookmark = _bookmark()
    response = loopback_client.get(reverse("core:dashboard_workspace"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["urls"]["bookmarks"] == "/bookmarks/"
    assert payload["urls"]["bitbucket"] == "/bitbucket/"
    assert payload["bookmarkMetrics"]
    assert payload["topViewed"][0]["bookmark"]["id"] == bookmark.pk
    assert payload["topViewed"][0]["bookmark"]["openUrl"] == (f"/bookmarks/{bookmark.pk}/open/")
    assert "url" not in payload["topViewed"][0]["bookmark"]
    assert payload["database"]["available"] is True
    assert response.headers["Cache-Control"] == "no-store"
