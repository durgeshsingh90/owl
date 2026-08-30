from __future__ import annotations

import errno
import logging
import os
import sqlite3
import subprocess
import sys
from logging.handlers import BufferingHandler
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from bitbucket_search.services.logging_events import get_logger, log_event, logging_context
from core import logging as core_logging
from core.logging import ProcessSafeRotatingFileHandler, SecretSafeFormatter, redact_log_text
from owl.settings import _env_log_level


def recorder():
    logger = logging.Logger("owl.bitbucket.synthetic", logging.DEBUG)
    handler = BufferingHandler(100)
    logger.addHandler(handler)
    return logger, handler.buffer


@pytest.mark.parametrize(
    "level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
)
def test_all_standard_event_levels_and_safe_context(level):
    logger, records = recorder()

    log_event(
        logger,
        level,
        "synthetic_event",
        repository_id=12,
        job_id=34,
        document_id=56,
        policy_id=78,
        phase="extracting",
        elapsed_ms=4.125,
        remote_url="https://example.invalid/synthetic-repo.git",
        filename="private",
        query="private",
        text="private",
    )

    assert len(records) == 1
    assert records[0].levelno == level
    message = records[0].getMessage()
    assert "repository_id=12 job_id=34 document_id=56" in message
    assert "policy_id=78" in message
    assert "phase=extracting elapsed_ms=4.125" in message
    assert "private" not in message
    assert "remote_url=" not in message


def test_invalid_values_and_exception_contents_are_not_persisted():
    logger, records = recorder()
    private = "synthetic-private-pdf-text-never-log-this"
    error = PermissionError(errno.EACCES, private, "/private/source.pdf")
    log_event(
        logger,
        logging.ERROR,
        "read_failed",
        error=error,
        error_code="read_failed",
        reason=f"{private}\nforged event",
        byte_count=float("nan"),
    )

    message = records[0].getMessage()
    assert "error_type=PermissionError errno=13" in message
    assert private not in message
    assert "/private" not in message
    assert "reason=" not in message
    assert "nan" not in message
    assert records[0].exc_info is None


def test_code_frames_without_source_paths_locals_or_exception_messages():
    logger, records = recorder()
    namespace = {"__name__": "bitbucket_search.synthetic"}
    exec(
        compile(
            "def operation():\n    raise RuntimeError('private parser contents')\n",
            "/private/checkout/secret.py",
            "exec",
        ),
        namespace,
    )
    try:
        namespace["operation"]()
    except RuntimeError as error:
        log_event(logger, logging.ERROR, "parser_failed", error=error)

    message = records[0].getMessage()
    assert "frames=bitbucket_search.synthetic.operation:2" in message
    assert "private" not in message
    assert "secret.py" not in message
    assert "raise RuntimeError" not in message


def test_sqlite_codes_and_unknown_exception_names_are_safe():
    logger, records = recorder()
    error = sqlite3.OperationalError("private SQL")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    log_event(logger, logging.ERROR, "database_failed", error=error)
    custom = type("PrivateDocumentContents", (RuntimeError,), {"__module__": "synthetic_test"})
    log_event(logger, logging.ERROR, "custom_failed", error=custom("private message"))

    assert "sqlite_code=5" in records[0].getMessage()
    assert "error_type=RuntimeError" in records[1].getMessage()
    assert "PrivateDocumentContents" not in records[1].getMessage()


def test_nested_operation_context_is_reset_and_explicit_ids_win():
    logger, records = recorder()
    with logging_context(
        repository_id=7, job_id=8, remote_url="https://example.invalid/synthetic-repo.git"
    ):
        with logging_context(document_id=9):
            log_event(logger, logging.DEBUG, "nested", job_id=10)
        log_event(logger, logging.INFO, "outer")
    log_event(logger, logging.INFO, "outside")

    assert "repository_id=7 job_id=10 document_id=9" in records[0].getMessage()
    assert "document_id=" not in records[1].getMessage()
    assert "repository_id=" not in records[2].getMessage()
    assert "private" not in " ".join(record.getMessage() for record in records)
    assert "remote_url=" not in " ".join(record.getMessage() for record in records)


def test_formatter_redacts_repository_url_credentials_and_query_secrets():
    username = "synthetic-log-user"
    credential = "synthetic-log-credential-never-valid"
    remote = f"https://{username}:{credential}@example.invalid/docs?access_token={credential}"

    rendered = redact_log_text(remote)

    assert username not in rendered
    assert credential not in rendered
    assert "[REDACTED]" in rendered


def test_credential_redaction_preserves_a_valid_source_url():
    username = "synthetic-log-user"
    credential = "not-a-real-secret"
    remote = f"https://{username}:{credential}@example.invalid/docs"

    assert redact_log_text(remote) == "https://example.invalid/docs"


