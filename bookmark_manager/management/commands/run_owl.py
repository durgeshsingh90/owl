"""Run OWL's local web server together with its resident schedulers and workers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from bitbucket_search.models import (
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPipelineRecovery,
    PDFPipelineRecoveryState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
)
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import (
    stale_extraction_worker_pids,
    sweep_pdf_extraction_queue,
)
from bitbucket_search.services.pdf_pipeline_controller import (
    ControllerEvaluationError,
    PDFPipelineController,
)
from bitbucket_search.services.pdf_pipeline_metrics import (
    _eligible_input_snapshot,
    sample_pipeline_metrics,
)
from bitbucket_search.services.pdf_pipeline_runs import reconcile_open_pipeline_runs
from bitbucket_search.services.pdf_recovery import (
    RecoveryConflict,
    RecoveryNotDue,
    RecoveryReasonCode,
    RecoveryScope,
    RecoveryTransitionRejected,
    begin_recovery_attempt,
    configured_recovery_stability_seconds,
    ensure_recovery_scope,
    escalate_correlated_recovery,
    extraction_slot_scope,
    fail_recovery_attempt,
    record_recovery_incident,
    succeed_recovery_attempt,
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


@dataclass(frozen=True, slots=True)
class _WorkerRecoveryProbe:
    """One supervised replacement process waiting to pass its stability gate."""

    scope: str
    attempt_id: uuid.UUID
    generation: int
    started_monotonic: float
    started_at: datetime


@dataclass(frozen=True, slots=True)
class _WorkerLaunchPermission:
    """Fail-closed launch decision derived from one durable recovery scope."""

    allowed: bool
    probe: _WorkerRecoveryProbe | None = None


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


def _pdf_recovery_scope_for_role(role: str) -> str | None:
    """Map only PDF components into this feature's independent recovery budget."""

    if role == "pdf-writer-1":
        return RecoveryScope.PUBLISHER.value
    if role.startswith("pdf-index-"):
        try:
            slot = int(role.removeprefix("pdf-index-"))
        except ValueError:
            return None
        return extraction_slot_scope(slot)
    return None


def _prepare_pdf_worker_launch(
    role: str,
    *,
    monotonic_now: float | None = None,
    active_parent_scopes: frozenset[str] = frozenset(),
) -> _WorkerLaunchPermission:
    """Return whether an absent PDF role may launch under its durable circuit."""

    scope = _pdf_recovery_scope_for_role(role)
    if scope is None or not getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
        return _WorkerLaunchPermission(allowed=True)

    parent_scopes = [RecoveryScope.PIPELINE.value]
    if scope.startswith("extraction_slot:"):
        parent_scopes.extend((RecoveryScope.EXTRACTION_POOL.value, RecoveryScope.PUBLISHER.value))
    for parent_scope in parent_scopes:
        parent = ensure_recovery_scope(parent_scope)
        parent_probe_active = bool(
            parent_scope in active_parent_scopes
            and parent.state
            in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
        )
        # A publisher probe may drain durable staged output, but new parser
        # admission stays closed until that downstream probe is stable.
        if parent_scope == RecoveryScope.PUBLISHER and parent.state != (
            PDFPipelineRecoveryState.HEALTHY
        ):
            return _WorkerLaunchPermission(allowed=False)
        if parent.state != PDFPipelineRecoveryState.HEALTHY and not parent_probe_active:
            return _WorkerLaunchPermission(allowed=False)

    # A broader probe owns the experiment. Do not nest a child probe and count
    # one failed process against two recovery budgets.
    if active_parent_scopes:
        recovery = ensure_recovery_scope(scope)
        return _WorkerLaunchPermission(allowed=recovery.state == PDFPipelineRecoveryState.HEALTHY)

    return _prepare_recovery_scope_launch(scope, monotonic_now=monotonic_now)


