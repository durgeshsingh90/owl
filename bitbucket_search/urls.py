"""Routes for PDF discovery and repository status."""

from django.urls import path

from bitbucket_search import views

app_name = "bitbucket_search"

urlpatterns = [
    path("", views.index, name="index"),
    path("repositories/", views.repositories, name="repositories"),
    path("status/", views.index_status, name="index_status"),
]
