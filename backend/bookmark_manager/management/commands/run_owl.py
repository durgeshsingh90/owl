"""Run OWL's web server with its resident schedulers and bookmark index workers."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from bitbucket.services.remote_sync import wake_sync_worker
from bitbucket.services.scheduler import queue_due_daily_refreshes
from bookmark_manager.models import NotificationKind, NotificationState
from bookmark_manager.services.bookmark_refresh import queue_due_scheduled_refresh
from bookmark_manager.services.logging_events import get_logger, log_event
from bookmark_manager.services.notifications import publish_notification
from core.process_supervision import RESIDENT_SUPERVISOR_PID_ENV
from semantic_search.services.jobs import sweep_semantic_index_queue

logger = get_logger("supervisor")
bitbucket_logger = logging.getLogger("owl.bitbucket.supervisor")
SCHEDULER_EVENT_KEY = "confluence-refresh:scheduler"


def _publish_scheduler_state(*, recovered: bool) -> None:
    if recovered:
        publish_notification(
            event_key=SCHEDULER_EVENT_KEY,
            kind=NotificationKind.CONFLUENCE_REFRESH,
            state=NotificationState.SUCCESS,
            title="Weekly refresh scheduler resumed",
            message="OWL is checking the durable Confluence refresh schedule again.",
            target_path="/bookmarks/",
            occurred_at=timezone.now(),
        )
        return
    publish_notification(
        event_key=SCHEDULER_EVENT_KEY,
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=NotificationState.ERROR,
        title="Weekly refresh scheduler needs attention",
        message="OWL will keep retrying the background schedule check automatically.",
        target_path="/bookmarks/",
        occurred_at=timezone.now(),
    )


def _scheduler_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    """Keep Confluence schedule checks resident and recover after transient errors."""

    log_event(logger, logging.INFO, "resident_refresh_scheduler_started")
    try:
        _run_refresh_scheduler_loop(stop_event, poll_seconds=poll_seconds)
    except BaseException as exc:
        log_event(
            logger,
            logging.CRITICAL,
            "resident_refresh_scheduler_terminated",
            error=exc,
        )
        raise
    finally:
        log_event(logger, logging.INFO, "resident_refresh_scheduler_stopped")


def _run_refresh_scheduler_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    scheduler_failed = False
    while not stop_event.is_set():
        try:
            close_old_connections()
            queue_due_scheduled_refresh()
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "resident_refresh_scheduler_check_failed",
                error=exc,
                stage="schedule_check",
            )
            if not scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=False)
                except Exception as notification_error:
                    log_event(
                        logger,
                        logging.ERROR,
                        "resident_refresh_scheduler_notification_failed",
                        error=notification_error,
                        stage="failure_notification",
                    )
            scheduler_failed = True
        else:
            if scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=True)
                except Exception as notification_error:
                    log_event(
                        logger,
                        logging.ERROR,
                        "resident_refresh_scheduler_notification_failed",
                        error=notification_error,
                        stage="recovery_notification",
                    )
                log_event(logger, logging.INFO, "resident_refresh_scheduler_recovered")
            scheduler_failed = False
        finally:
            close_old_connections()
        stop_event.wait(poll_seconds)


def _bitbucket_scheduler_loop(stop_event: threading.Event, *, poll_seconds: int = 60) -> None:
    """Queue daily REST API refreshes for the standalone Bitbucket app."""

    while not stop_event.is_set():
        try:
            close_old_connections()
            if queue_due_daily_refreshes():
                wake_sync_worker()
        except Exception:
            bitbucket_logger.exception("daily_refresh_schedule_failed")
        finally:
            close_old_connections()
        stop_event.wait(poll_seconds)


def _semantic_worker_command() -> tuple[str, ...]:
    return (
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "semantic_index_worker",
        "--no-startup-sweep",
    )


def _start_semantic_worker() -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment[RESIDENT_SUPERVISOR_PID_ENV] = str(os.getpid())
    return subprocess.Popen(
        _semantic_worker_command(),
        cwd=settings.BASE_DIR,
        env=environment,
        stdin=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _semantic_supervisor_loop(stop_event: threading.Event) -> None:
    """Maintain the configured bookmark embedding worker pool."""

    processes: list[subprocess.Popen[bytes]] = []
    try:
        close_old_connections()
        sweep_semantic_index_queue(interrupt_running=True)
        close_old_connections()
        while not stop_event.is_set():
            processes = [process for process in processes if process.poll() is None]
            while len(processes) < settings.SEMANTIC_MAX_WORKERS and not stop_event.is_set():
                processes.append(_start_semantic_worker())
            stop_event.wait(1)
    except Exception as exc:
        log_event(
            logger,
            logging.CRITICAL,
            "semantic_supervisor_terminated",
            error=exc,
        )
    finally:
        for process in processes:
            _stop_worker(process)
        close_old_connections()


class Command(BaseCommand):
    help = "Run the local OWL website with its schedulers and bookmark index workers."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "addrport",
            nargs="?",
            default="127.0.0.1:8000",
            help="Optional port number, or ipaddr:port (default: 127.0.0.1:8000).",
        )

    def handle(self, *args, **options) -> None:
        stop_event = threading.Event()
        threads = [
            threading.Thread(
                target=_scheduler_loop,
                kwargs={
                    "stop_event": stop_event,
                    "poll_seconds": settings.CONFLUENCE_REFRESH_SCHEDULER_POLL_SECONDS,
                },
                name="owl-refresh-scheduler",
                daemon=True,
            ),
            threading.Thread(
                target=_bitbucket_scheduler_loop,
                kwargs={"stop_event": stop_event},
                name="owl-bitbucket-scheduler",
                daemon=True,
            ),
        ]
        if settings.SEMANTIC_SEARCH_ENABLED:
            threads.append(
                threading.Thread(
                    target=_semantic_supervisor_loop,
                    kwargs={"stop_event": stop_event},
                    name="owl-semantic-supervisor",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        self.stdout.write(
            self.style.SUCCESS(
                "OWL started its Confluence and Bitbucket schedulers. "
                "Bookmark semantic workers run in parallel."
            )
        )
        try:
            call_command("runserver", options["addrport"], use_reloader=False)
        finally:
            stop_event.set()
            for thread in threads:
                thread.join(timeout=7)
