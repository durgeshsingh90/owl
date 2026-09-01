"""Routes for PDF discovery and repository status."""

from django.urls import path

from bitbucket_search import views

app_name = "bitbucket_search"

urlpatterns = [
    path("", views.index, name="index"),
    path("people/groups/add/", views.add_people_group, name="people_group_create"),
    path("documents/page/", views.document_page, name="document_page"),
    path(
        "documents/<int:document_id>/open/",
        views.open_document,
        name="document_open",
    ),
    path(
        "documents/open/",
        views.open_documents,
        name="documents_open_all",
    ),
    path(
        "documents/<int:document_id>/reveal/",
        views.reveal_document,
        name="document_reveal",
    ),
    path(
        "documents/<int:document_id>/exclude/",
        views.exclude_document,
        name="document_exclude",
    ),
    path(
        "documents/<int:document_id>/include/",
        views.resume_document,
        name="document_resume",
    ),
    path(
        "documents/<int:document_id>/delete/",
        views.delete_document,
        name="document_delete",
    ),
    path("repositories/", views.repositories, name="repositories"),
    path("repositories/add/", views.add_repository, name="repository_add"),
    path("repositories/selected/", views.selected_repositories, name="repositories_selected"),
    path(
        "repositories/<int:repository_id>/exclude/",
        views.exclude_repository,
        name="repository_exclude",
    ),
    path(
        "repositories/<int:repository_id>/remove/",
        views.remove_repository_view,
        name="repository_remove",
    ),
    path(
        "repositories/refresh/",
        views.refresh_all_repositories,
        name="repositories_refresh_all",
    ),
    path(
        "repositories/<int:repository_id>/refresh/",
        views.refresh_repository,
        name="repository_refresh",
    ),
    path(
        "repositories/schedule/tick/",
        views.tick_repository_schedule,
        name="repository_schedule_tick",
    ),
    path("repositories/status/", views.repository_status, name="repository_status"),
    path("repositories/<int:repository_id>/logs/", views.repository_logs, name="repository_logs"),
    path(
        "repositories/<int:repository_id>/indexing/cancel/",
        views.cancel_repository_indexing,
        name="repository_index_cancel",
    ),
    path("status/", views.index_status, name="index_status"),
]
