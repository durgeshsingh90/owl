"""Local log formatting that redacts credential-shaped values."""

from __future__ import annotations

import logging
import re

from django.conf import settings

REDACTED = "[REDACTED]"
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s,;]+"),
    re.compile(
        r"(?i)((?:pat|token|password|passwd|secret|api[_-]?key)\s*[:=]\s*)"
        r"[^\s,;]+"
    ),
)


def redact_log_text(value: str) -> str:
    redacted = value
    environment_pat = getattr(settings, "CONFLUENCE_PAT", "")
    if environment_pat:
        redacted = redacted.replace(str(environment_pat), REDACTED)
    for pattern in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(rf"\1{REDACTED}", redacted)
    return redacted


class SecretSafeFormatter(logging.Formatter):
    """Format a complete record first, then redact its message and traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))
