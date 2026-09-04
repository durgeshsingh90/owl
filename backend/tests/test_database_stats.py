from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from django.db import connection

from bookmark_manager.models import BookmarkFolder
from core.services import database_stats

pytestmark = pytest.mark.django_db


def test_database_stats_count_tables_and_rows_without_exposing_internal_names():
    before = database_stats.get_database_stats(force_refresh=True)
    BookmarkFolder.objects.create(name="Database statistics fixture")
    after = database_stats.get_database_stats(force_refresh=True)

    assert before.available is True
    assert after.available is True
    assert after.size_bytes > 0
    assert after.size_label != "Unavailable"
    assert after.table_count > 0
    assert after.table_count == before.table_count
    assert after.row_count == before.row_count + 1
    assert "bookmark_manager" not in repr(after)
    assert str(connection.settings_dict["NAME"]) not in repr(after)


def test_database_stats_include_sqlite_wal_in_physical_size(tmp_path):
    database_path = tmp_path / "owl.sqlite3"
    database_path.write_bytes(b"database")
    (tmp_path / "owl.sqlite3-wal").write_bytes(b"pending")
    (tmp_path / "owl.sqlite3-shm").write_bytes(b"transient")

    assert database_stats._sqlite_physical_size(database_path) == len(b"databasepending")


def test_database_stats_cache_is_stable_until_forced(monkeypatch):
    cache.clear()
    monkeypatch.setattr(database_stats, "_is_memory_database", lambda _connection: False)
    first_time = datetime(2026, 8, 29, 9, tzinfo=UTC)
    second_time = datetime(2026, 8, 29, 10, tzinfo=UTC)

    first = database_stats.get_database_stats(force_refresh=True, at=first_time)
    BookmarkFolder.objects.create(name="Created after cached snapshot")
    cached = database_stats.get_database_stats(at=second_time)
    refreshed = database_stats.get_database_stats(force_refresh=True, at=second_time)

    assert cached == first
    assert refreshed.measured_at == second_time
    assert refreshed.row_count == first.row_count + 1


def test_database_stats_failure_is_redacted_and_does_not_break_dashboard(monkeypatch):
    def fail_collection(*_args, **_kwargs):
        raise OSError("sensitive synthetic path")

    monkeypatch.setattr(database_stats, "_collect_database_stats", fail_collection)

    result = database_stats.get_database_stats(
        force_refresh=True,
        at=datetime(2026, 8, 29, 11, tzinfo=UTC),
    )

    assert result.available is False
    assert result.size_label == "Unavailable"
    assert result.table_count == 0
    assert result.row_count == 0
    assert "sensitive synthetic path" not in result.detail


def test_database_stats_reject_naive_measurement_time():
    with pytest.raises(ValueError, match="include a timezone"):
        database_stats.get_database_stats(at=datetime(2026, 8, 29, 11))
