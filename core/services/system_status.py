from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from bookmark_manager.services.configuration import get_configuration_summary
from bookmark_manager.services.secret_store import get_secret_store


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    key: str
    label: str
    state: str
    summary: str
    detail: str = ""


def _database_status() -> ComponentStatus:
    probe_table = f"owl_write_probe_{secrets.token_hex(8)}"
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.execute(f'CREATE TABLE "{probe_table}" (id INTEGER)')
            transaction.set_rollback(True)
    except Exception:
        return ComponentStatus(
            key="database",
            label="Database",
            state="error",
            summary="Unavailable",
            detail="OWL could not query its local database.",
        )
    return ComponentStatus(
        key="database",
        label="Database",
        state="ready",
        summary="Ready",
        detail="The canonical local database accepted a rollback-only write check.",
    )


def _fts_status() -> ComponentStatus:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pragma_module_list WHERE lower(name) = 'fts5' LIMIT 1")
            available = cursor.fetchone() is not None
    except Exception:
        available = False
    return ComponentStatus(
        key="fts5",
        label="Full-text search",
        state="ready" if available else "error",
        summary="FTS5 available" if available else "FTS5 unavailable",
        detail=(
            "SQLite can create the local search index."
            if available
            else "Install a Python/SQLite build with FTS5 support."
        ),
    )


def _data_root_status() -> ComponentStatus:
    root = Path(settings.OWL_DATA_ROOT)
    try:
        usage = shutil.disk_usage(root)
        writable = root.is_dir() and os.access(root, os.W_OK)
    except OSError:
        writable = False
        usage = None

    free_text = ""
    if usage is not None:
        free_gib = usage.free / (1024**3)
        free_text = f" {free_gib:.1f} GiB free."

    return ComponentStatus(
        key="data_root",
        label="Local data storage",
        state="ready" if writable else "error",
        summary="Writable" if writable else "Unavailable",
        detail=f"Data root: {root}.{free_text}",
    )


def _credential_store_status() -> ComponentStatus:
    try:
        available = get_secret_store().is_available()
    except Exception:
        available = False
    return ComponentStatus(
        key="credential_store",
        label="Credential store",
        state="ready" if available else "attention",
        summary="Available" if available else "Unavailable",
        detail=(
            "The operating-system credential store can be used."
            if available
            else "Use a supported credential store or a complete environment profile."
        ),
    )


def _confluence_status() -> ComponentStatus:
    summary = get_configuration_summary()
    if summary.complete:
        state = "ready"
    elif summary.state == "configuration_error":
        state = "error"
    else:
        state = "attention"
    return ComponentStatus(
        key="confluence",
        label="Confluence",
        state=state,
        summary=summary.label,
        detail=summary.detail,
    )


def get_system_status() -> dict[str, object]:
    components = [
        _database_status(),
        _fts_status(),
        _data_root_status(),
        _credential_store_status(),
        _confluence_status(),
        ComponentStatus(
            key="worker",
            label="Background worker",
            state="planned",
            summary="Not required yet",
            detail="The durable worker is introduced with refresh and indexing phases.",
        ),
    ]
    blocking = {"database", "fts5", "data_root"}
    overall = "ready"
    if any(item.state == "error" and item.key in blocking for item in components):
        overall = "error"
    elif any(item.state in {"attention", "error"} for item in components):
        overall = "attention"

    return {
        "overall_state": overall,
        "generated_at": timezone.now(),
        "components": [asdict(component) for component in components],
    }
