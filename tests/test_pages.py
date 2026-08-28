import re
from html import unescape

import pytest
from django.contrib.staticfiles import finders

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    return client


@pytest.mark.parametrize(
    ("path", "expected_heading"),
    [
        ("/", "Your apps"),
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
    response = loopback_client.get("/bookmarks/")
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
    assert 'href="/bookmarks/" aria-current="true">Bookmark Manager</a>' in html
    assert 'href="/static/vendor/bootstrap/bootstrap.min.css"' in html
    assert "data-theme-toggle" in html


def test_home_lists_every_available_app(loopback_client):
    response = loopback_client.get("/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="all-apps-heading">Your apps</h1>' in html
    assert "See the whole picture." not in html
    assert (
        'class="knowledge-app-card knowledge-app-card--bitbucket" href="/pdfs/" '
        'aria-describedby="bitbucket-card-summary"' in html
    )
    assert (
        'class="knowledge-app-card knowledge-app-card--bookmarks" href="/bookmarks/" '
        'aria-describedby="bookmark-card-summary"' in html
    )
    assert html.count('class="knowledge-app-card ') == 2
    assert "data-theme-toggle" in html


@pytest.mark.parametrize(
    ("path", "selector_class"),
    [
        ("/", "knowledge-brand__mark"),
        ("/bookmarks/", "bookmark-brand__mark"),
        ("/pdfs/", "bb-brand__mark"),
    ],
)
def test_primary_app_surfaces_use_the_supplied_owl_artwork(
    loopback_client,
    path,
    selector_class,
):
    response = loopback_client.get(path)
    html = response.content.decode()

    assert response.status_code == 200
    assert re.search(
        rf'class="{selector_class}"[^>]*>\s*<img '
        r'src="/static/owl/owl\.png" alt="" width="512" height="512" decoding="async">',
        html,
    )


def test_supplied_owl_artwork_is_available_through_staticfiles():
    asset_path = finders.find("owl/owl.png")

    assert asset_path is not None
    with open(asset_path, "rb") as asset:
        assert asset.read(8) == b"\x89PNG\r\n\x1a\n"


def test_bookmark_manager_topbar_matches_the_app_workspace_contract(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()

    assert response.status_code == 200
    assert html.count('class="bookmark-topbar"') == 1
    assert "<h1>Bookmark Manager</h1>" in html
    assert 'class="bookmark-app-title"' in html
    assert 'class="bookmark-topbar__actions"' in html
    assert "bookmark-connection-summary" not in html
    assert "bookmarks.css?v=url-search-v10" in html
    assert 'aria-label="Applications"' in html
    assert 'aria-label="Confluence settings"' in html
    assert "data-theme-toggle" in html
    assert html.count('id="main-content"') == 1
    assert '<header class="app-header">' not in html
    assert 'class="product-toolbar"' not in html


@pytest.mark.parametrize(
    ("path", "active_app", "sidebar_label", "current_function"),
    [
        ("/bookmarks/", "bookmarks", "Bookmark Manager functions", "All bookmarks"),
        (
            "/bookmarks/settings/",
            "bookmarks",
            "Bookmark Manager functions",
            None,
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
    if path == "/pdfs/":
        assert 'class="bitbucket-workspace"' in html
        sidebar = re.search(
            rf'<nav class="bb-rail-links" aria-label="{re.escape(sidebar_label)}">(.*?)</nav>',
            html,
            re.DOTALL,
        )
    else:
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
    assert current_labels == ([current_function] if current_function else [])

    app_nav = re.search(
        r'<nav class="primary-nav" aria-label="Applications">(.*?)</nav>',
        html,
        re.DOTALL,
    )
    assert app_nav is not None
    active_apps = re.findall(r'<a[^>]*aria-current="true"[^>]*>([^<]+)</a>', app_nav.group(1))
    assert active_apps == [app_name]


def test_bitbucket_search_uses_the_repository_workspace_shell(loopback_client):
    response = loopback_client.get("/pdfs/")
    html = response.content.decode()

    assert response.status_code == 200
    assert '<body class="bitbucket-shell" data-theme="dark">' in html
    assert 'class="bb-repository-rail" aria-labelledby="bb-repositories-heading"' in html
    assert 'class="bb-stage" aria-label="Bitbucket Search workspace"' in html
    assert 'class="bb-rail-links" aria-label="Bitbucket Search functions"' in html
    assert 'href="/pdfs/" aria-current="page"' in html
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert "Repository setup" in html
    assert 'aria-label="System status"' in html
    assert "System status and help" not in html
    assert 'placeholder="Search repositories…" disabled' in html
    assert 'placeholder="Search repositories, file names, content, paths…" disabled' in html
    assert "No repositories connected" in html
    assert "Showing <strong>0 PDFs</strong>" in html
    assert "Not configured" in html
    assert "<dt>Total repositories</dt><dd>0</dd>" in html
    assert "<dt>Total PDFs</dt><dd>0</dd>" in html
    assert "18,420 PDFs" not in html
    assert "Up to date" not in html
    assert "networking" not in html
    assert '<button class="bb-control-button" type="button" disabled>' in html
    assert re.search(r'<button type="button" disabled>.*?Copy all \(0\)', html, re.DOTALL)
    assert re.search(r'<button type="button" disabled>.*?Open all \(0\)', html, re.DOTALL)
    assert re.search(r'<button type="button" disabled>.*?Export list', html, re.DOTALL)
    assert '<button type="button" disabled aria-label="List view">' in html
    assert '<button type="button" disabled aria-label="Grid view">' in html
    assert "<select disabled><option>50</option></select>" in html
    assert '<input type="number" value="1" min="1" disabled>' in html


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
    assert 'type="hidden" name="auth_mode" value="bearer"' in html
    assert "Authentication mode" not in html
    assert ">Advanced<" not in html
    assert "Bookmark data" in html
    assert 'href="/bookmarks/export/">Export JSON</a>' in html
    assert 'action="/bookmarks/import/"' in html
    assert "Import bookmarks" in html
    assert 'accept=".json,.txt,application/json,text/plain"' in html
    assert "data-bookmark-import-form" in html
    assert "data-import-progress" in html


def test_bookmark_manager_settings_gear_has_the_required_accessible_name(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()

    assert 'aria-label="Confluence settings"' in html
    assert 'data-settings-fallback="/bookmarks/settings/"' in html
    sidebar = re.search(
        r'<nav class="app-side-nav" aria-label="Bookmark Manager functions">(.*?)</nav>',
        html,
        re.DOTALL,
    )
    assert sidebar is not None
    labels = [
        unescape(re.sub(r"<[^>]+>", "", value)).strip()
        for value in re.findall(r"<a[^>]*>(.*?)</a>", sidebar.group(1), re.DOTALL)
    ]
    assert labels == [
        "All bookmarks",
        "Favorites",
        "Pinned",
        "Recently viewed",
        "Frequently viewed",
        "Never viewed",
    ]
    for removed in (
        "Data",
        "Connection",
        "System status",
        "Saved timeline",
        "Filters",
        "Saved views",
        "Import JSON",
        "Export JSON",
    ):
        assert removed not in sidebar.group(1)


def test_bookmark_manager_uses_one_input_for_search_and_saving(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()
    controls = re.search(
        r'<section class="bookmark-controls"[^>]*>(.*?)</section>', html, re.DOTALL
    )

    assert controls is not None
    assert 'class="bookmark-unified-form"' in html
    assert 'placeholder="Search bookmarks, or paste any URL"' in html
    assert ">Search bookmarks</button>" not in html
    assert "Separate words with spaces to match each word independently" in html
    assert 'formaction="/bookmarks/save/"' in html
    assert "Add bookmark" in html
    assert 'name="csrfmiddlewaretoken"' not in controls.group(1)
    assert 'data-csrf-token="' not in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert "bookmark-save-form" not in html
    assert "bookmark-search-form" not in html


def test_pdf_preview_foundation_uses_the_master_plan_phase(loopback_client):
    response = loopback_client.get("/pdfs/")
    html = response.content.decode()

    assert "PDF indexing and search are not active yet" in html
    assert "No repository has been accessed" in html
    assert "matched-page preview will appear here in Phase 7" in html
    assert "People &amp; commits" in html
    assert "Missing push evidence will be labelled <strong>Unavailable</strong>" in html
    assert (
        "author, committer, pusher, PR creator, fulfilled-state merger, and non-merge closer roles separate"
        in html
    )


def test_state_changes_use_dedicated_post_only_routes(loopback_client):
    assert loopback_client.post("/pdfs/").status_code == 405
    assert loopback_client.post("/bookmarks/settings/").status_code == 405
    assert loopback_client.post("/bookmarks/").status_code == 405
    assert loopback_client.get("/bookmarks/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/remove/").status_code == 405
