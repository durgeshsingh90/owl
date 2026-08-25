"""Root URL configuration for OWL."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include(("core.urls", "core"), namespace="core")),
    path(
        "bookmarks/",
        include(
            ("bookmark_manager.urls", "bookmark_manager"),
            namespace="bookmark_manager",
        ),
    ),
    path(
        "pdfs/",
        include(
            ("bitbucket_search.urls", "bitbucket_search"),
            namespace="bitbucket_search",
        ),
    ),
]