def test_logging_config_routes_both_namespaces_to_dedicated_files():
    assert settings.BITBUCKET_LOG_LEVEL == "DEBUG"
    for namespace in ("owl.bitbucket", "bitbucket_search"):
        config = settings.LOGGING["loggers"][namespace]
        assert config["level"] == "DEBUG"
        assert config["propagate"] is False
        assert config["handlers"].count("bitbucket_errors") == 1
    for name, filename, level in (
        ("bitbucket_file", "bitbucket.log", "DEBUG"),
        ("bitbucket_errors", "bitbucket-errors.log", "ERROR"),
    ):
        config = settings.LOGGING["handlers"][name]
        assert config["filename"] == settings.LOG_ROOT / filename
        assert config["level"] == level
        assert config["class"] == "core.logging.ProcessSafeRotatingFileHandler"
        assert config["maxBytes"] == settings.OWL_LOG_MAX_BYTES
        assert config["backupCount"] == settings.OWL_LOG_BACKUP_COUNT
    assert get_logger("sync").name == "owl.bitbucket.sync"


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_bitbucket_level_accepts_all_standard_thresholds(monkeypatch, level):
    monkeypatch.setenv("BITBUCKET_LOG_LEVEL", level.lower())
    assert _env_log_level("BITBUCKET_LOG_LEVEL", "DEBUG") == level


def test_invalid_bitbucket_level_is_rejected(monkeypatch):
    monkeypatch.setenv("BITBUCKET_LOG_LEVEL", "everything")
    with pytest.raises(ImproperlyConfigured):
        _env_log_level("BITBUCKET_LOG_LEVEL", "DEBUG")


def test_error_file_keeps_errors_even_with_critical_diagnostic_threshold(tmp_path):
    logger = logging.Logger("owl.bitbucket.synthetic", logging.DEBUG)
    detailed = ProcessSafeRotatingFileHandler(tmp_path / "all.log", maxBytes=1024, backupCount=2)
    errors = ProcessSafeRotatingFileHandler(tmp_path / "errors.log", maxBytes=1024, backupCount=2)
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
            log_event(logger, level, f"level_{level}")
        assert "level_40" in (tmp_path / "errors.log").read_text()
        assert "level_50" in (tmp_path / "errors.log").read_text()
        assert "level_30" not in (tmp_path / "errors.log").read_text()
        assert "level_40" not in (tmp_path / "all.log").read_text()
        assert errors.stream is None
    finally:
        detailed.close()
        errors.close()


def test_write_failure_does_not_expose_record_or_interrupt_work(tmp_path, capsys):
    handler = ProcessSafeRotatingFileHandler(tmp_path / "missing" / "events.log")
    record = logging.LogRecord(
        "owl.bitbucket.synthetic", logging.ERROR, __file__, 1, "private PDF contents", (), None
    )
    handler.emit(record)
    output = capsys.readouterr().err
    assert "check log storage" in output
    assert "private PDF contents" not in output
    assert "Traceback" not in output


def test_empty_lock_sidecar_is_not_written_before_acquiring_lock(tmp_path):
    handler = ProcessSafeRotatingFileHandler(tmp_path / "events.log")
    record = logging.LogRecord(
        "owl.bitbucket.synthetic", logging.INFO, __file__, 1, "synthetic", (), None
    )
    try:
        handler.emit(record)
        assert (tmp_path / "events.log").read_text() == "synthetic\n"
        assert (tmp_path / "events.log.lock").read_bytes() == b""
    finally:
        handler.close()


def test_windows_lock_uses_first_byte_range_without_initializing_file(tmp_path, monkeypatch):
    handler = ProcessSafeRotatingFileHandler(tmp_path / "events.log")
    calls = []
    handle = SimpleNamespace(
        seek=lambda position: calls.append(("seek", position)), fileno=lambda: 7
    )
    windows = SimpleNamespace(
        LK_NBLCK=2,
        LK_UNLCK=0,
        locking=lambda descriptor, mode, size: calls.append((descriptor, mode, size)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", windows)
    # Scope the platform override to this module, not Python/pathlib globally.
    monkeypatch.setattr(core_logging, "os", SimpleNamespace(name="nt"))
    try:
        handler._lock(handle)
        handler._unlock(handle)
        assert calls == [("seek", 0), (7, 2, 1), ("seek", 0), (7, 0, 1)]
    finally:
        handler.close()


def test_parallel_worker_writes_and_rotation_keep_every_record(tmp_path):
    path = tmp_path / "parallel.log"
    program = """
import logging, sys
from core.logging import ProcessSafeRotatingFileHandler
handler = ProcessSafeRotatingFileHandler(sys.argv[1], maxBytes=512, backupCount=50, encoding='utf-8')
logger = logging.Logger('synthetic', logging.DEBUG)
logger.addHandler(handler)
for number in range(50):
    logger.info('worker=%s event=%s padding=synthetic-verification-only', sys.argv[2], number)
handler.close()
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(path), str(worker)],
            cwd=settings.BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for worker in range(4)
    ]
    for worker in workers:
        _out, error = worker.communicate(timeout=30)
        assert worker.returncode == 0, error.decode()
        assert not error
    files = [item for item in tmp_path.glob("parallel.log*") if not item.name.endswith(".lock")]
    lines = [line for item in files for line in item.read_text().splitlines()]
    assert len(lines) == 200
    assert len(set(lines)) == 200
    assert {
        f"worker={worker} event={number} padding=synthetic-verification-only"
        for worker in range(4)
        for number in range(50)
    } == set(lines)
    assert len(files) > 1
    if os.name != "nt":
        assert all(item.stat().st_mode & 0o777 == 0o600 for item in files)
