from datetime import UTC, datetime

import pytest
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkActivityCoverage,
    BookmarkActivityType,
    BookmarkDailyActivity,
    ConfluencePageNode,
)
from bookmark_manager.services.bookmark_analytics import (
    get_bookmark_dashboard,
    record_daily_activity,
)
from bookmark_manager.services.bookmark_domain import record_successful_open
from bookmark_manager.services.bookmark_productivity import update_bookmark_organisation
from bookmark_manager.services.web_bookmarks import save_web_bookmark

pytestmark = pytest.mark.django_db


def _bookmark(page_id: str, title: str, **values) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.test/pages/{page_id}",
    )
    defaults = {
        "tree_node": node,
        "title": title,
        "url": node.url,
    }
    defaults.update(values)
    return Bookmark.objects.create(page_id=page_id, **defaults)


def test_dashboard_metrics_use_exact_utf8_size_and_rank_top_ten():
    now = datetime(2026, 8, 28, 10, tzinfo=UTC)
    _bookmark("100", "Unicode", page_text="café", open_count=2, last_viewed_at=now)
    _bookmark("101", "Plain", page_text="owl", open_count=5, last_viewed_at=now)
    for index in range(10):
        _bookmark(str(200 + index), f"Page {index}", open_count=index)

    dashboard = get_bookmark_dashboard(at=now)
    metrics = {metric.label: metric for metric in dashboard.metrics}

    assert metrics["Bookmarks saved"].value == "12"
    assert metrics["Pages indexed"].value == "2"
    assert metrics["Total indexed text"].value == "8 B"
    assert metrics["Average indexed text"].value == "4 B"
    assert metrics["Total clicks"].value == "52"
    assert len(dashboard.top_viewed) == 10
    assert dashboard.top_viewed[0].bookmark.open_count == 9
    assert [item.rank for item in dashboard.top_viewed] == list(range(1, 11))


def test_daily_activity_uses_local_calendar_day_and_builds_year_heatmap():
    BookmarkActivityCoverage.objects.update_or_create(
        pk=1,
        defaults={
            "detailed_tracking_started_at": datetime(2026, 3, 1, 9, tzinfo=UTC),
        },
    )
    # Europe/Dublin is UTC+1 at this instant, so the exact local day is 30 March.
    occurred_at = datetime(2026, 3, 29, 23, 30, tzinfo=UTC)
    record_daily_activity(BookmarkActivityType.ADDED, occurred_at=occurred_at, count=2)
    record_daily_activity(BookmarkActivityType.OPENED, occurred_at=occurred_at)

    dashboard = get_bookmark_dashboard(year="2026", activity_type="all", at=occurred_at)
    activity = dashboard.activity
    day = next(
        day for week in activity.weeks for day in week.days if day.date.isoformat() == "2026-03-30"
    )

    assert day.count == 3
    assert day.level == 4
    assert activity.total == 3
    assert activity.added_count == 2
    assert activity.opened_count == 1
    assert "from 1 March 2026" in activity.tracking_note
    assert len(activity.month_labels) == 12


def test_open_and_changed_note_increment_only_successful_activity():
    bookmark = _bookmark("300", "Tracked")
    observed_at = timezone.now()

    record_successful_open(bookmark, opened_at=observed_at)
    update_bookmark_organisation(bookmark, notes="First note", raw_tags="")
    update_bookmark_organisation(bookmark, notes="First note", raw_tags="")

    counters = {
        row.activity_type: row.count
        for row in BookmarkDailyActivity.objects.filter(
            activity_date=timezone.localdate(observed_at)
        )
    }
    assert counters[BookmarkActivityType.OPENED] == 1
    assert counters[BookmarkActivityType.NOTES] == 1


def test_general_web_save_records_added_activity_and_existing_url_does_not_duplicate():
    observed_date = timezone.localdate()

    first = save_web_bookmark("https://example.test/reference")
    second = save_web_bookmark("https://example.test/reference")

    assert first.created is True
    assert second.created is False
    assert (
        BookmarkDailyActivity.objects.get(
            activity_date=observed_date,
            activity_type=BookmarkActivityType.ADDED,
        ).count
        == 1
    )