def _maintain_parent_recovery_probe(
    scope: str,
    probe: _WorkerRecoveryProbe | None,
    *,
    monotonic_now: float | None = None,
) -> _WorkerLaunchPermission:
    """Keep one in-memory owner for a durable group-scope probe."""

    recovery = ensure_recovery_scope(scope)
    if probe is not None:
        if (
            recovery.state
            in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
            and recovery.active_attempt_id == probe.attempt_id
            and recovery.generation == probe.generation
        ):
            return _WorkerLaunchPermission(allowed=True, probe=probe)
        probe = None
    return _prepare_recovery_scope_launch(scope, monotonic_now=monotonic_now)


def _prepare_recovery_scope_launch(
    scope: str,
    *,
    monotonic_now: float | None = None,
) -> _WorkerLaunchPermission:
    """Resolve a canonical component scope into one launch/stability decision."""

    recovery = ensure_recovery_scope(scope)
    if recovery.state == PDFPipelineRecoveryState.HEALTHY:
        return _WorkerLaunchPermission(allowed=True)
    if recovery.state == PDFPipelineRecoveryState.PAUSED:
        return _WorkerLaunchPermission(allowed=False)

    # A recovering record with no corresponding live child means the prior
    # stability probe was interrupted. Finalize that same attempt exactly once
    # before considering another launch; never manufacture a fresh attempt.
    if recovery.state in {
        PDFPipelineRecoveryState.RECOVERING,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }:
        if recovery.active_attempt_id is not None:
            fail_recovery_attempt(
                scope,
                attempt_id=recovery.active_attempt_id,
                expected_generation=recovery.generation,
                reason_code=RecoveryReasonCode.PROCESS_EXIT,
            )
        return _WorkerLaunchPermission(allowed=False)

    if recovery.state not in {
        PDFPipelineRecoveryState.RETRY_WAIT,
        PDFPipelineRecoveryState.RESUME_REQUESTED,
    }:
        return _WorkerLaunchPermission(allowed=False)
    try:
        transition = begin_recovery_attempt(
            scope,
            expected_generation=recovery.generation,
        )
    except RecoveryNotDue:
        return _WorkerLaunchPermission(allowed=False)
    attempt_id = transition.recovery.active_attempt_id
    if attempt_id is None:
        return _WorkerLaunchPermission(allowed=False)
    return _WorkerLaunchPermission(
        allowed=True,
        probe=_WorkerRecoveryProbe(
            scope=scope,
            attempt_id=attempt_id,
            generation=transition.recovery.generation,
            started_monotonic=(time.monotonic() if monotonic_now is None else monotonic_now),
            started_at=timezone.now(),
        ),
    )


def _record_pdf_worker_failure(
    role: str,
    *,
    reason_code: RecoveryReasonCode,
    probe: _WorkerRecoveryProbe | None,
) -> None:
    """Persist one real failed process/probe without counting supervisor polls."""

    scope = _pdf_recovery_scope_for_role(role)
    if scope is None or not getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
        return
    owning_scopes = [RecoveryScope.PIPELINE.value]
    if scope.startswith("extraction_slot:"):
        owning_scopes.append(RecoveryScope.EXTRACTION_POOL.value)
    for owning_scope in owning_scopes:
        owner = ensure_recovery_scope(owning_scope)
        if owner.state == PDFPipelineRecoveryState.HEALTHY:
            continue
        if (
            owner.state
            in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
            and owner.active_attempt_id is not None
        ):
            fail_recovery_attempt(
                owning_scope,
                attempt_id=owner.active_attempt_id,
                expected_generation=owner.generation,
                reason_code=reason_code,
            )
        # A broader episode already owns this component's incident stream.
        # Do not reopen a child budget or count the same process exit twice.
        return
    _record_recovery_scope_failure(scope, reason_code=reason_code, probe=probe)
    if scope.startswith("extraction_slot:"):
        _escalate_correlated_extraction_failures(reason_code)


