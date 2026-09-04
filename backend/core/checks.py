from pathlib import Path

from django.conf import settings
from django.core.checks import Error, register
from django.db import connection

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@register()
def owl_runtime_checks(app_configs, **kwargs):
    errors = []

    non_loopback = set(settings.ALLOWED_HOSTS) - LOOPBACK_HOSTS
    if non_loopback and not settings.OWL_ALLOW_NON_LOOPBACK:
        errors.append(
            Error(
                "OWL is configured with non-loopback hosts.",
                hint="Remove non-loopback hosts or make a separate authenticated deployment decision.",
                id="owl.E001",
            )
        )

    data_root = Path(settings.OWL_DATA_ROOT).resolve()
    base_dir = Path(settings.BASE_DIR).resolve()
    unsafe_repository_location = base_dir.is_relative_to(data_root)
    if data_root.is_relative_to(base_dir):
        relative_path = data_root.relative_to(base_dir)
        unsafe_repository_location = (
            not relative_path.parts
            or relative_path.parts[0] not in settings.SAFE_REPOSITORY_DATA_DIRECTORIES
        )
    if unsafe_repository_location:
        errors.append(
            Error(
                "OWL_DATA_ROOT is not a dedicated ignored data directory.",
                hint="Use var, data, media, or another dedicated folder outside the repository.",
                id="owl.E002",
            )
        )

    database_name = str(settings.DATABASES["default"]["NAME"])
    is_memory_database = database_name == ":memory:" or database_name.startswith("file:memorydb_")
    if not is_memory_database:
        database_path = Path(database_name).resolve()
        if not database_path.is_relative_to(data_root):
            errors.append(
                Error(
                    "The SQLite database is outside OWL_DATA_ROOT.",
                    hint="Keep the canonical database beneath the ignored OWL data root.",
                    id="owl.E003",
                )
            )

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pragma_module_list WHERE lower(name) = 'fts5' LIMIT 1")
            fts5_available = cursor.fetchone() is not None
    except Exception:
        fts5_available = False
    if not fts5_available:
        errors.append(
            Error(
                "SQLite FTS5 is unavailable.",
                hint="Use a Python/SQLite build compiled with FTS5 support.",
                id="owl.E004",
            )
        )

    return errors
