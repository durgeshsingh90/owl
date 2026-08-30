from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkSource,
    ConfluencePageNode,
)
from bookmark_manager.services.people_analytics import (
    BOOKMARK_PEOPLE_PERIOD_LABELS,
    _valid_source_date,
    get_bookmark_people_dashboard,
)

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 30, 11, 30, tzinfo=UTC)


def _bookmark(**values):
    page_id = str(1000 + ConfluencePageNode.objects.count())
    node = ConfluencePageNode.objects.create(
        page_id=page_id,
        title=f"Page {page_id}",
        url=f"https://confluence.example.test/pages/{page_id}",
    )
    defaults = {
        "page_id": page_id,
        "tree_node": node,
        "title": node.title,
        "url": node.url,
        "author_name": "Alice",
        "created_by_name": "Alice",
        "created_at": NOW - timedelta(days=1),
        "modified_by_name": "Bob",
        "updated_at": NOW,
        "version": 2,
    }
    defaults.update(values)
    return Bookmark.objects.create(**defaults)


def test_counts_saved_pages_by_writer_and_latest_editor_not_local_activity():
    _bookmark()
    _bookmark(author_name="Carol", created_by_name="David")
    _bookmark(created_at=NOW - timedelta(days=100), modified_by_name="Alice")

    dashboard = get_bookmark_people_dashboard(now=NOW)

    assert dashboard.written_pages == 2
    assert dashboard.updated_pages == 3
    assert dashboard.active_people == 4
    assert [(person.name, person.page_count) for person in dashboard.writers] == [
        ("Alice", 1),
        ("Carol", 1),
        ("David", 1),
    ]
    assert [(person.name, person.page_count) for person in dashboard.updaters] == [
        ("Bob", 2),
        ("Alice", 1),
    ]
    assert dashboard.coverage.total_pages == 3
    assert dashboard.coverage.written_metadata_pages == 3
    assert dashboard.coverage.updated_metadata_pages == 3
    assert dashboard.has_data and dashboard.has_confluence_pages


def test_author_creator_and_editor_aliases_are_folded_without_duplicate_page_credit():
    _bookmark(author_name="Alice Smith", created_by_name="  Ａlice   Smith  ")
    _bookmark(author_name="Alice Smith", created_by_name="ALICE SMITH")
    _bookmark(
        author_name="Alice Smith", created_by_name="alice smith", modified_by_name="ALICE SMITH"
    )

    dashboard = get_bookmark_people_dashboard(now=NOW)

    (writer,) = dashboard.writers
    assert writer.name == "Alice Smith"
    assert writer.aliases == ("Alice Smith", "ALICE SMITH", "alice smith")
    assert writer.page_count == 3
    assert dashboard.written_pages == 3
    assert dashboard.updated_pages == 3
    assert dashboard.active_people == 2
    updater = next(person for person in dashboard.updaters if person.name == writer.name)
    assert updater.aliases == writer.aliases
    assert updater.page_count == 1


def test_same_person_in_both_roles_is_one_active_person():
    _bookmark(modified_by_name="Alice")
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.written_pages == dashboard.updated_pages == 1
    assert dashboard.active_people == 1
    assert dashboard.writers[0].page_count == dashboard.updaters[0].page_count == 1


def test_latest_editor_replaces_earlier_snapshot_not_a_historical_edit_counter():
    bookmark = _bookmark(modified_by_name="First editor")
    first = get_bookmark_people_dashboard(now=NOW)
    assert first.updaters[0].name == "First editor"

    bookmark.modified_by_name = "Latest editor"
    bookmark.version = 25
    bookmark.save(update_fields=["modified_by_name", "version"])
    latest = get_bookmark_people_dashboard(now=NOW)
    assert latest.updated_pages == 1
    assert latest.updaters[0].name == "Latest editor"
    assert latest.updaters[0].page_count == 1
    assert not any(person.name == "First editor" for person in latest.updaters)


def test_creation_only_version_one_is_not_an_update_but_legacy_later_date_is():
    _bookmark(version=1, created_at=NOW, updated_at=NOW)
    _bookmark(version=1, created_at=None, updated_at=NOW)
    _bookmark(version=1, created_at=NOW - timedelta(days=2), updated_at=NOW)
    # A later version is evidence of an update even if source timestamp precision
    # makes creation/modification equal, or the source creation date is missing.
    _bookmark(version=2, created_at=NOW, updated_at=NOW)
    _bookmark(version=2, created_at=None, updated_at=NOW)

    dashboard = get_bookmark_people_dashboard(now=NOW)

    assert dashboard.written_pages == 3
    assert dashboard.updated_pages == 3
    assert dashboard.coverage.updated_metadata_pages == 3
    assert dashboard.coverage.missing_written_metadata_pages == 2
    assert dashboard.coverage.missing_updated_metadata_pages == 0


