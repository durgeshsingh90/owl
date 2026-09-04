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
        ("/bookmarks/settings/", "Settings"),
        ("/pdfs/", "Bitbucket Search"),
        ("/pdfs/repositories/", "Bitbucket Search"),
        ("/pdfs/status/", "Repository logs"),
        ("/bitbucket/", "Your repositories"),
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
        ("/bitbucket/", "Bitbucket"),
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
    assert (
        'class="knowledge-app-card knowledge-app-card--bitbucket-browser" href="/bitbucket/" '
        'aria-describedby="bitbucket-browser-card-summary"' in html
    )
    assert html.count('class="knowledge-app-card ') == 3
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
    assert "bookmarks.css?v=workspace-ui-v24" in html
    assert 'aria-label="Applications"' in html
    assert 'aria-label="Integration settings"' in html
    assert 'aria-label="Import bookmarks"' in html
    assert "data-theme-toggle" in html
    assert html.count('id="main-content"') == 1
    assert '<header class="app-header">' not in html
    assert 'class="product-toolbar"' not in html


def test_bookmark_manager_workspace_uses_minimal_edges_and_fills_page_height():
    asset_path = finders.find("bookmark_manager/bookmarks.css")

    assert asset_path is not None
    with open(asset_path, encoding="utf-8") as stylesheet:
        css = stylesheet.read()

    assert "--bookmark-page-inline-edge: 0rem;" in css
    assert "max-width: none;" in css
    assert "margin-inline: 0;" in css
    assert "padding-bottom: 0;" in css
    assert "padding: 1.15rem var(--bookmark-page-inline-edge) 0;" in css
    assert "flex: 1 0 500px;" in css
    assert "padding: 0.4rem 0.25rem 0 !important;" in css
    assert "padding: 0.4rem 0.15rem 0 0.35rem;" in css


def test_bookmark_search_feedback_reserves_layout_space_on_laptops_and_mobile():
    asset_path = finders.find("bookmark_manager/bookmarks.css")

    assert asset_path is not None
    with open(asset_path, encoding="utf-8") as stylesheet:
        css = stylesheet.read()

    form_rules = re.findall(r"\.bookmark-manager-shell \.bookmark-unified-form\s*\{([^}]*)\}", css)
    assert "display: grid;" in form_rules[0]
    assert "grid-template-columns: minmax(0, 1fr) auto;" in form_rules[0]
    mobile_css = css.rsplit("@media (max-width: 620px)", 1)[1]
    mobile_form = re.search(
        r"\.bookmark-manager-shell \.bookmark-unified-form\s*\{([^}]*)\}",
        mobile_css,
    )
    assert mobile_form is not None
    assert "grid-template-columns: minmax(0, 1fr);" in mobile_form.group(1)

    css_without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css_rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css_without_comments)
    for suffix in (".field-help", ".field-error", ".inline-message", "> .text-link"):
        selector = f".bookmark-manager-shell .bookmark-unified-form {suffix}"
        feedback_rules = [
            declarations
            for selectors, declarations in css_rules
            if selector in [item.strip() for item in selectors.split(",")]
        ]
        assert feedback_rules, selector
        assert not any("position: absolute;" in rule for rule in feedback_rules), selector
        assert any(
            all(
                declaration in rule
                for declaration in (
                    "position: static;",
                    "grid-column: 1 / -1;",
                    "min-width: 0;",
                    "max-width: none;",
                    "overflow-wrap: anywhere;",
                )
            )
            for rule in feedback_rules
        ), selector

    workspace_rule = re.search(r"\.bookmark-manager-shell \.bookmark-workspace\s*\{([^}]*)\}", css)
    assert workspace_rule is not None
    assert "margin-top: 0;" in workspace_rule.group(1)


