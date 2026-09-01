"""Run OWL's local web server together with its resident schedulers and workers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import sweep_pdf_extraction_queue
from bitbucket_search.services.repository_sync import (
    queue_due_daily_repository_refreshes,
    repository_status_snapshot,
    resident_repository_workers_active,
    set_resident_repository_workers_active,
)
from bookmark_manager.models import NotificationKind, NotificationState
from bookmark_manager.services.bookmark_refresh import queue_due_scheduled_refresh
from bookmark_manager.services.logging_events import get_logger as get_bookmark_logger
from bookmark_manager.services.logging_events import log_event as log_bookmark_event
from bookmark_manager.services.notifications import publish_notification
from semantic_search.services.jobs import sweep_semantic_index_queue
from semantic_search.services.logging_events import get_logger as get_semantic_logger
from semantic_search.services.logging_events import log_event as log_semantic_event

logger = get_bookmark_logger("supervisor")
bitbucket_logger = get_logger("supervisor")
semantic_logger = get_semantic_logger("supervisor")
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

    started_at = time.monotonic()
    log_bookmark_event(logger, logging.INFO, "resident_refresh_scheduler_started")
    try:
        _run_refresh_scheduler_loop(stop_event, poll_seconds=poll_seconds)
    except BaseException as exc:
        log_bookmark_event(
            logger,
            logging.CRITICAL,
            "resident_refresh_scheduler_terminated",
            error=exc,
        )
        raise
    finally:
        log_bookmark_event(
            logger,
            logging.INFO,
            "resident_refresh_scheduler_stopped",
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )


def _run_refresh_scheduler_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    scheduler_failed = False
    while not stop_event.is_set():
        try:
            close_old_connections()
            queue_due_scheduled_refresh()
        except Exception as exc:
            log_bookmark_event(
                logger,
                logging.ERROR,
                "resident_refresh_scheduler_check_failed",
                error=exc,
                stage="schedule_check",
            )
            if not scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=False)
                except Exception as exc:
                    log_bookmark_event(
                        logger,
                        logging.ERROR,
                        "resident_refresh_scheduler_notification_failed",
                        error=exc,
                        stage="failure_notification",
                    )
            scheduler_failed = True
        else:
            if scheduler_failed:
                try:
                    _publish_scheduler_state(recovered=True)
                except Exception as exc:
                    log_bookmark_event(
                        logger,
                        logging.ERROR,
                        "resident_refresh_scheduler_notification_failed",
                        error=exc,
                        stage="recovery_notification",
                    )
                log_bookmark_event(logger, logging.INFO, "resident_refresh_scheduler_recovered")
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


def _resident_semantic_worker_specs() -> tuple[tuple[str, str], ...]:
    """Describe the independent local embedding pool shared by both applications."""

    if not settings.SEMANTIC_SEARCH_ENABLED:
        return ()
    return tuple(
        (f"semantic-index-{worker_number}", "semantic_index_worker")
        for worker_number in range(1, settings.SEMANTIC_MAX_WORKERS + 1)
    )


def _resident_worker_specs() -> tuple[tuple[str, str], ...]:
    return (*_resident_bitbucket_worker_specs(), *_resident_semantic_worker_specs())


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
    elif command in {"bitbucket_index_worker", "semantic_index_worker"}:
        arguments.append("--no-startup-sweep")

    try:
        process = subprocess.Popen(
            arguments,
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "resident_worker_spawn_failed",
            error=exc,
            operation=command,
        )
        raise
    log_event(
        bitbucket_logger,
        logging.INFO,
        "resident_worker_spawned",
        operation=command,
        worker_pid=getattr(process, "pid", None),
    )
    return process


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
    except ProcessLookupError:
        log_event(
            bitbucket_logger,
            logging.DEBUG,
            "resident_worker_already_stopped",
            worker_pid=getattr(process, "pid", None),
        )
        return
    except OSError as exc:
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "resident_worker_stop_failed",
            error=exc,
            worker_pid=getattr(process, "pid", None),
        )
        return
    except subprocess.TimeoutExpired as exc:
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "resident_worker_stop_timeout",
            error=exc,
            worker_pid=getattr(process, "pid", None),
        )
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            log_event(
                bitbucket_logger,
                logging.DEBUG,
                "resident_worker_already_stopped",
                worker_pid=getattr(process, "pid", None),
            )
            return
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "resident_worker_force_stop_failed",
                error=exc,
                worker_pid=getattr(process, "pid", None),
            )
            return
    log_event(
        bitbucket_logger,
        logging.INFO,
        "resident_worker_stopped",
        worker_pid=getattr(process, "pid", None),
    )


def _bitbucket_queue_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    started = time.monotonic()
    log_event(
        bitbucket_logger,
        logging.INFO,
        "bitbucket_supervisor_started",
        worker_pid=os.getpid(),
        worker_count=len(_resident_worker_specs()),
    )
    try:
        _run_bitbucket_queue_loop(stop_event, poll_seconds=poll_seconds)
    except Exception as exc:
        log_event(
            bitbucket_logger,
            logging.CRITICAL,
            "bitbucket_supervisor_crashed",
            error=exc,
            worker_pid=os.getpid(),
        )
        raise
    finally:
        log_event(
            bitbucket_logger,
            logging.INFO,
            "bitbucket_supervisor_stopped",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _run_bitbucket_queue_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    """Sweep durable queues and supervise their bounded resident controllers."""

    workers: dict[str, subprocess.Popen[bytes]] = {}
    startup_pdf_sweep_pending = True
    startup_semantic_sweep_pending = True
    next_semantic_reconcile_at = 0.0
    repository_workers_active = resident_repository_workers_active()
    failed_previous_pass = False
    semantic_failed_previous_pass = False

    def publish_repository_worker_state(active: bool) -> None:
        nonlocal repository_workers_active
        if active == repository_workers_active:
            return
        set_resident_repository_workers_active(active)
        repository_workers_active = active
        log_event(
            bitbucket_logger,
            logging.DEBUG,
            "resident_repository_worker_availability_changed",
            active_count=int(active),
        )

    def reconcile_repository_worker_state() -> None:
        publish_repository_worker_state(
            any(
                role.startswith("repository-sync-") and worker.poll() is None
                for role, worker in workers.items()
            )
        )

    # Clear a stale process-local marker before this supervisor owns a live
    # repository controller. It will be raised only after a successful launch.
    publish_repository_worker_state(False)
    try:
        while not stop_event.is_set():
            stage = "daily_schedule"
            try:
                close_old_connections()
                # These calls are idempotent. The startup extraction sweep first
                # revokes every inherited RUNNING lease, so an old detached worker
                # cannot publish after OWL has restarted. Later sweeps use the
                # normal heartbeat timeout and bounded retry policy.
                queue_due_daily_repository_refreshes()
                stage = "repository_lease_recovery"
                repository_status_snapshot()
                stage = "pdf_lease_recovery"
                sweep_pdf_extraction_queue(interrupt_running=startup_pdf_sweep_pending)
                startup_pdf_sweep_pending = False
                monotonic_now = time.monotonic()
                if monotonic_now >= next_semantic_reconcile_at:
                    next_semantic_reconcile_at = monotonic_now + settings.SEMANTIC_RECONCILE_SECONDS
                    try:
                        sweep_semantic_index_queue(interrupt_running=startup_semantic_sweep_pending)
                    except Exception as exc:
                        semantic_failed_previous_pass = True
                        log_semantic_event(
                            semantic_logger,
                            logging.ERROR,
                            "semantic_reconciliation_failed",
                            error=exc,
                            stage="lease_recovery",
                        )
                    else:
                        startup_semantic_sweep_pending = False
                        if semantic_failed_previous_pass:
                            log_semantic_event(
                                semantic_logger,
                                logging.INFO,
                                "semantic_reconciliation_recovered",
                                stage="lease_recovery",
                            )
                        semantic_failed_previous_pass = False

                for role, command in _resident_worker_specs():
                    worker = workers.get(role)
                    return_code = worker.poll() if worker is not None else None
                    if worker is not None and return_code is not None:
                        log_event(
                            bitbucket_logger,
                            logging.ERROR if return_code else logging.INFO,
                            "resident_worker_exited",
                            operation=command,
                            worker_pid=getattr(worker, "pid", None),
                            return_code=return_code,
                        )
                    if worker is None or return_code is not None:
                        stage = "resident_worker_launch"
                        workers[role] = _launch_resident_bitbucket_worker(command)
                if failed_previous_pass:
                    log_event(
                        bitbucket_logger,
                        logging.INFO,
                        "bitbucket_supervisor_recovered",
                        worker_count=len(workers),
                    )
                failed_previous_pass = False
            except Exception as exc:
                failed_previous_pass = True
                log_event(
                    bitbucket_logger,
                    logging.ERROR,
                    "bitbucket_supervisor_pass_failed",
                    error=exc,
                    stage=stage,
                )
            finally:
                try:
                    reconcile_repository_worker_state()
                finally:
                    close_old_connections()
            stop_event.wait(poll_seconds)
    finally:
        try:
            for worker in workers.values():
                _stop_resident_bitbucket_worker(worker)
        finally:
            publish_repository_worker_state(False)


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
                " Local PDF/bookmark semantic workers are supervised in parallel."
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
