"""Bitbucket's application boundary for shared content-free diagnostics."""

from core.logging_events import get_logger as _get_logger
from core.logging_events import log_event, logging_context

__all__ = ["get_logger", "log_event", "logging_context"]


def get_logger(component: str):
    return _get_logger(component, namespace="owl.bitbucket_app")
