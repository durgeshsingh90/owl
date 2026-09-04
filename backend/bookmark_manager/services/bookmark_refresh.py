"""Durable, background Confluence bookmark refresh orchestration."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkRefreshFailure,
    BookmarkRefreshRun,
    BookmarkRefreshSchedule,
    BookmarkRefreshStatus,
    BookmarkRefreshTrigger,
    BookmarkSource,
    NotificationKind,
    NotificationState,
)
from bookmark_manager.services.bookmark_application import (
    ClientFactory,
    build_confluence_client,
    finish_confluence_bookmark,
    snapshot_from_confluence_page,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluencePageSnapshot,
    record_refresh_failure,
    upsert_bookmark,
)
from bookmark_manager.services.configuration import (
    ActiveConfluenceProfile,
    ConfigurationUnavailable,
    get_active_profile,
)
from bookmark_manager.services.confluence_adapter import ConfluenceResultCode
from bookmark_manager.services.logging_events import get_logger, log_event, logging_context
from bookmark_manager.services.notifications import publish_notification
from core.logging import redact_log_text

logger = get_logger("refresh")

ACTIVE_REFRESH_STATUSES = (
    BookmarkRefreshStatus.QUEUED,
    BookmarkRefreshStatus.RUNNING,
)
TERMINAL_REFRESH_STATUSES = (
    BookmarkRefreshStatus.SUCCEEDED,
    BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
    BookmarkRefreshStatus.FAILED,
    BookmarkRefreshStatus.INTERRUPTED,
)
STALE_REFRESH_AFTER = timedelta(minutes=5)
MAX_REFRESH_ATTEMPTS = 3
RETRYABLE_REFRESH_ERROR_CODES = {
    "access_denied",
    "invalid_credential",
    "rate_limited",
    "refresh_error",
    "unreachable",
    "unsupported_response",
}
NON_RETRYABLE_IMMEDIATE_ERROR_CODES = {
    "access_denied",
    "invalid_credential",
    "not_found",
}


@dataclass(frozen=True, slots=True)
class RefreshFetchResult:
    bookmark_id: int
    page_id: str
    snapshot: ConfluencePageSnapshot | None = None
    error_code: str = ""
    error_message: str = ""
    availability_status: str = BookmarkAvailability.ACTIVE

    @property
    def ok(self) -> bool:
        return self.snapshot is not None


RefreshFetcher = Callable[[ActiveConfluenceProfile, int, str], RefreshFetchResult]

_SAFE_ERROR_CODE = re.compile(r"[^a-z0-9_.-]+")
_SAFE_FAILURE_QUERY_KEYS = {"pageid"}
_LOGGED_ERROR_CODES = frozenset(code.value for code in ConfluenceResultCode) | {
    "refresh_error",
    "temporarily_unavailable",
}


def _logged_page_id(page_id: object) -> str | None:
    # Stored identifiers may predate validation; never treat arbitrary user text
    # as an ID merely because it fits the logger's scalar-token syntax.
    return page_id if isinstance(page_id, str) and re.fullmatch(r"[0-9]{1,32}", page_id) else None


def _logged_error_code(error_code: str) -> str:
    return error_code if error_code in _LOGGED_ERROR_CODES else "refresh_error"


def _log_refresh_errors(stage: str):
    """Keep failed persistence/notification operations observable before re-raising."""

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            first = args[0] if args else kwargs.get("run_id")
            run_id = first.pk if isinstance(first, BookmarkRefreshRun) else first
            context = {"stage": stage}
            if isinstance(run_id, int) and not isinstance(run_id, bool):
                context["run_id"] = run_id
            with logging_context(**context):
                try:
                    return function(*args, **kwargs)
                except Exception as exc:
                    log_event(logger, logging.ERROR, "refresh_operation_failed", error=exc)
                    raise

        return wrapped

    return decorate


def _safe_error_code(value: object) -> str:
    candidate = _SAFE_ERROR_CODE.sub("_", str(value or "refresh_error").strip().casefold())
    return candidate.strip("_")[:64] or "refresh_error"


def _safe_failure_reason(
    value: object,
    *,
    profile_token: str = "",
) -> str:
    candidate = str(value or "OWL could not refresh this Confluence page.")
    if profile_token:
        candidate = candidate.replace(profile_token, "[REDACTED]")
    candidate = redact_log_text(candidate)
    candidate = " ".join(candidate.replace("\r", " ").replace("\n", " ").split())
    return candidate[:500] or "OWL could not refresh this Confluence page."


def _safe_failure_url(value: object) -> str:
    """Return a useful page URL without credentials, fragments, or arbitrary query data."""

    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    hostname = parsed.hostname.casefold().rstrip(".")
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    port_suffix = "" if port is None or default_port else f":{port}"
    netloc = f"[{hostname}]{port_suffix}" if ":" in hostname else f"{hostname}{port_suffix}"
    safe_query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=False)
            if key.casefold() in _SAFE_FAILURE_QUERY_KEYS and value.isdecimal()
        ]
    )
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", safe_query, ""))[:2048]


def _availability_for_result(code: ConfluenceResultCode) -> str:
    if code is ConfluenceResultCode.NOT_FOUND:
        return BookmarkAvailability.NOT_FOUND
    if code is ConfluenceResultCode.ACCESS_DENIED:
        return BookmarkAvailability.ACCESS_DENIED
    if code is ConfluenceResultCode.INVALID_CREDENTIAL:
        return BookmarkAvailability.AUTH_ERROR
    return BookmarkAvailability.REFRESH_ERROR


@_log_refresh_errors("fetch")
def fetch_confluence_snapshot(
    profile: ActiveConfluenceProfile,
    bookmark_id: int,
    page_id: str,
    *,
    client_factory: ClientFactory | None = None,
) -> RefreshFetchResult:
    """Fetch one page without touching SQLite, making it safe for a thread pool."""

    started_at = time.monotonic()
    log_event(
        logger,
        logging.DEBUG,
        "refresh_fetch_started",
        bookmark_id=bookmark_id,
        page_id=_logged_page_id(page_id),
    )
    try:
        client = (client_factory or build_confluence_client)(profile)
        result = client.get_page(page_id)
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "refresh_fetch_failed",
            error=exc,
            bookmark_id=bookmark_id,
            page_id=_logged_page_id(page_id),
            error_code="refresh_error",
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            error_code="refresh_error",
            error_message="OWL could not complete the Confluence request.",
            availability_status=BookmarkAvailability.REFRESH_ERROR,
        )
    if not result.ok or result.page is None:
        log_event(
            logger,
            logging.ERROR,
            "refresh_fetch_failed",
            bookmark_id=bookmark_id,
            page_id=_logged_page_id(page_id),
            error_code=_logged_error_code(result.code.value),
            http_status=result.http_status,
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            error_code=result.code.value,
            error_message=result.message,
            availability_status=_availability_for_result(result.code),
        )
    snapshot = snapshot_from_confluence_page(result.page)
    log_event(
        logger,
        logging.DEBUG,
        "refresh_fetch_completed",
        bookmark_id=bookmark_id,
        page_id=_logged_page_id(page_id),
        count=len(snapshot.ancestors),
        elapsed_ms=(time.monotonic() - started_at) * 1000,
    )
    return RefreshFetchResult(
        bookmark_id=bookmark_id,
        page_id=page_id,
        snapshot=snapshot,
    )


def _refresh_interval() -> timedelta:
    return timedelta(seconds=settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS)


def _refresh_retry_delay() -> timedelta:
    return timedelta(seconds=settings.CONFLUENCE_REFRESH_RETRY_SECONDS)


def _run_needs_scheduled_retry(run: BookmarkRefreshRun) -> bool:
    """Retry interrupted/fatal runs and partial runs with temporary page failures."""

    if run.status in {BookmarkRefreshStatus.FAILED, BookmarkRefreshStatus.INTERRUPTED}:
        return True
    if run.status != BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS:
        return False
    return run.failures.filter(error_code__in=RETRYABLE_REFRESH_ERROR_CODES).exists()


def _initial_schedule_time(*, at: datetime) -> datetime:
    latest = (
        BookmarkRefreshRun.objects.filter(
            status__in=TERMINAL_REFRESH_STATUSES,
            completed_at__isnull=False,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if latest is None or latest.completed_at is None:
        return at + _refresh_interval()
    delay = _refresh_retry_delay() if _run_needs_scheduled_retry(latest) else _refresh_interval()
    return max(at, latest.completed_at + delay)


def _backfill_refresh_schedule_history(
    schedule: BookmarkRefreshSchedule,
    *,
    at: datetime,
) -> BookmarkRefreshSchedule:
    """Populate schedule timestamps for installations upgraded with existing runs."""

    if schedule.last_attempt_at is not None or schedule.last_run_id is not None:
        return schedule
    latest = (
        BookmarkRefreshRun.objects.filter(
            status__in=TERMINAL_REFRESH_STATUSES,
            completed_at__isnull=False,
        )
        .prefetch_related("failures")
        .order_by("-completed_at", "-id")
        .first()
    )
    if latest is None or latest.completed_at is None:
        return schedule
    latest_success = next(
        (
            candidate
            for candidate in BookmarkRefreshRun.objects.filter(
                status__in=(
                    BookmarkRefreshStatus.SUCCEEDED,
                    BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
                ),
                completed_at__isnull=False,
            )
            .prefetch_related("failures")
            .order_by("-completed_at", "-id")
            if not _run_needs_scheduled_retry(candidate)
        ),
        None,
    )
    retry_required = _run_needs_scheduled_retry(latest)
    schedule.last_run = latest
    schedule.last_attempt_at = latest.completed_at
    schedule.last_success_at = latest_success.completed_at if latest_success else None
    schedule.consecutive_failures = 1 if retry_required else 0
    delay = _refresh_retry_delay() if retry_required else _refresh_interval()
    schedule.next_run_at = max(at, latest.completed_at + delay)
    schedule.save(
        update_fields=(
            "last_run",
            "last_attempt_at",
            "last_success_at",
            "consecutive_failures",
            "next_run_at",
            "updated_at",
        )
    )
    return schedule


@_log_refresh_errors("schedule_load")
def get_refresh_schedule(*, at=None) -> BookmarkRefreshSchedule:
    """Return the singleton schedule, initializing its first weekly due time."""

    observed_at = at or timezone.now()
    schedule, _created = BookmarkRefreshSchedule.objects.get_or_create(
        pk=1,
        defaults={"next_run_at": _initial_schedule_time(at=observed_at)},
    )
    if schedule.next_run_at is None:
        schedule.next_run_at = _initial_schedule_time(at=observed_at)
        schedule.save(update_fields=("next_run_at", "updated_at"))
    return _backfill_refresh_schedule_history(schedule, at=observed_at)


@_log_refresh_errors("schedule_update")
def _update_refresh_schedule(
    run: BookmarkRefreshRun,
    *,
    at=None,
) -> BookmarkRefreshSchedule:
    """Advance the durable schedule exactly once for one completed run."""

    if run.status not in TERMINAL_REFRESH_STATUSES:
        return get_refresh_schedule(at=at)
    observed_at = at or run.completed_at or timezone.now()
    with transaction.atomic():
        schedule = BookmarkRefreshSchedule.objects.select_for_update().filter(pk=1).first()
        if schedule is None:
            schedule = BookmarkRefreshSchedule.objects.create(
                pk=1,
                next_run_at=_initial_schedule_time(at=observed_at),
            )
        if schedule.last_run_id == run.pk:
            return schedule

        retry_required = _run_needs_scheduled_retry(run)
        schedule.last_run = run
        schedule.last_attempt_at = run.completed_at or observed_at
        if retry_required:
            schedule.consecutive_failures += 1
            schedule.next_run_at = observed_at + _refresh_retry_delay()
        else:
            schedule.consecutive_failures = 0
            schedule.next_run_at = observed_at + _refresh_interval()
            if run.status in {
                BookmarkRefreshStatus.SUCCEEDED,
                BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
            }:
                schedule.last_success_at = run.completed_at or observed_at
        schedule.save(
            update_fields=(
                "last_run",
                "last_attempt_at",
                "last_success_at",
                "consecutive_failures",
                "next_run_at",
                "updated_at",
            )
        )
        context = {
            "run_id": run.pk,
            "stage": "schedule_update",
            "retry_count": schedule.consecutive_failures,
            "delay_seconds": (
                _refresh_retry_delay() if retry_required else _refresh_interval()
            ).total_seconds(),
        }
        transaction.on_commit(
            lambda: log_event(
                logger,
                logging.WARNING if retry_required else logging.INFO,
                "refresh_retry_scheduled" if retry_required else "refresh_schedule_advanced",
                **context,
            )
        )
        return schedule


@_log_refresh_errors("stale_recovery")
def _interrupt_stale_runs(*, at=None) -> None:
    observed_at = at or timezone.now()
    cutoff = observed_at - STALE_REFRESH_AFTER
    stale_runs: list[BookmarkRefreshRun] = []
    stale_running = BookmarkRefreshRun.objects.filter(
        status=BookmarkRefreshStatus.RUNNING,
        heartbeat_at__lt=cutoff,
    )
    running_ids = tuple(stale_running.values_list("pk", flat=True))
    if running_ids:
        for run_id in running_ids:
            log_event(
                logger,
                logging.ERROR,
                "refresh_run_interrupted",
                run_id=run_id,
                reason="heartbeat_expired",
                status=BookmarkRefreshStatus.RUNNING,
            )
        stale_running.update(
            status=BookmarkRefreshStatus.INTERRUPTED,
            completed_at=observed_at,
            last_error_message="The background worker stopped before this refresh completed.",
        )
        stale_runs.extend(BookmarkRefreshRun.objects.filter(pk__in=running_ids))

    stale_queued = BookmarkRefreshRun.objects.filter(
        status=BookmarkRefreshStatus.QUEUED,
        requested_at__lt=cutoff,
    )
    queued_ids = tuple(stale_queued.values_list("pk", flat=True))
    if queued_ids:
        for run_id in queued_ids:
            log_event(
                logger,
                logging.ERROR,
                "refresh_run_interrupted",
                run_id=run_id,
                reason="worker_start_expired",
                status=BookmarkRefreshStatus.QUEUED,
            )
        stale_queued.update(
            status=BookmarkRefreshStatus.INTERRUPTED,
            completed_at=observed_at,
            last_error_message="The background worker did not start this refresh.",
        )
        stale_runs.extend(BookmarkRefreshRun.objects.filter(pk__in=queued_ids))

    for stale_run in stale_runs:
        _update_refresh_schedule(stale_run, at=observed_at)
        publish_refresh_notification(stale_run)


@_log_refresh_errors("queue")
def create_or_get_refresh_run(
    *,
    trigger: str = BookmarkRefreshTrigger.MANUAL,
) -> tuple[BookmarkRefreshRun, bool]:
    """Create one queued run, or return the already active global refresh."""

    if trigger not in BookmarkRefreshTrigger.values:
        raise ValueError("The refresh trigger is not supported.")
    _interrupt_stale_runs()
    with transaction.atomic():
        active = (
            BookmarkRefreshRun.objects.select_for_update()
            .filter(status__in=ACTIVE_REFRESH_STATUSES)
            .first()
        )
        if active is not None:
            log_event(
                logger,
                logging.DEBUG,
                "refresh_active_run_reused",
                run_id=active.pk,
                status=active.status,
            )
            return active, False
        try:
            run = BookmarkRefreshRun.objects.create(trigger=trigger)
        except IntegrityError as exc:
            log_event(logger, logging.ERROR, "refresh_queue_conflict", error=exc)
            active = BookmarkRefreshRun.objects.filter(status__in=ACTIVE_REFRESH_STATUSES).first()
            if active is None:
                raise
            log_event(
                logger,
                logging.WARNING,
                "refresh_active_run_reused_after_conflict",
                run_id=active.pk,
                status=active.status,
            )
            return active, False
        run_id = run.pk
        transaction.on_commit(
            lambda: log_event(
                logger,
                logging.INFO,
                "refresh_run_queued",
                run_id=run_id,
                stage="queue",
                trigger=trigger,
                status=BookmarkRefreshStatus.QUEUED,
            )
        )
        return run, True


@_log_refresh_errors("notification")
def publish_refresh_notification(run: BookmarkRefreshRun) -> None:
    if run.status in ACTIVE_REFRESH_STATUSES:
        state = NotificationState.RUNNING
        title = (
            "Scheduled Confluence refresh started"
            if run.trigger != BookmarkRefreshTrigger.MANUAL
            else "Confluence refresh started"
        )
        message = (
            f"{run.processed_bookmarks} of {run.total_bookmarks} saved pages processed "
            "in the background."
        )
        occurred_at = run.started_at or run.requested_at
    elif run.status == BookmarkRefreshStatus.SUCCEEDED:
        state = NotificationState.SUCCESS
        title = "Confluence refresh completed"
        message = f"Updated {run.succeeded_bookmarks} saved Confluence pages."
        occurred_at = run.completed_at or timezone.now()
    elif run.status == BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS:
        state = NotificationState.WARNING
        title = "Confluence refresh completed with issues"
        retry_note = (
            " Temporary failures will retry after two hours."
            if _run_needs_scheduled_retry(run)
            else " Deleted or inaccessible pages remain available as local references."
        )
        message = (
            f"Updated {run.succeeded_bookmarks}; {run.failed_bookmarks} need attention.{retry_note}"
        )
        occurred_at = run.completed_at or timezone.now()
    else:
        state = NotificationState.ERROR
        title = "Confluence refresh failed"
        message = (
            run.last_error_message or "The background refresh could not finish."
        ) + " OWL will retry after two hours."
        occurred_at = run.completed_at or timezone.now()

    publish_notification(
        event_key=f"confluence-refresh:{run.pk}",
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=state,
        title=title,
        message=message,
        target_path=f"/bookmarks/?refresh_run={run.pk}#refresh-result",
        occurred_at=occurred_at,
    )


@_log_refresh_errors("schedule_tick")
def queue_due_scheduled_refresh(
    *,
    at=None,
) -> tuple[BookmarkRefreshRun | None, bool]:
    """Queue one due weekly/retry run without blocking the caller."""

    observed_at = at or timezone.now()
    _interrupt_stale_runs(at=observed_at)
    get_refresh_schedule(at=observed_at)
    with transaction.atomic():
        schedule = BookmarkRefreshSchedule.objects.select_for_update().get(pk=1)
        if (
            not schedule.enabled
            or schedule.next_run_at is None
            or schedule.next_run_at > observed_at
        ):
            return None, False
        trigger = (
            BookmarkRefreshTrigger.RETRY
            if schedule.consecutive_failures
            else BookmarkRefreshTrigger.SCHEDULED
        )
        # Reserve the due slot before launching. Completion replaces this with the
        # next weekly or two-hour retry time.
        schedule.next_run_at = observed_at + _refresh_retry_delay()
        schedule.save(update_fields=("next_run_at", "updated_at"))

    run, created = create_or_get_refresh_run(trigger=trigger)
    if not created:
        return run, False
    publish_refresh_notification(run)
    try:
        launch_refresh_worker(run.pk)
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "refresh_scheduled_launch_failed",
            error=exc,
            run_id=run.pk,
        )
        mark_refresh_launch_failed(run.pk)
        run.refresh_from_db()
        return run, False
    return run, True


@_log_refresh_errors("schedule_snapshot")
def refresh_schedule_snapshot(
    *,
    at=None,
    initialize: bool = True,
) -> dict[str, object]:
    observed_at = at or timezone.now()
    schedule = (
        get_refresh_schedule(at=observed_at)
        if initialize
        else BookmarkRefreshSchedule.objects.filter(pk=1).first()
    )
    if schedule is None:
        return {
            "enabled": True,
            "next_run_at": _initial_schedule_time(at=observed_at).isoformat(),
            "last_attempt_at": None,
            "last_success_at": None,
            "retrying": False,
            "consecutive_failures": 0,
        }
    return {
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_attempt_at": (
            schedule.last_attempt_at.isoformat() if schedule.last_attempt_at else None
        ),
        "last_success_at": (
            schedule.last_success_at.isoformat() if schedule.last_success_at else None
        ),
        "retrying": schedule.consecutive_failures > 0,
        "consecutive_failures": schedule.consecutive_failures,
    }


@_log_refresh_errors("worker_launch")
def launch_refresh_worker(run_id: int) -> subprocess.Popen[bytes]:
    """Detach one secret-free management-command process for the queued run."""

    manage_py = Path(settings.BASE_DIR) / "manage.py"
    command = [
        sys.executable,
        str(manage_py),
        "bookmark_refresh_worker",
        "--run-id",
        str(run_id),
    ]
    close_old_connections()
    process = subprocess.Popen(
        command,
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    log_event(
        logger,
        logging.INFO,
        "refresh_worker_spawned",
        run_id=run_id,
        worker_pid=process.pid,
    )
    return process


@_log_refresh_errors("launch_failure_persistence")
def mark_refresh_launch_failed(run_id: int) -> None:
    log_event(logger, logging.ERROR, "refresh_worker_launch_failed", run_id=run_id)
    now = timezone.now()
    updated = BookmarkRefreshRun.objects.filter(
        pk=run_id,
        status=BookmarkRefreshStatus.QUEUED,
    ).update(
        status=BookmarkRefreshStatus.FAILED,
        started_at=now,
        heartbeat_at=now,
        completed_at=now,
        last_error_message="OWL could not start the background refresh worker.",
    )
    if updated:
        run = BookmarkRefreshRun.objects.get(pk=run_id)
        _update_refresh_schedule(run, at=now)
        publish_refresh_notification(run)


@_log_refresh_errors("worker_failure_persistence")
def mark_refresh_worker_failed(run_id: int) -> None:
    log_event(logger, logging.ERROR, "refresh_worker_interrupted", run_id=run_id)
    now = timezone.now()
    updated = BookmarkRefreshRun.objects.filter(
        pk=run_id,
        status__in=ACTIVE_REFRESH_STATUSES,
    ).update(
        status=BookmarkRefreshStatus.INTERRUPTED,
        heartbeat_at=now,
        completed_at=now,
        last_error_message="The background worker stopped before this refresh completed.",
    )
    if updated:
        run = BookmarkRefreshRun.objects.get(pk=run_id)
        _update_refresh_schedule(run, at=now)
        publish_refresh_notification(run)


@_log_refresh_errors("claim")
def _claim_run(run_id: int) -> tuple[BookmarkRefreshRun, tuple[tuple[int, str], ...]] | None:
    with transaction.atomic():
        run = BookmarkRefreshRun.objects.select_for_update().filter(pk=run_id).first()
        if run is None or run.status != BookmarkRefreshStatus.QUEUED:
            return None
        bookmarks = tuple(
            Bookmark.objects.filter(source_type=BookmarkSource.CONFLUENCE)
            .order_by("id")
            .values_list("id", "page_id")
        )
        now = timezone.now()
        run.status = BookmarkRefreshStatus.RUNNING
        run.total_bookmarks = len(bookmarks)
        run.started_at = now
        run.heartbeat_at = now
        run.worker_pid = os.getpid()
        run.save(
            update_fields=(
                "status",
                "total_bookmarks",
                "started_at",
                "heartbeat_at",
                "worker_pid",
            )
        )
        publish_refresh_notification(run)
        context = {
            "run_id": run.pk,
            "stage": "claim",
            "count": len(bookmarks),
            "worker_pid": run.worker_pid,
        }
        transaction.on_commit(
            lambda: log_event(logger, logging.INFO, "refresh_run_claimed", **context)
        )
        return run, bookmarks


@_log_refresh_errors("completion")
def _finish_run(run: BookmarkRefreshRun, *, fatal_message: str = "") -> BookmarkRefreshRun:
    now = timezone.now()
    run.completed_at = now
    run.heartbeat_at = now
    if fatal_message:
        run.status = BookmarkRefreshStatus.FAILED
        run.last_error_message = " ".join(fatal_message.split())[:500]
    elif run.failed_bookmarks:
        run.status = BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS
        run.last_error_message = (
            f"{run.failed_bookmarks} bookmark"
            f"{'s' if run.failed_bookmarks != 1 else ''} could not be refreshed."
        )
    else:
        run.status = BookmarkRefreshStatus.SUCCEEDED
        run.last_error_message = ""
    context = {
        "run_id": run.pk,
        "stage": "completion",
        "status": run.status,
        "succeeded_count": run.succeeded_bookmarks,
        "failed_count": run.failed_bookmarks,
        "elapsed_ms": max(0, (now - (run.started_at or run.requested_at)).total_seconds() * 1000),
    }
    if run.status != BookmarkRefreshStatus.SUCCEEDED:
        log_event(logger, logging.ERROR, "refresh_run_failed_outcome", **context)
    run.save(update_fields=("status", "heartbeat_at", "completed_at", "last_error_message"))
    _update_refresh_schedule(run, at=now)
    publish_refresh_notification(run)
    transaction.on_commit(
        lambda: log_event(logger, logging.INFO, "refresh_run_completed", **context)
    )
    return run


@_log_refresh_errors("progress_persistence")
def _save_run_progress(
    run: BookmarkRefreshRun,
    *,
    outcome: str | None = None,
) -> None:
    """Persist finalized bookmark progress or just a retry heartbeat."""

    if outcome is not None:
        run.processed_bookmarks += 1
        if outcome == "succeeded":
            run.succeeded_bookmarks += 1
        else:
            run.failed_bookmarks += 1
    run.heartbeat_at = timezone.now()
    run.save(
        update_fields=(
            "processed_bookmarks",
            "succeeded_bookmarks",
            "failed_bookmarks",
            "heartbeat_at",
        )
    )
    # Retry-only heartbeats carry no new progress; keep frequent writes quiet.
    if outcome is not None:
        log_event(
            logger,
            logging.DEBUG,
            "refresh_progress_updated",
            run_id=run.pk,
            processed_count=run.processed_bookmarks,
            succeeded_count=run.succeeded_bookmarks,
            failed_count=run.failed_bookmarks,
        )


@_log_refresh_errors("page_failure_persistence")
def _finalize_bookmark_failure(
    run: BookmarkRefreshRun,
    fetched: RefreshFetchResult,
    *,
    attempted_at,
    attempt_count: int,
    profile_token: str,
) -> None:
    """Publish diagnostics only after every retry has been exhausted."""

    log_event(
        logger,
        logging.ERROR,
        "refresh_page_failed_outcome",
        run_id=run.pk,
        bookmark_id=fetched.bookmark_id,
        page_id=_logged_page_id(fetched.page_id),
        error_code=_logged_error_code(fetched.error_code),
        attempt=attempt_count,
    )
    error_code = _safe_error_code(fetched.error_code)
    reason = _safe_failure_reason(fetched.error_message, profile_token=profile_token)
    availability_status = (
        fetched.availability_status
        if fetched.availability_status in BookmarkAvailability.values
        else BookmarkAvailability.REFRESH_ERROR
    )
    try:
        bookmark = record_refresh_failure(
            fetched.bookmark_id,
            error_code=error_code,
            error_message=reason,
            availability_status=availability_status,
            attempted_at=attempted_at,
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "refresh_page_failure_record_failed",
            error=exc,
            run_id=run.pk,
            bookmark_id=fetched.bookmark_id,
            page_id=_logged_page_id(fetched.page_id),
        )
        return
    BookmarkRefreshFailure.objects.update_or_create(
        refresh_run=run,
        bookmark=bookmark,
        defaults={
            "page_id": str(fetched.page_id)[:64],
            "url": _safe_failure_url(bookmark.url),
            "error_code": error_code,
            "reason": reason,
            "attempt_count": attempt_count,
        },
    )


def _failed_fetch_result(
    bookmark_id: int,
    page_id: str,
    *,
    message: str,
) -> RefreshFetchResult:
    return RefreshFetchResult(
        bookmark_id=bookmark_id,
        page_id=page_id,
        error_code="refresh_error",
        error_message=message,
        availability_status=BookmarkAvailability.REFRESH_ERROR,
    )


@_log_refresh_errors("execute")
def execute_refresh_run(
    run_id: int,
    *,
    profile: ActiveConfluenceProfile | None = None,
    fetcher: RefreshFetcher | None = None,
    client_factory: ClientFactory | None = None,
    max_workers: int | None = None,
) -> BookmarkRefreshRun | None:
    """Refresh concurrently, then retry only failed pages in two bounded rounds."""

    claimed = _claim_run(run_id)
    if claimed is None:
        return None
    run, bookmarks = claimed
    log_event(
        logger,
        logging.INFO,
        "refresh_run_started",
        run_id=run.pk,
        count=len(bookmarks),
        worker_count=min(max_workers or settings.CONFLUENCE_MAX_WORKERS, max(1, len(bookmarks))),
        retries_remaining=MAX_REFRESH_ATTEMPTS - 1,
    )
    if not bookmarks:
        return _finish_run(run)
    try:
        active_profile = profile or get_active_profile()
    except ConfigurationUnavailable as exc:
        log_event(
            logger,
            logging.ERROR,
            "refresh_configuration_unavailable",
            error=exc,
            run_id=run.pk,
            stage="configuration",
        )
        return _finish_run(
            run,
            fatal_message=_safe_failure_reason(str(exc)),
        )

    worker_count = min(max_workers or settings.CONFLUENCE_MAX_WORKERS, len(bookmarks))
    selected_fetcher = fetcher
    if selected_fetcher is None:

        def selected_fetcher(
            selected_profile: ActiveConfluenceProfile,
            bookmark_id: int,
            page_id: str,
        ) -> RefreshFetchResult:
            return fetch_confluence_snapshot(
                selected_profile,
                bookmark_id,
                page_id,
                client_factory=client_factory,
            )

    def fetch_with_context(bookmark_id: int, page_id: str, attempt: int) -> RefreshFetchResult:
        with logging_context(
            bookmark_id=bookmark_id,
            page_id=_logged_page_id(page_id),
            attempt=attempt,
        ):
            return selected_fetcher(active_profile, bookmark_id, page_id)

    pending = list(bookmarks)
    latest_failures: dict[int, RefreshFetchResult] = {}
    attempt_counts: dict[int, int] = {}
    close_old_connections()
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="owl-confluence-refresh",
        ) as pool:
            for attempt_number in range(1, MAX_REFRESH_ATTEMPTS + 1):
                if not pending:
                    break
                log_event(
                    logger,
                    logging.DEBUG,
                    "refresh_fetch_round_started",
                    run_id=run.pk,
                    attempt=attempt_number,
                    count=len(pending),
                )
                futures = {
                    pool.submit(
                        copy_context().run,
                        fetch_with_context,
                        bookmark_id,
                        page_id,
                        attempt_number,
                    ): (
                        bookmark_id,
                        page_id,
                    )
                    for bookmark_id, page_id in pending
                }
                failed_ids: set[int] = set()
                for future in as_completed(futures):
                    bookmark_id, page_id = futures[future]
                    attempted_at = timezone.now()
                    try:
                        fetched = future.result()
                        if not isinstance(fetched, RefreshFetchResult):
                            raise TypeError("Unsupported refresh fetch result")
                        fetched = replace(
                            fetched,
                            bookmark_id=bookmark_id,
                            page_id=page_id,
                        )
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.ERROR,
                            "refresh_fetch_future_failed",
                            error=exc,
                            run_id=run.pk,
                            bookmark_id=bookmark_id,
                            page_id=_logged_page_id(page_id),
                            attempt=attempt_number,
                            stage="fetch",
                        )
                        fetched = _failed_fetch_result(
                            bookmark_id,
                            page_id,
                            message="OWL could not complete the Confluence request.",
                        )

                    if fetched.ok and fetched.snapshot is not None:
                        try:
                            finish_confluence_bookmark(
                                upsert_bookmark(
                                    fetched.snapshot,
                                    observed_at=attempted_at,
                                    record_refresh=True,
                                )
                            )
                        except Exception as exc:
                            log_event(
                                logger,
                                logging.ERROR,
                                "refresh_page_publish_failed",
                                error=exc,
                                run_id=run.pk,
                                bookmark_id=bookmark_id,
                                page_id=_logged_page_id(page_id),
                                attempt=attempt_number,
                                stage="page_publication",
                            )
                            fetched = _failed_fetch_result(
                                bookmark_id,
                                page_id,
                                message="OWL could not publish the refreshed page safely.",
                            )
                        else:
                            context = {
                                "run_id": run.pk,
                                "bookmark_id": bookmark_id,
                                "page_id": _logged_page_id(page_id),
                                "attempt": attempt_number,
                                "stage": "page_publication",
                            }
                            transaction.on_commit(
                                lambda context=context: log_event(
                                    logger, logging.INFO, "refresh_page_published", **context
                                )
                            )
                            latest_failures.pop(bookmark_id, None)
                            _save_run_progress(run, outcome="succeeded")
                            continue

                    log_event(
                        logger,
                        logging.ERROR,
                        "refresh_page_attempt_failed",
                        run_id=run.pk,
                        bookmark_id=bookmark_id,
                        page_id=_logged_page_id(page_id),
                        error_code=_logged_error_code(fetched.error_code),
                        attempt=attempt_number,
                    )
                    latest_failures[bookmark_id] = fetched
                    attempt_counts[bookmark_id] = attempt_number
                    if fetched.error_code not in NON_RETRYABLE_IMMEDIATE_ERROR_CODES:
                        failed_ids.add(bookmark_id)
                    _save_run_progress(run)

                # Preserve original bookmark order for each retry round. No retry is
                # submitted until every page in the preceding round has completed.
                pending = [item for item in pending if item[0] in failed_ids]
                if pending and attempt_number < MAX_REFRESH_ATTEMPTS:
                    log_event(
                        logger,
                        logging.WARNING,
                        "refresh_page_retry_round_queued",
                        run_id=run.pk,
                        count=len(pending),
                        retry_count=attempt_number,
                        retries_remaining=MAX_REFRESH_ATTEMPTS - attempt_number,
                    )
    finally:
        close_old_connections()

    for bookmark_id, fetched in latest_failures.items():
        _finalize_bookmark_failure(
            run,
            fetched,
            attempted_at=timezone.now(),
            attempt_count=attempt_counts.get(bookmark_id, 1),
            profile_token=active_profile.token,
        )
        _save_run_progress(run, outcome="failed")

    return _finish_run(run)


@_log_refresh_errors("status_snapshot")
def refresh_status_snapshot() -> tuple[BookmarkRefreshRun | None, BookmarkRefreshRun | None]:
    """Return the active/latest run and the latest completed global refresh."""

    _interrupt_stale_runs()
    latest = BookmarkRefreshRun.objects.first()
    completed = (
        BookmarkRefreshRun.objects.filter(
            status__in=(
                BookmarkRefreshStatus.SUCCEEDED,
                BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
            )
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    return latest, completed
