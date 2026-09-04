"""Django data and mount views for the shared OWL shell."""

from dataclasses import asdict, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from bookmark_manager.services.bookmark_analytics import get_bookmark_dashboard
from bookmark_manager.services.people_analytics import (
    BOOKMARK_PEOPLE_PERIOD_LABELS,
    get_bookmark_people_dashboard,
)
from core.services.database_stats import get_database_stats
from core.services.system_status import get_system_status


def _with_people_period(href: str, *, people_period: str) -> str:
    """Keep the people filter when navigating the bookmark dashboard."""

    parts = urlsplit(href)
    query = dict(parse_qsl(parts.query))
    query.update(people_period=people_period)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _dashboard_context(request):
    """Build the one canonical Home snapshot used by HTML and JSON views."""
    observed_at = timezone.now()
    bookmark_dashboard = get_bookmark_dashboard(
        year=request.GET.get("year"),
        activity_type=request.GET.get("activity", "all"),
        at=observed_at,
    )
    bookmark_people_dashboard = get_bookmark_people_dashboard(
        period=request.GET.get("people_period", "week"), now=observed_at
    )
    selected_bookmark_href = next(
        item.href for item in bookmark_dashboard.activity.filters if item.selected
    )
    bookmark_people_activity_filters = tuple(
        {
            "label": label,
            "href": _with_people_period(
                selected_bookmark_href,
                people_period=period,
            )
            + "#bookmark-people-activity",
            "selected": period == bookmark_people_dashboard.period,
        }
        for period, label in BOOKMARK_PEOPLE_PERIOD_LABELS.items()
    )

    def preserve_periods(item):
        return replace(
            item,
            href=_with_people_period(
                item.href,
                people_period=bookmark_people_dashboard.period,
            ),
        )

    activity = bookmark_dashboard.activity
    bookmark_dashboard = replace(
        bookmark_dashboard,
        activity=replace(
            activity,
            filters=tuple(preserve_periods(item) for item in activity.filters),
            years=tuple(preserve_periods(item) for item in activity.years),
        ),
    )
    return {
        "active_nav": "home",
        "active_app": "home",
        "page_title": "Home",
        "status_message": "Ready · Home",
        "dashboard": bookmark_dashboard,
        "bookmark_people_dashboard": bookmark_people_dashboard,
        "bookmark_people_activity_filters": bookmark_people_activity_filters,
        "database_stats": get_database_stats(),
    }


def _home_bookmark_payload(bookmark):
    return {
        "id": bookmark.pk,
        "title": bookmark.title,
        "space": bookmark.space_name or bookmark.space_key or bookmark.get_source_type_display(),
        "openCount": bookmark.open_count,
        "openUrl": reverse("bookmark_manager:open", args=(bookmark.pk,)),
        "selectUrl": (
            f"{reverse('bookmark_manager:index')}?"
            f"{urlencode({'selected': bookmark.pk, 'located': bookmark.pk})}"
        ),
    }


def _home_workspace_payload(request):
    context = _dashboard_context(request)
    dashboard = context["dashboard"]
    activity = dashboard.activity
    database = context["database_stats"]
    people = context["bookmark_people_dashboard"]
    return {
        "ok": True,
        "csrfToken": get_token(request),
        "statusMessage": context["status_message"],
        "urls": {
            "home": reverse("core:dashboard"),
            "bookmarks": reverse("bookmark_manager:index"),
            "bookmarkSettings": reverse("bookmark_manager:settings"),
            "bitbucket": reverse("bitbucket:index"),
            "systemStatus": reverse("core:system_status"),
        },
        "bookmarkMetrics": [asdict(metric) for metric in dashboard.metrics],
        "bookmarkActivity": {
            "total": activity.total,
            "label": activity.label,
            "filters": [asdict(item) for item in activity.filters],
            "years": [asdict(item) for item in activity.years],
            "monthLabels": [asdict(item) for item in activity.month_labels],
            "weeks": [
                {
                    "days": [
                        {
                            "date": day.date.isoformat(),
                            "ariaLabel": day.aria_label,
                            "count": day.count,
                            "level": day.level,
                            "inYear": day.in_year,
                        }
                        for day in week.days
                    ]
                }
                for week in activity.weeks
            ],
            "trackingNote": activity.tracking_note,
            "addedCount": activity.added_count,
            "openedCount": activity.opened_count,
            "refreshedCount": activity.refreshed_count,
            "notesCount": activity.notes_count,
            "mostActiveDay": activity.most_active_day,
        },
        "topViewed": [
            {
                "rank": item.rank,
                "sizeLabel": item.size_label,
                "lastViewedLabel": item.last_viewed_label,
                "bookmark": _home_bookmark_payload(item.bookmark),
            }
            for item in dashboard.top_viewed
        ],
        "interesting": [
            {
                "title": group.title,
                "summary": group.summary,
                "emptyMessage": group.empty_message,
                "href": group.href,
                "items": [
                    {"meta": item.meta, "bookmark": _home_bookmark_payload(item.bookmark)}
                    for item in group.items
                ],
            }
            for group in dashboard.interesting
        ],
        "bookmarkPeople": {
            **asdict(people),
            "hasData": people.has_data,
            "hasConfluencePages": people.has_confluence_pages,
            "filters": list(context["bookmark_people_activity_filters"]),
        },
        "database": asdict(database),
    }


@require_GET
@ensure_csrf_cookie
def dashboard(request):
    """Render the minimal document that mounts the React Home application."""

    return render(request, "core/dashboard.html", _dashboard_context(request))


@require_GET
def dashboard_workspace(request):
    """Return Home data without exposing database paths or credentials."""

    response = JsonResponse(_home_workspace_payload(request))
    response.headers["Cache-Control"] = "no-store"
    return response


@require_GET
def global_search(request):
    """Describe the planned shared search capability without presenting it as an app."""

    return render(
        request,
        "core/foundation_state.html",
        {
            "active_nav": "global_search",
            "page_title": "Global Search",
            "eyebrow": "Shared capability · Planned for Phase 8",
            "heading": "Search your workspace in one place",
            "description": (
                "Global Search will combine locally stored Confluence bookmarks "
                "with document metadata from Bitbucket. "
                "It is intentionally unavailable in the current Phase 3 release."
            ),
            "capabilities": (
                "Search bookmark titles, metadata, tags, and notes",
                "Search Bitbucket PDF filenames, paths, repositories, and contributors",
                "Keep source type and match explanations visible",
            ),
            "status_message": "Shared Global Search is planned · No search was run",
        },
    )


@require_GET
def system_status(request):
    """Render a redacted snapshot of local OWL dependencies."""

    snapshot = get_system_status()
    overall_label = {
        "ready": "Ready",
        "attention": "Needs attention",
        "error": "Unavailable",
        "planned": "Foundation only",
    }.get(snapshot["overall_state"], "Status unknown")

    return render(
        request,
        "core/system_status.html",
        {
            "active_nav": "system_status",
            "page_title": "System Status",
            "system_status": snapshot,
            "overall_label": overall_label,
            "status_message": f"System status checked · {overall_label}",
        },
    )
