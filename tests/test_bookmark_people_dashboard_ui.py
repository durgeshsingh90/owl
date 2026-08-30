from __future__ import annotations

import re
from html import escape, unescape
from types import SimpleNamespace

import pytest
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.utils import translation


def _dashboard(**overrides):
    values = {
        "label": "Last 7 days",
        "has_data": True,
        "has_confluence_pages": True,
        "written_pages": 12,
        "updated_pages": 7,
        "active_people": 3,
        "writers": (
            SimpleNamespace(name="Alex Writer", aliases=("Alex W",), page_count=8),
            SimpleNamespace(name="Sam Writer", aliases=(), page_count=4),
        ),
        "updaters": (
            SimpleNamespace(name="Casey Editor", aliases=(), page_count=5),
            SimpleNamespace(name="Alex Writer", aliases=("Alex W",), page_count=2),
        ),
        "coverage": SimpleNamespace(
            total_pages=20,
            written_metadata_pages=20,
            updated_metadata_pages=20,
            missing_written_metadata_pages=0,
            missing_updated_metadata_pages=0,
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _render(dashboard=None, filters=()):
    return render_to_string(
        "core/_bookmark_people_activity.html",
        {
            "bookmark_people_dashboard": dashboard or _dashboard(),
            "bookmark_people_activity_filters": filters,
        },
    )


def test_people_activity_shows_page_totals_and_distinct_writer_editor_rankings():
    html = _render()
    assert 'id="bookmark-people-activity-heading">Bookmark Manager activity</h2>' in html
    assert "<dt>Pages written</dt><dd>12</dd>" in html
    assert "<dt>Pages updated</dt><dd>7</dd>" in html
    assert "<dt>People</dt><dd>3</dd>" in html
    assert html.count('role="list"') == 2
    assert html.count("Top 10") == 2
    assert "Most pages written" in html
    assert "Most pages updated" in html
    assert "Most recently updated" not in html
    assert "Writers / creators · page creation date" in html
    assert "Pages per latest stored editor · latest update date" in html
    assert "Saved Confluence pages only" in html
    assert "latest stored editor and date, not complete edit history" in html
    assert "Co-written pages can appear under multiple people" in html
    assert re.findall(
        r'<progress class="knowledge-bookmark-people__bar" value="(\d+)" max="(\d+)" aria-hidden="true"></progress>',
        html,
    ) == [("8", "8"), ("4", "8"), ("5", "5"), ("2", "5")]
    assert "style=" not in html
    assert "--bookmark-people-fill" not in html
    assert "commits" not in html


def test_people_progress_values_stay_numeric_with_localized_page_counts(settings):
    settings.USE_THOUSAND_SEPARATOR = True
    person = SimpleNamespace(name="Alex Writer", aliases=(), page_count=2000)
    with translation.override("de"):
        html = _render(_dashboard(writers=(person,), updaters=(person,)))
    assert html.count('value="2000" max="2000"') == 2
    assert 'value="2.000"' not in html


def test_people_period_filters_preserve_all_supplied_queries_and_anchor():
    filters = tuple(
        {
            "label": label,
            "href": f"?year=2025&activity=opened&git_period=six_months&people_period={period}#bookmark-people-activity",
            "selected": period == "today",
        }
        for period, label in (
            ("today", "Today"),
            ("week", "Last 7 days"),
            ("month", "Last month"),
            ("year", "Last year"),
        )
    )
    html = _render(filters=filters)
    nav = re.search(
        r'<nav[^>]+aria-label="Bookmark people activity period">(.*?)</nav>', html, re.DOTALL
    )
    assert nav is not None
    links = re.findall(r'href="([^"]+)"', nav.group(1))
    assert [unescape(link) for link in links] == [item["href"] for item in filters]
    assert nav.group(1).count('aria-current="page"') == 1
    assert 'class="is-active" aria-current="page">Today</a>' in nav.group(1)


@pytest.mark.parametrize(
    ("has_pages", "has_metadata", "message"),
    [
        (False, False, "No saved Confluence pages yet"),
        (True, False, "People metadata is not available yet"),
        (True, True, "No page activity in this period"),
    ],
)
def test_people_empty_states_distinguish_unsaved_missing_and_out_of_period(
    has_pages, has_metadata, message
):
    dashboard = _dashboard(
        has_data=False,
        has_confluence_pages=has_pages,
        written_pages=0,
        updated_pages=0,
        active_people=0,
    )
    dashboard.coverage.written_metadata_pages = int(has_metadata)
    dashboard.coverage.updated_metadata_pages = int(has_metadata)
    html = _render(dashboard)
    assert message in html
    assert 'class="knowledge-bookmark-people__rankings"' not in html
    if not has_metadata:
        assert "No page activity in this period" not in html
    if has_pages and not has_metadata:
        assert "Refresh your saved Confluence pages" in html


def test_missing_metadata_coverage_is_about_saved_pages_not_only_selected_period():
    dashboard = _dashboard()
    dashboard.coverage.missing_written_metadata_pages = 1
    dashboard.coverage.missing_updated_metadata_pages = 3
    html = _render(dashboard)
    assert "Based on 20 saved Confluence pages" in html
    assert "Writer or creation-date details are missing on 1 page." in html
    assert "Latest-editor or update-date details are missing on 3 pages." in html
    assert "People metadata is not available yet" not in html


def test_people_names_are_escaped_with_full_title_and_no_misleading_filter_links():
    name = 'Long <script>alert("synthetic")</script> name ' + "x" * 160
    person = SimpleNamespace(name=name, aliases=("Alias <writer>",), page_count=5)
    html = _render(_dashboard(writers=(person,), updaters=(person,)))
    assert "<script>" not in html
    assert escape(name) in html
    assert f'title="{escape(name)} · Names in saved metadata: Alias &lt;writer&gt;"' in html
    assert "?writer=" not in html
    assert "?editor=" not in html
    assert re.findall(r'href="([^"]+)"', html) == ["/bookmarks/"]


def test_people_rankings_are_limited_to_ten_rows_and_explain_one_empty_side():
    writers = tuple(
        SimpleNamespace(name=f"Writer {index}", aliases=(), page_count=20 - index)
        for index in range(12)
    )
    html = _render(_dashboard(writers=writers, updaters=()))
    assert "Writer 9" in html
    assert "Writer 10" not in html
    assert "Writer 11" not in html
    assert "No latest page updates with editor and date metadata in this period" in html


def test_home_places_people_activity_after_bookmark_statistics_before_database():
    html = render_to_string("core/dashboard.html", {"bookmark_people_dashboard": _dashboard()})
    assert html.index('aria-labelledby="library-pulse-heading"') < html.index(
        'id="bookmark-people-activity"'
    )
    assert html.index('id="bookmark-people-activity"') < html.index(
        'aria-labelledby="database-stats-heading"'
    )
    assert 'id="bitbucket-activity"' in html


def test_people_styles_are_scoped_with_mobile_reflow_and_long_name_bounds():
    path = finders.find("owl/owl.css")
    with open(path, encoding="utf-8") as stylesheet:
        css = stylesheet.read().split("/* Home: saved Confluence writer/editor activity", 1)[1]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "text-overflow: ellipsis" in css
    assert "@media (max-width: 620px)" in css
    assert ".knowledge-bookmark-people a:focus-visible" in css
    assert ".knowledge-bookmark-people__bar::-webkit-progress-bar" in css
    assert ".knowledge-bookmark-people__bar::-webkit-progress-value" in css
    assert ".knowledge-bookmark-people__bar::-moz-progress-bar" in css
    assert ".knowledge-bookmark-people__bar::before" not in css
    assert "--bookmark-people-fill" not in css
    assert ".knowledge-git-activity" not in css
