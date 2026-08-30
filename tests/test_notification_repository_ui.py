from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory

PROJECT_ROOT = Path(__file__).parents[1]


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def with_hook(self, hook):
        return [(tag, attrs) for tag, attrs in self.elements if hook in attrs]


def _render(template):
    return render_to_string(template, request=RequestFactory().get("/bookmarks/"))


def test_repository_status_panel_owns_current_status_and_background_schedule():
    html = _render("owl/_repository_status.html")
    elements = _Elements(html)

    ((_, status_root),) = elements.with_hook("data-repository-status-center")
    ((_, indicator),) = elements.with_hook("data-repository-status-indicator")
    assert status_root["data-state"] == "unknown"
    assert indicator["aria-hidden"] == "true"
    assert "Repository status" in html
    assert "Current status of all repositories" in html
    assert "Loading repository status…" in html
    for hook in ("repositories", "repository-count", "repository-message", "repository-list"):
        assert len(elements.with_hook(f"data-notification-{hook}")) == 1
    ((list_tag, list_attrs),) = elements.with_hook("data-notification-repository-list")
    assert list_tag == "ul"
    assert list_attrs["aria-busy"] == "true"
    assert "hidden" in list_attrs
    ((schedule_tag, schedule_attrs),) = elements.with_hook("data-notification-background")
    assert schedule_tag == "details"
    assert "open" not in schedule_attrs
    for hook in ("next-run", "last-attempt", "last-success", "retry", "progress-card"):
        assert len(elements.with_hook(f"data-notification-{hook}")) == 1

    assert "Notification history" not in html
    for hook in ("center", "toggle", "panel", "badge", "list", "read-all", "live"):
        assert not elements.with_hook(f"data-notification-{hook}")
    assert not any(tag in {"form", "iframe"} for tag, _attrs in elements.elements)
    assert "csrfmiddlewaretoken" not in html
    assert "data-bitbucket-schedule-tick-form" not in html


def test_notification_bell_contains_history_and_keeps_its_existing_hidden_forms_once():
    html = _render("owl/_notification_center.html")
    elements = _Elements(html)

    assert "Notification history" in html
    for hook in ("center", "toggle", "panel", "badge", "read-all", "list", "empty", "live"):
        assert len(elements.with_hook(f"data-notification-{hook}")) == 1
    for hook in (
        "repositories",
        "repository-count",
        "repository-message",
        "repository-list",
        "background",
        "background-state",
        "progress-card",
        "schedule",
        "activity",
    ):
        assert not elements.with_hook(f"data-notification-{hook}")
    assert "Repository status" not in html
    assert "Confluence schedule" not in html
    assert "data-repository-status-center" not in html
    forms = [(tag, attrs) for tag, attrs in elements.elements if tag == "form"]
    assert len(forms) == 2
    assert (
        sum("notification-center__csrf" in attrs.get("class", "").split() for _, attrs in forms)
        == 1
    )
    ((schedule_tag, schedule_attrs),) = elements.with_hook("data-bitbucket-schedule-tick-form")
    assert schedule_tag == "form"
    assert schedule_attrs["method"] == "post"
    assert schedule_attrs["action"] == "/pdfs/repositories/schedule/tick/"
    assert "hidden" in schedule_attrs
    frames = [attrs for tag, attrs in elements.elements if tag == "iframe"]
    assert len(frames) == 1
    assert frames[0]["name"] == schedule_attrs["target"]
    assert frames[0]["tabindex"] == "-1"
    assert "hidden" in frames[0]
    assert sum(attrs.get("name") == "csrfmiddlewaretoken" for _, attrs in elements.elements) == 2