def _escalate_correlated_extraction_failures(
    reason_code: RecoveryReasonCode,
    *,
    occurred_at: datetime | None = None,
) -> None:
    """Move a burst of equivalent slot failures into one pool episode."""

    now = occurred_at or timezone.now()
    cutoff = now - timedelta(seconds=int(settings.PDF_PIPELINE_RECOVERY_CORRELATION_WINDOW_SECONDS))
    required = int(settings.PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT)
    scopes = tuple(
        PDFPipelineRecovery.objects.filter(
            scope__startswith="extraction_slot:",
            reason_code=reason_code.value,
            last_failure_at__gte=cutoff,
        )
        .exclude(state=PDFPipelineRecoveryState.HEALTHY)
        .order_by("last_failure_at", "scope")
        .values_list("scope", flat=True)
    )
    if len(scopes) < required:
        return
    escalate_correlated_recovery(
        list(scopes),
        target_scope=RecoveryScope.EXTRACTION_POOL,
        reason_code=reason_code,
        correlation_id=uuid.uuid4(),
        occurred_at=now,
    )


def _record_recovery_scope_failure(
    scope: str,
    *,
    reason_code: RecoveryReasonCode,
    probe: _WorkerRecoveryProbe | None,
) -> None:
    """Record one detected component incident or finalize its active probe."""

    if probe is not None:
        current = ensure_recovery_scope(scope)
        if (
            current.state
            in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
            and current.active_attempt_id == probe.attempt_id
        ):
            fail_recovery_attempt(
                scope,
                attempt_id=probe.attempt_id,
                expected_generation=current.generation,
                reason_code=reason_code,
            )
            return
        if current.state != PDFPipelineRecoveryState.HEALTHY:
            return
    recovery = ensure_recovery_scope(scope)
    if recovery.state == PDFPipelineRecoveryState.HEALTHY:
        record_recovery_incident(
            scope,
            reason_code=reason_code,
            incident_id=uuid.uuid4(),
            expected_generation=recovery.generation,
        )


def _complete_stable_pdf_worker_probes(
    workers: dict[str, subprocess.Popen[bytes]],
    probes: dict[str, _WorkerRecoveryProbe],
    *,
    monotonic_now: float | None = None,
) -> None:
    """Close replacement episodes only after the demand-aware stability gate."""

    now = time.monotonic() if monotonic_now is None else monotonic_now
    stability_seconds = configured_recovery_stability_seconds()
    for role, probe in tuple(probes.items()):
        worker = workers.get(role)
        if worker is None or worker.poll() is not None:
            continue
        if now - probe.started_monotonic < stability_seconds:
            continue
        if not _worker_probe_progress_confirmed(role, worker, probe):
            # Allow one extra stability window for a process that is between
            # short jobs. If eligible demand still produces no fresh owned
            # heartbeat or durable boundary, fail the already-counted probe.
            if now - probe.started_monotonic < stability_seconds * 2:
                continue
            _stop_resident_bitbucket_worker(worker)
            fail_recovery_attempt(
                probe.scope,
                attempt_id=probe.attempt_id,
                expected_generation=probe.generation,
                reason_code=RecoveryReasonCode.NO_FORWARD_PROGRESS,
            )
            probes.pop(role, None)
            continue
        succeed_recovery_attempt(
            probe.scope,
            attempt_id=probe.attempt_id,
            expected_generation=probe.generation,
            stability_confirmed=True,
        )
        probes.pop(role, None)


def _worker_probe_progress_confirmed(
    role: str,
    worker: subprocess.Popen[bytes],
    probe: _WorkerRecoveryProbe,
) -> bool:
    """Require progress under demand; permit a full stable idle window otherwise."""

    worker_pid = getattr(worker, "pid", None)
    fresh_cutoff = timezone.now() - timedelta(
        seconds=int(getattr(settings, "PDF_EXTRACTION_JOB_LEASE_SECONDS", 30))
    )
    if role == "pdf-writer-1":
        candidates = PDFExtractionJob.objects.filter(
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
        )
        if not candidates.exists():
            return True
        return bool(
            candidates.filter(
                worker_pid=worker_pid,
                heartbeat_at__gte=fresh_cutoff,
            ).exists()
            or PDFExtractionJob.objects.filter(published_at__gte=probe.started_at).exists()
        )

    eligible_jobs, _oldest = _eligible_input_snapshot()
    owned = PDFExtractionJob.objects.filter(
        status=PDFExtractionJobStatus.RUNNING,
        worker_pid=worker_pid,
        heartbeat_at__gte=fresh_cutoff,
    ).exclude(phase=PDFExtractionJobPhase.PUBLISHING)
    if eligible_jobs <= 0 and not owned.exists():
        return True
    return bool(
        owned.exists() or PDFExtractionJob.objects.filter(staged_at__gte=probe.started_at).exists()
    )