def test_names_dates_and_future_or_reversed_metadata_are_not_invented():
    _bookmark(author_name="", created_by_name="", modified_by_name="")
    _bookmark(created_at=None, updated_at=None)
    _bookmark(created_at=NOW + timedelta(days=1), updated_at=NOW + timedelta(days=2))
    _bookmark(created_at=NOW, updated_at=NOW - timedelta(days=1))
    _bookmark(updated_at=NOW + timedelta(microseconds=1))

    dashboard = get_bookmark_people_dashboard(now=NOW)

    # The last two pages retain valid creation credit, despite bad update dates.
    assert dashboard.written_pages == 2
    assert dashboard.updated_pages == 0
    assert dashboard.active_people == 1
    assert dashboard.coverage.total_pages == 5
    assert dashboard.coverage.missing_written_metadata_pages == 3
    assert dashboard.coverage.missing_updated_metadata_pages == 5


@pytest.mark.parametrize("name", ["", " \n\t ", "\x00", "Bad\x00Name", "\u200b"])
def test_empty_or_invalid_names_do_not_create_people(name):
    _bookmark(author_name=name, created_by_name=name, modified_by_name=name)
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.written_pages == dashboard.updated_pages == 0
    assert dashboard.active_people == 0
    assert dashboard.writers == dashboard.updaters == ()


def test_ordinary_whitespace_and_nfkc_name_variants_are_canonical():
    _bookmark(author_name="  Ａlice\tSmith\n", created_by_name="Alice Smith")
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.writers[0].name == "Alice Smith"
    assert dashboard.writers[0].aliases == ("Alice Smith",)
    assert dashboard.writers[0].page_count == 1


def test_source_dates_never_fall_back_to_saved_refresh_or_detected_dates():
    _bookmark(
        created_at=None,
        updated_at=None,
        saved_at=NOW,
        last_refreshed_at=NOW,
        last_refresh_attempt_at=NOW,
        last_change_detected_at=NOW,
    )
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.written_pages == dashboard.updated_pages == 0
    assert dashboard.coverage.missing_written_metadata_pages == 1
    assert dashboard.coverage.missing_updated_metadata_pages == 1


def test_ordinary_web_bookmarks_and_ancestor_only_nodes_are_excluded():
    _bookmark(
        source_type=BookmarkSource.WEB, author_name="Web writer", modified_by_name="Web editor"
    )
    ConfluencePageNode.objects.create(
        page_id="ancestor",
        title="Unsaved ancestor",
        url="https://confluence.example.test/pages/ancestor",
    )
    empty = get_bookmark_people_dashboard(now=NOW)
    assert not empty.has_confluence_pages
    assert empty.coverage.total_pages == empty.active_people == 0

    _bookmark()
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.coverage.total_pages == 1
    assert dashboard.active_people == 2


def test_saved_unavailable_pages_remain_consistent_with_people_sidebar():
    for status in BookmarkAvailability.values:
        _bookmark(availability_status=status)
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.coverage.total_pages == len(BookmarkAvailability.values)
    assert dashboard.written_pages == dashboard.updated_pages == len(BookmarkAvailability.values)


def test_time_windows_include_start_and_end_but_not_older_or_future_activity():
    for at in (NOW - timedelta(days=7), NOW):
        _bookmark(created_at=at, updated_at=at)
    _bookmark(created_at=NOW - timedelta(days=7, microseconds=1), updated_at=None)
    _bookmark(created_at=NOW + timedelta(microseconds=1), updated_at=None)
    _bookmark(created_at=None, updated_at=NOW - timedelta(days=7, microseconds=1))
    _bookmark(created_at=None, updated_at=NOW + timedelta(microseconds=1))

    dashboard = get_bookmark_people_dashboard(now=NOW)

    assert dashboard.written_pages == 2
    assert dashboard.updated_pages == 2


@pytest.mark.parametrize(
    ("period", "at", "expected"),
    [
        ("today", (2026, 8, 31, 11, 30), (2026, 8, 31, 0, 0)),
        ("week", (2026, 8, 31, 11, 30), (2026, 8, 24, 11, 30)),
        ("month", (2026, 3, 31, 11, 30), (2026, 2, 28, 11, 30)),
        ("month", (2024, 3, 31, 11, 30), (2024, 2, 29, 11, 30)),
        ("month", (2026, 1, 31, 11, 30), (2025, 12, 31, 11, 30)),
        ("year", (2024, 2, 29, 11, 30), (2023, 2, 28, 11, 30)),
    ],
)
def test_periods_use_calendar_intervals_with_month_end_clamping(period, at, expected):
    with timezone.override("UTC"):
        dashboard = get_bookmark_people_dashboard(period, now=datetime(*at, tzinfo=UTC))
    assert dashboard.started_at == datetime(*expected, tzinfo=UTC)
    assert dashboard.label == BOOKMARK_PEOPLE_PERIOD_LABELS[period]


def test_today_uses_local_midnight_across_the_daylight_saving_fall_back():
    at = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    local_midnight = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    _bookmark(created_at=local_midnight, updated_at=local_midnight)
    _bookmark(created_at=local_midnight - timedelta(microseconds=1), updated_at=None)

    with timezone.override("Europe/Dublin"):
        dashboard = get_bookmark_people_dashboard("today", now=at)

    assert dashboard.started_at == local_midnight
    assert dashboard.written_pages == dashboard.updated_pages == 1


