from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from types import SimpleNamespace

import pytest
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.utils import translation

PERIOD_LABELS = (
    ("today", "Today"),
    ("week", "This week"),
    ("last_week", "Last week"),
    ("month", "This month"),
    ("six_months", "Last 6 months"),
    ("year", "This year"),
)


def _dashboard(**overrides):
    values = {
        "period": "week",
        "label": "This week",
        "has_data": True,
        "has_repositories": True,
        "total_commits": 12,
        "active_people": 2,
        "active_repositories": 1,
        "active_folders": 2,
        "people": (
            SimpleNamespace(
                name="Alex Developer", aliases=("Alex D",), commit_count=8, repository_count=1
            ),
            SimpleNamespace(name="Sam Developer", aliases=(), commit_count=4, repository_count=1),
        ),
        "repositories": (SimpleNamespace(repository_id=7, name="Source notes", commit_count=12),),
        "folders": (
            SimpleNamespace(
                repository_id=7, repository_name="Source notes", path="", commit_count=10
            ),
            SimpleNamespace(
                repository_id=7,
                repository_name="Source notes",
                path="src/components",
                commit_count=3,
            ),
        ),
        "coverage": SimpleNamespace(
            total_repositories=1,
            indexed_repositories=1,
            pending_repositories=0,
            stale_repositories=0,
            shallow_repositories=0,
            last_indexed_at=datetime(2026, 8, 30, 9, 30, tzinfo=UTC),
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _render(dashboard=None, filters=()):
    return render_to_string(
        "core/_bitbucket_activity.html",
        {"bitbucket_dashboard": dashboard or _dashboard(), "bitbucket_activity_filters": filters},
    )


def test_activity_ui_shows_metrics_three_rankings_and_truthful_git_semantics():
    html = _render()
    assert 'id="bitbucket-activity-heading">Bitbucket activity</h2>' in html
    assert "<dt>Commits</dt><dd>12</dd>" in html
    assert "<dt>People</dt><dd>2</dd>" in html
    assert "<dt>Repositories</dt><dd>1</dd>" in html
    assert "<dt>Folders</dt><dd>2</dd>" in html
    assert html.count('role="list"') == 3
    assert html.count("Top 10") == 3
    assert "Commits by person" in html
    assert "Busiest repositories" in html
    assert "Busiest folders" in html
    assert "Git committers, not push actors" in html
    assert "all file types, not only PDFs" in html
    assert "direct parent" in html
    assert "one commit can count in several folders" in html
    assert 'href="/pdfs/?repository=7"' in html
    assert "(root)" in html
    assert re.findall(
        r'<progress class="knowledge-git-activity__bar" value="(\d+)" max="(\d+)" aria-hidden="true"></progress>',
        html,
    ) == [("8", "8"), ("4", "8"), ("12", "12"), ("10", "10"), ("3", "10")]
    assert "style=" not in html
    assert "--git-activity-fill" not in html


def test_git_progress_values_stay_numeric_with_localized_commit_counts(settings):
    settings.USE_THOUSAND_SEPARATOR = True
    dashboard = _dashboard(
        people=(
            SimpleNamespace(
                name="Alex Developer", aliases=(), commit_count=2000, repository_count=1
            ),
        ),
        repositories=(SimpleNamespace(repository_id=7, name="Source notes", commit_count=2000),),
        folders=(
            SimpleNamespace(
                repository_id=7, repository_name="Source notes", path="", commit_count=2000
            ),
        ),
    )
    with translation.override("de"):
        html = _render(dashboard)
    assert html.count('value="2000" max="2000"') == 3
    assert 'value="2.000"' not in html


@pytest.mark.parametrize(("selected_period", "selected_label"), PERIOD_LABELS)
def test_period_filters_preserve_supplied_queries_and_have_one_anchor(
    selected_period, selected_label
):
    filters = tuple(
        {
            "label": label,
            "href": f"?year=2025&activity=opened&people_period=today&git_period={period}#bitbucket-activity",
            "selected": period == selected_period,
        }
        for period, label in PERIOD_LABELS
    )
    html = _render(_dashboard(period=selected_period, label=selected_label), filters=filters)
    nav = re.search(r'<nav[^>]+aria-label="Bitbucket activity period">(.*?)</nav>', html, re.DOTALL)
    assert nav is not None
    links = re.findall(r'href="([^"]+)"', nav.group(1))
    assert [unescape(link) for link in links] == [item["href"] for item in filters]
    assert nav.group(1).count('aria-current="page"') == 1
    assert f'class="is-active" aria-current="page">{selected_label}</a>' in nav.group(1)
    assert html.index(f">{selected_label}</a>") < html.index(
        'class="knowledge-git-activity__metrics"'
    )
    summary = re.search(
        r'<div class="knowledge-git-activity__summary">(.*?)</div>', html, re.DOTALL
    )
    assert summary and selected_label in summary.group(1)


@pytest.mark.parametrize(
    ("has_repositories", "indexed", "message"),
    [
        (False, 0, "Add repositories in Bitbucket Search"),
        (True, 0, "Refresh your repositories in Bitbucket Search"),
        (True, 2, "No commits in this period"),
    ],
)
@pytest.mark.parametrize(("period", "label"), PERIOD_LABELS)
def test_activity_empty_states_do_not_claim_missing_data_is_no_activity(
    has_repositories, indexed, message, period, label
):
    dashboard = _dashboard(
        period=period,
        label=label,
        has_data=False,
        has_repositories=has_repositories,
        total_commits=0,
        active_people=0,
        active_repositories=0,
        active_folders=0,
    )
    dashboard.coverage.indexed_repositories = indexed
    html = _render(dashboard)
    assert message in html
    assert 'class="knowledge-git-activity__rankings"' not in html
    assert "<dt>Commits</dt><dd>0</dd>" in html
    assert "<dt>People</dt><dd>0</dd>" in html
    assert label in html
    if indexed == 0:
        assert "No commits in this period" not in html


def test_partial_coverage_identifies_pending_stale_and_shallow_history():
    dashboard = _dashboard()
    dashboard.coverage = SimpleNamespace(
        total_repositories=5,
        indexed_repositories=3,
        pending_repositories=2,
        stale_repositories=1,
        shallow_repositories=1,
        last_indexed_at=datetime(2026, 8, 30, 9, 30, tzinfo=UTC),
    )
    html = _render(dashboard)
    assert "3 of 5 repositories indexed" in html
    assert "2 awaiting first history refresh" in html
    assert "1 awaiting latest history; showing the previous snapshot" in html
    assert "1 repository has limited (shallow) history" in html
    rendered_at = re.search(r'<time datetime="([^"]+)">', html)
    assert rendered_at is not None
    assert datetime.fromisoformat(rendered_at.group(1)) == dashboard.coverage.last_indexed_at
    assert "Available Git history" in html


def test_ranking_names_are_escaped_and_full_values_are_available_in_titles():
    name = 'A very long <script>alert("synthetic")</script> name ' + "x" * 100
    dashboard = _dashboard(
        people=(
            SimpleNamespace(
                name=name, aliases=("Alternate <name>",), commit_count=12, repository_count=1
            ),
        ),
        repositories=(SimpleNamespace(repository_id=7, name=name, commit_count=12),),
        folders=(
            SimpleNamespace(
                repository_id=7, repository_name=name, path="src/" + "long/" * 70, commit_count=12
            ),
        ),
    )
    html = _render(dashboard)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Names in history: Alternate &lt;name&gt;" in html
    assert (
        f'title="{name.replace("<", "&lt;").replace(">", "&gt;").replace(chr(34), "&quot;")}"'
        in html
    )
    assert "src/" + "long/" * 70 in html


def test_rankings_are_visually_bounded_to_ten_rows():
    dashboard = _dashboard(
        people=tuple(
            SimpleNamespace(
                name=f"Person {index}", aliases=(), commit_count=20 - index, repository_count=1
            )
            for index in range(12)
        ),
        repositories=(),
        folders=(),
    )
    html = _render(dashboard)
    assert "Person 9" in html
    assert "Person 10" not in html
    assert "Person 11" not in html
    assert "No folder changes are available for this period" in html


def test_home_places_git_activity_between_app_cards_and_bookmark_statistics():
    html = render_to_string("core/dashboard.html", {"bitbucket_dashboard": _dashboard()})
    assert html.index('id="your-apps"') < html.index('id="bitbucket-activity"')
    assert html.index('id="bitbucket-activity"') < html.index(
        'aria-labelledby="library-pulse-heading"'
    )
    assert "Foundation</span>" not in html
    assert "Sync repositories and search their PDFs locally" in html


def test_git_activity_styles_bound_long_names_and_reflow_mobile():
    path = finders.find("owl/owl.css")
    with open(path, encoding="utf-8") as stylesheet:
        css = stylesheet.read().split("/* Home: compact Git-history rankings", 1)[1]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "text-overflow: ellipsis" in css
    assert "@media (max-width: 620px)" in css
    assert ".knowledge-git-activity a:focus-visible" in css
    assert ".knowledge-git-activity__bar::-webkit-progress-bar" in css
    assert ".knowledge-git-activity__bar::-webkit-progress-value" in css
    assert ".knowledge-git-activity__bar::-moz-progress-bar" in css
    assert ".knowledge-git-activity__bar::before" not in css
    assert "--git-activity-fill" not in css
