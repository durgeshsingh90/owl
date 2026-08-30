from __future__ import annotations

import logging
from logging.handlers import BufferingHandler

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, transaction

from bitbucket_search.services.logging_events import get_logger as get_bitbucket_logger
from bookmark_manager.services import bookmark_query
from bookmark_manager.services.bookmark_application import BookmarkActionError
from bookmark_manager.services.logging_events import (
    get_logger,
    log_event,
    logged_operation,
    logging_context,
)
from core.logging import ProcessSafeRotatingFileHandler, SecretSafeFormatter
from core.logging_events import get_logger as get_application_logger
from owl.settings import _env_log_level


def test_bookmark_namespace_configuration_and_shared_infrastructure():
    for namespace in ("owl.bookmarks", "bookmark_manager"):
        config = settings.LOGGING["loggers"][namespace]
        assert config["level"] == "DEBUG"
        assert config["propagate"] is False
        assert config["handlers"] == ["bookmarks_console", "bookmarks_file", "bookmarks_errors"]
    for name, filename, level in (
        ("bookmarks_file", "bookmarks.log", settings.BOOKMARK_LOG_LEVEL),
        ("bookmarks_errors", "bookmarks-errors.log", "ERROR"),
    ):
        config = settings.LOGGING["handlers"][name]
        assert config["filename"] == settings.LOG_ROOT / filename
        assert config["level"] == level
        assert config["class"] == "core.logging.ProcessSafeRotatingFileHandler"
        assert config["maxBytes"] == settings.OWL_LOG_MAX_BYTES
        assert config["backupCount"] == settings.OWL_LOG_BACKUP_COUNT
    assert get_logger("refresh").name == "owl.bookmarks.refresh"
    assert get_bitbucket_logger("sync").name == "owl.bitbucket.sync"


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_bookmark_log_level_accepts_all_five_levels(monkeypatch, level):
    monkeypatch.setenv("BOOKMARK_LOG_LEVEL", level.lower())
    assert _env_log_level("BOOKMARK_LOG_LEVEL", "DEBUG") == level


def test_bookmark_default_and_invalid_level(monkeypatch):
    monkeypatch.delenv("BOOKMARK_LOG_LEVEL", raising=False)
    assert _env_log_level("BOOKMARK_LOG_LEVEL", "DEBUG") == "DEBUG"
    monkeypatch.setenv("BOOKMARK_LOG_LEVEL", "all")
    with pytest.raises(ImproperlyConfigured):
        _env_log_level("BOOKMARK_LOG_LEVEL", "DEBUG")


@pytest.mark.parametrize(
    "namespace,component", [("unrelated", "refresh"), ("owl.bookmarks", "private value")]
)
def test_shared_logger_rejects_arbitrary_names(namespace, component):
    with pytest.raises(ValueError):
        get_application_logger(component, namespace=namespace)


def test_bookmark_context_and_code_frames_never_include_content():
    logger = logging.Logger("owl.bookmarks.synthetic", logging.DEBUG)
    handler = BufferingHandler(10)
    logger.addHandler(handler)
    namespace = {"__name__": "bookmark_manager.services.synthetic"}
    exec(
        compile(
            "def operation():\n    raise RuntimeError('synthetic private page content')\n",
            "/private/source/synthetic.py",
            "exec",
        ),
        namespace,
    )
    with logging_context(run_id=12, bookmark_id=34, page_id="56", query="private"):
        try:
            namespace["operation"]()
        except RuntimeError as exc:
            log_event(logger, logging.ERROR, "bookmark_operation_failed", error=exc)
    log_event(logger, logging.INFO, "outside_operation")

    error = handler.buffer[0]
    assert error.levelno == logging.ERROR
    assert "run_id=12 bookmark_id=34 page_id=56" in error.getMessage()
    assert "frames=bookmark_manager.services.synthetic.operation:2" in error.getMessage()
    assert "private" not in error.getMessage()
    assert "source" not in error.getMessage()
    assert error.exc_info is None
    assert "run_id=" not in handler.buffer[1].getMessage()


def test_logging_context_preserves_frozen_exception_identity_and_resets(bookmark_events):
    error = BookmarkActionError("not_found", "synthetic private page")
    with logging_context(run_id=12):
        try:
            with logging_context(bookmark_id=34):
                raise error
        except BookmarkActionError as received:
            assert received is error
            assert received.code == "not_found"
        log_event(get_logger("verification"), logging.INFO, "after_frozen_exception")
    log_event(get_logger("verification"), logging.INFO, "after_outer_context")
    records = bookmark_events.records
    assert "run_id=12" in records[0].getMessage()
    assert "bookmark_id=" not in records[0].getMessage()
    assert "run_id=" not in records[1].getMessage()


