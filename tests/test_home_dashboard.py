from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkActivityCoverage,
    BookmarkActivityType,
    BookmarkAvailability,
    BookmarkDailyActivity,
)
from bookmark_manager.services.bookmark_analytics import (
    get_bookmark_dashboard,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluencePageSnapshot,
    record_successful_open,
    upsert_bookmark,
)
from bookmark_manager.services.web_bookmarks import save_web_bookmark

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _bookmark(
    page_id: str,
    title: str,
    *,
    page_text: str = "",
    updated_at: datetime | None = None,
) -> Bookmark:
    return upsert_bookmark(
        ConfluencePageSnapshot(
            page_id=page_id,
            title=title,
            url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
            space_name="Engineering",
            space_key="ENG",
            version=1,
            created_at=datetime(2025, 1, 2, 9, tzinfo=UTC),
            updated_at=updated_at or datetime(2026, 8, 20, 9, tzinfo=UTC),
            page_text=page_text,
        )
    ).bookmark


def _metric_map(dashboard):
    return {metric.kind: metric for metric in dashboard.metrics}


def _activity_days(activity):
    return {day.date: day for week in activity.weeks for day in week.days if day.in_year}


def test_dashboard_zero_state_is_truthful_and_zero_safe():
    Bookmark.objects.all().delete()
    BookmarkDailyActivity.objects.all().delete()

    dashboard = get_bookmark_dashboard(
        year=2026,
        at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )
    metrics = _metric_map(dashboard)

    assert metrics["bookmarks"].value == "0"
    assert metrics["bookmarks"].detail == "0 Confluence · 0 web"
    assert metrics["indexed"].value == "0"
    assert metrics["average-size"].value == "0 B"
    assert metrics["total-size"].value == "0 B"
    assert metrics["clicks"].value == "0"
    assert metrics["attention"].value == "0"
    assert dashboard.activity.total == 0
    assert dashboard.activity.label == "0 activities in 2026"
    assert dashboard.activity.most_active_day == "No activity yet"
    assert dashboard.top_viewed == ()
    assert len(dashboard.interesting) == 4
    assert all(group.items == () for group in dashboard.interesting)


def test_dashboard_metrics_use_exact_utf8_bytes_and_owl_open_counts():
    first = _bookmark("910001", "Unicode page", page_text="é🙂")
    second = _bookmark("910002", "ASCII page", page_text="text")
    web = save_web_bookmark("https://docs.example.test/start").bookmark

    first.open_count = 5
    first.availability_status = BookmarkAvailability.REFRESH_ERROR
    first.save(update_fields=("open_count", "availability_status"))
    second.open_count = 2
    second.save(update_fields=("open_count",))

    dashboard = get_bookmark_dashboard(
        year=2026,
        at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )
    metrics = _metric_map(dashboard)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.page_text_size_bytes == 6
    assert second.page_text_size_bytes == 4
    assert web.page_text_size_bytes == 0
    assert metrics["bookmarks"].value == "3"
    assert metrics["bookmarks"].detail == "2 Confluence · 1 web"
    assert metrics["indexed"].value == "2"
    assert metrics["average-size"].value == "5 B"
    assert metrics["average-size"].detail == "per page with indexed text"
    assert metrics["total-size"].value == "10 B"
    assert metrics["total-size"].detail == "UTF-8 text only; attachments excluded"
    assert metrics["clicks"].value == "7"
    assert metrics["clicks"].detail == "opens initiated from OWL"
    assert metrics["attention"].value == "1"


def test_top_viewed_is_limited_to_ten_and_uses_stable_usage_order():
    observed_at = datetime(2026, 8, 28, 12, tzinfo=UTC)
    bookmarks = [_bookmark(str(920000 + index), f"Page {index:02d}") for index in range(12)]
    for index, bookmark in enumerate(bookmarks):
        bookmark.open_count = 20 - index
        bookmark.last_viewed_at = observed_at - timedelta(days=index + 1)
        bookmark.save(update_fields=("open_count", "last_viewed_at"))

    # Open count wins first; for a tie, the most recently viewed page wins.
    bookmarks[1].open_count = bookmarks[0].open_count
    bookmarks[1].last_viewed_at = observed_at - timedelta(minutes=5)
    bookmarks[1].save(update_fields=("open_count", "last_viewed_at"))

    dashboard = get_bookmark_dashboard(year=2026, at=observed_at)

    assert len(dashboard.top_viewed) == 10
    assert [item.rank for item in dashboard.top_viewed] == list(range(1, 11))
    assert [item.bookmark.pk for item in dashboard.top_viewed[:2]] == [
        bookmarks[1].pk,
        bookmarks[0].pk,
    ]
    assert [item.bookmark.open_count for item in dashboard.top_viewed] == sorted(
        (item.bookmark.open_count for item in dashboard.top_viewed),
        reverse=True,
    )
    assert bookmarks[-1].pk not in {item.bookmark.pk for item in dashboard.top_viewed}