@pytest.mark.parametrize(
    ("template", "prefix"),
    (
        ("owl/_repository_status.html", "repository-status"),
        ("owl/_notification_center.html", "notification"),
    ),
)
def test_both_popovers_have_distinct_accessible_toggle_dialog_and_live_region(template, prefix):
    elements = _Elements(_render(template))
    ((toggle_tag, toggle),) = elements.with_hook(f"data-{prefix}-toggle")
    ((panel_tag, panel),) = elements.with_hook(f"data-{prefix}-panel")
    ((_, live),) = elements.with_hook(f"data-{prefix}-live")

    assert toggle_tag == "button"
    assert toggle["type"] == "button"
    assert toggle["aria-label"]
    assert toggle["title"]
    assert toggle["aria-haspopup"] == "dialog"
    assert toggle["aria-expanded"] == "false"
    assert toggle["aria-controls"] == panel["id"]
    assert panel_tag == "section"
    assert panel["role"] == "dialog"
    assert panel["aria-modal"] == "false"
    assert panel["tabindex"] == "-1"
    assert "hidden" in panel
    headings = [
        (tag, attrs)
        for tag, attrs in elements.elements
        if attrs.get("id") == panel["aria-labelledby"]
    ]
    assert len(headings) == 1 and headings[0][0] in {"h2", "h3"}
    assert live["role"] == "status"
    assert live["aria-live"] == "polite"
    assert live["aria-atomic"] == "true"


@pytest.mark.parametrize(
    "template",
    (
        "owl/base.html",
        "core/dashboard.html",
        "bookmark_manager/index.html",
        "bitbucket_search/index.html",
    ),
)
def test_each_rendered_shell_has_status_before_bell_once_without_duplicate_dialog_ids(template):
    html = render_to_string(
        template,
        {
            "query_string": "",
            "page_title": "Synthetic workspace",
            "selected_person_count": 0,
            "selected_group_count": 0,
        },
        request=RequestFactory().get("/"),
    )
    elements = _Elements(html)
    for prefix in ("repository-status", "notification"):
        for suffix in ("center", "toggle", "panel", "live"):
            assert len(elements.with_hook(f"data-{prefix}-{suffix}")) == 1
    assert html.index("data-repository-status-center") < html.index("data-notification-center")
    assert len(elements.with_hook("data-bitbucket-schedule-tick-form")) == 1
    panel_ids = [
        attrs["id"]
        for _tag, attrs in elements.elements
        if "data-repository-status-panel" in attrs or "data-notification-panel" in attrs
    ]
    assert len(set(panel_ids)) == 2
    id_counts = Counter(attrs["id"] for _tag, attrs in elements.elements if "id" in attrs)
    assert all(id_counts[panel_id] == 1 for panel_id in panel_ids)
    assert html.count('class="notification-center__csrf"') == 1


def test_notification_repository_assets_include_compact_accessible_layout():
    css = (PROJECT_ROOT / "static/owl/owl.css").read_text()
    base = (PROJECT_ROOT / "templates/owl/base.html").read_text()
    assert "max-height: min(240px, 35vh)" in css
    assert ".notification-repository__summary:focus-visible" in css
    assert "text-overflow: ellipsis" in css
    assert '.notification-center__repository-list[data-stale="true"]' in css
    assert "prefers-reduced-motion: reduce" in css
    assert ".repository-status-center" in css
    assert ".repository-status__indicator" in css
    assert ".notification-center__toggle:focus-visible" in css
    for state in ("unknown", "neutral", "active", "error", "ready"):
        assert (
            f'.repository-status-center[data-state="{state}"] .repository-status__indicator' in css
        )
    assert "@media (max-width: 620px)" in css
    assert "max-height: calc(100dvh - var(--notification-mobile-top, 62px) - 8px)" in css
    assert "left: 8px" in css and "right: 8px" in css
    assert "{% static 'owl/owl.css' %}?v=" in base
    assert "{% static 'owl/owl.js' %}?v=" in base


def test_notification_repository_javascript_behaviors():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated notification JavaScript tests.")
    result = subprocess.run(
        [node, "--test", str(PROJECT_ROOT / "tests/js/notification_repository_status.test.js")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
