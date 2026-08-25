"""Application configuration for Bookmark Manager."""

from django.apps import AppConfig


class BookmarkManagerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bookmark_manager"
    verbose_name = "Bookmark Manager"
