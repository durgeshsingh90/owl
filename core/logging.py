"""Local log formatting that redacts credential-shaped values."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from django.conf import settings

REDACTED = "[REDACTED]"
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@]+@")
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s,;]+"),
    re.compile(
        r"(?i)((?:pat|token|password|passwd|secret|api[_-]?key)\s*[:=]\s*)"
        r"[^\s,;]+"
    ),
    re.compile(
        r"(?i)([?&](?:access_token|refresh_token|token|password|passwd|secret|api[_-]?key)=)"
        r"[^\s&#]+"
    ),
)


def redact_log_text(value: str) -> str:
    redacted = value
    for setting_name in ("CONFLUENCE_PAT", "SECRET_KEY"):
        secret = getattr(settings, setting_name, "")
        if secret:
            redacted = redacted.replace(str(secret), REDACTED)
    # Keep a valid credential-free URL: import diagnostics also reuse this
    # sanitizer before parsing and storing their safe source URL.
    redacted = _URL_USERINFO.sub(r"\1", redacted)
    for pattern in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(rf"\1{REDACTED}", redacted)
    return redacted


class SecretSafeFormatter(logging.Formatter):
    """Format a complete record first, then redact its message and traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class ProcessSafeRotatingFileHandler(RotatingFileHandler):
    """Serialize writes/rotation across OWL's independent worker processes.

    Open log files only while holding a cross-process sidecar lock. Closing them
    after each event also lets Windows rotate files opened by different workers.
    Logging I/O failures never leak the record or interrupt application work.
    """

    def __init__(
        self, filename, mode="a", maxBytes=0, backupCount=0, encoding=None, delay=True, errors=None
    ):
        # All writers must open only inside emit's process lock, even when a
        # caller leaves the standard handler's delay argument at its default.
        super().__init__(filename, mode, maxBytes, backupCount, encoding, True, errors)

    def _lock(self, handle) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _unlock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _open(self):
        # RotatingFileHandler calls this only under our sidecar lock.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            os.chmod(self.baseFilename, 0o600)
            return os.fdopen(descriptor, "a", encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(descriptor)
            raise

    def emit(self, record: logging.LogRecord) -> None:
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(f"{self.baseFilename}.lock", flags, 0o600)
            with os.fdopen(descriptor, "r+b") as lock_file:
                # Windows permits locking past EOF. Never initialize a byte
                # before locking: another writer may already own that region.
                self._lock(lock_file)
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
                    self._unlock(lock_file)
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # logging's default handler prints the original record and traceback,
        # which could expose sensitive data when a formatter/write fails.
        with suppress(Exception):
            sys.stderr.write("ERROR OWL could not write a local log record; check log storage.\n")
