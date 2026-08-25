"""Application configuration for Bitbucket Search."""

from django.apps import AppConfig


class BitbucketSearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bitbucket_search"
    verbose_name = "Bitbucket Search"
