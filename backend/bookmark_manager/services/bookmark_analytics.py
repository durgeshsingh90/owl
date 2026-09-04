"""Read-only Home analytics and exact privacy-preserving daily counters."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from bookmark_manager.models import (
    Bookmark,
    BookmarkActivityCoverage,
    BookmarkActivityType,
    BookmarkAvailability,
    BookmarkDailyActivity,
    BookmarkSource,
)
from bookmark_manager.services.logging_events import logged_operation

ALL_ACTIVITY = "all"
SUPPORTED_ACTIVITY_FILTERS = (ALL_ACTIVITY, *BookmarkActivityType.values)


@dataclass(frozen=True, slots=True)
class DashboardMetric:
    label: str
    value: str
    detail: str
    kind: str


@dataclass(frozen=True, slots=True)
class DashboardActivityFilter:
    label: str
    href: str
    selected: bool


@dataclass(frozen=True, slots=True)
class DashboardActivityYear:
    label: str
    href: str
    selected: bool


@dataclass(frozen=True, slots=True)
class DashboardMonthLabel:
    label: str
    column: int


@dataclass(frozen=True, slots=True)
class DashboardActivityDay:
    date: date
    aria_label: str
    count: int
    level: int
    in_year: bool


@dataclass(frozen=True, slots=True)
class DashboardActivityWeek:
    days: tuple[DashboardActivityDay, ...]


@dataclass(frozen=True, slots=True)
class DashboardActivity:
    total: int
    label: str
    filters: tuple[DashboardActivityFilter, ...]
    years: tuple[DashboardActivityYear, ...]
    month_labels: tuple[DashboardMonthLabel, ...]
    weeks: tuple[DashboardActivityWeek, ...]
    tracking_note: str
    added_count: int
    opened_count: int
    changed_count: int
    refreshed_count: int
    notes_count: int
    most_active_day: str


@dataclass(frozen=True, slots=True)
class DashboardTopViewedItem:
    bookmark: Bookmark
    rank: int
    size_label: str
    last_viewed_label: str


@dataclass(frozen=True, slots=True)
class DashboardInterestingItem:
    bookmark: Bookmark
    meta: str


@dataclass(frozen=True, slots=True)
class DashboardInterestingGroup:
    title: str
    summary: str
    items: tuple[DashboardInterestingItem, ...]
    empty_message: str
    href: str


@dataclass(frozen=True, slots=True)
class BookmarkDashboard:
    metrics: tuple[DashboardMetric, ...]
    activity: DashboardActivity
    top_viewed: tuple[DashboardTopViewedItem, ...]
    interesting: tuple[DashboardInterestingGroup, ...]


def _require_aware(value: datetime) -> None:
    if timezone.is_naive(value):
        raise ValueError("Bookmark activity timestamps must include a timezone.")


@logged_operation("record_daily_activity", quiet=True, expected_errors=(ValueError,))
def record_daily_activity(
    activity_type: str,
    *,
    occurred_at: datetime | None = None,
    count: int = 1,
) -> None:
    """Increment one exact local-day counter without retaining bookmark content."""

    if activity_type not in BookmarkActivityType.values:
        raise ValueError("The bookmark activity type is not supported.")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Bookmark activity counts must be positive integers.")
    event_time = occurred_at or timezone.now()
    _require_aware(event_time)
    activity_date = timezone.localdate(event_time)

    updated = BookmarkDailyActivity.objects.filter(
        activity_date=activity_date,
        activity_type=activity_type,
    ).update(count=F("count") + count)
    if updated:
        return

    try:
        # The savepoint keeps a concurrent unique-key race from breaking a caller's
        # outer transaction. The retry then increments the row created by the winner.
        with transaction.atomic():
            BookmarkDailyActivity.objects.create(
                activity_date=activity_date,
                activity_type=activity_type,
                count=count,
            )
    except IntegrityError:
        BookmarkDailyActivity.objects.filter(
            activity_date=activity_date,
            activity_type=activity_type,
        ).update(count=F("count") + count)


def _human_size(byte_count: int) -> str:
    value = float(max(0, byte_count))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def _relative_label(value: datetime | None, *, now: datetime) -> str:
    if value is None:
        return "Never opened"
    if value >= now - timedelta(minutes=1):
        return "Just now"
    return f"{timesince(value, now)} ago"


def _dashboard_href(*, selected_year: int, activity_type: str) -> str:
    query = urlencode({"year": selected_year, "activity": activity_type})
    return f"{reverse('core:dashboard')}?{query}"


def _selected_year(value: object, *, current_year: int) -> int:
    try:
        candidate = int(str(value))
    except (TypeError, ValueError):
        return current_year
    return candidate if 1 <= candidate <= 9999 else current_year


def _selected_activity(value: object) -> str:
    candidate = str(value or ALL_ACTIVITY).strip().casefold()
    return candidate if candidate in SUPPORTED_ACTIVITY_FILTERS else ALL_ACTIVITY


def _activity_level(count: int, maximum: int) -> int:
    if count <= 0 or maximum <= 0:
        return 0
    return max(1, min(4, ceil((count / maximum) * 4)))


def _activity_label(activity_type: str, total: int, year: int) -> str:
    noun = {
        ALL_ACTIVITY: "activities",
        BookmarkActivityType.ADDED: "pages added",
        BookmarkActivityType.OPENED: "pages opened",
        BookmarkActivityType.REFRESHED: "pages refreshed",
        BookmarkActivityType.NOTES: "note updates",
    }[activity_type]
    return f"{total:,} {noun} in {year}"


def _tracking_note() -> str:
    coverage = BookmarkActivityCoverage.objects.filter(pk=1).first()
    if coverage is None:
        return (
            "Saved-page dates are complete. Detailed open, refresh, and note activity "
            "starts when Home activity tracking is enabled."
        )
    local_start = timezone.localtime(coverage.detailed_tracking_started_at)
    date_label = f"{local_start.day} {local_start:%B %Y}"
    return (
        "Saved-page dates are complete. Opens, refreshes, and note edits are counted "
        f"from {date_label}; earlier per-day detail was not available."
    )


def _build_activity(
    *,
    selected_year: int,
    selected_activity: str,
    current_year: int,
) -> DashboardActivity:
    start = date(selected_year, 1, 1)
    end = date(selected_year + 1, 1, 1) if selected_year < 9999 else date.max
    rows = tuple(
        BookmarkDailyActivity.objects.filter(
            activity_date__gte=start,
            activity_date__lt=end,
        ).values_list("activity_date", "activity_type", "count")
    )

    breakdown = {activity_type: 0 for activity_type in BookmarkActivityType.values}
    selected_counts: dict[date, int] = {}
    for activity_date, activity_type, count in rows:
        breakdown[activity_type] += count
        if selected_activity in {ALL_ACTIVITY, activity_type}:
            selected_counts[activity_date] = selected_counts.get(activity_date, 0) + count

    total = sum(selected_counts.values())
    maximum = max(selected_counts.values(), default=0)
    grid_start = start - timedelta(days=(start.weekday() + 1) % 7)
    last_day = date(selected_year, 12, 31)
    grid_end = last_day + timedelta(days=6 - ((last_day.weekday() + 1) % 7))
    day_count = (grid_end - grid_start).days + 1
    calendar_days = []
    noun = "activities" if selected_activity == ALL_ACTIVITY else "events"
    for offset in range(day_count):
        activity_date = grid_start + timedelta(days=offset)
        in_year = activity_date.year == selected_year
        count = selected_counts.get(activity_date, 0) if in_year else 0
        calendar_days.append(
            DashboardActivityDay(
                date=activity_date,
                aria_label=(f"{count:,} {noun} on {activity_date.day} {activity_date:%B %Y}"),
                count=count,
                level=_activity_level(count, maximum),
                in_year=in_year,
            )
        )
    weeks = tuple(
        DashboardActivityWeek(days=tuple(calendar_days[offset : offset + 7]))
        for offset in range(0, len(calendar_days), 7)
    )

    month_labels = tuple(
        DashboardMonthLabel(
            label=calendar.month_abbr[month],
            column=((date(selected_year, month, 1) - grid_start).days // 7) + 1,
        )
        for month in range(1, 13)
    )
    available_years = {
        value.year
        for value in BookmarkDailyActivity.objects.dates(
            "activity_date",
            "year",
            order="DESC",
        )
    }
    available_years.update({current_year, selected_year})

    filter_labels = (
        (ALL_ACTIVITY, "All"),
        (BookmarkActivityType.ADDED, "Added"),
        (BookmarkActivityType.OPENED, "Opened"),
        (BookmarkActivityType.REFRESHED, "Refreshed"),
        (BookmarkActivityType.NOTES, "Notes"),
    )
    filters = tuple(
        DashboardActivityFilter(
            label=label,
            href=_dashboard_href(selected_year=selected_year, activity_type=value),
            selected=value == selected_activity,
        )
        for value, label in filter_labels
    )
    years = tuple(
        DashboardActivityYear(
            label=str(year),
            href=_dashboard_href(selected_year=year, activity_type=selected_activity),
            selected=year == selected_year,
        )
        for year in sorted(available_years, reverse=True)
    )

    if selected_counts:
        most_active_date, most_active_count = max(
            selected_counts.items(),
            key=lambda item: (item[1], item[0]),
        )
        most_active_day = (
            f"{most_active_date.day} {most_active_date:%b} · {most_active_count:,} activities"
        )
    else:
        most_active_day = "No activity yet"

    return DashboardActivity(
        total=total,
        label=_activity_label(selected_activity, total, selected_year),
        filters=filters,
        years=years,
        month_labels=month_labels,
        weeks=weeks,
        tracking_note=_tracking_note(),
        added_count=breakdown[BookmarkActivityType.ADDED],
        opened_count=breakdown[BookmarkActivityType.OPENED],
        # Compatibility alias for the Home template contract; the visible label must
        # remain "Refreshed" because this counts successful refreshes, not only changes.
        changed_count=breakdown[BookmarkActivityType.REFRESHED],
        refreshed_count=breakdown[BookmarkActivityType.REFRESHED],
        notes_count=breakdown[BookmarkActivityType.NOTES],
        most_active_day=most_active_day,
    )


def _interesting_groups(*, now: datetime) -> tuple[DashboardInterestingGroup, ...]:
    bookmark_url = reverse("bookmark_manager:index")
    recently_added = tuple(Bookmark.objects.order_by("-saved_at", "-pk")[:5])
    recently_opened = tuple(
        Bookmark.objects.filter(last_viewed_at__isnull=False).order_by(
            "-last_viewed_at",
            "-pk",
        )[:5]
    )
    recently_updated = tuple(
        Bookmark.objects.filter(
            source_type=BookmarkSource.CONFLUENCE,
            updated_at__isnull=False,
        ).order_by("-updated_at", "-pk")[:5]
    )
    never_opened = tuple(Bookmark.objects.filter(open_count=0).order_by("saved_at", "pk")[:5])
    return (
        DashboardInterestingGroup(
            title="Recently added",
            summary="Newest pages in your local library.",
            items=tuple(
                DashboardInterestingItem(
                    bookmark=bookmark,
                    meta=f"Saved {_relative_label(bookmark.saved_at, now=now)}",
                )
                for bookmark in recently_added
            ),
            empty_message="No bookmarks have been saved yet.",
            href=f"{bookmark_url}?sort=added_newest",
        ),
        DashboardInterestingGroup(
            title="Recently opened",
            summary="Pages you returned to most recently.",
            items=tuple(
                DashboardInterestingItem(
                    bookmark=bookmark,
                    meta=_relative_label(bookmark.last_viewed_at, now=now),
                )
                for bookmark in recently_opened
            ),
            empty_message="Open a bookmark to begin this list.",
            href=f"{bookmark_url}?sort=recently_opened",
        ),
        DashboardInterestingGroup(
            title="Recently updated",
            summary="Latest known Confluence page changes.",
            items=tuple(
                DashboardInterestingItem(
                    bookmark=bookmark,
                    meta=f"Updated {_relative_label(bookmark.updated_at, now=now)}",
                )
                for bookmark in recently_updated
            ),
            empty_message="No Confluence update dates are available yet.",
            href=f"{bookmark_url}?sort=updated_newest",
        ),
        DashboardInterestingGroup(
            title="Still to explore",
            summary="Oldest saved pages that have never been opened from OWL.",
            items=tuple(
                DashboardInterestingItem(
                    bookmark=bookmark,
                    meta=f"Saved {_relative_label(bookmark.saved_at, now=now)}",
                )
                for bookmark in never_opened
            ),
            empty_message="Every saved page has been opened at least once.",
            href=f"{bookmark_url}?max_open=0&sort=added_oldest",
        ),
    )


@logged_operation("load_dashboard", quiet=True, expected_errors=(ValueError,))
def get_bookmark_dashboard(
    *,
    year: object = None,
    activity_type: object = ALL_ACTIVITY,
    at: datetime | None = None,
) -> BookmarkDashboard:
    """Return the complete, local-only analytics contract for OWL Home."""

    observed_at = at or timezone.now()
    _require_aware(observed_at)
    current_year = timezone.localtime(observed_at).year
    selected_year = _selected_year(year, current_year=current_year)
    selected_activity = _selected_activity(activity_type)

    summary = Bookmark.objects.aggregate(
        total_bookmarks=Count("pk"),
        confluence_pages=Count(
            "pk",
            filter=Q(source_type=BookmarkSource.CONFLUENCE),
        ),
        indexed_pages=Count("pk", filter=Q(page_text_size_bytes__gt=0)),
        total_indexed_bytes=Sum("page_text_size_bytes"),
        total_clicks=Sum("open_count"),
        unavailable_pages=Count(
            "pk",
            filter=~Q(availability_status=BookmarkAvailability.ACTIVE),
        ),
    )
    total_bookmarks = summary["total_bookmarks"] or 0
    indexed_pages = summary["indexed_pages"] or 0
    total_indexed_bytes = summary["total_indexed_bytes"] or 0
    total_clicks = summary["total_clicks"] or 0
    average_indexed_bytes = round(total_indexed_bytes / indexed_pages) if indexed_pages else 0
    metrics = (
        DashboardMetric(
            label="Bookmarks saved",
            value=f"{total_bookmarks:,}",
            detail=(
                f"{summary['confluence_pages'] or 0:,} Confluence · "
                f"{total_bookmarks - (summary['confluence_pages'] or 0):,} web"
            ),
            kind="bookmarks",
        ),
        DashboardMetric(
            label="Pages indexed",
            value=f"{indexed_pages:,}",
            detail="pages with locally searchable text",
            kind="indexed",
        ),
        DashboardMetric(
            label="Average indexed text",
            value=_human_size(average_indexed_bytes),
            detail="per page with indexed text",
            kind="average-size",
        ),
        DashboardMetric(
            label="Total indexed text",
            value=_human_size(total_indexed_bytes),
            detail="UTF-8 text only; attachments excluded",
            kind="total-size",
        ),
        DashboardMetric(
            label="Total clicks",
            value=f"{total_clicks:,}",
            detail="opens initiated from OWL",
            kind="clicks",
        ),
        DashboardMetric(
            label="Needs attention",
            value=f"{summary['unavailable_pages'] or 0:,}",
            detail="pages with a refresh or availability issue",
            kind="attention",
        ),
    )

    top_bookmarks = tuple(
        Bookmark.objects.filter(open_count__gt=0).order_by(
            "-open_count",
            F("last_viewed_at").desc(nulls_last=True),
            "pk",
        )[:10]
    )
    top_viewed = tuple(
        DashboardTopViewedItem(
            bookmark=bookmark,
            rank=rank,
            size_label=_human_size(bookmark.page_text_size_bytes),
            last_viewed_label=_relative_label(bookmark.last_viewed_at, now=observed_at),
        )
        for rank, bookmark in enumerate(top_bookmarks, start=1)
    )

    return BookmarkDashboard(
        metrics=metrics,
        activity=_build_activity(
            selected_year=selected_year,
            selected_activity=selected_activity,
            current_year=current_year,
        ),
        top_viewed=top_viewed,
        interesting=_interesting_groups(now=observed_at),
    )
