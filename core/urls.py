"""Routes for the shared OWL shell."""

from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.global_search, name="global_search"),
    path("system-status/", views.system_status, name="system_status"),
]
