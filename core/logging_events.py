"""Content-free diagnostic events shared by OWL's local applications."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from contextvars import ContextVar

_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_FIELDS = frozenset(
    [
        "repository_id",
        "source_id",
        "source_type",
        "job_id",
        "document_id",
        "policy_id",
        "run_id",
        "bookmark_id",
        "page_id",
        "folder_id",
        "notification_id",
        "record_number",
        "worker_pid",
        "phase",
        "status",
        "operation",
        "trigger",
        "error_code",
        "stage",
        "reason",
        "count",
        "queued_count",
        "active_count",
        "failed_count",
        "pdf_count",
        "vsdx_count",
        "page_count",
        "byte_count",
        "elapsed_ms",
        "progress",
        "retry_count",
        "retry_number",
        "retries_remaining",
        "delay_seconds",
        "return_code",
        "errno",
        "winerror",
        "indexed_count",
        "embedded_count",
        "chunk_count",
        "removed_count",
        "skipped_count",
        "result_count",
        "limit",
        "worker_count",
        "http_status",
        "attempt",
        "timeout_seconds",
        "processed_count",
        "succeeded_count",
        "imported_count",
        "updated_count",
        "filter_count",
        "redirect_count",
        "saved_count",
    ]
)
_KNOWN_EXCEPTION_MODULES = (
    "builtins",
    "sqlite3",
    "django.db",
    "django.core.exceptions",
    "subprocess",
    "bitbucket_search.services",
    "bookmark_manager.services",
    "semantic_search.services",
)
_CONTEXT: ContextVar[dict[str, object] | None] = ContextVar("owl_log_context", default=None)


class _LoggingContext:
    """Reset correlation scope without modifying a propagating exception."""

    def __init__(self, context: dict[str, object]):
        self.context = context

    def __enter__(self):
        self.token = _CONTEXT.set({**(_CONTEXT.get() or {}), **self.context})

    def __exit__(self, exc_type, exc_value, traceback):
        _CONTEXT.reset(self.token)
        return False


def logging_context(**context: object):
    """Carry operation IDs without leaking scopes or mutating frozen exceptions."""

    return _LoggingContext(context)


def get_logger(component: str, *, namespace: str) -> logging.Logger:
    """Select one application hierarchy without accepting arbitrary logger names."""

    if (
        namespace not in {"owl.bitbucket", "owl.bitbucket_app", "owl.bookmarks", "owl.semantic"}
        or not isinstance(component, str)
        or not _TOKEN.fullmatch(component)
    ):
        raise ValueError("A fixed OWL log component and application namespace are required.")
    return logging.getLogger(f"{namespace}.{component}")


def _exception_category(error: BaseException) -> str:
    kind = type(error)
    if any(
        kind.__module__ == module or kind.__module__.startswith(f"{module}.")
        for module in _KNOWN_EXCEPTION_MODULES
    ) and _TOKEN.fullmatch(kind.__name__):
        return kind.__name__
    # Never persist a dynamically supplied exception class name.
    for category in (
        PermissionError,
        FileNotFoundError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        LookupError,
        ArithmeticError,
        RuntimeError,
    ):
        if isinstance(error, category):
            return category.__name__
    return "Exception"


def _exception_context(error: BaseException) -> list[str]:
    details = [f"error_type={_exception_category(error)}"]
    if isinstance(error, OSError):
        for field in ("errno", "winerror"):
            value = getattr(error, field, None)
            if isinstance(value, int) and not isinstance(value, bool):
                details.append(f"{field}={value}")
    cause = error if isinstance(error, sqlite3.Error) else error.__cause__
    sqlite_code = getattr(cause, "sqlite_errorcode", None)
    if isinstance(cause, sqlite3.Error) and isinstance(sqlite_code, int):
        details.append(f"sqlite_code={sqlite_code}")
    frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = str(frame.f_globals.get("__name__", ""))
        function = frame.f_code.co_name.replace("<", "").replace(">", "")
        if (
            module.startswith(
                ("bitbucket_search.", "bookmark_manager.", "semantic_search.", "core.")
            )
            and _TOKEN.fullmatch(module)
            and _TOKEN.fullmatch(function)
        ):
            frames.append(f"{module}.{function}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    if frames:
        details.append(f"frames={'|'.join(frames[-8:])}")
    return details


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    error: BaseException | None = None,
    **context: object,
) -> None:
    """Log fixed events and safe scalar context, never exception messages or content.

    Callers must use static event/reason/code labels. User text, URLs, filenames,
    paths, query strings and Git/parser output are intentionally not accepted.
    Code-frame locations replace raw tracebacks (no source lines or locals).
    """

    if not logger.isEnabledFor(level):
        return
    event_name = event if isinstance(event, str) and _TOKEN.fullmatch(event) else "invalid_event"
    details = [f"event={event_name}"]
    for field, value in {**(_CONTEXT.get() or {}), **context}.items():
        if field not in _FIELDS or value is None:
            continue
        if isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            rendered = f"{value:.3f}"
        elif isinstance(value, str) and _TOKEN.fullmatch(value):
            rendered = value
        else:
            continue
        details.append(f"{field}={rendered}")
    if error is not None:
        details.extend(_exception_context(error))
    logger.log(level, " ".join(details))
