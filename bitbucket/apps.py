"""Application configuration for Bitbucket."""

from django.apps import AppConfig


class BitbucketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bitbucket"
    verbose_name = "Bitbucket"
