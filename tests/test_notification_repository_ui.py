from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory

PROJECT_ROOT = Path(__file__).parents[1]


def test_notification_shell_separates_current_repositories_and_history():
    html = render_to_string(
        "owl/_notification_center.html", request=RequestFactory().get("/bookmarks/")
    )
    assert "Repository status" in html
    assert "Current status of all repositories" in html
    assert "Loading repository status…" in html
    assert "data-notification-repository-count" in html
    assert "data-notification-repository-message" in html
    assert "Notification history" in html
    assert html.index("Repository status") < html.index("Notification history")
    assert '<details class="notification-center__background" data-notification-background>' in html
    for hook in ("next-run", "last-attempt", "last-success", "retry"):
        assert f"data-notification-{hook}" in html


def test_notification_repository_assets_include_compact_accessible_layout():
    css = (PROJECT_ROOT / "static/owl/owl.css").read_text()
    base = (PROJECT_ROOT / "templates/owl/base.html").read_text()
    assert "max-height: min(240px, 35vh)" in css
    assert ".notification-repository__summary:focus-visible" in css
    assert "text-overflow: ellipsis" in css
    assert '.notification-center__repository-list[data-stale="true"]' in css
    assert "prefers-reduced-motion: reduce" in css
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