@pytest.mark.parametrize(
    ("query", "has_url_feedback"),
    [
        ({"pinned": "on", "sort": "added_newest"}, False),
        ({"q": "https://docs.example.com/runbooks/new-private-dns"}, True),
    ],
)
def test_bookmark_search_feedback_precedes_active_filters_without_revealing_hidden_status(
    loopback_client, query, has_url_feedback
):
    response = loopback_client.get("/bookmarks/", query)
    html = response.content.decode()
    controls = re.search(
        r'<section class="bookmark-controls"[^>]*>(.*?)</section>', html, re.DOTALL
    )
    filters = re.search(r'<div class="active-filter-row"[^>]*>(.*?)</div>', html, re.DOTALL)

    assert response.status_code == 200
    assert controls is not None
    assert filters is not None
    assert controls.end() < filters.start()
    assert 'class="field-help"' in controls.group(1)
    assert ">Clear all</a>" in controls.group(1)
    save_status = re.search(r"<p\b(?=[^>]*\bdata-bookmark-save-result)[^>]*>", controls.group(1))
    assert save_status is not None
    assert " hidden" in save_status.group(0)
    for field in ("category", "sort"):
        assert re.search(
            rf'<input\b(?=[^>]*\bname="{field}")(?=[^>]*\btype="hidden")[^>]*>',
            controls.group(1),
        )
    if has_url_feedback:
        assert "data-url-search-result" in controls.group(1)
        assert (
            "No saved bookmark matches this Page ID or URL. Press Enter to add it."
            in controls.group(1)
        )
        assert controls.group(1).index('class="field-help"') < controls.group(1).index(
            "data-url-search-result"
        )
    else:
        assert "data-url-search-result" not in controls.group(1)
        assert "Pinned" in filters.group(1)


