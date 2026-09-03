"""Run OWL's local web server together with its resident schedulers and workers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from bitbucket_search.models import (
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositorySyncJob,
    RepositorySyncJobStatus,
)
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import (
    stale_extraction_worker_pids,
    sweep_pdf_extraction_queue,
)
from bitbucket_search.services.pdf_search import ensure_search_index_available
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    resident_worker_supervisor_lock,
)
from bitbucket_search.services.repository_sync import (
    interrupt_repository_worker_leases,
    queue_due_daily_repository_refreshes,
    repository_status_snapshot,
    resident_repository_workers_active,
    set_resident_repository_workers_active,
    stale_repository_worker_pids,
)
from bookmark_manager.models import NotificationKind, NotificationState
from bookmark_manager.services.bookmark_refresh import queue_due_scheduled_refresh
from bookmark_manager.services.logging_events import get_logger as get_bookmark_logger
from bookmark_manager.services.logging_events import log_event as log_bookmark_event
from bookmark_manager.services.notifications import publish_notification
from core.process_supervision import RESIDENT_SUPERVISOR_PID_ENV
from semantic_search.models import SemanticIndexJob, SemanticIndexJobStatus
from semantic_search.services.jobs import (
    stale_semantic_worker_pids,
    sweep_semantic_index_queue,
)
from semantic_search.services.logging_events import get_logger as get_semantic_logger
from semantic_search.services.logging_events import log_event as log_semantic_event

logger = get_bookmark_logger("supervisor")
bitbucket_logger = get_logger("supervisor")
semantic_logger = get_semantic_logger("supervisor")
SCHEDULER_EVENT_KEY = "confluence-refresh:scheduler"
CAFFEINATE_PATH = Path("/usr/bin/caffeinate")
WINDOWS_ES_SYSTEM_REQUIRED = 0x00000001
WINDOWS_ES_DISPLAY_REQUIRED = 0x00000002
WINDOWS_ES_CONTINUOUS = 0x80000000
WINDOWS_AWAKE_EXECUTION_STATE = (
    WINDOWS_ES_CONTINUOUS | WINDOWS_ES_SYSTEM_REQUIRED | WINDOWS_ES_DISPLAY_REQUIRED
)


@dataclass(frozen=True, slots=True)
class _DisplayAwakeAssertion:
    backend: Literal["macos", "windows"]
    owner_thread_id: int
    process: subprocess.Popen[bytes] | None = None


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
        ("pdf-writer-1", "bitbucket_pdf_writer"),
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


def _background_work_active() -> bool:
    """Return whether durable OWL queues currently need the computer kept awake."""

    if RepositorySyncJob.objects.filter(
        status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
    ).exists():
        return True
    if PDFExtractionJob.objects.filter(
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
    ).exists():
        return True
    return bool(
        settings.SEMANTIC_SEARCH_ENABLED
        and SemanticIndexJob.objects.filter(
            status__in=(SemanticIndexJobStatus.QUEUED, SemanticIndexJobStatus.RUNNING)
        ).exists()
    )


def _display_awake_backend() -> Literal["macos", "windows"] | None:
    if not settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK:
        return None
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return None


def _set_windows_execution_state(flags: int) -> None:
    """Set the current Windows thread's continuous power requirements."""

    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = kernel.SetThreadExecutionState
    operation.argtypes = (ctypes.c_uint32,)
    operation.restype = ctypes.c_uint32
    if not operation(flags):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "SetThreadExecutionState failed")