def test_bookmark_error_file_ignores_diagnostic_threshold(tmp_path):
    logger = logging.Logger("owl.bookmarks.synthetic", logging.DEBUG)
    detailed = ProcessSafeRotatingFileHandler(tmp_path / "bookmarks.log")
    errors = ProcessSafeRotatingFileHandler(tmp_path / "bookmarks-errors.log")
    detailed.setLevel(logging.CRITICAL)
    errors.setLevel(logging.ERROR)
    for handler in (detailed, errors):
        handler.setFormatter(SecretSafeFormatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
    try:
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ):
            log_event(logger, level, f"synthetic_level_{level}")
        content = (tmp_path / "bookmarks-errors.log").read_text()
        assert "ERROR event=synthetic_level_40" in content
        assert "CRITICAL event=synthetic_level_50" in content
        assert "synthetic_level_30" not in content
        assert "synthetic_level_40" not in (tmp_path / "bookmarks.log").read_text()
    finally:
        detailed.close()
        errors.close()


def test_bookmark_and_bitbucket_events_reach_only_their_own_files():
    marker = "synthetic_app_routing_verification"
    log_event(get_logger("verification"), logging.ERROR, marker, bookmark_id=78)
    log_event(get_bitbucket_logger("verification"), logging.ERROR, marker, repository_id=90)
    for filename in ("bookmarks.log", "bookmarks-errors.log"):
        content = (settings.LOG_ROOT / filename).read_text()
        assert f"event={marker} bookmark_id=78" in content
        assert f"event={marker} repository_id=90" not in content
    for filename in ("bitbucket.log", "bitbucket-errors.log"):
        content = (settings.LOG_ROOT / filename).read_text()
        assert f"event={marker} repository_id=90" in content
        assert f"event={marker} bookmark_id=78" not in content


@pytest.fixture
def bookmark_events(caplog):
    logger = logging.getLogger("owl.bookmarks")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger=logger.name)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.mark.django_db
def test_search_logs_only_counts_and_timing(bookmark_events):
    bookmark_query.query_bookmarks(
        bookmark_query.BookmarkQuery(search="synthetic private search", people=("private person",))
    )
    assert "event=bookmark_search_started" in bookmark_events.text
    assert (
        "event=bookmark_search_completed result_count=0 filter_count=2 elapsed_ms="
        in bookmark_events.text
    )
    assert "private" not in bookmark_events.text
    assert all(record.levelno == logging.DEBUG for record in bookmark_events.records)


def test_search_logs_database_failure_without_sql_or_input(bookmark_events, monkeypatch):
    def fail(*args, **kwargs):
        raise OperationalError("synthetic private SQL and query")

    monkeypatch.setattr(bookmark_query, "_query_bookmarks", fail)
    with pytest.raises(OperationalError):
        bookmark_query.query_bookmarks()
    errors = [record for record in bookmark_events.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "event=bookmark_search_failed" in errors[0].getMessage()
    assert "error_type=OperationalError" in errors[0].getMessage()
    assert "private" not in bookmark_events.text


def test_invalid_search_uses_warning_without_private_value(bookmark_events):
    with pytest.raises(bookmark_query.InvalidBookmarkQuery):
        bookmark_query.query_bookmarks("synthetic private query")
    assert "event=bookmark_search_rejected" in bookmark_events.text
    assert "private" not in bookmark_events.text
    assert any(record.levelno == logging.WARNING for record in bookmark_events.records)
    assert not any(record.levelno >= logging.ERROR for record in bookmark_events.records)


@pytest.mark.django_db
def test_local_operation_completion_waits_for_commit_and_preserves_ids(
    bookmark_events, django_capture_on_commit_callbacks
):
    @logged_operation("synthetic_local_save")
    def save():
        return 42

    with django_capture_on_commit_callbacks(execute=True):
        with logging_context(bookmark_id=87):
            assert save() == 42
        assert "event=bookmark_operation_completed" not in bookmark_events.text
    assert (
        "event=bookmark_operation_completed bookmark_id=87 operation=synthetic_local_save"
        in bookmark_events.text
    )


@pytest.mark.django_db
def test_rolled_back_local_operation_does_not_log_completion(
    bookmark_events, django_capture_on_commit_callbacks
):
    @logged_operation("synthetic_rollback")
    def save():
        return None

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        save()
        raise RuntimeError("synthetic rollback")
    assert "event=bookmark_operation_started" in bookmark_events.text
    assert "event=bookmark_operation_completed" not in bookmark_events.text


def test_quiet_service_logs_only_failures(bookmark_events):
    @logged_operation("synthetic_read", quiet=True)
    def read(*, fail=False):
        if fail:
            raise OperationalError("synthetic private SQL")
        return 42

    assert read() == 42
    assert not bookmark_events.records
    with pytest.raises(OperationalError):
        read(fail=True)
    assert len(bookmark_events.records) == 1
    assert bookmark_events.records[0].levelno == logging.ERROR
    assert "operation=synthetic_read" in bookmark_events.text
    assert "private" not in bookmark_events.text
