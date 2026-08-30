"""Bookmark Manager's application boundary for shared content-free diagnostics."""

import logging
from contextvars import copy_context
from functools import wraps
from time import perf_counter

from django.db import transaction

from core.logging_events import get_logger as _get_logger
from core.logging_events import log_event, logging_context

__all__ = ["get_logger", "log_event", "logged_operation", "logging_context"]


def get_logger(component: str):
    return _get_logger(component, namespace="owl.bookmarks")


def logged_operation(operation: str, *, expected_errors=(), quiet: bool = False):
    """Trace a local service boundary without inspecting arguments or return values."""

    logger = get_logger("operations")

    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            started = perf_counter()
            if not quiet:
                log_event(logger, logging.INFO, "bookmark_operation_started", operation=operation)
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING if isinstance(exc, expected_errors) else logging.ERROR,
                    "bookmark_operation_failed",
                    error=exc,
                    operation=operation,
                    elapsed_ms=round((perf_counter() - started) * 1000),
                )
                raise
            if not quiet:
                elapsed_ms = round((perf_counter() - started) * 1000)
                context = copy_context()
                transaction.on_commit(
                    lambda: context.run(
                        log_event,
                        logger,
                        logging.INFO,
                        "bookmark_operation_completed",
                        operation=operation,
                        elapsed_ms=elapsed_ms,
                    )
                )
            return result

        return invoke

    return decorate
