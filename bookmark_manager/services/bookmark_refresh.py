"""Durable, background Confluence bookmark refresh orchestration."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import timedelta
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
    BookmarkRefreshStatus,
    BookmarkSource,
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
from core.logging import redact_log_text

logger = logging.getLogger("owl.bookmarks.refresh")

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


def fetch_confluence_snapshot(
    profile: ActiveConfluenceProfile,
    bookmark_id: int,
    page_id: str,
    *,
    client_factory: ClientFactory | None = None,
) -> RefreshFetchResult:
    """Fetch one page without touching SQLite, making it safe for a thread pool."""

    try:
        client = (client_factory or build_confluence_client)(profile)
        result = client.get_page(page_id)
    except Exception:
        logger.error(
            "Confluence refresh fetch raised bookmark_id=%s page_id=%s",
            bookmark_id,
            page_id,
        )
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            error_code="refresh_error",
            error_message="OWL could not complete the Confluence request.",
            availability_status=BookmarkAvailability.REFRESH_ERROR,
        )
    if not result.ok or result.page is None:
        logger.warning(
            "Confluence refresh fetch failed bookmark_id=%s page_id=%s result=%s",
            bookmark_id,
            page_id,
            result.code.value,
        )
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            error_code=result.code.value,
            error_message=result.message,
            availability_status=_availability_for_result(result.code),
        )
    snapshot = snapshot_from_confluence_page(result.page)
    logger.info(
        "Confluence refresh fetched bookmark_id=%s page_id=%s ancestors=%d page_text_characters=%d",
        bookmark_id,
        page_id,
        len(snapshot.ancestors),
        len(snapshot.page_text),
    )
    return RefreshFetchResult(
        bookmark_id=bookmark_id,
        page_id=page_id,
        snapshot=snapshot,
    )


def _interrupt_stale_runs(*, at=None) -> None:
    observed_at = at or timezone.now()
    cutoff = observed_at - STALE_REFRESH_AFTER
    BookmarkRefreshRun.objects.filter(
        status=BookmarkRefreshStatus.RUNNING,
        heartbeat_at__lt=cutoff,
    ).update(
        status=BookmarkRefreshStatus.INTERRUPTED,
        completed_at=observed_at,
        last_error_message="The background worker stopped before this refresh completed.",
    )
    BookmarkRefreshRun.objects.filter(
        status=BookmarkRefreshStatus.QUEUED,
        requested_at__lt=cutoff,
    ).update(
        status=BookmarkRefreshStatus.INTERRUPTED,
        completed_at=observed_at,
        last_error_message="The background worker did not start this refresh.",
    )


def create_or_get_refresh_run() -> tuple[BookmarkRefreshRun, bool]:
    """Create one queued run, or return the already active global refresh."""

    _interrupt_stale_runs()
    with transaction.atomic():
        active = (
            BookmarkRefreshRun.objects.select_for_update()
            .filter(status__in=ACTIVE_REFRESH_STATUSES)
            .first()
        )
        if active is not None:
            return active, False
        try:
            return BookmarkRefreshRun.objects.create(), True
        except IntegrityError:
            active = BookmarkRefreshRun.objects.filter(status__in=ACTIVE_REFRESH_STATUSES).first()
            if active is None:
                raise
            return active, False


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
    return subprocess.Popen(
        command,
        cwd=settings.BASE_DIR,
        close_fds=True,
        start_new_session=os.name != "nt",
    )


def mark_refresh_launch_failed(run_id: int) -> None:
    now = timezone.now()
    BookmarkRefreshRun.objects.filter(
        pk=run_id,
        status=BookmarkRefreshStatus.QUEUED,
    ).update(
        status=BookmarkRefreshStatus.FAILED,
        started_at=now,
        heartbeat_at=now,
        completed_at=now,
        last_error_message="OWL could not start the background refresh worker.",
    )


def mark_refresh_worker_failed(run_id: int) -> None:
    now = timezone.now()
    BookmarkRefreshRun.objects.filter(
        pk=run_id,
        status__in=ACTIVE_REFRESH_STATUSES,
    ).update(
        status=BookmarkRefreshStatus.INTERRUPTED,
        heartbeat_at=now,
        completed_at=now,
        last_error_message="The background worker stopped before this refresh completed.",
    )


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
        return run, bookmarks


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
    run.save(update_fields=("status", "heartbeat_at", "completed_at", "last_error_message"))
    return run


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


def _finalize_bookmark_failure(
    run: BookmarkRefreshRun,
    fetched: RefreshFetchResult,
    *,
    attempted_at,
    attempt_count: int,
    profile_token: str,
) -> None:
    """Publish diagnostics only after every retry has been exhausted."""

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
    except Exception:
        logger.error(
            "Confluence refresh final failure state could not be recorded "
            "run_id=%s bookmark_id=%s page_id=%s",
            run.pk,
            fetched.bookmark_id,
            fetched.page_id,
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
    logger.info(
        "Global Confluence refresh started run_id=%s bookmarks=%s workers=%s attempts=%s",
        run.pk,
        len(bookmarks),
        min(max_workers or settings.CONFLUENCE_MAX_WORKERS, max(1, len(bookmarks))),
        MAX_REFRESH_ATTEMPTS,
    )
    if not bookmarks:
        return _finish_run(run)
    try:
        active_profile = profile or get_active_profile()
    except ConfigurationUnavailable as exc:
        logger.warning("Global Confluence refresh unavailable run_id=%s", run.pk)
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

    pending = list(bookmarks)
    latest_failures: dict[int, RefreshFetchResult] = {}
    close_old_connections()
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="owl-confluence-refresh",
        ) as pool:
            for attempt_number in range(1, MAX_REFRESH_ATTEMPTS + 1):
                if not pending:
                    break
                futures = {
                    pool.submit(selected_fetcher, active_profile, bookmark_id, page_id): (
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
                    except Exception:
                        logger.error(
                            "Confluence refresh fetch raised run_id=%s bookmark_id=%s "
                            "page_id=%s attempt=%s",
                            run.pk,
                            bookmark_id,
                            page_id,
                            attempt_number,
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
                        except Exception:
                            logger.error(
                                "Confluence refresh publish failed run_id=%s "
                                "bookmark_id=%s page_id=%s attempt=%s",
                                run.pk,
                                bookmark_id,
                                page_id,
                                attempt_number,
                            )
                            fetched = _failed_fetch_result(
                                bookmark_id,
                                page_id,
                                message="OWL could not publish the refreshed page safely.",
                            )
                        else:
                            latest_failures.pop(bookmark_id, None)
                            _save_run_progress(run, outcome="succeeded")
                            continue

                    latest_failures[bookmark_id] = fetched
                    failed_ids.add(bookmark_id)
                    _save_run_progress(run)

                # Preserve original bookmark order for each retry round. No retry is
                # submitted until every page in the preceding round has completed.
                pending = [item for item in pending if item[0] in failed_ids]
    finally:
        close_old_connections()

    for bookmark_id, _page_id in pending:
        fetched = latest_failures[bookmark_id]
        _finalize_bookmark_failure(
            run,
            fetched,
            attempted_at=timezone.now(),
            attempt_count=MAX_REFRESH_ATTEMPTS,
            profile_token=active_profile.token,
        )
        _save_run_progress(run, outcome="failed")

    completed = _finish_run(run)
    logger.info(
        "Global Confluence refresh completed run_id=%s status=%s succeeded=%s failed=%s",
        completed.pk,
        completed.status,
        completed.succeeded_bookmarks,
        completed.failed_bookmarks,
    )
    return completed


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
