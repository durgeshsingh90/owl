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


@pytest.mark.parametrize(("kind", "label"), [("sync", "Refreshing"), ("indexing", "Indexing PDFs")])
def test_repository_timer_carries_worker_clock_data_but_waits_for_javascript(kind, label):
    timing = {
        "startedAt": "2026-08-30T10:00:00+00:00",
        "observedAt": "2026-08-30T10:01:23+00:00",
        "label": label,
        "kind": kind,
    }
    html = _render(_repository(worker_timing=timing))
    timer = _timer_markup(html)

    assert f'data-worker-started-at="{timing["startedAt"]}"' in timer
    assert f'data-worker-observed-at="{timing["observedAt"]}"' in timer
    assert f'data-worker-label="{label}"' in timer
    assert f'data-worker-kind="{kind}"' in timer
    assert 'aria-live="off"' in timer
    assert 'title="Elapsed time for the current worker"' in timer
    assert re.search(r"\bhidden\s*></small>", timer)
    assert re.search(r">\s*</small>$", timer)
    assert "<svg" not in timer
    assert html.index("data-repository-documents") < html.index("data-repository-worker-timer")
    assert html.index("data-repository-worker-timer") < html.index("data-repository-refresh-form")
    assert "12 PDF · 3 VSDX" in html


@pytest.mark.parametrize("timing", [None, {}])
def test_repository_without_active_worker_keeps_empty_hidden_timer(timing):
    timer = _timer_markup(_render(_repository(worker_timing=timing)))
    for attribute in ("started-at", "observed-at", "label", "kind"):
        assert f'data-worker-{attribute}=""' in timer
    assert re.search(r"\bhidden\s*></small>", timer)
    assert ">00:00</small>" not in timer


def test_repository_timer_handles_missing_timing_and_escapes_attribute_values():
    assert 'data-worker-started-at=""' in _timer_markup(_render(_repository()))
    label = 'Indexing "notes" <script>synthetic</script>'
    timer = _timer_markup(
        _render(
            _repository(
                worker_timing={
                    "startedAt": "2026-08-30T10:00:00+00:00",
                    "observedAt": "2026-08-30T10:01:23+00:00",
                    "label": label,
                    "kind": "indexing",
                }
            )
        )
    )
    assert f'data-worker-label="{escape(label)}"' in timer
    assert "<script>" not in timer


def test_sidebar_and_mobile_include_can_each_have_an_independent_timer():
    repository = _repository(worker_timing=None)
    html = _render(repository) + _render(repository)
    assert html.count("data-repository-worker-timer") == 2
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
