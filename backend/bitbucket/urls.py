from django.urls import path

from bitbucket import views

app_name = "bitbucket"

urlpatterns = [
    path("", views.index, name="index"),
    path("workspace/", views.workspace, name="workspace"),
    path("repositories/add/", views.repository_add, name="repository_add"),
    path("settings/save/", views.settings_save, name="settings_save"),
    path("schedule/", views.schedule_tick, name="schedule_tick"),
    path("sync/status/", views.sync_status, name="sync_status"),
    path("sync/<uuid:job_id>/retry/", views.sync_retry, name="sync_retry"),
    path("sync/<uuid:job_id>/cancel/", views.sync_cancel, name="sync_cancel"),
    path("documents/<int:document_id>/open/", views.document_open, name="document_open"),
]