def test_month_uses_local_calendar_time_across_daylight_saving():
    at = datetime(2026, 3, 31, 10, 30, tzinfo=UTC)
    start = datetime(2026, 2, 28, 11, 30, tzinfo=UTC)
    _bookmark(created_at=start, updated_at=start)
    _bookmark(created_at=start - timedelta(microseconds=1), updated_at=None)

    with timezone.override("Europe/Dublin"):
        dashboard = get_bookmark_people_dashboard("month", now=at)

    assert dashboard.started_at == start
    assert dashboard.written_pages == dashboard.updated_pages == 1


@pytest.mark.parametrize("period", [None, "", "invalid", [], 3])
def test_invalid_period_falls_back_to_week(period):
    dashboard = get_bookmark_people_dashboard(period, now=NOW)
    assert dashboard.period == "week"
    assert dashboard.started_at == timezone.localtime(NOW) - timedelta(days=7)


def test_clock_can_be_naive_local_or_default_current_time(monkeypatch):
    monkeypatch.setattr("bookmark_manager.services.people_analytics.timezone.now", lambda: NOW)
    assert get_bookmark_people_dashboard().ended_at == NOW
    with timezone.override("Europe/Dublin"):
        assert get_bookmark_people_dashboard(now=datetime(2026, 8, 30, 12, 30)).ended_at == NOW


def test_malformed_or_naive_source_dates_are_not_accepted():
    for value in (None, "2026-08-30", datetime(2026, 8, 30), object()):
        assert not _valid_source_date(value, ended_at=NOW)


def test_no_recent_results_differ_from_missing_metadata_and_empty_library():
    empty = get_bookmark_people_dashboard(now=NOW)
    assert not empty.has_data and not empty.has_confluence_pages

    _bookmark(created_at=NOW - timedelta(days=30), updated_at=NOW - timedelta(days=20))
    quiet = get_bookmark_people_dashboard(now=NOW)
    assert quiet.has_confluence_pages and not quiet.has_data
    assert quiet.coverage.written_metadata_pages == quiet.coverage.updated_metadata_pages == 1
    assert quiet.coverage.missing_written_metadata_pages == 0

    _bookmark(author_name="", created_by_name="", modified_by_name="")
    incomplete = get_bookmark_people_dashboard(now=NOW)
    assert incomplete.has_confluence_pages and not incomplete.has_data
    assert incomplete.coverage.missing_written_metadata_pages == 1
    assert incomplete.coverage.missing_updated_metadata_pages == 1


def test_top_ten_rankings_keep_full_totals_and_are_deterministic():
    for index in reversed(range(15)):
        _bookmark(
            author_name=f"Person {index:02}",
            created_by_name=f"Person {index:02}",
            modified_by_name=f"Person {index:02}",
        )
    dashboard = get_bookmark_people_dashboard(now=NOW)
    assert dashboard.written_pages == dashboard.updated_pages == dashboard.active_people == 15
    assert len(dashboard.writers) == len(dashboard.updaters) == 10
    assert [person.name for person in dashboard.writers] == [
        f"Person {index:02}" for index in range(10)
    ]
    assert dashboard.writers == dashboard.updaters


def test_one_read_only_query_loads_only_metadata_and_returns_eager_values(
    django_assert_num_queries,
):
    for index in range(75):
        _bookmark(author_name=f"Person {index % 15:02}", created_by_name="")

    with CaptureQueriesContext(connection) as queries, django_assert_num_queries(1):
        dashboard = get_bookmark_people_dashboard(now=NOW)
    sql = queries[0]["sql"]
    assert sql.lstrip().upper().startswith("SELECT")
    for unused_field in ("page_text", "title", "url", "saved_at", "last_refreshed_at"):
        assert f'"{unused_field}"' not in sql
    with django_assert_num_queries(0):
        assert dashboard.written_pages == dashboard.updated_pages == 75
        assert dashboard.active_people == 16
        assert dashboard.updaters[0].page_count == 75
        assert dashboard.writers[0].page_count == 5
        assert dashboard.coverage.total_pages == 75


def test_result_dataclasses_are_immutable():
    _bookmark()
    dashboard = get_bookmark_people_dashboard(now=NOW)
    with pytest.raises(FrozenInstanceError):
        dashboard.written_pages = 100
    with pytest.raises(FrozenInstanceError):
        dashboard.writers[0].page_count = 100


def test_errors_are_logged_at_error_without_raw_source_content(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("private page name and credential")

    monkeypatch.setattr("bookmark_manager.services.people_analytics._rank_people", fail)
    with caplog.at_level("ERROR", logger="owl.bookmarks.operations"), pytest.raises(RuntimeError):
        get_bookmark_people_dashboard(now=NOW)
    assert "event=bookmark_operation_failed" in caplog.text
    assert "operation=get_bookmark_people_dashboard" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "credential" not in caplog.text
