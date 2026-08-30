"""Bounded, credential-redacted Git output for the local per-repository log view.

This is separate from content-free application diagnostics. Never send raw Git
output to the Python logger, and never store successful metadata-command output.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from contextvars import ContextVar

from django.db import DatabaseError
from django.utils import timezone

from bitbucket_search.models import RepositorySyncJob, RepositorySyncJobStatus
from bitbucket_search.services.logging_events import get_logger, log_event
from core.logging import REDACTED, redact_log_text

MAX_LOG_CHARACTERS = 32_768
MAX_LOG_LINES = 200
MAX_RAW_LINE = 16_384
MAX_LINE_CHARACTERS = 1_024
_ANSI = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)|"
    r"(?:\x1b[P_X^]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)|"
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b(?![\[\]P_X^])[ -/]*[@-~]",
    re.DOTALL,
)
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_SCP_URL = re.compile(r"[^\s<>\"'@]+@(?:\[[^\]\s]+\]|[^\s<>\"'@:]+):[^\s<>\"']+")
_SENSITIVE_LINE = re.compile(
    r"(?i)(?:authorization|cookie|private[ _-]*key|credential|"
    r"pass[ _-]*(?:word|wd|phrase)|api[ _-]*key|"
    r"(?:access|refresh|session|auth)[ _-]*token|client[ _-]*secret|"
    r"\b(?:pat|token|secret|username)\b|\b(?:basic|bearer)\s+)"
)
_TOKEN_VALUE = re.compile(
    r"(?i)(?<![\w-])(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|ATATT[A-Za-z0-9_=-]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]+|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)(?![\w-])"
)
# Standalone key material can arrive separately from its BEGIN/END marker. It
# is safer to hide an occasional long object identifier than expose such data.
_ENCODED_VALUE = re.compile(r"(?<![\w+/=-])[A-Za-z0-9+/_-]{48,}={0,2}(?![\w+/=-])")
_KEY_BEGIN = re.compile(r"(?i)-{2,}BEGIN [^-\r\n]*PRIVATE KEY[^-\r\n]*-{2,}")
_KEY_END = re.compile(r"(?i)-{2,}END [^-\r\n]*PRIVATE KEY[^-\r\n]*-{2,}")
_CREDENTIAL_ONLY_LABEL = re.compile(
    r"(?i)(?:authorization|cookie|credential|private[ _-]*key|password|passwd|passphrase|"
    r"api[ _-]*key|(?:access|refresh|session|auth)[ _-]*token|"
    r"client[ _-]*secret|pat|token|secret|username)[\"']?\s*[:=]\s*$"
)
_HIDDEN_CREDENTIAL = "[Credential-related Git output hidden]"
_OMITTED_LINE = "[Git output line omitted: too long]"
_SINK: ContextVar[RepositoryGitLog | None] = ContextVar("repository_git_output", default=None)
logger = get_logger("git_output")


def sanitize_git_output(text: str) -> str:
    """Sanitize complete transport-output lines before buffering or displaying."""

    # Only CR/LF delimit progress lines. Python's splitlines also splits at
    # vertical tabs and C1 controls, which could split a credential marker before
    # it is recognized. Drop overlong raw lines rather than expose their suffix.
    raw_lines = re.split(r"\r\n|[\r\n]", str(text))
    raw_text = "\n".join(
        raw_line if len(raw_line) <= MAX_RAW_LINE else _OMITTED_LINE for raw_line in raw_lines
    )
    raw_text = _ANSI.sub("", raw_text)
    cleaned = []
    private_key = False
    credential_value_next = False
    for raw_line in raw_text.split("\n"):
        # Remove terminal/control tricks before matching credential markers.
        line = "".join(
            c
            for c in unicodedata.normalize("NFKC", raw_line)
            if unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        )
        if not line.strip():
            continue
        has_key_begin = _KEY_BEGIN.search(line) is not None
        has_key_end = _KEY_END.search(line) is not None
        hide_continuation = credential_value_next
        credential_value_next = _CREDENTIAL_ONLY_LABEL.search(line) is not None
        if private_key or has_key_begin or has_key_end:
            private_key = not has_key_end
            line = _HIDDEN_CREDENTIAL
        elif hide_continuation or _SENSITIVE_LINE.search(line):
            line = _HIDDEN_CREDENTIAL
        elif "\x08" in raw_line or "\x1b" in raw_line:
            # Backspace/unfinished escape sequences can alter what a user sees;
            # do not attempt to interpret them as a terminal emulator would.
            line = "[Terminal-control Git output hidden]"
        else:
            line = redact_log_text(line)
            # Even unexpected redirects/remote errors must not disclose URL
            # userinfo, query strings or fragments. The repository is named in UI.
            line = _URL.sub("[remote URL]", line)
            line = _SCP_URL.sub("[remote URL]", line)
            line = _TOKEN_VALUE.sub(REDACTED, line)
            line = _ENCODED_VALUE.sub(REDACTED, line)
        if line.strip():
            cleaned.append(line[:MAX_LINE_CHARACTERS])
    return "\n".join(cleaned)


def bounded_git_output(text: str) -> tuple[str, bool]:
    """Defense in depth for older/manually edited database log values."""

    lines = sanitize_git_output(text).splitlines()
    truncated = len(lines) > MAX_LOG_LINES
    lines = lines[-MAX_LOG_LINES:]
    while lines and len("\n".join(lines)) > MAX_LOG_CHARACTERS:
        lines.pop(0)
        truncated = True
    return "\n".join(lines), truncated


def emit_git_output(text: str, *, operation: str = "", level: str = "info") -> None:
    """Emit only to the current job; direct Git calls do not create log storage."""

    sink = _SINK.get()
    if sink is not None:
        sink.append(text, operation=operation, level=level)


def flush_git_output() -> None:
    """Let silent-command heartbeats publish the last buffered output too."""

    sink = _SINK.get()
    if sink is not None:
        sink.flush()


class RepositoryGitLog:
    """One worker-owned tail with throttled writes, isolated from other jobs."""

    def __init__(self, job: RepositorySyncJob):
        self.job = job
        safe, clipped = bounded_git_output(job.output_log)
        self.lines = deque(safe.splitlines())
        self.truncated = job.output_log_truncated or clipped
        self.last_saved_at = 0.0
        self.dirty = False
        self.private_key_block = False
        self.hide_next_line = False
        self.control_string: str | None = None
        self.control_string_escape = False
        self.pending_escape = False

    def __enter__(self):
        self.token = _SINK.set(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.flush(force=True)
        finally:
            _SINK.reset(self.token)
        return False

    def _without_control_strings(self, text: str) -> str:
        """Discard OSC/DCS payloads across calls without retaining their bytes.

        The Git transport reader emits complete CR/LF-delimited lines, but a
        terminal control string can span those lines. Keep only its line breaks
        until the proper terminator; ordinary ANSI formatting still goes through
        the complete-line sanitizer. A terminator split between calls is safe.
        """

        visible = []
        c1_starts = {
            "\x9d": "osc",
            "\x90": "other",
            "\x98": "other",
            "\x9e": "other",
            "\x9f": "other",
        }
        for character in text:
            if self.control_string is not None:
                if (
                    character == "\x9c"
                    or (self.control_string == "osc" and character == "\x07")
                    or (self.control_string_escape and character == "\\")
                ):
                    self.control_string = None
                    self.control_string_escape = False
                else:
                    self.control_string_escape = character == "\x1b"
                    if character in "\r\n":
                        visible.append(character)
                continue
            if character in c1_starts:
                if self.pending_escape:
                    visible.append("\x1b")
                    self.pending_escape = False
                self.control_string = c1_starts[character]
            elif self.pending_escape:
                if character in "]P_X^":
                    self.control_string = "osc" if character == "]" else "other"
                    self.pending_escape = False
                elif character == "\x1b":
                    visible.append("\x1b")
                else:
                    visible.extend(("\x1b", character))
                    self.pending_escape = False
            elif character == "\x1b":
                self.pending_escape = True
            else:
                visible.append(character)
        return "".join(visible)

    def append(self, text: str, *, operation: str = "", level: str = "info") -> None:
        text = self._without_control_strings(str(text))
        visible_lines = []
        for raw_line in re.split(r"\r\n|[\r\n]", text):
            plain = "".join(
                c
                for c in unicodedata.normalize("NFKC", _ANSI.sub("", raw_line))
                if unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            )
            if not plain.strip():
                continue
            if _KEY_BEGIN.search(plain):
                self.private_key_block = _KEY_END.search(plain) is None
                visible_lines.append(_HIDDEN_CREDENTIAL)
            elif self.private_key_block:
                if _KEY_END.search(plain):
                    self.private_key_block = False
            elif self.hide_next_line:
                self.hide_next_line = _CREDENTIAL_ONLY_LABEL.search(plain) is not None
                visible_lines.append("[Credential value hidden]")
            else:
                visible_lines.append(raw_line)
                if _CREDENTIAL_ONLY_LABEL.search(plain):
                    self.hide_next_line = True
        safe = sanitize_git_output("\n".join(visible_lines))
        if not safe:
            return
        label = (
            operation
            if operation
            in {"clone", "fetch", "merge", "checkout", "sparse-checkout", "lfs", "connection"}
            else "worker"
        )
        severity = {"error": "ERROR", "warning": "WARNING", "debug": "DEBUG"}.get(level, "INFO")
        stamp = timezone.now().strftime("%H:%M:%S UTC")
        for line in safe.splitlines():
            self.lines.append(f"{stamp} {severity} [{label}] {line}")
        while len(self.lines) > MAX_LOG_LINES or len("\n".join(self.lines)) > MAX_LOG_CHARACTERS:
            self.lines.popleft()
            self.truncated = True
        self.dirty = True
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        observed = time.monotonic()
        if not self.dirty or (not force and observed - self.last_saved_at < 1.0):
            return
        self.last_saved_at = observed
        try:
            # A stale worker cannot overwrite a newer worker's log/lease. A
            # terminal transition by this worker still needs its final tail.
            updated = RepositorySyncJob.objects.filter(
                pk=self.job.pk,
                worker_pid=self.job.worker_pid,
                status__in=(
                    RepositorySyncJobStatus.RUNNING,
                    RepositorySyncJobStatus.SUCCEEDED,
                    RepositorySyncJobStatus.FAILED,
                    RepositorySyncJobStatus.INTERRUPTED,
                ),
            ).update(
                output_log="\n".join(self.lines),
                output_log_truncated=self.truncated,
                output_log_updated_at=timezone.now(),
            )
            if updated:
                self.dirty = False
        except DatabaseError as exc:
            # Losing optional console output must not fail a clone or hide its
            # primary error. Retain the bounded tail for the next flush attempt.
            log_event(
                logger, logging.ERROR, "git_output_save_failed", error=exc, job_id=self.job.pk
            )
