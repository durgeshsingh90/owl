from __future__ import annotations

import re
from html import escape
from types import SimpleNamespace

import pytest
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string


def _repository(**overrides):
    values = {
        "pk": 7,
        "display_name": "Design notes",
        "canonical_remote_key": "bitbucket.org/workspace/design-notes",
        "default_branch": "main",
        "sync_state": "fetching",
        "get_sync_state_display": "Fetching",
        "pdf_count": 12,
        "vsdx_count": 3,
        "has_active_sync": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _render(repository):
    return render_to_string(
        "bitbucket_search/_repository_list.html", {"repositories": (repository,)}
    )


def _timer_markup(html):
    timer = re.search(r"<small\s+class=\"bb-repository-worker-timer\".*?</small>", html, re.DOTALL)
    assert timer is not None
    return timer.group()


def test_repository_timer_carries_run_clock_data_but_waits_for_javascript():
    timing = {
        "gitStartedAt": "2026-08-30T10:00:00+00:00",
        "gitEndedAt": "2026-08-30T10:01:00+00:00",
        "indexStartedAt": "2026-08-30T10:01:00+00:00",
        "indexEndedAt": None,
    }
    html = _render(_repository(activity={"runTiming": timing}))
    timer = _timer_markup(html)

    assert f'data-git-started-at="{timing["gitStartedAt"]}"' in timer
    assert f'data-git-ended-at="{timing["gitEndedAt"]}"' in timer
    assert f'data-index-started-at="{timing["indexStartedAt"]}"' in timer
    assert 'data-index-ended-at=""' in timer
    assert "data-repository-run-timer" in timer
    assert 'aria-live="off"' in timer
    assert re.search(r"\bhidden\s*></small>", timer)
    assert re.search(r">\s*</small>$", timer)
    assert "<svg" not in timer
    assert html.index("data-repository-documents") < html.index("data-repository-run-timer")
    assert html.index("data-repository-run-timer") < html.index("data-repository-select")
    assert "data-repository-refresh-form" not in html
    assert "12 PDF · 3 VSDX" in html


def test_repository_without_active_run_keeps_empty_hidden_timer():
    timer = _timer_markup(_render(_repository(activity={})))
    for attribute in ("git-started-at", "git-ended-at", "index-started-at", "index-ended-at"):
        assert f'data-{attribute}=""' in timer
    assert re.search(r"\bhidden\s*></small>", timer)
    assert ">00:00</small>" not in timer


def test_sidebar_and_mobile_include_can_each_have_an_independent_timer():
    repository = _repository(activity={})
    html = _render(repository) + _render(repository)
    assert html.count("data-repository-run-timer") == 2
    assert not re.search(r"<small[^>]+\bid=", html)


def test_repository_timer_css_preserves_hidden_state_and_second_row_layout():
    with open(finders.find("bitbucket_search/bitbucket_search.css"), encoding="utf-8") as source:
        sidebar_css = source.read()
    with open(finders.find("owl/owl.css"), encoding="utf-8") as source:
        shared_css = source.read()

    assert ".bb-repository-worker-timer[hidden] {\n    display: none;" in sidebar_css
    sidebar_timer = re.search(
        r"\.bb-repository-card__copy \.bb-repository-worker-timer \{([^}]+)\}", sidebar_css
    )
    assert sidebar_timer is not None
    assert "font-variant-numeric: tabular-nums;" in sidebar_timer.group(1)
    assert ".notification-repository__timer[hidden] {\n    display: none;" in shared_css
    status_timer = re.search(r"\.notification-repository__timer \{([^}]+)\}", shared_css)
    assert status_timer is not None
    for declaration in (
        "grid-column: 2 / 5;",
        "grid-row: 2;",
        "min-width: 0;",
        "font-variant-numeric: tabular-nums;",
        "text-overflow: ellipsis;",
    ):
        assert declaration in status_timer.group(1)
    assert (
        ".notification-repository__summary::after {\n    grid-column: 5;\n    grid-row: 1;"
        in shared_css
    )
