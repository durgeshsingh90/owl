"""Cached, aggregate-only statistics for OWL's local database."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.utils import timezone

DATABASE_STATS_CACHE_SECONDS = 15 * 60
DATABASE_STATS_CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class DatabaseStats:
    """A safe dashboard snapshot that never exposes schema names or paths."""

    available: bool
    size_bytes: int
    size_label: str
    table_count: int
    row_count: int
    measured_at: datetime
    detail: str = ""


def _human_size(byte_count: int) -> str:
    value = float(max(0, byte_count))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def _is_memory_database(connection: BaseDatabaseWrapper) -> bool:
    database_name = str(connection.settings_dict.get("NAME", ""))
    return database_name == ":memory:" or database_name.startswith("file:memorydb_")


def _cache_key(connection: BaseDatabaseWrapper) -> str:
    database_identity = (
        f"{connection.alias}\0{connection.vendor}\0{connection.settings_dict.get('NAME', '')}"
    )
    digest = hashlib.sha256(database_identity.encode("utf-8")).hexdigest()[:20]
    return f"owl:database-stats:v{DATABASE_STATS_CACHE_VERSION}:{digest}"


def _sqlite_physical_size(database_name: object) -> int:
    """Return allocated SQLite and WAL bytes without including transient shared memory."""

    candidate = str(database_name or "")
    if not candidate or candidate == ":memory:" or candidate.startswith("file:"):
        return 0
    database_path = Path(candidate)
    total = 0
    for path in (database_path, Path(f"{database_path}-wal")):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _sqlite_allocated_size(
    connection: BaseDatabaseWrapper,
    cursor,
) -> int:
    if connection.vendor != "sqlite":
        return 0
    cursor.execute("PRAGMA page_count")
    page_count = int(cursor.fetchone()[0])
    cursor.execute("PRAGMA page_size")
    page_size = int(cursor.fetchone()[0])
    return max(0, page_count * page_size)


def _collect_database_stats(
    connection: BaseDatabaseWrapper,
    *,
    measured_at: datetime,
) -> DatabaseStats:
    with connection.cursor() as cursor:
        tables = tuple(connection.introspection.table_names(cursor, include_views=False))
        row_count = 0
        for table_name in tables:
            quoted_name = connection.ops.quote_name(table_name)
            cursor.execute(f"SELECT COUNT(*) FROM {quoted_name}")
            result = cursor.fetchone()
            row_count += int(result[0]) if result is not None else 0

        allocated_size = _sqlite_allocated_size(connection, cursor)

    physical_size = (
        _sqlite_physical_size(connection.settings_dict.get("NAME"))
        if connection.vendor == "sqlite"
        else 0
    )
    size_bytes = max(physical_size, allocated_size)
    return DatabaseStats(
        available=True,
        size_bytes=size_bytes,
        size_label=_human_size(size_bytes),
        table_count=len(tables),
        row_count=row_count,
        measured_at=measured_at,
    )


def get_database_stats(
    *,
    alias: str = DEFAULT_DB_ALIAS,
    force_refresh: bool = False,
    at: datetime | None = None,
) -> DatabaseStats:
    """Return a short-lived aggregate snapshot without exposing database internals."""

    measured_at = at or timezone.now()
    if timezone.is_naive(measured_at):
        raise ValueError("Database statistics timestamps must include a timezone.")

    connection = connections[alias]
    cache_enabled = not _is_memory_database(connection)
    cache_key = _cache_key(connection)
    if cache_enabled and not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, DatabaseStats):
            return cached

    try:
        snapshot = _collect_database_stats(connection, measured_at=measured_at)
    except (DatabaseError, OSError, TypeError, ValueError):
        return DatabaseStats(
            available=False,
            size_bytes=0,
            size_label="Unavailable",
            table_count=0,
            row_count=0,
            measured_at=measured_at,
            detail="OWL could not measure the local database right now.",
        )

    if cache_enabled:
        cache.set(cache_key, snapshot, DATABASE_STATS_CACHE_SECONDS)
    return snapshot
