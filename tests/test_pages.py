import re
from html import unescape

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    return client


@pytest.mark.parametrize(
    ("path", "expected_heading"),
    [
        ("/", "Your trusted knowledge owl"),
        ("/search/", "Search both knowledge sources in one place"),
        ("/bookmarks/", "Bookmark Manager"),
        ("/bookmarks/settings/", "Confluence Settings"),
        ("/pdfs/", "Bitbucket Search"),
        ("/pdfs/repositories/", "Connect repositories safely"),
        ("/pdfs/status/", "Track durable background work"),
        ("/system-status/", "System Status"),
    ],
)
def test_phase_one_pages_render_honest_visible_states(loopback_client, path, expected_heading):
    response = loopback_client.get(path)

    assert response.status_code == 200
    assert expected_heading in response.content.decode()
    assert "owl-test-pat-never-valid" not in response.content.decode()


def test_shared_shell_has_navigation_and_accessible_status_region(loopback_client):
    response = loopback_client.get("/")
    html = response.content.decode()

    application_nav = re.search(
        r'<nav class="primary-nav" aria-label="Applications">(.*?)</nav>',
        html,
        re.DOTALL,
    )
    assert application_nav is not None
    application_links = re.findall(
        r'<a class="nav-link[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
        application_nav.group(1),
    )
    assert application_links == [
        ("/", "Home"),
        ("/bookmarks/", "Bookmark Manager"),
        ("/pdfs/", "Bitbucket Search"),
    ]
    assert "Global Search" not in application_nav.group(1)
    assert "Repositories" not in application_nav.group(1)
    assert "System Status" not in application_nav.group(1)
    assert 'href="/system-status/"' in html
    assert 'aria-live="polite"' in html
    assert 'href="#main-content"' in html
    assert 'id="main-content"' in html
    assert 'aria-label="OWL Home"' in html
    assert 'href="/" aria-current="true">Home</a>' in html
    assert 'href="/static/vendor/bootstrap/bootstrap.min.css"' in html


@pytest.mark.parametrize(
    ("path", "active_app", "sidebar_label", "current_function"),
    [
        ("/bookmarks/", "bookmarks", "Bookmark Manager functions", "All bookmarks"),
        (
            "/bookmarks/settings/",
            "bookmarks",
            "Bookmark Manager functions",
            "Confluence settings",
        ),
        ("/pdfs/", "bitbucket", "Bitbucket Search functions", "Search PDFs"),
        (
            "/pdfs/repositories/",
            "bitbucket",
            "Bitbucket Search functions",
            "Repositories",
        ),
        (
            "/pdfs/status/",
            "bitbucket",
            "Bitbucket Search functions",
            "Index & refresh status",
        ),
    ],
)
def test_each_app_route_uses_its_own_left_sidebar(
    loopback_client,
    path,
    active_app,
    sidebar_label,
    current_function,
):
    response = loopback_client.get(path)
    html = response.content.decode()

    assert response.context["active_app"] == active_app
    app_name = "Bookmark Manager" if active_app == "bookmarks" else "Bitbucket Search"
    sidebar_id = "bookmark-app-sidebar" if active_app == "bookmarks" else "bitbucket-app-sidebar"
    assert (
        f'<button class="app-sidebar-toggle" type="button" aria-expanded="false" '
        f'aria-controls="{sidebar_id}" data-app-sidebar-toggle>{app_name} menu</button>'
    ) in html
    assert f'id="{sidebar_id}"' in html
    sidebar = re.search(
        rf'<nav class="app-side-nav" aria-label="{re.escape(sidebar_label)}">(.*?)</nav>',
        html,
        re.DOTALL,
    )
    assert sidebar is not None
    current_links = re.findall(
        r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>',
        sidebar.group(1),
        re.DOTALL,
    )
    current_labels = [unescape(re.sub(r"<[^>]+>", "", value)).strip() for value in current_links]
    assert current_labels == [current_function]

    app_nav = re.search(
        r'<nav class="primary-nav" aria-label="Applications">(.*?)</nav>',
        html,
        re.DOTALL,
    )
    assert app_nav is not None
    active_apps = re.findall(r'<a[^>]*aria-current="true"[^>]*>([^<]+)</a>', app_nav.group(1))
    assert active_apps == [app_name]


def test_security_headers_are_present_on_visible_pages(loopback_client):
    response = loopback_client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "same-origin"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "object-src" in csp


def test_phase_two_settings_page_uses_a_blank_secure_pat_field(loopback_client):
    response = loopback_client.get("/bookmarks/settings/")
    html = response.content.decode()

    assert response.status_code == 200
    assert "Confluence Settings" in html
    assert 'type="password"' in html
    assert 'autocomplete="new-password"' in html
    assert "Stored securely" not in html
    assert "Save settings" in html


def test_bookmark_manager_settings_gear_has_the_required_accessible_name(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()

    assert 'aria-label="Confluence settings"' in html
    assert 'data-settings-fallback="/bookmarks/settings/"' in html


def test_bookmark_manager_uses_one_input_for_search_and_saving(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()
    controls = re.search(
        r'<section class="bookmark-controls"[^>]*>(.*?)</section>', html, re.DOTALL
    )

    assert controls is not None
    assert 'class="bookmark-unified-form"' in html
    assert 'placeholder="Search saved bookmarks, or paste a Confluence URL or Page ID"' in html
    assert "Search bookmarks" in html
    assert 'formaction="/bookmarks/save/"' in html
    assert "Save page" in html
    assert 'name="csrfmiddlewaretoken"' not in controls.group(1)
    assert 'data-csrf-token="' in html
    assert "bookmark-save-form" not in html
    assert "bookmark-search-form" not in html


def test_pdf_preview_foundation_uses_the_master_plan_phase(loopback_client):
    response = loopback_client.get("/pdfs/")

    assert "matched-page preview will appear here in Phase 7" in response.content.decode()


def test_state_changes_use_dedicated_post_only_routes(loopback_client):
    assert loopback_client.post("/bookmarks/settings/").status_code == 405
    assert loopback_client.post("/bookmarks/").status_code == 405
    assert loopback_client.get("/bookmarks/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/remove/").status_code == 405
