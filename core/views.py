"""Server-rendered views for the shared OWL shell."""

from django.shortcuts import render
from django.views.decorators.http import require_GET

from bookmark_manager.services.bookmark_analytics import get_bookmark_dashboard
from core.services.database_stats import get_database_stats
from core.services.system_status import get_system_status


@require_GET
def dashboard(request):
    """Render OWL's compact Home overview."""

    bookmark_dashboard = get_bookmark_dashboard(
        year=request.GET.get("year"),
        activity_type=request.GET.get("activity", "all"),
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
