"""Application configuration for shared semantic search."""

from django.apps import AppConfig


class SemanticSearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "semantic_search"
    verbose_name = "Semantic Search"

    def ready(self) -> None:
        from semantic_search import signals  # noqa: F401