def _launch_display_awake_assertion() -> _DisplayAwakeAssertion | None:
    """Hold display and idle-system assertions for OWL's active queues."""

    backend = _display_awake_backend()
    if backend is None:
        return None
    owner_thread_id = threading.get_ident()
    if backend == "windows":
        try:
            _set_windows_execution_state(WINDOWS_AWAKE_EXECUTION_STATE)
        except OSError as exc:
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "display_awake_assertion_start_failed",
                error=exc,
                assertion_backend=backend,
                assertion_thread_id=owner_thread_id,
            )
            return None
        assertion = _DisplayAwakeAssertion(
            backend=backend,
            owner_thread_id=owner_thread_id,
        )
        log_event(
            bitbucket_logger,
            logging.INFO,
            "display_awake_assertion_started",
            assertion_backend=backend,
            assertion_thread_id=owner_thread_id,
        )
        return assertion

    if not CAFFEINATE_PATH.is_file():
        return None
    try:
        process = subprocess.Popen(
            [str(CAFFEINATE_PATH), "-di", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "display_awake_assertion_start_failed",
            error=exc,
            assertion_backend=backend,
            assertion_thread_id=owner_thread_id,
        )
        return None
    assertion = _DisplayAwakeAssertion(
        backend=backend,
        owner_thread_id=owner_thread_id,
        process=process,
    )
    log_event(
        bitbucket_logger,
        logging.INFO,
        "display_awake_assertion_started",
        assertion_backend=backend,
        assertion_thread_id=owner_thread_id,
        assertion_pid=getattr(process, "pid", None),
    )
    return assertion


def _stop_display_awake_assertion(assertion: _DisplayAwakeAssertion | None) -> bool:
    """Release exactly OWL's temporary platform power assertion."""

    if assertion is None:
        return True
    if assertion.backend == "windows":
        current_thread_id = threading.get_ident()
        if current_thread_id != assertion.owner_thread_id:
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "display_awake_assertion_stop_failed",
                reason="owner_thread_mismatch",
                assertion_backend=assertion.backend,
                assertion_thread_id=assertion.owner_thread_id,
                current_thread_id=current_thread_id,
            )
            return False
        try:
            _set_windows_execution_state(WINDOWS_ES_CONTINUOUS)
        except OSError as exc:
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "display_awake_assertion_stop_failed",
                error=exc,
                assertion_backend=assertion.backend,
                assertion_thread_id=assertion.owner_thread_id,
            )
            return False
        log_event(
            bitbucket_logger,
            logging.INFO,
            "display_awake_assertion_stopped",
            assertion_backend=assertion.backend,
            assertion_thread_id=assertion.owner_thread_id,
        )
        return True

    process = assertion.process
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=2)
    except ProcessLookupError:
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "display_awake_assertion_stop_failed",
                error=exc,
                assertion_backend=assertion.backend,
                assertion_thread_id=assertion.owner_thread_id,
                assertion_pid=getattr(process, "pid", None),
            )
            return False
    except OSError as exc:
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "display_awake_assertion_stop_failed",
            error=exc,
            assertion_backend=assertion.backend,
            assertion_thread_id=assertion.owner_thread_id,
            assertion_pid=getattr(process, "pid", None),
        )
        return False
    log_event(
        bitbucket_logger,
        logging.INFO,
        "display_awake_assertion_stopped",
        assertion_backend=assertion.backend,
        assertion_thread_id=assertion.owner_thread_id,
        assertion_pid=getattr(process, "pid", None),
    )
    return True


def _reconcile_display_awake_assertion(
    assertion: _DisplayAwakeAssertion | None,
    *,
    work_active: bool,
) -> _DisplayAwakeAssertion | None:
    """Start, retain, replace, or release OWL's temporary awake assertion."""

    backend = _display_awake_backend()
    if backend is None or not work_active:
        return None if _stop_display_awake_assertion(assertion) else assertion
    if assertion is not None and assertion.backend == backend:
        if backend == "windows" and assertion.owner_thread_id == threading.get_ident():
            return assertion
        if (
            backend == "macos"
            and assertion.process is not None
            and assertion.process.poll() is None
        ):
            return assertion
    if assertion is not None:
        return_code = assertion.process.poll() if assertion.process is not None else None
        log_event(
            bitbucket_logger,
            logging.WARNING,
            "display_awake_assertion_exited",
            assertion_backend=assertion.backend,
            assertion_thread_id=assertion.owner_thread_id,
            assertion_pid=getattr(assertion.process, "pid", None),
            return_code=return_code,
        )
        if not _stop_display_awake_assertion(assertion):
            return assertion
    return _launch_display_awake_assertion()