def _complete_parent_recovery_probe(
    scope: str,
    workers: dict[str, subprocess.Popen[bytes]],
    probe: _WorkerRecoveryProbe | None,
    *,
    monotonic_now: float | None = None,
) -> _WorkerRecoveryProbe | None:
    """Finish one pool/pipeline probe only after demand-aware progress."""

    if probe is None:
        return None
    now = time.monotonic() if monotonic_now is None else monotonic_now
    stability_seconds = configured_recovery_stability_seconds()
    if now - probe.started_monotonic < stability_seconds:
        return probe
    role_prefixes = (
        ("pdf-index-",) if scope == RecoveryScope.EXTRACTION_POOL else ("pdf-index-", "pdf-writer-")
    )
    live_workers = {
        role: worker
        for role, worker in workers.items()
        if role.startswith(role_prefixes) and worker.poll() is None
    }
    progress_confirmed = bool(live_workers) and _parent_probe_progress_confirmed(
        scope,
        live_workers,
        probe,
    )
    if not progress_confirmed:
        if now - probe.started_monotonic < stability_seconds * 2:
            return probe
        for worker in live_workers.values():
            _stop_resident_bitbucket_worker(worker)
        fail_recovery_attempt(
            probe.scope,
            attempt_id=probe.attempt_id,
            expected_generation=probe.generation,
            reason_code=RecoveryReasonCode.NO_FORWARD_PROGRESS,
        )
        return None
    succeed_recovery_attempt(
        probe.scope,
        attempt_id=probe.attempt_id,
        expected_generation=probe.generation,
        stability_confirmed=True,
    )
    return None


def _parent_probe_progress_confirmed(
    scope: str,
    workers: dict[str, subprocess.Popen[bytes]],
    probe: _WorkerRecoveryProbe,
) -> bool:
    """Accept idle stability or fresh owned work at a durable boundary."""

    worker_pids = tuple(
        worker.pid for worker in workers.values() if isinstance(getattr(worker, "pid", None), int)
    )
    eligible_jobs, _oldest = _eligible_input_snapshot()
    publishing_exists = PDFExtractionJob.objects.filter(
        status=PDFExtractionJobStatus.RUNNING,
        phase=PDFExtractionJobPhase.PUBLISHING,
    ).exists()
    demand_exists = eligible_jobs > 0 or (
        scope != RecoveryScope.EXTRACTION_POOL and publishing_exists
    )
    if not demand_exists:
        return True
    fresh_cutoff = max(
        probe.started_at,
        timezone.now()
        - timedelta(seconds=int(getattr(settings, "PDF_EXTRACTION_JOB_LEASE_SECONDS", 30))),
    )
    owned = PDFExtractionJob.objects.filter(
        status=PDFExtractionJobStatus.RUNNING,
        worker_pid__in=worker_pids,
        heartbeat_at__gte=fresh_cutoff,
    )
    if scope == RecoveryScope.EXTRACTION_POOL:
        owned = owned.exclude(phase=PDFExtractionJobPhase.PUBLISHING)
    return bool(
        owned.exists()
        or PDFExtractionJob.objects.filter(staged_at__gte=probe.started_at).exists()
        or (
            scope != RecoveryScope.EXTRACTION_POOL
            and PDFExtractionJob.objects.filter(published_at__gte=probe.started_at).exists()
        )
    )


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


