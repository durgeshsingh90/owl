"""Honest foundation views for later local PDF discovery phases."""

from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def index(request):
    """Render an honest foundation state for Bitbucket Search."""

    return render(
        request,
        "bitbucket_search/index.html",
        {
            "active_app": "bitbucket",
            "active_section": "search",
            "active_nav": "pdf_search",
            "page_title": "Bitbucket Search",
            "status_message": "Bitbucket Search foundation · No files were scanned",
        },
    )


@require_GET
def repositories(request):
    """Describe the planned repository workflow."""

    return render(
        request,
        "bitbucket_search/foundation_state.html",
        {
            "active_app": "bitbucket",
            "active_section": "repositories",
            "active_nav": "repositories",
            "page_title": "Repositories",
            "eyebrow": "Planned for Phase 5",
            "heading": "Connect repositories safely",
            "description": (
                "Repository registration and synchronization are not active in "
                "the current Phase 3 release. No Git command has been run."
            ),
            "capabilities": (
                "Register approved Git or Bitbucket repository URLs",
                "Clone once, then synchronize incrementally",
                "Keep private working copies outside the public repository",
            ),
            "status_message": "Repositories are planned · No Git command run",
        },
    )


@require_GET
def index_status(request):
    """Describe the planned durable indexing workflow."""

    return render(
        request,
        "bitbucket_search/foundation_state.html",
        {
            "active_app": "bitbucket",
            "active_section": "index_status",
            "active_nav": "index_status",
            "page_title": "Index & Refresh Status",
            "eyebrow": "Introduced in Phase 4",
            "heading": "Track durable background work",
            "description": (
                "Refresh, indexing, and progress records are not active in the "
                "current Phase 3 release. Nothing is queued or running."
            ),
            "capabilities": (
                "See queued, running, completed, and interrupted jobs",
                "Inspect repository and PDF progress without blocking navigation",
                "Retry failed work with redacted, action-oriented diagnostics",
            ),
            "status_message": "Indexing is planned · No jobs are running",
        },
    )
