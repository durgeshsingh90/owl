"""Server-rendered views for the shared OWL shell."""

from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from bitbucket_search.services.activity_analytics import (
    ACTIVITY_PERIOD_LABELS,
    get_bitbucket_dashboard,
)
from bookmark_manager.services.bookmark_analytics import get_bookmark_dashboard
from bookmark_manager.services.people_analytics import (
    BOOKMARK_PEOPLE_PERIOD_LABELS,
    get_bookmark_people_dashboard,
)
from core.services.database_stats import get_database_stats
from core.services.system_status import get_system_status


def _with_dashboard_periods(href: str, *, git_period: str, people_period: str) -> str:
    """Keep the independent dashboard filters when navigating any section."""

    parts = urlsplit(href)
    query = dict(parse_qsl(parts.query))
    query.update(git_period=git_period, people_period=people_period)
    return urlunsplit(parts._replace(query=urlencode(query)))


@require_GET
def dashboard(request):
    """Render OWL's compact Home overview."""

    observed_at = timezone.now()
    bookmark_dashboard = get_bookmark_dashboard(
        year=request.GET.get("year"),
        activity_type=request.GET.get("activity", "all"),
        at=observed_at,
    )
    bitbucket_dashboard = get_bitbucket_dashboard(
        period=request.GET.get("git_period", "week"), now=observed_at
    )
    bookmark_people_dashboard = get_bookmark_people_dashboard(
        period=request.GET.get("people_period", "week"), now=observed_at
    )
    selected_bookmark_href = next(
        item.href for item in bookmark_dashboard.activity.filters if item.selected
    )
    bitbucket_activity_filters = tuple(
        {
            "label": label,
            "href": _with_dashboard_periods(
                selected_bookmark_href,
                git_period=period,
                people_period=bookmark_people_dashboard.period,
            )
            + "#bitbucket-activity",
            "selected": period == bitbucket_dashboard.period,
        }
        for period, label in ACTIVITY_PERIOD_LABELS.items()
    )
    bookmark_people_activity_filters = tuple(
        {
            "label": label,
            "href": _with_dashboard_periods(
                selected_bookmark_href,
                git_period=bitbucket_dashboard.period,
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
            href=_with_dashboard_periods(
                item.href,
                git_period=bitbucket_dashboard.period,
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
    return render(
        request,
        "core/dashboard.html",
        {
            "active_nav": "home",
            "active_app": "home",
            "page_title": "Home",
            "status_message": "Ready · Home",
            "dashboard": bookmark_dashboard,
            "bitbucket_dashboard": bitbucket_dashboard,
            "bitbucket_activity_filters": bitbucket_activity_filters,
            "bookmark_people_dashboard": bookmark_people_dashboard,
            "bookmark_people_activity_filters": bookmark_people_activity_filters,
            "database_stats": get_database_stats(),
        },
    )


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
            "heading": "Search both knowledge sources in one place",
            "description": (
                "Global Search will combine locally stored Confluence bookmarks "
                "from Bookmark Manager with indexed PDFs from Bitbucket Search. "
                "It is intentionally unavailable in the current Phase 3 release."
            ),
            "capabilities": (
                "Search bookmark titles, metadata, tags, and notes",
                "Search PDF filenames, paths, repositories, and page text",
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