def _launch_resident_bitbucket_worker(
    command: str,
    *,
    role: str | None = None,
) -> subprocess.Popen[bytes]:
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
        if command == "bitbucket_index_worker" and role and role.startswith("pdf-index-"):
            arguments.extend(("--slot-number", role.removeprefix("pdf-index-")))

    environment = os.environ.copy()
    environment[RESIDENT_SUPERVISOR_PID_ENV] = str(os.getpid())
    # Each management-command child opens its own Django connection after exec.
    # Do not carry this supervisor thread's SQLite handle across the spawn.
    if connection.in_atomic_block:
        close_old_connections()
    else:
        connections.close_all()
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


def _bitbucket_queue_loop(
    stop_event: threading.Event,
    *,
    poll_seconds: int,
    recovery_probe: _WorkerRecoveryProbe | None = None,
) -> bool:
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
            _run_bitbucket_queue_loop(
                stop_event,
                poll_seconds=poll_seconds,
                supervisor_recovery_probe=recovery_probe,
            )
        return True
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
        return False
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
    """Recover the queue supervisor with persisted backoff and a bounded circuit."""

    restart_delay = min(max(float(poll_seconds), 1.0), 5.0)
    while not stop_event.is_set():
        recovery_probe: _WorkerRecoveryProbe | None = None
        if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
            try:
                permission = _prepare_recovery_scope_launch(RecoveryScope.SUPERVISOR.value)
            except Exception as exc:
                # Without readable canonical control state, launching another
                # controller could bypass an open circuit. Stay fail-closed.
                log_event(
                    bitbucket_logger,
                    logging.ERROR,
                    "bitbucket_supervisor_recovery_state_unavailable",
                    error=exc,
                )
                stop_event.wait(restart_delay)
                continue
            if not permission.allowed:
                stop_event.wait(restart_delay)
                continue
            recovery_probe = permission.probe
        try:
            call_options: dict[str, object] = {"poll_seconds": poll_seconds}
            if recovery_probe is not None:
                call_options["recovery_probe"] = recovery_probe
            acquired = _bitbucket_queue_loop(stop_event, **call_options)
        except Exception as exc:
            if stop_event.is_set():
                return
            if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                try:
                    _record_recovery_scope_failure(
                        RecoveryScope.SUPERVISOR.value,
                        reason_code=RecoveryReasonCode.SUPERVISOR_LOOP_FAILED,
                        probe=recovery_probe,
                    )
                except Exception as recovery_exc:
                    log_event(
                        bitbucket_logger,
                        logging.ERROR,
                        "bitbucket_supervisor_recovery_record_failed",
                        error=recovery_exc,
                    )
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
            if acquired is False:
                if recovery_probe is not None:
                    try:
                        _record_recovery_scope_failure(
                            RecoveryScope.SUPERVISOR.value,
                            reason_code=RecoveryReasonCode.TEMPORARY_RESOURCE,
                            probe=recovery_probe,
                        )
                    except Exception as recovery_exc:
                        log_event(
                            bitbucket_logger,
                            logging.ERROR,
                            "bitbucket_supervisor_recovery_record_failed",
                            error=recovery_exc,
                        )
                stop_event.wait(restart_delay)
                continue
            if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                try:
                    _record_recovery_scope_failure(
                        RecoveryScope.SUPERVISOR.value,
                        reason_code=RecoveryReasonCode.SUPERVISOR_LOOP_FAILED,
                        probe=recovery_probe,
                    )
                except Exception as recovery_exc:
                    log_event(
                        bitbucket_logger,
                        logging.ERROR,
                        "bitbucket_supervisor_recovery_record_failed",
                        error=recovery_exc,
                    )
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
) -> tuple[str, ...]:
    """Stop only live child processes tracked by this exact supervisor."""

    stopped_roles: list[str] = []
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
        stopped_roles.append(role)
    return tuple(stopped_roles)