def _reconcile_display_awake_for_current_queues(
    assertion: _DisplayAwakeAssertion | None,
) -> _DisplayAwakeAssertion | None:
    """Reconcile the assertion, staying awake when queue state is temporarily unknown."""

    if _display_awake_backend() is None:
        return _reconcile_display_awake_assertion(assertion, work_active=False)
    try:
        work_active = _background_work_active()
    except Exception as exc:
        # A transient database failure must not let an active extraction put the
        # machine to sleep. The next healthy supervisor pass releases this if idle.
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "display_awake_state_check_failed",
            error=exc,
        )
        work_active = True
    return _reconcile_display_awake_assertion(assertion, work_active=work_active)


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

    environment = os.environ.copy()
    environment[RESIDENT_SUPERVISOR_PID_ENV] = str(os.getpid())
    try:
        process = subprocess.Popen(
            arguments,
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
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
        with resident_worker_supervisor_lock():
            _run_bitbucket_queue_loop(stop_event, poll_seconds=poll_seconds)
    except RepositoryCheckoutBusy:
        # Another healthy OWL process owns this data root's pool. Suppress
        # request-triggered fallback workers in this process while the
        # watchdog waits to take ownership if that supervisor disappears.
        set_resident_repository_workers_active(True)
        log_event(
            bitbucket_logger,
            logging.WARNING,
            "bitbucket_supervisor_already_running",
            worker_pid=os.getpid(),
        )
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


def _bitbucket_supervisor_watchdog_loop(
    stop_event: threading.Event,
    *,
    poll_seconds: int,
) -> None:
    """Restart the queue supervisor if its controller loop escapes unexpectedly."""

    restart_delay = min(max(float(poll_seconds), 1.0), 5.0)
    while not stop_event.is_set():
        try:
            _bitbucket_queue_loop(stop_event, poll_seconds=poll_seconds)
        except Exception as exc:
            if stop_event.is_set():
                return
            log_event(
                bitbucket_logger,
                logging.ERROR,
                "bitbucket_supervisor_restart_scheduled",
                error=exc,
                delay_seconds=restart_delay,
            )
        else:
            if stop_event.is_set():
                return
            log_event(
                bitbucket_logger,
                logging.WARNING,
                "bitbucket_supervisor_restart_scheduled",
                reason="controller_stopped",
                delay_seconds=restart_delay,
            )
        stop_event.wait(restart_delay)


def _owned_worker_pids(
    workers: dict[str, subprocess.Popen[bytes]],
    *,
    exited: bool,
    role_prefix: str | None = None,
) -> tuple[int, ...]:
    return tuple(
        worker.pid
        for role, worker in workers.items()
        if role_prefix is None or role.startswith(role_prefix)
        if isinstance(getattr(worker, "pid", None), int) and ((worker.poll() is not None) is exited)
    )


def _stop_stale_owned_workers(
    workers: dict[str, subprocess.Popen[bytes]],
    stale_worker_pids: set[int],
) -> None:
    """Stop only live child processes tracked by this exact supervisor."""

    for role, worker in tuple(workers.items()):
        worker_pid = getattr(worker, "pid", None)
        if worker_pid not in stale_worker_pids or worker.poll() is not None:
            continue
        log_event(
            bitbucket_logger,
            logging.ERROR,
            "resident_worker_unresponsive",
            operation=role,
            worker_pid=worker_pid,
        )
        _stop_resident_bitbucket_worker(worker)


def _run_bitbucket_queue_loop(stop_event: threading.Event, *, poll_seconds: int) -> None:
    """Sweep durable queues and supervise their bounded resident controllers."""

    workers: dict[str, subprocess.Popen[bytes]] = {}
    display_awake_assertion: _DisplayAwakeAssertion | None = None
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
        # Protect already-durable work before recovery or worker launch can block.
        display_awake_assertion = _reconcile_display_awake_for_current_queues(
            display_awake_assertion
        )
        while not stop_event.is_set():
            stage = "daily_schedule"
            try:
                close_old_connections()
                # This process owns stale-lease recovery for the whole pass,
                # including the brief startup window before child controllers
                # have launched. Web status polling and supervised workers must
                # not race this pass by terminalizing a lease first.
                publish_repository_worker_state(True)
                # These calls are idempotent. The startup extraction sweep first
                # revokes every inherited RUNNING lease, so an old detached worker
                # cannot publish after OWL has restarted. Later sweeps use the
                # normal heartbeat timeout and bounded retry policy.
                queue_due_daily_repository_refreshes()
                exited_worker_pids = set(_owned_worker_pids(workers, exited=True))
                stale_worker_pids = (
                    set(stale_repository_worker_pids())
                    | set(stale_extraction_worker_pids())
                    | set(stale_semantic_worker_pids())
                )
                stale_owned_worker_pids = stale_worker_pids & set(
                    _owned_worker_pids(workers, exited=False)
                )
                _stop_stale_owned_workers(workers, stale_owned_worker_pids)
                interrupted_worker_pids = exited_worker_pids | stale_owned_worker_pids
                semantic_worker_pids = set(
                    _owned_worker_pids(
                        workers,
                        exited=True,
                        role_prefix="semantic-index-",
                    )
                ) | set(
                    _owned_worker_pids(
                        workers,
                        exited=False,
                        role_prefix="semantic-index-",
                    )
                )
                interrupted_semantic_worker_pids = interrupted_worker_pids & semantic_worker_pids
                if interrupted_worker_pids:
                    interrupt_repository_worker_leases(tuple(interrupted_worker_pids))
                stage = "repository_lease_recovery"
                repository_status_snapshot()
                stage = "pdf_lease_recovery"
                pdf_sweep_options: dict[str, object] = {
                    "interrupt_running": startup_pdf_sweep_pending,
                }
                if interrupted_worker_pids:
                    pdf_sweep_options["interrupt_worker_pids"] = tuple(interrupted_worker_pids)
                pdf_recovery = sweep_pdf_extraction_queue(**pdf_sweep_options)
                recovered_worker_pids = getattr(
                    pdf_recovery,
                    "interrupted_worker_pids",
                    (),
                )
                if not isinstance(recovered_worker_pids, (tuple, list, set)):
                    recovered_worker_pids = ()
                _stop_stale_owned_workers(
                    workers,
                    set(recovered_worker_pids),
                )
                startup_pdf_sweep_pending = False
                monotonic_now = time.monotonic()
                if monotonic_now >= next_semantic_reconcile_at or interrupted_semantic_worker_pids:
                    next_semantic_reconcile_at = monotonic_now + settings.SEMANTIC_RECONCILE_SECONDS
                    try:
                        semantic_sweep_options: dict[str, object] = {
                            "interrupt_running": startup_semantic_sweep_pending,
                        }
                        if interrupted_semantic_worker_pids:
                            semantic_sweep_options["interrupt_worker_pids"] = tuple(
                                interrupted_semantic_worker_pids
                            )
                        sweep_semantic_index_queue(**semantic_sweep_options)
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
                    try:
                        display_awake_assertion = _reconcile_display_awake_for_current_queues(
                            display_awake_assertion
                        )
                    finally:
                        close_old_connections()
            stop_event.wait(poll_seconds)
    finally:
        try:
            for worker in workers.values():
                _stop_resident_bitbucket_worker(worker)
        finally:
            try:
                _stop_display_awake_assertion(display_awake_assertion)
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
        try:
            search_index_ready = ensure_search_index_available()
        except Exception as exc:
            raise CommandError(
                "OWL could not prepare its local PDF search index. No background "
                "workers or website were started. Stop any other OWL instance and "
                "restart OWL; check the error log if this repeats."
            ) from exc
        if not search_index_ready:
            raise CommandError(
                "OWL could not prepare its local PDF search index. No background "
                "workers or website were started. Stop any other OWL instance and "
                "restart OWL; check the error log if this repeats."
            )
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
            target=_bitbucket_supervisor_watchdog_loop,
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
