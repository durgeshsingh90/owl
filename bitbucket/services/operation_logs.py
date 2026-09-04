"""Append-only, content-safe repository operation logs and cursor queries."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime

from django.db import DatabaseError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from bitbucket.models import (
    RepositoryOperationLogChannel,
    RepositoryOperationLogEntry,
    RepositoryOperationLogSeverity,
)
from bitbucket.services.logging_events import get_logger, log_event
from core.logging import redact_log_text

MAX_OPERATION_LOG_MESSAGE_CHARACTERS = 1_024
_EVENT_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PHASE_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
logger = get_logger("operation_log")


def _safe_message(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    visible = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        or character in "\t "
    )
    message = " ".join(redact_log_text(visible).split())
    return message[:MAX_OPERATION_LOG_MESSAGE_CHARACTERS]


def build_operation_log_entry(
    *,
    repository_id: int,
    channel: str,
    event: str,
    message: object,
    sync_job_id: int | None = None,
    extraction_job_id: int | None = None,
    severity: str = RepositoryOperationLogSeverity.INFO,
    phase: str = "",
    progress: int | None = None,
    worker_pid: int | None = None,
    occurred_at: datetime | None = None,
) -> RepositoryOperationLogEntry:
    """Build one validated unsaved entry for single or batched publication."""

    if channel not in RepositoryOperationLogChannel.values:
        raise ValueError("A fixed repository operation log channel is required.")
    if severity not in RepositoryOperationLogSeverity.values:
        raise ValueError("A fixed repository operation log severity is required.")
    if not isinstance(event, str) or not _EVENT_TOKEN.fullmatch(event):
        raise ValueError("A fixed repository operation log event is required.")
    if phase and (not isinstance(phase, str) or not _PHASE_TOKEN.fullmatch(phase)):
        raise ValueError("The repository operation log phase is invalid.")
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
        raise ValueError("A repository id is required for operation logging.")
    if sync_job_id is None and extraction_job_id is None:
        raise ValueError("A repository sync or PDF extraction job is required.")
    if progress is not None and (
        not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100
    ):
        raise ValueError("Repository operation log progress must be between 0 and 100.")
    if worker_pid is not None and (
        not isinstance(worker_pid, int) or isinstance(worker_pid, bool) or worker_pid <= 0
    ):
        raise ValueError("Repository operation log worker pid is invalid.")
    safe_message = _safe_message(message)
    if not safe_message:
        raise ValueError("Repository operation log messages cannot be empty.")
    timestamp = occurred_at or timezone.now()
    if timezone.is_naive(timestamp):
        raise ValueError("Repository operation log timestamps must include a timezone.")
    return RepositoryOperationLogEntry(
        repository_id=repository_id,
        sync_job_id=sync_job_id,
        extraction_job_id=extraction_job_id,
        channel=channel,
        severity=severity,
        phase=phase,
        event=event,
        message=safe_message,
        progress=progress,
        worker_pid=worker_pid,
        occurred_at=timestamp,
    )


def append_operation_log_entry(**values) -> RepositoryOperationLogEntry:
    """Insert one immutable entry and return its database cursor."""

    entry = build_operation_log_entry(**values)
    entry.save(force_insert=True)
    return entry


def append_operation_log_entry_safely(**values) -> RepositoryOperationLogEntry | None:
    """Best-effort operation logging that cannot fail the primary worker action."""

    try:
        # A savepoint keeps an optional log failure from breaking a caller's
        # state-transition transaction. At top level this is still one short,
        # atomic insert.
        with transaction.atomic():
            return append_operation_log_entry(**values)
    except DatabaseError as exc:
        log_event(
            logger,
            logging.ERROR,
            "operation_log_write_failed",
            error=exc,
            repository_id=values.get("repository_id"),
            job_id=values.get("extraction_job_id") or values.get("sync_job_id"),
            stage="operation_log_insert",
        )
        return None


def repository_operation_log_entries(
    repository_id: int,
    *,
    sync_job_id: int | None = None,
    after_id: int | None = None,
    before_id: int | None = None,
) -> QuerySet[RepositoryOperationLogEntry]:
    """Return an ascending cursor queryset for a local repository log endpoint."""

    if after_id is not None and before_id is not None:
        raise ValueError("Use only one repository operation log cursor at a time.")
    entries = RepositoryOperationLogEntry.objects.filter(repository_id=repository_id)
    if sync_job_id is not None:
        entries = entries.filter(sync_job_id=sync_job_id)
    if after_id is not None:
        entries = entries.filter(pk__gt=after_id)
    if before_id is not None:
        entries = entries.filter(pk__lt=before_id)
    return entries.select_related(
        "sync_job",
        "extraction_job__document",
    ).order_by("id")
