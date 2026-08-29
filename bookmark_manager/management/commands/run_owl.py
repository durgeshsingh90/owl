"""Run OWL's local web server together with its resident schedulers and workers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from bitbucket_search.services.pdf_indexing import sweep_pdf_extraction_queue
from bitbucket_search.services.repository_sync import (
    queue_due_daily_repository_refreshes,
    repository_status_snapshot,
)
from bookmark_manager.models import NotificationKind, NotificationState
from bookmark_manager.services.bookmark_refresh import queue_due_scheduled_refresh
from bookmark_manager.services.notifications import publish_notification

logger = logging.getLogger("owl.bookmarks.refresh")
bitbucket_logger = logging.getLogger("owl.bitbucket.worker")
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
    """Keep schedule checks resident in the web process and recover after errors."""

    scheduler_failed = False
    while not stop_event.is_set():
        try:
            close_old_connections()
            queue_due_scheduled_refresh()
        except Exception:
            logger.exception("The resident Confluence refresh scheduler check failed")
            if not scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=False)
                except Exception:
                    logger.exception("The scheduler failure notification could not be recorded")
            scheduler_failed = True
        else:
            if scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=True)
                except Exception:
                    logger.exception("The scheduler recovery notification could not be recorded")
            scheduler_failed = False
        finally:
            close_old_connections()
        stop_event.wait(poll_seconds)


def _resident_bitbucket_worker_specs() -> tuple[tuple[str, str], ...]:
    """Describe the bounded set of queue controllers owned by ``run_owl``."""

    repository_workers = max(1, int(settings.BITBUCKET_MAX_REPO_WORKERS))
    extraction_workers = max(1, int(settings.PDF_MAX_EXTRACTION_WORKERS))
    return (
        *tuple(
            (f"repository-sync-{worker_number}", "bitbucket_sync_worker")
            for worker_number in range(1, repository_workers + 1)
        ),
        *tuple(
            (f"pdf-index-{worker_number}", "bitbucket_index_worker")
            for worker_number in range(1, extraction_workers + 1)
        ),
    )


def _launch_resident_bitbucket_worker(command: str) -> subprocess.Popen[bytes]:
    """Start one supervised queue controller owned by ``run_owl``."""

    arguments = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        command,
    ]
    if command == "bitbucket_sync_worker":
        # Repository and PDF pools have independent configured limits. Resident
        # repository controllers therefore never claim extraction work or launch
        # detached parser helpers.
        arguments.extend(
            (
                "--repository-only",
                "--no-spawn-index-workers",
                "--no-startup-index-sweep",
            )
        )
    elif command == "bitbucket_index_worker":
        arguments.append("--no-startup-sweep")

    return subprocess.Popen(
        arguments,
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name != "nt",
    )


def _stop_resident_bitbucket_worker(process: subprocess.Popen[bytes]) -> None:
    """Stop exactly the supervised controller and its parser process group."""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, ProcessLookupError):
        return
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return


def _bitbucket_queue_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    """Sweep durable queues and supervise their bounded resident controllers."""

    workers: dict[str, subprocess.Popen[bytes]] = {}
    startup_sweep_pending = True
    try:
        while not stop_event.is_set():
            try:
                close_old_connections()
                # These calls are idempotent. The startup extraction sweep first
                # revokes every inherited RUNNING lease, so an old detached worker
                # cannot publish after OWL has restarted. Later sweeps use the
                # normal heartbeat timeout and bounded retry policy.
                queue_due_daily_repository_refreshes()
                repository_status_snapshot()
                sweep_pdf_extraction_queue(interrupt_running=startup_sweep_pending)
                startup_sweep_pending = False

                for role, command in _resident_bitbucket_worker_specs():
                    worker = workers.get(role)
                    if worker is None or worker.poll() is not None:
                        workers[role] = _launch_resident_bitbucket_worker(command)
            except Exception:
                bitbucket_logger.exception("The resident Bitbucket/PDF queue supervisor failed")
            finally:
                close_old_connections()
            stop_event.wait(poll_seconds)
    finally:
        for worker in workers.values():
            _stop_resident_bitbucket_worker(worker)


class Command(BaseCommand):
    help = "Run OWL with its resident Confluence and Bitbucket/PDF background services."

    def add_arguments(self, parser):
        parser.add_argument(
            "addrport",
            nargs="?",
            default="127.0.0.1:8000",
            help="Optional port number, or ipaddr:port (default: 127.0.0.1:8000).",
        )

    def handle(self, *args, **options):
        stop_event = threading.Event()
        scheduler = threading.Thread(
            target=_scheduler_loop,
            kwargs={
                "stop_event": stop_event,
                "poll_seconds": settings.CONFLUENCE_REFRESH_SCHEDULER_POLL_SECONDS,
            },
            name="owl-refresh-scheduler",
            daemon=True,
        )
        scheduler.start()
        bitbucket_supervisor = threading.Thread(
            target=_bitbucket_queue_loop,
            kwargs={
                "stop_event": stop_event,
                "poll_seconds": settings.BITBUCKET_SUPERVISOR_POLL_SECONDS,
            },
            name="owl-bitbucket-supervisor",
            daemon=True,
        )
        bitbucket_supervisor.start()
        self.stdout.write(
            self.style.SUCCESS(
                "OWL started its Confluence scheduler and daily Bitbucket/PDF queue supervisor."
            )
        )
        try:
            call_command(
                "runserver",
                options["addrport"],
                use_reloader=False,
            )
        finally:
            stop_event.set()
            scheduler.join(timeout=5)
            bitbucket_supervisor.join(timeout=7)