def _run_bitbucket_queue_loop(
    stop_event: threading.Event,
    *,
    poll_seconds: int,
    supervisor_recovery_probe: _WorkerRecoveryProbe | None = None,
) -> None:
    """Sweep durable queues and supervise their bounded resident controllers."""

    workers: dict[str, subprocess.Popen[bytes]] = {}
    recovery_probes: dict[str, _WorkerRecoveryProbe] = {}
    pending_failure_reasons: dict[str, RecoveryReasonCode] = {}
    display_awake_assertion: _DisplayAwakeAssertion | None = None
    startup_pdf_sweep_pending = True
    startup_semantic_sweep_pending = True
    next_semantic_reconcile_at = 0.0
    repository_workers_active = resident_repository_workers_active()
    failed_previous_pass = False
    semantic_failed_previous_pass = False
    pipeline_controller = PDFPipelineController()
    controller_recovery_probe: _WorkerRecoveryProbe | None = None
    pipeline_recovery_probe: _WorkerRecoveryProbe | None = None
    extraction_pool_recovery_probe: _WorkerRecoveryProbe | None = None

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
            stage = "pdf_recovery_preflight"
            try:
                close_old_connections()
                if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                    # Canonical circuit state is read before stale leases are
                    # reconciled or any affected role is spawned. A database
                    # failure therefore fails closed for this whole pass.
                    for role, _command in _resident_bitbucket_worker_specs():
                        scope = _pdf_recovery_scope_for_role(role)
                        if scope is not None:
                            ensure_recovery_scope(scope)
                    ensure_recovery_scope(RecoveryScope.PIPELINE.value)
                    ensure_recovery_scope(RecoveryScope.EXTRACTION_POOL.value)
                    ensure_recovery_scope(RecoveryScope.CONTROLLER.value)
                # This process owns stale-lease recovery for the whole pass,
                # including the brief startup window before child controllers
                # have launched. Web status polling and supervised workers must
                # not race this pass by terminalizing a lease first.
                publish_repository_worker_state(True)
                # These calls are idempotent. The startup extraction sweep first
                # revokes every inherited RUNNING lease, so an old detached worker
                # cannot publish after OWL has restarted. Later sweeps use the
                # normal heartbeat timeout and bounded retry policy.
                stage = "daily_schedule"
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
                stopped_stale_roles = _stop_stale_owned_workers(
                    workers,
                    stale_owned_worker_pids,
                )
                pending_failure_reasons.update(
                    {
                        role: RecoveryReasonCode.STALE_HEARTBEAT
                        for role in stopped_stale_roles
                        if _pdf_recovery_scope_for_role(role) is not None
                    }
                )
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
                additionally_stopped_roles = _stop_stale_owned_workers(
                    workers,
                    set(recovered_worker_pids),
                )
                pending_failure_reasons.update(
                    {
                        role: RecoveryReasonCode.STALE_HEARTBEAT
                        for role in additionally_stopped_roles
                        if _pdf_recovery_scope_for_role(role) is not None
                    }
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

                active_parent_scopes: set[str] = set()
                if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                    pipeline_permission = _maintain_parent_recovery_probe(
                        RecoveryScope.PIPELINE.value,
                        pipeline_recovery_probe,
                    )
                    pipeline_recovery_probe = pipeline_permission.probe
                    if pipeline_recovery_probe is not None:
                        active_parent_scopes.add(RecoveryScope.PIPELINE.value)
                    if pipeline_permission.allowed:
                        pool_permission = _maintain_parent_recovery_probe(
                            RecoveryScope.EXTRACTION_POOL.value,
                            extraction_pool_recovery_probe,
                        )
                        extraction_pool_recovery_probe = pool_permission.probe
                        if extraction_pool_recovery_probe is not None:
                            active_parent_scopes.add(RecoveryScope.EXTRACTION_POOL.value)

                failed_owner_scope_this_pass: str | None = None

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
                        role_scope = _pdf_recovery_scope_for_role(role)
                        if role_scope is not None:
                            stage = "pdf_worker_failure_record"
                            try:
                                failure_reason = pending_failure_reasons.pop(
                                    role,
                                    RecoveryReasonCode.PROCESS_EXIT,
                                )
                                child_probe = recovery_probes.pop(role, None)
                                if pipeline_recovery_probe is not None:
                                    _record_recovery_scope_failure(
                                        RecoveryScope.PIPELINE.value,
                                        reason_code=failure_reason,
                                        probe=pipeline_recovery_probe,
                                    )
                                    pipeline_recovery_probe = None
                                    failed_owner_scope_this_pass = RecoveryScope.PIPELINE.value
                                elif failed_owner_scope_this_pass == RecoveryScope.PIPELINE.value:
                                    pass
                                elif (
                                    role_scope.startswith("extraction_slot:")
                                    and extraction_pool_recovery_probe is not None
                                ):
                                    _record_recovery_scope_failure(
                                        RecoveryScope.EXTRACTION_POOL.value,
                                        reason_code=failure_reason,
                                        probe=extraction_pool_recovery_probe,
                                    )
                                    extraction_pool_recovery_probe = None
                                    failed_owner_scope_this_pass = (
                                        RecoveryScope.EXTRACTION_POOL.value
                                    )
                                elif (
                                    failed_owner_scope_this_pass
                                    == RecoveryScope.EXTRACTION_POOL.value
                                    and role_scope.startswith("extraction_slot:")
                                ):
                                    pass
                                else:
                                    _record_pdf_worker_failure(
                                        role,
                                        reason_code=failure_reason,
                                        probe=child_probe,
                                    )
                            except (RecoveryConflict, RecoveryTransitionRejected) as exc:
                                log_event(
                                    bitbucket_logger,
                                    logging.WARNING,
                                    "pdf_worker_recovery_state_changed",
                                    error=exc,
                                    operation=role,
                                )
                        workers.pop(role, None)
                        worker = None
                    if worker is not None:
                        continue

                    stage = "resident_worker_launch_permission"
                    try:
                        permission = _prepare_pdf_worker_launch(
                            role,
                            active_parent_scopes=frozenset(active_parent_scopes),
                        )
                    except (RecoveryConflict, RecoveryTransitionRejected) as exc:
                        log_event(
                            bitbucket_logger,
                            logging.WARNING,
                            "resident_worker_launch_deferred",
                            error=exc,
                            operation=role,
                            reason="recovery_state_changed",
                        )
                        continue
                    if not permission.allowed:
                        continue
                    stage = "resident_worker_launch"
                    try:
                        launched_worker = _launch_resident_bitbucket_worker(command, role=role)
                    except OSError:
                        if _pdf_recovery_scope_for_role(role) is None:
                            raise
                        try:
                            role_scope = _pdf_recovery_scope_for_role(role)
                            if pipeline_recovery_probe is not None:
                                _record_recovery_scope_failure(
                                    RecoveryScope.PIPELINE.value,
                                    reason_code=RecoveryReasonCode.LAUNCH_FAILED,
                                    probe=pipeline_recovery_probe,
                                )
                                pipeline_recovery_probe = None
                                failed_owner_scope_this_pass = RecoveryScope.PIPELINE.value
                            elif (
                                role_scope is not None
                                and role_scope.startswith("extraction_slot:")
                                and extraction_pool_recovery_probe is not None
                            ):
                                _record_recovery_scope_failure(
                                    RecoveryScope.EXTRACTION_POOL.value,
                                    reason_code=RecoveryReasonCode.LAUNCH_FAILED,
                                    probe=extraction_pool_recovery_probe,
                                )
                                extraction_pool_recovery_probe = None
                                failed_owner_scope_this_pass = RecoveryScope.EXTRACTION_POOL.value
                            else:
                                _record_pdf_worker_failure(
                                    role,
                                    reason_code=RecoveryReasonCode.LAUNCH_FAILED,
                                    probe=permission.probe,
                                )
                        except (RecoveryConflict, RecoveryTransitionRejected) as exc:
                            log_event(
                                bitbucket_logger,
                                logging.WARNING,
                                "pdf_worker_recovery_state_changed",
                                error=exc,
                                operation=role,
                            )
                        continue
                    workers[role] = launched_worker
                    if permission.probe is not None:
                        recovery_probes[role] = permission.probe

                stage = "pdf_worker_stability"
                _complete_stable_pdf_worker_probes(workers, recovery_probes)
                extraction_pool_recovery_probe = _complete_parent_recovery_probe(
                    RecoveryScope.EXTRACTION_POOL.value,
                    workers,
                    extraction_pool_recovery_probe,
                )
                pipeline_recovery_probe = _complete_parent_recovery_probe(
                    RecoveryScope.PIPELINE.value,
                    workers,
                    pipeline_recovery_probe,
                )
                supervisor_recovery_probe = _complete_parent_recovery_probe(
                    RecoveryScope.SUPERVISOR.value,
                    workers,
                    supervisor_recovery_probe,
                )
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
                        try:
                            reconcile_open_pipeline_runs()
                        except Exception as exc:
                            log_event(
                                bitbucket_logger,
                                logging.ERROR,
                                "pdf_pipeline_run_reconciliation_failed",
                                error=exc,
                                stage="run_reconciliation",
                            )
                        controller_for_sample: PDFPipelineController | None = pipeline_controller
                        if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                            try:
                                if controller_recovery_probe is None:
                                    permission = _prepare_recovery_scope_launch(
                                        RecoveryScope.CONTROLLER.value
                                    )
                                    controller_for_sample = (
                                        pipeline_controller if permission.allowed else None
                                    )
                                    controller_recovery_probe = permission.probe
                            except (RecoveryConflict, RecoveryTransitionRejected) as exc:
                                controller_for_sample = None
                                log_event(
                                    bitbucket_logger,
                                    logging.WARNING,
                                    "pdf_controller_evaluation_deferred",
                                    error=exc,
                                    reason="recovery_state_changed",
                                )
                            except Exception as exc:
                                # Canonical circuit state is required before
                                # evaluating. An unreadable store fails closed.
                                controller_for_sample = None
                                log_event(
                                    bitbucket_logger,
                                    logging.ERROR,
                                    "pdf_controller_recovery_state_unavailable",
                                    error=exc,
                                )
                        try:
                            sample_pipeline_metrics(
                                resident_roles=workers,
                                controller=controller_for_sample,
                            )
                        except ControllerEvaluationError as exc:
                            if getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
                                try:
                                    _record_recovery_scope_failure(
                                        RecoveryScope.CONTROLLER.value,
                                        reason_code=RecoveryReasonCode.ERROR_LOOP,
                                        probe=controller_recovery_probe,
                                    )
                                except (
                                    RecoveryConflict,
                                    RecoveryTransitionRejected,
                                ) as recovery_exc:
                                    log_event(
                                        bitbucket_logger,
                                        logging.WARNING,
                                        "pdf_controller_recovery_state_changed",
                                        error=recovery_exc,
                                    )
                            controller_recovery_probe = None
                            log_event(
                                bitbucket_logger,
                                logging.ERROR,
                                "pdf_controller_evaluation_failed",
                                error=exc,
                                stage="controller",
                            )
                        except Exception as exc:
                            log_event(
                                bitbucket_logger,
                                logging.ERROR,
                                "pdf_pipeline_metrics_sample_failed",
                                error=exc,
                                stage="metrics",
                            )
                        else:
                            if (
                                controller_recovery_probe is not None
                                and time.monotonic() - controller_recovery_probe.started_monotonic
                                >= configured_recovery_stability_seconds()
                            ):
                                try:
                                    succeed_recovery_attempt(
                                        controller_recovery_probe.scope,
                                        attempt_id=controller_recovery_probe.attempt_id,
                                        expected_generation=controller_recovery_probe.generation,
                                        stability_confirmed=True,
                                    )
                                except (RecoveryConflict, RecoveryTransitionRejected) as exc:
                                    log_event(
                                        bitbucket_logger,
                                        logging.WARNING,
                                        "pdf_controller_recovery_state_changed",
                                        error=exc,
                                    )
                                controller_recovery_probe = None
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
