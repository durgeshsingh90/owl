from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import connection

from bitbucket_search.services import pdf_indexing
from bookmark_manager.management.commands import run_owl

pytestmark = pytest.mark.django_db


def test_configured_sqlite_busy_timeout_is_applied(settings):
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        busy_timeout_ms = cursor.fetchone()[0]

    assert busy_timeout_ms == settings.DATABASES["default"]["OPTIONS"]["timeout"] * 1_000


def test_parser_closes_worker_connections_before_the_fork_exec_boundary(
    tmp_path,
    settings,
    monkeypatch,
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "parser-stage"
    events: list[str] = []
    monkeypatch.setattr(
        pdf_indexing,
        "connection",
        SimpleNamespace(in_atomic_block=False),
    )
    monkeypatch.setattr(
        pdf_indexing.connections,
        "close_all",
        lambda: events.append("connections_closed"),
    )

    def failed_spawn(*_args, **_kwargs):
        events.append("spawn_attempted")
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(pdf_indexing.subprocess, "Popen", failed_spawn)

    with pytest.raises(pdf_indexing.PDFIndexingError, match="isolated PDF extractor"):
        pdf_indexing.run_isolated_pdf_extractor(tmp_path / "document.pdf", Mock())

    assert events == ["connections_closed", "spawn_attempted"]


def test_supervisor_closes_its_connection_before_spawning_database_workers(
    monkeypatch,
):
    events: list[str] = []
    process = Mock(pid=90210)
    monkeypatch.setattr(run_owl, "connection", SimpleNamespace(in_atomic_block=False))
    monkeypatch.setattr(
        run_owl.connections,
        "close_all",
        lambda: events.append("connections_closed"),
    )

    def spawn(*_args, **_kwargs):
        events.append("spawn_attempted")
        return process

    monkeypatch.setattr(run_owl.subprocess, "Popen", spawn)

    assert run_owl._launch_resident_bitbucket_worker("bitbucket_pdf_writer") is process
    assert events == ["connections_closed", "spawn_attempted"]
