from __future__ import annotations

from types import SimpleNamespace

from django.template.loader import render_to_string


def _repository(**overrides):
    values = {
        "pk": 7,
        "display_name": "Design notes",
        "canonical_remote_key": "bitbucket.org/workspace/design-notes",
        "default_branch": "main",
        "sync_state": "ready",
        "get_sync_state_display": "Ready",
        "pdf_count": 12,
        "vsdx_count": 3,
        "has_active_sync": False,
        "git_succeeded": True,
        "index_succeeded": True,
        "activity": {"active": False},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _render(repository):
    return render_to_string(
        "bitbucket_search/_repository_list.html", {"repositories": (repository,)}
    )


def test_repository_card_uses_two_success_ticks_without_timestamps_or_totals():
    html = _render(_repository())

    assert "data-repository-success-ticks" in html
    assert 'data-git-succeeded="true"' in html
    assert 'data-index-succeeded="true"' in html
    assert "data-repository-run-timer" not in html
    assert "data-repository-index-counts" not in html
    assert "git-started-at" not in html
    assert "index-started-at" not in html


def test_each_repository_render_gets_its_own_double_tick_state():
    html = _render(_repository(index_succeeded=False)) + _render(_repository())

    assert html.count("data-repository-success-ticks") == 2
    assert html.count('data-index-succeeded="false"') == 1
    assert html.count('data-index-succeeded="true"') == 1