@pytest.mark.parametrize(
    ("path", "active_app", "sidebar_label", "current_function"),
    [
        ("/bookmarks/", "bookmarks", "Bookmark Manager functions", "All bookmarks"),
        ("/pdfs/", "bitbucket", "Bitbucket Search functions", None),
        (
            "/pdfs/repositories/",
            "bitbucket",
            "Bitbucket Search functions",
            None,
        ),
        (
            "/pdfs/status/",
            "bitbucket",
            "Bitbucket Search functions",
            "Repository logs",
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
    if path in {"/pdfs/", "/pdfs/repositories/"}:
        assert 'class="bitbucket-workspace"' in html
        assert "bb-rail-links" not in html
        sidebar = re.search(
            r'<aside class="bb-repository-rail"[^>]*>(.*?)</aside>',
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
        if active_app == "bitbucket":
            assert sidebar is not None
            assert 'href="/pdfs/">Search PDFs</a>' in sidebar.group(1)
            assert 'href="/pdfs/repositories/">Repositories</a>' in sidebar.group(1)
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


def test_settings_uses_dedicated_navigation_without_the_bookmark_sidebar(loopback_client):
    response = loopback_client.get("/bookmarks/settings/")
    html = response.content.decode()

    assert response.status_code == 200
    assert response.context["active_app"] == ""
    assert 'aria-label="Settings sections"' in html
    assert 'aria-current="page">Overview</a>' in html
    assert "bookmark-app-sidebar" not in html
    assert "Bookmark Manager functions" not in html


def test_bitbucket_search_uses_the_repository_workspace_shell(loopback_client):
    response = loopback_client.get("/pdfs/")
    html = response.content.decode()
    repository_rail = re.search(
        r'<aside class="bb-repository-rail"[^>]*>(.*?)</aside>',
        html,
        re.DOTALL,
    )
    mobile_functions = re.search(
        r'<nav aria-label="Bitbucket Search mobile functions">(.*?)</nav>', html, re.DOTALL
    )

    assert response.status_code == 200
    assert '<body class="bitbucket-shell" data-theme="dark">' in html
    assert 'class="bb-repository-rail" aria-labelledby="bb-repositories-heading"' in html
    assert 'class="bb-stage" aria-label="Bitbucket Search workspace"' in html
    assert "bb-rail-links" not in html
    assert repository_rail is not None
    assert mobile_functions is not None
    assert "Search PDFs" not in repository_rail.group(1)
    assert "Index &amp; refresh status" not in repository_rail.group(1)
    assert "Search PDFs" not in mobile_functions.group(1)
    assert 'href="/pdfs/repositories/">Repositories</a>' in mobile_functions.group(1)
    assert 'href="/pdfs/status/">Repository logs</a>' in mobile_functions.group(1)
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert re.search(
        r'<summary class="bb-add-repository" aria-label="Add a new repository">.*?New.*?</summary>',
        html,
        re.DOTALL,
    )
    assert "Filter the repositories managed by this OWL workspace." not in html
    assert 'aria-label="System status"' in html
    assert "System status and help" not in html
    assert 'placeholder="Search repositories…" disabled' in html
    assert 'placeholder="Search extracted text, filenames or repo paths…"' in html
    assert "data-pdf-search-input disabled" not in html
    assert "No repositories connected" in html
    assert "Showing <strong>0 PDFs</strong>" in html
    assert "Not configured" in html
    assert "<dt>Total repositories</dt><dd data-total-repositories>0</dd>" in html
    assert "<dt>PDF files</dt><dd data-total-pdfs>0</dd>" in html
    assert "<dt>VSDX files</dt><dd data-total-vsdx>0</dd>" in html
    assert "18,420 PDFs" not in html
    assert "Up to date" not in html
    assert "networking" not in html
    assert "bb-search-submit" not in html
    assert 'name="page_size" value="100"' in html
    assert '<details class="bb-search-filter-menu">' in html
    assert re.search(
        r'<button type="button" data-tooltip="Copy selected paths".*?Copy selected paths \(0\)',
        html,
        re.DOTALL,
    )
    assert re.search(
        r'<button type="submit" data-tooltip="Open selected PDFs".*?Open selected \(0\)',
        html,
        re.DOTALL,
    )
    assert re.search(
        r'<button type="button" data-tooltip="Export list".*?Export list', html, re.DOTALL
    )
    assert 'data-tooltip="List view" disabled aria-label="List view"' in html
    assert 'data-tooltip="Grid view" disabled aria-label="Grid view"' in html
    assert "Rows per page" not in html
    assert 'aria-label="PDF result pages"' not in html


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
    response = loopback_client.get(
        "/bookmarks/settings/", {"section": "confluence", "task": "confluence"}
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "<h1>Settings</h1>" in html
    assert 'type="password"' in html
    assert 'autocomplete="new-password"' in html
    assert "Stored securely" not in html
    assert "Save settings" in html
    assert 'type="hidden" name="auth_mode" value="bearer"' in html
    assert "Authentication mode" not in html
    assert ">Advanced<" not in html
    assert "data-bookmark-import-form" not in html

    data_response = loopback_client.get("/bookmarks/settings/", {"section": "bookmark-data"})
    data_html = data_response.content.decode()
    assert 'action="/bookmarks/export/"' in data_html
    assert ">Export JSON</button>" in data_html
    assert 'action="/bookmarks/import/"' in data_html
    assert "Import bookmarks" in data_html
    assert 'accept=".json,.txt,application/json,text/plain"' in data_html
    assert "data-bookmark-import-form" in data_html
    assert "data-import-progress" in data_html


def test_bookmark_manager_settings_gear_has_the_required_accessible_name(loopback_client):
    response = loopback_client.get("/bookmarks/")
    html = response.content.decode()

    assert 'aria-label="Integration settings"' in html
    assert 'data-settings-fallback="/bookmarks/settings/"' in html
    assert 'aria-label="Import bookmarks"' in html
    dialog = html.split('<dialog class="settings-dialog"', 1)[1]
    assert "Confluence" in dialog
    assert "Repository access" in dialog
    assert "Open full Settings" in dialog
    assert "<form" not in dialog
    assert 'type="password"' not in dialog
    assert "Bookmark data" not in dialog
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
        "Deleted pages",
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


def test_pdf_search_page_exposes_active_indexing_and_phrase_controls(loopback_client):
    response = loopback_client.get("/pdfs/")
    html = response.content.decode()

    assert response.status_code == 200
    assert "data-pdf-search-form" in html
    assert 'placeholder="Search extracted text, filenames or repo paths…"' in html
    assert "<legend>Phrase matching</legend>" in html
    assert "<legend>Search in</legend>" in html
    assert 'value="content" checked' in html
    assert "Exact phrase search" in html
    assert (
        "Clone, refresh, PDF extraction, and indexing all happen outside this web request" in html
    )
    assert "PDF indexing and search are not active yet" not in html


def test_bitbucket_logs_page_reports_workers_and_daily_refresh_instead_of_a_placeholder(
    loopback_client,
):
    response = loopback_client.get("/pdfs/status/")
    html = response.content.decode()

    assert response.status_code == 200
    assert "Repository logs" in html
    assert "Daily automation" in html
    assert "Git workers" in html
    assert "PDF index workers" in html
    assert "This feature is not active yet" not in html


def test_state_changes_use_dedicated_post_only_routes(loopback_client):
    assert loopback_client.post("/pdfs/").status_code == 405
    assert loopback_client.post("/bookmarks/settings/").status_code == 405
    assert loopback_client.post("/bookmarks/").status_code == 405
    assert loopback_client.get("/bookmarks/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/save/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/remove/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/repository-hosts/add/").status_code == 405
    assert loopback_client.get("/bookmarks/settings/repository-hosts/remove/").status_code == 405
    assert loopback_client.get("/pdfs/repositories/add/").status_code == 405
