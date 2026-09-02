"""Bookmark Manager routes."""

from django.urls import path

from bookmark_manager import views

app_name = "bookmark_manager"

urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save_bookmark, name="save"),
    path("refresh/start/", views.start_global_refresh, name="refresh_start"),
    path("refresh/status/", views.global_refresh_status, name="refresh_status"),
    path("refresh/schedule/tick/", views.tick_refresh_schedule, name="refresh_schedule_tick"),
    path("notifications/", views.notifications_status, name="notifications"),
    path("notifications/read/", views.read_notification, name="notifications_read"),
    path(
        "notifications/read-all/",
        views.read_all_notifications,
        name="notifications_read_all",
    ),
    path("delete-selected/", views.delete_selected_bookmarks, name="delete_selected"),
    path("folders/create/", views.create_folder, name="folder_create"),
    path("folders/move/", views.move_bookmarks_to_manual_folder, name="folder_move"),
    path("import/", views.import_bookmarks, name="import"),
    path("export/", views.export_bookmarks, name="export"),
    path("views/save/", views.save_view, name="view_save"),
    path("views/<int:pk>/delete/", views.delete_view, name="view_delete"),
    path("categories/<int:pk>/rename/", views.rename_category, name="category_rename"),
    path("<int:pk>/open/", views.open_bookmark, name="open"),
    path("<int:pk>/link/", views.bookmark_link, name="link"),
    path("<int:pk>/open-parent/", views.open_parent, name="open_parent"),
    path("<int:pk>/organise/", views.update_organisation, name="organise"),
    path("<int:pk>/favorite/", views.toggle_favorite, name="favorite"),
    path("<int:pk>/pin/", views.toggle_pin, name="pin"),
    path("<int:pk>/delete/", views.delete_bookmark, name="delete"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/test/", views.test_connection, name="settings_test"),
    path("settings/save/", views.save_settings, name="settings_save"),
    path("settings/remove/", views.remove_settings, name="settings_remove"),
    path(
        "settings/bitbucket-https/save/",
        views.save_bitbucket_https_credential,
        name="bitbucket_https_credential_save",
    ),
    path(
        "settings/bitbucket-https/remove/",
        views.remove_bitbucket_https_credential,
        name="bitbucket_https_credential_remove",
    ),
]
