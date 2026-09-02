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
    assert "Repository logs" in html
    assert "Current activity and logs for all repositories" in html
    assert "Latest Git output and PDF indexing activity · updates live while workers run." in html
    assert "Loading repository logs…" in html
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


def test_repository_activity_icons_and_progress_hooks_are_distinct_and_accessible():
    html = _render("owl/_repository_status.html")
    elements = _Elements(html)

    assert len(elements.with_hook("data-repository-status-idle-icon")) == 1
    ((_, activities),) = elements.with_hook("data-repository-status-activities")
    assert "hidden" in activities
    operation_nodes = elements.with_hook("data-repository-status-activity")
    assert [attrs["data-repository-status-activity"] for _, attrs in operation_nodes] == [
        "clone",
        "pull",
        "indexing",
    ]
    assert all(attrs["aria-hidden"] == "true" and "hidden" in attrs for _, attrs in operation_nodes)
    assert len(elements.with_hook("data-repository-status-activity-progress")) == 3
    compact_timers = elements.with_hook("data-repository-worker-timer")
    assert len(compact_timers) == 3
    assert all(
        attrs["data-worker-compact"] == "true" and "hidden" in attrs for _, attrs in compact_timers
    )
    for operation in ("clone", "pull", "indexing"):
        assert f"repository-status__activity--{operation}" in html

    owl_css = (PROJECT_ROOT / "static/owl/owl.css").read_text()
    repository_template = (
        PROJECT_ROOT / "templates/bitbucket_search/_repository_list.html"
    ).read_text()
    repository_css = (PROJECT_ROOT / "static/bitbucket_search/bitbucket_search.css").read_text()
    for operation in ("clone", "pull", "indexing"):
        assert f".repository-status__activity--{operation}" in owl_css
        assert f"bb-state-icon--{operation}" in repository_template
        assert f'[data-repository-operation="{operation}"]' in repository_css
    for hook in (
        "data-repository-operation",
        "data-repository-progress",
        "data-repository-progress-bar",
        "data-repository-progress-label",
        "data-repository-run-timer",
    ):
        assert hook in repository_template
    assert ".bb-repository-progress progress:not([value])" in repository_css
    assert "bb-progress-indeterminate" in repository_css


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
    assert "Repository logs" not in html
    assert "Confluence schedule" not in html
    assert "Git log" not in html
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
def test_each_rendered_shell_has_notification_once_and_no_repository_logs_control(template):
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
    for suffix in ("center", "toggle", "panel", "live"):
        assert len(elements.with_hook(f"data-notification-{suffix}")) == 1
    assert not elements.with_hook("data-repository-status-center")
    assert not elements.with_hook("data-repository-status-toggle")
    assert not elements.with_hook("data-repository-status-panel")
    assert len(elements.with_hook("data-bitbucket-schedule-tick-form")) == 1
    panel_ids = [
        attrs["id"]
        for _tag, attrs in elements.elements
        if "data-notification-panel" in attrs
    ]
    assert len(set(panel_ids)) == 1
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


def test_git_log_console_is_bounded_keyboard_accessible_and_keeps_hidden_states():
    css = (PROJECT_ROOT / "static/owl/owl.css").read_text()
    console_css = css.split(".notification-repository__git-log-output {", 1)[1].split("}", 1)[0]
    assert "max-height: 160px" in console_css
    assert "overflow: auto" in console_css
    assert "white-space: pre-wrap" in console_css
    assert "overflow-wrap: anywhere" in console_css
    assert "overscroll-behavior: contain" in console_css
    assert ".notification-repository__git-log-output:focus-visible" in css
    assert ".notification-repository__git-log-toggle:focus-visible" in css
    hidden_css = css.split(".notification-repository__git-log-output[hidden],", 1)[1].split("}", 1)[
        0
    ]
    assert ".notification-repository__git-log-message[hidden]" in hidden_css
    assert ".notification-repository__git-log-note[hidden]" in hidden_css
    assert "display: none" in hidden_css


def test_git_log_preview_is_compact_and_never_wraps_into_more_than_two_rows():
    css = (PROJECT_ROOT / "static/owl/owl.css").read_text()
    preview_css = css.split(".notification-repository__log-preview {", 1)[1].split("}", 1)[0]
    line_css = css.split(".notification-repository__log-line {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in preview_css
    assert "grid-column: 2 / -1" in preview_css
    assert "white-space: nowrap" in line_css
    assert "text-overflow: ellipsis" in line_css
    assert "overflow: hidden" in line_css
    assert ".notification-repository__log-preview[hidden]" in css
    assert ".notification-repository__log-line[hidden]" in css
