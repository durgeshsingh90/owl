"""Application configuration for the shared OWL shell."""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "OWL Core"

    def ready(self) -> None:
        """Register OWL's project-level Django system checks."""
        from core import checks  # noqa: F401
