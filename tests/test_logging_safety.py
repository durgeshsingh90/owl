import logging

from django.conf import settings
from django.test import override_settings

from core.logging import (
    REDACTED,
    ExpectedLoopbackDisconnectFilter,
    SecretSafeFormatter,
    redact_log_text,
)


@override_settings(CONFLUENCE_PAT="synthetic-log-pat-never-valid-91c73e2b")
def test_log_formatter_redacts_environment_pat_and_authorization_header():
    marker = settings.CONFLUENCE_PAT
    record = logging.LogRecord(
        name="owl.synthetic",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=f"failure pat={marker} Authorization: Bearer {marker}",
        args=(),
        exc_info=None,
    )

    rendered = SecretSafeFormatter("{levelname} {message}", style="{").format(record)

    assert marker not in rendered
    assert REDACTED in rendered


def test_logging_is_local_rotating_and_upload_limits_are_bounded():
    handler = settings.LOGGING["handlers"]["local_file"]

    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert handler["filename"] == settings.LOG_ROOT / "owl.log"
    assert handler["maxBytes"] == settings.OWL_LOG_MAX_BYTES
    assert handler["backupCount"] == settings.OWL_LOG_BACKUP_COUNT
    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE >= 1_024
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE >= 1_024
    assert redact_log_text("ordinary status") == "ordinary status"


def test_expected_loopback_disconnect_filter_suppresses_only_harmless_broken_pipes():
    expected = ExpectedLoopbackDisconnectFilter()
    loopback = logging.LogRecord(
        "django.server", logging.INFO, "", 0, "- Broken pipe from %s", (("127.0.0.1", 54321),), None
    )
    remote = logging.LogRecord(
        "django.server",
        logging.INFO,
        "",
        0,
        "- Broken pipe from %s",
        (("203.0.113.8", 54321),),
        None,
    )
    application_error = logging.LogRecord(
        "owl.bitbucket.indexing",
        logging.ERROR,
        "",
        0,
        "Broken pipe while writing an owned worker stream",
        (),
        None,
    )

    assert expected.filter(loopback) is False
    assert expected.filter(remote) is True
    assert expected.filter(application_error) is True