def test_repeated_opens_are_counted_on_their_exact_local_day():
    bookmark = _bookmark("930001", "Local-day activity")
    BookmarkDailyActivity.objects.all().delete()

    with timezone.override("Europe/Dublin"):
        record_successful_open(
            bookmark,
            opened_at=datetime(2026, 8, 28, 21, 30, tzinfo=UTC),
        )
        record_successful_open(
            bookmark,
            opened_at=datetime(2026, 8, 28, 22, 30, tzinfo=UTC),
        )
        record_successful_open(
            bookmark,
            opened_at=datetime(2026, 8, 28, 23, 30, tzinfo=UTC),
        )
        dashboard = get_bookmark_dashboard(
            year=2026,
            activity_type=BookmarkActivityType.OPENED,
            at=datetime(2026, 8, 29, 12, tzinfo=UTC),
        )

    days = _activity_days(dashboard.activity)
    bookmark.refresh_from_db()
    assert bookmark.open_count == 3
    assert dashboard.activity.total == 3
    assert dashboard.activity.opened_count == 3
    assert days[datetime(2026, 8, 28).date()].count == 2
    assert days[datetime(2026, 8, 29).date()].count == 1
    assert days[datetime(2026, 8, 28).date()].aria_label == "2 events on 28 August 2026"


def test_preexisting_open_count_is_not_fabricated_into_daily_history():
    bookmark = _bookmark("940001", "Imported usage")
    BookmarkDailyActivity.objects.all().delete()
    BookmarkActivityCoverage.objects.update_or_create(
        pk=1,
        defaults={
            "detailed_tracking_started_at": datetime(2026, 8, 28, 9, tzinfo=UTC),
        },
    )
    Bookmark.objects.filter(pk=bookmark.pk).update(
        open_count=8,
        last_viewed_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )

    dashboard = get_bookmark_dashboard(
        year=2026,
        activity_type=BookmarkActivityType.OPENED,
        at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )
    metrics = _metric_map(dashboard)

    assert metrics["clicks"].value == "8"
    assert [item.bookmark.pk for item in dashboard.top_viewed] == [bookmark.pk]
    assert dashboard.activity.total == 0
    assert dashboard.activity.opened_count == 0
    assert "earlier per-day detail was not available" in dashboard.activity.tracking_note


def test_activity_supports_leap_day_and_invalid_year_falls_back_to_current_year():
    BookmarkDailyActivity.objects.create(
        activity_date=datetime(2024, 2, 29).date(),
        activity_type=BookmarkActivityType.ADDED,
        count=4,
    )
    observed_at = datetime(2026, 8, 28, 12, tzinfo=UTC)

    leap_dashboard = get_bookmark_dashboard(year="2024", at=observed_at)
    leap_days = _activity_days(leap_dashboard.activity)
    invalid_dashboard = get_bookmark_dashboard(year="not-a-year", at=observed_at)
    out_of_range_dashboard = get_bookmark_dashboard(year="10000", at=observed_at)

    assert leap_days[datetime(2024, 2, 29).date()].count == 4
    assert leap_dashboard.activity.total == 4
    assert next(year for year in leap_dashboard.activity.years if year.selected).label == "2024"
    assert next(year for year in invalid_dashboard.activity.years if year.selected).label == "2026"
    assert (
        next(year for year in out_of_range_dashboard.activity.years if year.selected).label
        == "2026"
    )


def test_home_renders_accessible_activity_and_tracked_top_page_actions(loopback_client):
    bookmark = _bookmark("950001", "Most useful architecture", page_text="search text")
    bookmark.open_count = 9
    bookmark.last_viewed_at = datetime(2026, 8, 28, 9, tzinfo=UTC)
    bookmark.save(update_fields=("open_count", "last_viewed_at"))

    response = loopback_client.get(
        reverse("core:dashboard"),
        {"year": "2026", "activity": BookmarkActivityType.ADDED},
    )
    html = response.content.decode("utf-8")

    assert response.status_code == 200
    assert response.context["dashboard"].top_viewed[0].bookmark.pk == bookmark.pk
    assert 'aria-label="Bookmark activity calendar"' in html
    assert 'aria-label="Activity intensity from less to more"' in html
    assert 'aria-label="Activity type"' in html
    assert 'aria-label="Activity year"' in html
    assert 'aria-label="Top 10 most viewed pages"' in html
    assert "Most viewed" in html
    assert "Most useful architecture" in html
    assert f'action="{reverse("bookmark_manager:open", args=(bookmark.pk,))}"' in html
    assert 'method="post"' in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert f'href="{bookmark.url}"' not in html
    assert "No activity yet" not in html
