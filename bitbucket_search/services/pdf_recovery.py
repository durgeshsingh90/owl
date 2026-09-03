"""Durable, generation-safe recovery state for supervised PDF components.

This module deliberately owns only sparse component-recovery transitions.  The
existing per-document extraction retry policy remains in ``pdf_indexing`` and
must not consume this circuit's attempt budget.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from bitbucket_search.models import (
    PDFPipelineRecovery,
    PDFPipelineRecoveryEvent,
    PDFPipelineRecoveryEventKind,
    PDFPipelineRecoveryState,
)
from bitbucket_search.services import pdf_recovery_fallback
from bitbucket_search.services.repository_lock import _file_lock

logger = logging.getLogger(__name__)

DEFAULT_PAUSE_AFTER_ATTEMPTS: Final = 25
DEFAULT_BACKOFF_BASE_SECONDS: Final = 1
DEFAULT_BACKOFF_MAX_SECONDS: Final = 300
DEFAULT_BACKOFF_JITTER_FRACTION: Final = 0.20
DEFAULT_STABILITY_SECONDS: Final = 60
MAX_PAUSE_AFTER_ATTEMPTS: Final = 10_000
MAX_BACKOFF_SECONDS: Final = 86_400

_SCOPE_PATTERN = re.compile(
    r"(?:pipeline|supervisor|controller|extraction_pool|publisher|"
    r"extraction_slot:[1-9][0-9]{0,3}|repository:[1-9][0-9]{0,18})"
)
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~:+/=-]{16,256}")
_RECOVERY_EVENT_KEY_PATTERN = re.compile(
    r"pdf-pipeline-recovery:(?P<episode>[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
)
_RESUME_ACTION_TYPE: Final = "pdf_pipeline_resume"
_POPUP_CLAIM_ACTION_TYPE: Final = "pdf_pipeline_recovery_popup_claim"
_POPUP_ACKNOWLEDGE_ACTION_TYPE: Final = "pdf_pipeline_recovery_popup_acknowledge"
_RECOVERY_DETAILS_PATH: Final = "/pdfs/status/"
_RESUME_HMAC_SALT: Final = "owl.pdf-pipeline-recovery.resume.v1"
_MINIMUM_RESUME_DISK_BYTES: Final = 64 * 1024 * 1024


class RecoveryScope(StrEnum):
    """Stable singleton scopes; parameterized scopes use the helpers below."""

    PIPELINE = "pipeline"
    SUPERVISOR = "supervisor"
    CONTROLLER = "controller"
    EXTRACTION_POOL = "extraction_pool"
    PUBLISHER = "publisher"


class RecoveryReasonCode(StrEnum):
    """Redacted reason vocabulary accepted by the component circuit."""

    PROCESS_EXIT = "process_exit"
    STALE_HEARTBEAT = "stale_heartbeat"
    LAUNCH_FAILED = "launch_failed"
    SQLITE_BUSY = "sqlite_busy"
    SQLITE_LOCKED = "sqlite_locked"
    TEMPORARY_IO = "temporary_io"
    TEMPORARY_RESOURCE = "temporary_resource"
    ERROR_LOOP = "error_loop"
    NO_FORWARD_PROGRESS = "no_forward_progress"
    PUBLISHER_FAILED = "publisher_failed"
    SUPERVISOR_LOOP_FAILED = "supervisor_loop_failed"
    CRITICAL_DISK = "critical_disk"
    DATA_INTEGRITY = "data_integrity"
    MIGRATION_SCHEMA = "migration_schema"
    UNSAFE_CONFIGURATION = "unsafe_configuration"
    REPEATED_CORRUPTION = "repeated_corruption"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CONFIGURATION = "invalid_configuration"
    UNSUPPORTED_FORMAT = "unsupported_format"
    DETERMINISTIC_VALIDATION = "deterministic_validation"
    CORRUPT_PDF = "corrupt_pdf"
    ENCRYPTED_PDF = "encrypted_pdf"
    UNSUPPORTED_PDF = "unsupported_pdf"
    PDF_TOO_LARGE = "pdf_too_large"
    PDF_CHANGED = "pdf_changed"
    USER_CANCELLED = "user_cancelled"
    PLANNED_SHUTDOWN = "planned_shutdown"
    SUSPEND_WAKE = "suspend_wake"
    UNKNOWN_COMPONENT_FAILURE = "unknown_component_failure"
    CONTROL_STATE_UNAVAILABLE = "control_state_unavailable"
    CONTROL_STATE_CONFLICT = "control_state_conflict"


class RecoveryFailureDisposition(StrEnum):
    RETRY_COMPONENT = "retry_component"
    PAUSE_SAFETY = "pause_safety"
    PERMANENT_ITEM = "permanent_item"
    IGNORE = "ignore"


class ResumeSafetyState(StrEnum):
    SAFE = "safe"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class RecoveryFailureClassification:
    disposition: RecoveryFailureDisposition
    reason_family: str
    reason_code: str
    component_attempt_allowed: bool
    immediate_pause: bool


@dataclass(frozen=True, slots=True)
class ResumeSafetyResult:
    state: ResumeSafetyState
    reason_code: str = ""

    @classmethod
    def safe(cls) -> ResumeSafetyResult:
        return cls(ResumeSafetyState.SAFE)

    @classmethod
    def blocked(cls, reason_code: str) -> ResumeSafetyResult:
        return cls(ResumeSafetyState.BLOCKED, _canonical_resume_block_reason(reason_code))


@dataclass(frozen=True, slots=True)
class RecoveryTransitionResult:
    recovery: PDFPipelineRecovery
    changed: bool
    duplicate: bool = False

    @property
    def payload(self) -> dict[str, object]:
        return recovery_payload(self.recovery)


@dataclass(frozen=True, slots=True)
class RecoveryIncidentResult:
    classification: RecoveryFailureClassification
    transition: RecoveryTransitionResult | None


@dataclass(frozen=True, slots=True)
class RecoveryEscalationResult:
    """One correlation-safe transfer from narrower scopes to their owner."""

    transition: RecoveryTransitionResult
    correlation_id: uuid.UUID
    source_scopes: tuple[str, ...]
    transferred_attempt_ids: tuple[uuid.UUID, ...]


class RecoveryError(RuntimeError):
    """Base class for safe recovery-domain failures."""


class RecoveryNotFound(RecoveryError):
    pass


class RecoveryConflict(RecoveryError):
    pass


class RecoveryTransitionRejected(RecoveryError):
    pass


class RecoveryNotDue(RecoveryTransitionRejected):
    def __init__(self, next_retry_at: datetime):
        super().__init__("The recovery retry is not due yet.")
        self.next_retry_at = next_retry_at


class RecoveryResumeBlocked(RecoveryTransitionRejected):
    def __init__(self, reason_code: str, recovery: PDFPipelineRecovery):
        super().__init__("The recovery safety condition is still unresolved.")
        self.reason_code = reason_code
        self.recovery = recovery


class RecoveryControlUnavailable(RecoveryError):
    """Launch-blocking control-store failure with no embedded private details."""

    def __init__(self, *, durably_recorded: bool):
        super().__init__("PDF recovery control state is unavailable.")
        self.reason_code = RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE.value
        self.durably_recorded = durably_recorded


JitterFunction = Callable[[float, float], float]
ResumeSafetyCheck = Callable[[dict[str, object]], ResumeSafetyResult]

_TRANSIENT_COMPONENT_REASONS = frozenset(
    {
        RecoveryReasonCode.PROCESS_EXIT,
        RecoveryReasonCode.STALE_HEARTBEAT,
        RecoveryReasonCode.LAUNCH_FAILED,
        RecoveryReasonCode.SQLITE_BUSY,
        RecoveryReasonCode.SQLITE_LOCKED,
        RecoveryReasonCode.TEMPORARY_IO,
        RecoveryReasonCode.TEMPORARY_RESOURCE,
        RecoveryReasonCode.ERROR_LOOP,
        RecoveryReasonCode.NO_FORWARD_PROGRESS,
        RecoveryReasonCode.PUBLISHER_FAILED,
        RecoveryReasonCode.SUPERVISOR_LOOP_FAILED,
    }
)
_SAFETY_REASONS = frozenset(
    {
        RecoveryReasonCode.CRITICAL_DISK,
        RecoveryReasonCode.DATA_INTEGRITY,
        RecoveryReasonCode.MIGRATION_SCHEMA,
        RecoveryReasonCode.UNSAFE_CONFIGURATION,
        RecoveryReasonCode.REPEATED_CORRUPTION,
        RecoveryReasonCode.MISSING_CREDENTIALS,
        RecoveryReasonCode.INVALID_CONFIGURATION,
        RecoveryReasonCode.UNSUPPORTED_FORMAT,
        RecoveryReasonCode.DETERMINISTIC_VALIDATION,
        RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE,
        RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE,
        RecoveryReasonCode.CONTROL_STATE_CONFLICT,
    }
)
_PERMANENT_ITEM_REASONS = frozenset(
    {
        RecoveryReasonCode.CORRUPT_PDF,
        RecoveryReasonCode.ENCRYPTED_PDF,
        RecoveryReasonCode.UNSUPPORTED_PDF,
        RecoveryReasonCode.PDF_TOO_LARGE,
        RecoveryReasonCode.PDF_CHANGED,
    }
)
_IGNORED_REASONS = frozenset(
    {
        RecoveryReasonCode.USER_CANCELLED,
        RecoveryReasonCode.PLANNED_SHUTDOWN,
        RecoveryReasonCode.SUSPEND_WAKE,
    }
)

_REASON_FAMILIES: Final = {
    RecoveryReasonCode.PROCESS_EXIT: "process",
    RecoveryReasonCode.STALE_HEARTBEAT: "liveness",
    RecoveryReasonCode.LAUNCH_FAILED: "process",
    RecoveryReasonCode.SQLITE_BUSY: "sqlite",
    RecoveryReasonCode.SQLITE_LOCKED: "sqlite",
    RecoveryReasonCode.TEMPORARY_IO: "local_io",
    RecoveryReasonCode.TEMPORARY_RESOURCE: "local_resource",
    RecoveryReasonCode.ERROR_LOOP: "liveness",
    RecoveryReasonCode.NO_FORWARD_PROGRESS: "liveness",
    RecoveryReasonCode.PUBLISHER_FAILED: "publisher",
    RecoveryReasonCode.SUPERVISOR_LOOP_FAILED: "supervisor",
    RecoveryReasonCode.CRITICAL_DISK: "disk_safety",
    RecoveryReasonCode.DATA_INTEGRITY: "integrity",
    RecoveryReasonCode.MIGRATION_SCHEMA: "schema",
    RecoveryReasonCode.UNSAFE_CONFIGURATION: "configuration",
    RecoveryReasonCode.REPEATED_CORRUPTION: "integrity",
    RecoveryReasonCode.MISSING_CREDENTIALS: "configuration",
    RecoveryReasonCode.INVALID_CONFIGURATION: "configuration",
    RecoveryReasonCode.UNSUPPORTED_FORMAT: "input",
    RecoveryReasonCode.DETERMINISTIC_VALIDATION: "validation",
    RecoveryReasonCode.CORRUPT_PDF: "permanent_item",
    RecoveryReasonCode.ENCRYPTED_PDF: "permanent_item",
    RecoveryReasonCode.UNSUPPORTED_PDF: "permanent_item",
    RecoveryReasonCode.PDF_TOO_LARGE: "permanent_item",
    RecoveryReasonCode.PDF_CHANGED: "permanent_item",
    RecoveryReasonCode.USER_CANCELLED: "cancelled",
    RecoveryReasonCode.PLANNED_SHUTDOWN: "planned_stop",
    RecoveryReasonCode.SUSPEND_WAKE: "suspend_wake",
    RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE: "unknown",
    RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE: "control_state",
    RecoveryReasonCode.CONTROL_STATE_CONFLICT: "control_state",
}

_REASON_MESSAGES: Final = {
    RecoveryReasonCode.PROCESS_EXIT: "A supervised PDF component exited unexpectedly.",
    RecoveryReasonCode.STALE_HEARTBEAT: "A supervised PDF component stopped reporting progress.",
    RecoveryReasonCode.LAUNCH_FAILED: "A supervised PDF component could not be started.",
    RecoveryReasonCode.SQLITE_BUSY: "The PDF component is waiting for a temporary database lock.",
    RecoveryReasonCode.SQLITE_LOCKED: "The PDF component is waiting for a temporary database lock.",
    RecoveryReasonCode.TEMPORARY_IO: "A temporary local storage operation failed.",
    RecoveryReasonCode.TEMPORARY_RESOURCE: "A temporary local resource condition blocked recovery.",
    RecoveryReasonCode.ERROR_LOOP: "A PDF component repeatedly failed without making progress.",
    RecoveryReasonCode.NO_FORWARD_PROGRESS: "A PDF component stopped making expected progress.",
    RecoveryReasonCode.PUBLISHER_FAILED: "The PDF publisher failed unexpectedly.",
    RecoveryReasonCode.SUPERVISOR_LOOP_FAILED: "The PDF worker supervisor failed unexpectedly.",
    RecoveryReasonCode.CRITICAL_DISK: "Available disk space is below the safe recovery limit.",
    RecoveryReasonCode.DATA_INTEGRITY: "OWL paused to protect PDF index integrity.",
    RecoveryReasonCode.MIGRATION_SCHEMA: "The database schema is not safe for PDF recovery.",
    RecoveryReasonCode.UNSAFE_CONFIGURATION: "PDF recovery configuration is not safe.",
    RecoveryReasonCode.REPEATED_CORRUPTION: "Repeated integrity checks failed.",
    RecoveryReasonCode.MISSING_CREDENTIALS: "Required repository credentials are unavailable.",
    RecoveryReasonCode.INVALID_CONFIGURATION: "Required PDF pipeline configuration is invalid.",
    RecoveryReasonCode.UNSUPPORTED_FORMAT: "The input format is not supported.",
    RecoveryReasonCode.DETERMINISTIC_VALIDATION: "A repeatable validation check failed.",
    RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE: "OWL could not safely classify the component failure.",
    RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE: (
        "Canonical PDF recovery state is temporarily unavailable."
    ),
    RecoveryReasonCode.CONTROL_STATE_CONFLICT: (
        "PDF recovery control stores could not be ordered safely."
    ),
}


def extraction_slot_scope(slot: int) -> str:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 9_999:
        raise ValueError("An extraction slot must be an integer between 1 and 9999.")
    return f"extraction_slot:{slot}"


def repository_recovery_scope(repository_id: int) -> str:
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or not 1 <= repository_id <= 9_999_999_999_999_999_999
    ):
        raise ValueError("A recovery repository ID must be a positive integer.")
    return f"repository:{repository_id}"


def canonical_recovery_scope(scope: str | RecoveryScope) -> str:
    canonical = str(scope).strip().casefold()
    if not _SCOPE_PATTERN.fullmatch(canonical):
        raise ValueError("The PDF recovery scope is not supported.")
    return canonical


def classify_recovery_failure(reason_code: object) -> RecoveryFailureClassification:
    """Classify a stable code without ever retaining exception or input text."""

    try:
        reason = RecoveryReasonCode(str(reason_code).strip().casefold())
    except ValueError:
        reason = RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE
    if reason in _TRANSIENT_COMPONENT_REASONS:
        disposition = RecoveryFailureDisposition.RETRY_COMPONENT
    elif reason in _PERMANENT_ITEM_REASONS:
        disposition = RecoveryFailureDisposition.PERMANENT_ITEM
    elif reason in _IGNORED_REASONS:
        disposition = RecoveryFailureDisposition.IGNORE
    elif reason in _SAFETY_REASONS:
        disposition = RecoveryFailureDisposition.PAUSE_SAFETY
    else:  # pragma: no cover - exhaustive guard for future enum additions
        disposition = RecoveryFailureDisposition.PAUSE_SAFETY
    return RecoveryFailureClassification(
        disposition=disposition,
        reason_family=_REASON_FAMILIES[reason],
        reason_code=reason.value,
        component_attempt_allowed=disposition == RecoveryFailureDisposition.RETRY_COMPONENT,
        immediate_pause=disposition == RecoveryFailureDisposition.PAUSE_SAFETY,
    )


def recovery_backoff_seconds(
    failed_attempts: int,
    *,
    base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    maximum_seconds: int = DEFAULT_BACKOFF_MAX_SECONDS,
    jitter_fraction: float = DEFAULT_BACKOFF_JITTER_FRACTION,
    jitter: JitterFunction | None = None,
) -> int:
    """Return bounded exponential delay; tests can inject a deterministic jitter."""

    if isinstance(failed_attempts, bool) or not isinstance(failed_attempts, int):
        raise ValueError("failed_attempts must be an integer.")
    if failed_attempts < 1:
        raise ValueError("failed_attempts must be at least one.")
    if isinstance(base_seconds, bool) or not isinstance(base_seconds, int) or base_seconds < 1:
        raise ValueError("base_seconds must be a positive integer.")
    if (
        isinstance(maximum_seconds, bool)
        or not isinstance(maximum_seconds, int)
        or not base_seconds <= maximum_seconds <= MAX_BACKOFF_SECONDS
    ):
        raise ValueError("maximum_seconds must be between base_seconds and 86400.")
    if isinstance(jitter_fraction, bool) or not isinstance(jitter_fraction, (int, float)):
        raise ValueError("jitter_fraction must be a number.")
    if not 0 <= float(jitter_fraction) <= 1:
        raise ValueError("jitter_fraction must be between zero and one.")

    exponent = min(failed_attempts - 1, 62)
    nominal = min(maximum_seconds, base_seconds * (2**exponent))
    spread = nominal * float(jitter_fraction)
    lower = max(0.0, nominal - spread)
    upper = min(float(maximum_seconds), nominal + spread)
    selected = (jitter or random.uniform)(lower, upper)
    if isinstance(selected, bool) or not isinstance(selected, (int, float)):
        raise ValueError("The jitter function must return a number.")
    bounded = min(upper, max(lower, float(selected)))
    return max(1, min(maximum_seconds, int(round(bounded))))


def recovery_payload(recovery: PDFPipelineRecovery) -> dict[str, object]:
    """Return the exact version-one durable recovery subset used by APIs."""

    if not isinstance(recovery, PDFPipelineRecovery):
        raise TypeError("recovery must be a PDFPipelineRecovery.")
    active_episode = recovery.state != PDFPipelineRecoveryState.HEALTHY
    resumable = recovery.state == PDFPipelineRecoveryState.PAUSED
    classification = (
        classify_recovery_failure(recovery.reason_code) if recovery.reason_code else None
    )
    return {
        "schemaVersion": 1,
        "state": recovery.state,
        "halfOpen": recovery.state == PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
        "episodeId": str(recovery.episode_id) if active_episode else None,
        "generation": recovery.generation,
        "pauseGeneration": recovery.pause_generation,
        "scope": recovery.scope,
        "reasonFamily": classification.reason_family if classification is not None else None,
        "reasonCode": classification.reason_code if classification is not None else None,
        "consecutiveFailedAttempts": recovery.consecutive_failed_attempts,
        "lifetimeAttempts": recovery.lifetime_attempts,
        "pauseAfterAttempts": recovery.pause_after_attempts,
        "firstFailureAt": _isoformat(recovery.first_failure_at),
        "lastFailureAt": _isoformat(recovery.last_failure_at),
        "lastAttemptAt": _isoformat(recovery.last_attempt_at),
        "nextRetryAt": _isoformat(recovery.next_retry_at),
        "currentBackoffSeconds": recovery.current_backoff_seconds,
        "pausedReason": _public_pause_message(recovery) if recovery.paused_at is not None else None,
        "pausedAt": _isoformat(recovery.paused_at),
        "popupAcknowledgedGeneration": recovery.popup_acknowledged_generation,
        "popupClaimedGeneration": recovery.popup_claimed_generation,
        "resumeRequestedAt": _isoformat(recovery.resume_requested_at),
        "recoveredAt": _isoformat(recovery.recovered_at),
        "lastOutcome": _public_last_outcome(recovery),
        "activeAttemptId": (
            str(recovery.active_attempt_id) if recovery.active_attempt_id is not None else None
        ),
        "resumable": resumable,
        "resumeSafety": ResumeSafetyState.UNKNOWN
        if resumable
        else ResumeSafetyState.NOT_APPLICABLE,
        "resumeBlockedReason": None,
        "resumeAction": recovery_resume_action(recovery) if resumable else None,
        "stabilityWindowSeconds": configured_recovery_stability_seconds(),
    }


def ensure_recovery_scope(
    scope: str | RecoveryScope,
    *,
    pause_after_attempts: int | None = None,
) -> PDFPipelineRecovery:
    """Reconcile both stores before returning the one canonical scope record.

    Database outages create a redacted same-disk pause and raise a stable error;
    callers must not launch the affected role after that error.
    """

    canonical_scope = canonical_recovery_scope(scope)
    configured_threshold = _pause_after_attempts(pause_after_attempts)
    try:
        return _reconcile_recovery_scope(
            canonical_scope,
            configured_threshold=configured_threshold,
        )
    except RecoveryControlUnavailable:
        raise
    except (DatabaseError, OSError):
        try:
            durable = pdf_recovery_fallback.persist_database_unavailable_pause(canonical_scope)
        except Exception:
            durable = False
        logger.error(
            "event=pdf_recovery_control_unavailable reason=%s",
            "fallback_recorded" if durable else "in_memory_only",
        )
        raise RecoveryControlUnavailable(durably_recorded=durable) from None


def get_recovery_payload(scope: str | RecoveryScope) -> dict[str, object]:
    canonical_scope = canonical_recovery_scope(scope)
    try:
        recovery = PDFPipelineRecovery.objects.get(scope=canonical_scope)
    except PDFPipelineRecovery.DoesNotExist as exc:
        raise RecoveryNotFound("The PDF recovery scope does not exist.") from exc
    return recovery_payload(recovery)


def record_recovery_incident(
    scope: str | RecoveryScope,
    *,
    reason_code: object,
    incident_id: uuid.UUID | str | None = None,
    expected_generation: int | None = None,
    occurred_at: datetime | None = None,
    pause_after_attempts: int | None = None,
    jitter: JitterFunction | None = None,
) -> RecoveryIncidentResult:
    """Open one episode, or deliberately leave item/planned outcomes outside it."""

    classification = classify_recovery_failure(reason_code)
    if classification.disposition in {
        RecoveryFailureDisposition.PERMANENT_ITEM,
        RecoveryFailureDisposition.IGNORE,
    }:
        return RecoveryIncidentResult(classification, None)

    canonical_scope = canonical_recovery_scope(scope)
    event_id = _optional_uuid(incident_id, label="incident_id")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    threshold = _pause_after_attempts(pause_after_attempts)
    notify_unread = False
    with _recovery_lock(), transaction.atomic():
        duplicate = _event_duplicate(event_id)
        if duplicate is not None:
            if (
                duplicate.recovery.scope != canonical_scope
                or duplicate.reason_code != classification.reason_code
            ):
                raise RecoveryConflict("The recovery incident id was used for another incident.")
            return RecoveryIncidentResult(
                classification,
                RecoveryTransitionResult(
                    duplicate.recovery,
                    changed=False,
                    duplicate=True,
                ),
            )
        recovery, _created = PDFPipelineRecovery.objects.get_or_create(
            scope=canonical_scope,
            defaults={"pause_after_attempts": threshold},
        )
        _check_expected_generation(recovery, expected_generation)
        if recovery.state != PDFPipelineRecoveryState.HEALTHY:
            if classification.immediate_pause and recovery.state != PDFPipelineRecoveryState.PAUSED:
                superseded_attempt_id = recovery.active_attempt_id
                recovery = _cas_update(
                    recovery,
                    {
                        "state": PDFPipelineRecoveryState.PAUSED,
                        "reason_family": classification.reason_family,
                        "reason_code": classification.reason_code,
                        "last_failure_at": now,
                        "next_retry_at": None,
                        "current_backoff_seconds": 0,
                        "pause_generation": recovery.pause_generation + 1,
                        "paused_reason": _pause_message(
                            classification.reason_code,
                            threshold=recovery.pause_after_attempts,
                        ),
                        "paused_at": now,
                        "active_attempt_id": None,
                        "last_outcome": _reason_message(classification.reason_code),
                    },
                    now=now,
                )
                _create_event(
                    recovery,
                    kind=PDFPipelineRecoveryEventKind.PAUSED,
                    reason_code=classification.reason_code,
                    outcome=recovery.paused_reason,
                    occurred_at=now,
                    event_id=event_id,
                )
                if superseded_attempt_id is not None:
                    _create_event(
                        recovery,
                        kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                        attempt_id=superseded_attempt_id,
                        reason_code=classification.reason_code,
                        outcome="Active probe stopped at a safe boundary for the safety pause.",
                        occurred_at=now,
                    )
                result = RecoveryTransitionResult(recovery, changed=True)
                notify_unread = True
            else:
                if event_id is not None:
                    _create_event(
                        recovery,
                        kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                        reason_code=classification.reason_code,
                        outcome="Incident coalesced into the active recovery episode.",
                        occurred_at=now,
                        event_id=event_id,
                    )
                result = RecoveryTransitionResult(recovery, changed=False)
        else:
            immediate_pause = classification.immediate_pause or threshold == 0
            updates: dict[str, object] = {
                "episode_id": uuid.uuid4(),
                "state": (
                    PDFPipelineRecoveryState.PAUSED
                    if immediate_pause
                    else PDFPipelineRecoveryState.RETRY_WAIT
                ),
                "reason_family": classification.reason_family,
                "reason_code": classification.reason_code,
                "consecutive_failed_attempts": 0,
                "pause_after_attempts": threshold,
                "first_failure_at": now,
                "last_failure_at": now,
                "last_attempt_at": None,
                "recovered_at": None,
                "active_attempt_id": None,
                "resume_requested_at": None,
                "resume_idempotency_key_hash": "",
                "resume_predecessor_generation": None,
                "last_outcome": _reason_message(classification.reason_code),
            }
            if immediate_pause:
                updates.update(
                    next_retry_at=None,
                    current_backoff_seconds=0,
                    pause_generation=recovery.pause_generation + 1,
                    paused_reason=_pause_message(
                        classification.reason_code,
                        threshold=threshold,
                    ),
                    paused_at=now,
                )
                notify_unread = True
            else:
                delay = _configured_backoff(1, jitter=jitter)
                updates.update(
                    next_retry_at=now + timedelta(seconds=delay),
                    current_backoff_seconds=delay,
                    paused_reason="",
                    paused_at=None,
                )
            recovery = _cas_update(recovery, updates, now=now)
            _create_event(
                recovery,
                kind=PDFPipelineRecoveryEventKind.EPISODE_OPENED,
                reason_code=classification.reason_code,
                outcome=recovery.last_outcome,
                occurred_at=now,
                event_id=event_id,
            )
            _create_event(
                recovery,
                kind=(
                    PDFPipelineRecoveryEventKind.PAUSED
                    if immediate_pause
                    else PDFPipelineRecoveryEventKind.RETRY_SCHEDULED
                ),
                reason_code=classification.reason_code,
                outcome=(
                    recovery.paused_reason if immediate_pause else "Recovery retry scheduled."
                ),
                occurred_at=now,
            )
            result = RecoveryTransitionResult(recovery, changed=True)
    _notify_transition(result.recovery, mark_unread=notify_unread)
    return RecoveryIncidentResult(classification, result)


def escalate_correlated_recovery(
    source_scopes: tuple[str, ...] | list[str],
    *,
    target_scope: str | RecoveryScope,
    reason_code: object,
    correlation_id: uuid.UUID | str | None = None,
    occurred_at: datetime | None = None,
    pause_after_attempts: int | None = None,
    jitter: JitterFunction | None = None,
) -> RecoveryEscalationResult | None:
    """Transfer correlated failures into one broader owning episode.

    Attempt identities, rather than child counters, are the accounting unit.
    Replaying an escalation therefore cannot add the same failed probe twice.
    """

    classification = classify_recovery_failure(reason_code)
    if classification.disposition != RecoveryFailureDisposition.RETRY_COMPONENT:
        raise RecoveryTransitionRejected(
            "Only retryable component failures can be escalated between recovery scopes."
        )
    owner_scope = canonical_recovery_scope(target_scope)
    canonical_sources = tuple(sorted({canonical_recovery_scope(scope) for scope in source_scopes}))
    if len(canonical_sources) < 2 or owner_scope in canonical_sources:
        raise ValueError("Recovery escalation requires two distinct narrower scopes.")
    if owner_scope == RecoveryScope.EXTRACTION_POOL and any(
        not scope.startswith("extraction_slot:") for scope in canonical_sources
    ):
        raise ValueError("Only extraction slots can escalate to the extraction pool.")
    correlation = _optional_uuid(correlation_id, label="correlation_id") or uuid.uuid4()
    now = _aware_timestamp(occurred_at, label="occurred_at")
    threshold = _pause_after_attempts(pause_after_attempts)
    notify_unread = False

    with _recovery_lock(), transaction.atomic():
        existing_owner = (
            PDFPipelineRecovery.objects.select_for_update().filter(scope=owner_scope).first()
        )
        if (
            existing_owner is not None
            and existing_owner.events.filter(correlation_id=correlation).exists()
        ):
            return RecoveryEscalationResult(
                transition=RecoveryTransitionResult(
                    existing_owner,
                    changed=False,
                    duplicate=True,
                ),
                correlation_id=correlation,
                source_scopes=canonical_sources,
                transferred_attempt_ids=(),
            )
        sources = tuple(
            PDFPipelineRecovery.objects.select_for_update()
            .filter(scope__in=canonical_sources, reason_code=classification.reason_code)
            .exclude(state=PDFPipelineRecoveryState.HEALTHY)
            .order_by("scope")
        )
        if len(sources) < 2:
            return None

        owner = existing_owner or PDFPipelineRecovery.objects.create(
            scope=owner_scope,
            pause_after_attempts=threshold,
        )

        source_attempt_ids: set[uuid.UUID] = set()
        for source in sources:
            events = source.events.filter(
                kind=PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
                attempt_id__isnull=False,
            )
            if source.first_failure_at is not None:
                events = events.filter(occurred_at__gte=source.first_failure_at)
            source_attempt_ids.update(events.values_list("attempt_id", flat=True))
        already_owned = set(
            owner.events.filter(
                kind=PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
                attempt_id__in=source_attempt_ids,
            ).values_list("attempt_id", flat=True)
        )
        transferred_attempt_ids = tuple(sorted(source_attempt_ids - already_owned, key=str))
        transferred_failures = len(transferred_attempt_ids)
        opened_owner_episode = owner.state == PDFPipelineRecoveryState.HEALTHY
        prior_state = owner.state
        failed_attempts = min(
            owner.pause_after_attempts,
            (0 if opened_owner_episode else owner.consecutive_failed_attempts)
            + transferred_failures,
        )
        should_pause = (
            prior_state == PDFPipelineRecoveryState.PAUSED
            or owner.pause_after_attempts == 0
            or failed_attempts >= owner.pause_after_attempts
        )
        first_failure_at = min(
            (source.first_failure_at or now for source in sources),
            default=now,
        )
        updates: dict[str, object] = {
            "state": (
                PDFPipelineRecoveryState.PAUSED
                if should_pause
                else PDFPipelineRecoveryState.RETRY_WAIT
            ),
            "reason_family": classification.reason_family,
            "reason_code": classification.reason_code,
            "consecutive_failed_attempts": failed_attempts,
            "lifetime_attempts": owner.lifetime_attempts + transferred_failures,
            "first_failure_at": first_failure_at
            if opened_owner_episode
            else owner.first_failure_at,
            "last_failure_at": now,
            "active_attempt_id": None,
            "recovered_at": None,
            "last_outcome": "Correlated component failures moved to the owning recovery scope.",
        }
        if opened_owner_episode:
            updates.update(
                episode_id=uuid.uuid4(),
                pause_after_attempts=threshold,
                last_attempt_at=None,
                resume_requested_at=None,
                resume_idempotency_key_hash="",
                resume_predecessor_generation=None,
            )
            # The threshold used above came from the freshly created row and is
            # normally identical. Preserve an explicit test/config override.
            owner.pause_after_attempts = threshold
            failed_attempts = min(threshold, transferred_failures)
            should_pause = threshold == 0 or failed_attempts >= threshold
            updates["consecutive_failed_attempts"] = failed_attempts
            updates["state"] = (
                PDFPipelineRecoveryState.PAUSED
                if should_pause
                else PDFPipelineRecoveryState.RETRY_WAIT
            )
        if should_pause:
            updates.update(
                next_retry_at=None,
                current_backoff_seconds=0,
                pause_generation=(
                    owner.pause_generation + 1
                    if prior_state != PDFPipelineRecoveryState.PAUSED
                    else owner.pause_generation
                ),
                paused_reason=_pause_message(
                    classification.reason_code,
                    threshold=owner.pause_after_attempts,
                ),
                paused_at=owner.paused_at or now,
            )
            notify_unread = prior_state != PDFPipelineRecoveryState.PAUSED
        else:
            delay = _configured_backoff(max(1, failed_attempts), jitter=jitter)
            updates.update(
                next_retry_at=now + timedelta(seconds=delay),
                current_backoff_seconds=delay,
                paused_reason="",
                paused_at=None,
            )
        owner = _cas_update(owner, updates, now=now)
        if opened_owner_episode:
            _create_event(
                owner,
                kind=PDFPipelineRecoveryEventKind.EPISODE_OPENED,
                reason_code=classification.reason_code,
                outcome=owner.last_outcome,
                occurred_at=now,
                correlation_id=correlation,
            )
        for attempt_id in transferred_attempt_ids:
            _create_event(
                owner,
                kind=PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
                attempt_id=attempt_id,
                reason_code=classification.reason_code,
                outcome="Failed probe transferred during correlated scope escalation.",
                occurred_at=now,
                correlation_id=correlation,
            )
        if should_pause and prior_state != PDFPipelineRecoveryState.PAUSED:
            _create_event(
                owner,
                kind=PDFPipelineRecoveryEventKind.PAUSED,
                reason_code=classification.reason_code,
                outcome=owner.paused_reason,
                occurred_at=now,
                correlation_id=correlation,
            )
        elif not opened_owner_episode and not transferred_attempt_ids:
            _create_event(
                owner,
                kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                reason_code=classification.reason_code,
                outcome="Correlated incident coalesced into the owning recovery episode.",
                occurred_at=now,
                correlation_id=correlation,
            )

        for source in sources:
            superseded_attempt_id = source.active_attempt_id
            source = _cas_update(
                source,
                {
                    "state": PDFPipelineRecoveryState.HEALTHY,
                    "active_attempt_id": None,
                    "next_retry_at": None,
                    "current_backoff_seconds": 0,
                    "recovered_at": now,
                    "last_outcome": (
                        "Recovery episode superseded by correlated owning-scope recovery."
                    ),
                },
                now=now,
            )
            _create_event(
                source,
                kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                attempt_id=superseded_attempt_id,
                reason_code=classification.reason_code,
                outcome=source.last_outcome,
                occurred_at=now,
                correlation_id=correlation,
            )

        result = RecoveryEscalationResult(
            transition=RecoveryTransitionResult(owner, changed=True),
            correlation_id=correlation,
            source_scopes=tuple(source.scope for source in sources),
            transferred_attempt_ids=transferred_attempt_ids,
        )
    _notify_transition(result.transition.recovery, mark_unread=notify_unread)
    return result


def begin_recovery_attempt(
    scope: str | RecoveryScope,
    *,
    expected_generation: int | None = None,
    attempt_id: uuid.UUID | str | None = None,
    occurred_at: datetime | None = None,
) -> RecoveryTransitionResult:
    """Begin exactly one due automatic or user-authorized half-open probe."""

    canonical_scope = canonical_recovery_scope(scope)
    requested_attempt_id = _optional_uuid(attempt_id, label="attempt_id")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    should_notify = False
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        if recovery.state in {
            PDFPipelineRecoveryState.RECOVERING,
            PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
        }:
            if requested_attempt_id is None or recovery.active_attempt_id == requested_attempt_id:
                return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
            raise RecoveryConflict("A different recovery attempt is already active.")
        _check_expected_generation(recovery, expected_generation)
        if recovery.state not in {
            PDFPipelineRecoveryState.RETRY_WAIT,
            PDFPipelineRecoveryState.RESUME_REQUESTED,
        }:
            raise RecoveryTransitionRejected("The PDF recovery scope cannot begin a probe now.")
        if (
            recovery.state == PDFPipelineRecoveryState.RETRY_WAIT
            and recovery.next_retry_at is not None
            and now < recovery.next_retry_at
        ):
            raise RecoveryNotDue(recovery.next_retry_at)

        half_open = recovery.state == PDFPipelineRecoveryState.RESUME_REQUESTED
        active_attempt_id = requested_attempt_id or uuid.uuid4()
        recovery = _cas_update(
            recovery,
            {
                "state": (
                    PDFPipelineRecoveryState.RECOVERING_HALF_OPEN
                    if half_open
                    else PDFPipelineRecoveryState.RECOVERING
                ),
                "active_attempt_id": active_attempt_id,
                "lifetime_attempts": recovery.lifetime_attempts + 1,
                "last_attempt_at": now,
                "next_retry_at": None,
                "current_backoff_seconds": 0,
                "last_outcome": "Recovery stability probe started.",
            },
            now=now,
        )
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.ATTEMPT_STARTED,
            attempt_id=active_attempt_id,
            reason_code=recovery.reason_code,
            outcome=recovery.last_outcome,
            occurred_at=now,
        )
        result = RecoveryTransitionResult(recovery, changed=True)
        should_notify = half_open and recovery.pause_generation > 0
    if should_notify:
        _notify_transition(result.recovery, mark_unread=False)
    return result


def fail_recovery_attempt(
    scope: str | RecoveryScope,
    *,
    attempt_id: uuid.UUID | str,
    expected_generation: int | None = None,
    reason_code: object | None = None,
    occurred_at: datetime | None = None,
    jitter: JitterFunction | None = None,
) -> RecoveryTransitionResult:
    """Finalize one probe failure and either back off or open the circuit."""

    canonical_scope = canonical_recovery_scope(scope)
    canonical_attempt_id = _required_uuid(attempt_id, label="attempt_id")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    notify_unread = False
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        if _attempt_event_exists(
            recovery,
            canonical_attempt_id,
            PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
        ):
            return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
        _check_expected_generation(recovery, expected_generation)
        if (
            recovery.state
            not in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
            or recovery.active_attempt_id != canonical_attempt_id
        ):
            raise RecoveryConflict("The recovery attempt is stale or is not active.")

        classification = (
            classify_recovery_failure(reason_code)
            if reason_code is not None
            else classify_recovery_failure(recovery.reason_code)
        )
        if classification.disposition == RecoveryFailureDisposition.PERMANENT_ITEM:
            raise RecoveryTransitionRejected(
                "A permanent PDF outcome cannot fail a component recovery attempt."
            )
        if classification.disposition == RecoveryFailureDisposition.IGNORE:
            raise RecoveryTransitionRejected(
                "A planned or cancelled outcome cannot fail a component recovery attempt."
            )
        half_open = recovery.state == PDFPipelineRecoveryState.RECOVERING_HALF_OPEN
        threshold = recovery.pause_after_attempts
        if half_open:
            failed_attempts = threshold
            should_pause = True
        else:
            failed_attempts = min(recovery.consecutive_failed_attempts + 1, threshold)
            should_pause = (
                classification.immediate_pause or threshold == 0 or failed_attempts >= threshold
            )

        updates: dict[str, object] = {
            "state": (
                PDFPipelineRecoveryState.PAUSED
                if should_pause
                else PDFPipelineRecoveryState.RETRY_WAIT
            ),
            "reason_family": classification.reason_family,
            "reason_code": classification.reason_code,
            "consecutive_failed_attempts": failed_attempts,
            "last_failure_at": now,
            "active_attempt_id": None,
            "last_outcome": "Recovery attempt did not pass its stability check.",
        }
        if should_pause:
            updates.update(
                next_retry_at=None,
                current_backoff_seconds=0,
                pause_generation=recovery.pause_generation + 1,
                paused_reason=_pause_message(
                    classification.reason_code,
                    threshold=threshold,
                    half_open=half_open,
                ),
                paused_at=now,
            )
            notify_unread = True
        else:
            delay = _configured_backoff(max(1, failed_attempts), jitter=jitter)
            updates.update(
                next_retry_at=now + timedelta(seconds=delay),
                current_backoff_seconds=delay,
            )
        recovery = _cas_update(recovery, updates, now=now)
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
            attempt_id=canonical_attempt_id,
            reason_code=classification.reason_code,
            outcome=recovery.last_outcome,
            occurred_at=now,
        )
        _create_event(
            recovery,
            kind=(
                PDFPipelineRecoveryEventKind.PAUSED
                if should_pause
                else PDFPipelineRecoveryEventKind.RETRY_SCHEDULED
            ),
            attempt_id=canonical_attempt_id,
            reason_code=classification.reason_code,
            outcome=(recovery.paused_reason if should_pause else "Recovery retry scheduled."),
            occurred_at=now,
        )
        result = RecoveryTransitionResult(recovery, changed=True)
    _notify_transition(result.recovery, mark_unread=notify_unread)
    return result


def succeed_recovery_attempt(
    scope: str | RecoveryScope,
    *,
    attempt_id: uuid.UUID | str,
    stability_confirmed: bool,
    expected_generation: int | None = None,
    occurred_at: datetime | None = None,
) -> RecoveryTransitionResult:
    """Close an episode only after the caller's named stability gate passes."""

    if not isinstance(stability_confirmed, bool):
        raise ValueError("stability_confirmed must be a boolean.")
    if not stability_confirmed:
        raise RecoveryTransitionRejected("The recovery stability gate has not passed.")
    canonical_scope = canonical_recovery_scope(scope)
    canonical_attempt_id = _required_uuid(attempt_id, label="attempt_id")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    should_notify = False
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        if _attempt_event_exists(
            recovery,
            canonical_attempt_id,
            PDFPipelineRecoveryEventKind.ATTEMPT_SUCCEEDED,
        ):
            return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
        _check_expected_generation(recovery, expected_generation)
        if (
            recovery.state
            not in {
                PDFPipelineRecoveryState.RECOVERING,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
            }
            or recovery.active_attempt_id != canonical_attempt_id
        ):
            raise RecoveryConflict("The recovery attempt is stale or is not active.")
        should_notify = recovery.paused_at is not None
        recovery = _cas_update(
            recovery,
            {
                "state": PDFPipelineRecoveryState.HEALTHY,
                "consecutive_failed_attempts": 0,
                "active_attempt_id": None,
                "next_retry_at": None,
                "current_backoff_seconds": 0,
                "recovered_at": now,
                "last_outcome": "Recovery stability check passed.",
            },
            now=now,
        )
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.ATTEMPT_SUCCEEDED,
            attempt_id=canonical_attempt_id,
            reason_code=recovery.reason_code,
            outcome=recovery.last_outcome,
            occurred_at=now,
        )
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.RECOVERED,
            attempt_id=canonical_attempt_id,
            reason_code=recovery.reason_code,
            outcome=recovery.last_outcome,
            occurred_at=now,
        )
        result = RecoveryTransitionResult(recovery, changed=True)
    if should_notify:
        _notify_transition(result.recovery, mark_unread=False)
    return result


def pause_recovery(
    scope: str | RecoveryScope,
    *,
    reason_code: object,
    incident_id: uuid.UUID | str | None = None,
    expected_generation: int | None = None,
    occurred_at: datetime | None = None,
    pause_after_attempts: int | None = None,
) -> RecoveryTransitionResult:
    """Immediately open the circuit for a redacted safety condition."""

    classification = classify_recovery_failure(reason_code)
    if not classification.immediate_pause:
        raise RecoveryTransitionRejected("The supplied reason is not an immediate safety pause.")
    canonical_scope = canonical_recovery_scope(scope)
    event_id = _optional_uuid(incident_id, label="incident_id")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    configured_threshold = _pause_after_attempts(pause_after_attempts)
    with _recovery_lock(), transaction.atomic():
        duplicate = _event_duplicate(event_id)
        if duplicate is not None:
            if (
                duplicate.recovery.scope != canonical_scope
                or duplicate.reason_code != classification.reason_code
            ):
                raise RecoveryConflict("The recovery incident id was used for another incident.")
            return RecoveryTransitionResult(
                duplicate.recovery,
                changed=False,
                duplicate=True,
            )
        recovery, _created = PDFPipelineRecovery.objects.get_or_create(
            scope=canonical_scope,
            defaults={"pause_after_attempts": configured_threshold},
        )
        threshold = (
            configured_threshold
            if pause_after_attempts is not None or _created
            else recovery.pause_after_attempts
        )
        _check_expected_generation(recovery, expected_generation)
        if recovery.state == PDFPipelineRecoveryState.PAUSED:
            same_incident = recovery.reason_code == classification.reason_code
            if event_id is not None:
                _create_event(
                    recovery,
                    kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                    reason_code=classification.reason_code,
                    outcome="Safety incident coalesced into the open recovery circuit.",
                    occurred_at=now,
                    event_id=event_id,
                )
            return RecoveryTransitionResult(
                recovery,
                changed=False,
                duplicate=same_incident,
            )
        new_episode = recovery.state == PDFPipelineRecoveryState.HEALTHY
        superseded_attempt_id = recovery.active_attempt_id
        recovery = _cas_update(
            recovery,
            {
                "episode_id": uuid.uuid4() if new_episode else recovery.episode_id,
                "state": PDFPipelineRecoveryState.PAUSED,
                "reason_family": classification.reason_family,
                "reason_code": classification.reason_code,
                "pause_after_attempts": threshold,
                "first_failure_at": now if new_episode else recovery.first_failure_at or now,
                "last_failure_at": now,
                "next_retry_at": None,
                "current_backoff_seconds": 0,
                "pause_generation": recovery.pause_generation + 1,
                "paused_reason": _pause_message(classification.reason_code, threshold=threshold),
                "paused_at": now,
                "active_attempt_id": None,
                "recovered_at": None,
                "resume_requested_at": (None if new_episode else recovery.resume_requested_at),
                "resume_idempotency_key_hash": (
                    "" if new_episode else recovery.resume_idempotency_key_hash
                ),
                "resume_predecessor_generation": (
                    None if new_episode else recovery.resume_predecessor_generation
                ),
                "last_outcome": _reason_message(classification.reason_code),
            },
            now=now,
        )
        if new_episode:
            _create_event(
                recovery,
                kind=PDFPipelineRecoveryEventKind.EPISODE_OPENED,
                reason_code=classification.reason_code,
                outcome=recovery.last_outcome,
                occurred_at=now,
            )
        if superseded_attempt_id is not None:
            _create_event(
                recovery,
                kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
                attempt_id=superseded_attempt_id,
                reason_code=classification.reason_code,
                outcome="Active probe stopped at a safe boundary for the safety pause.",
                occurred_at=now,
            )
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.PAUSED,
            reason_code=classification.reason_code,
            outcome=recovery.paused_reason,
            occurred_at=now,
            event_id=event_id,
        )
        result = RecoveryTransitionResult(recovery, changed=True)
    _notify_transition(result.recovery, mark_unread=True)
    return result


def request_recovery_resume(
    scope: str | RecoveryScope,
    *,
    episode_id: uuid.UUID | str,
    expected_generation: int,
    pause_generation: int,
    idempotency_key: str,
    safety_check: ResumeSafetyCheck | None,
    occurred_at: datetime | None = None,
) -> RecoveryTransitionResult:
    """Record one fail-closed, generation-safe request for a half-open probe."""

    canonical_scope = canonical_recovery_scope(scope)
    canonical_episode_id = _required_uuid(episode_id, label="episode_id")
    expected = _generation(expected_generation, label="expected_generation")
    expected_pause = _generation(pause_generation, label="pause_generation")
    key_hash = _idempotency_key_hash(idempotency_key)
    now = _aware_timestamp(occurred_at, label="occurred_at")
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        exact_duplicate = (
            recovery.episode_id == canonical_episode_id
            and recovery.pause_generation == expected_pause
            and recovery.resume_predecessor_generation == expected
            and recovery.resume_idempotency_key_hash == key_hash
            and recovery.state
            in {
                PDFPipelineRecoveryState.RESUME_REQUESTED,
                PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
                PDFPipelineRecoveryState.HEALTHY,
            }
        )
        if exact_duplicate:
            return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
        if recovery.episode_id != canonical_episode_id:
            raise RecoveryConflict("The recovery episode is stale.")
        if recovery.pause_generation != expected_pause:
            raise RecoveryConflict("The recovery pause generation is stale.")
        _check_expected_generation(recovery, expected)
        if recovery.state != PDFPipelineRecoveryState.PAUSED:
            raise RecoveryTransitionRejected("The PDF recovery scope is not paused.")

        safety = (
            safety_check(recovery_payload(recovery))
            if safety_check is not None
            else ResumeSafetyResult(ResumeSafetyState.UNKNOWN, "safety_check_unavailable")
        )
        if not isinstance(safety, ResumeSafetyResult):
            raise TypeError("safety_check must return ResumeSafetyResult.")
        if safety.state != ResumeSafetyState.SAFE:
            reason = _canonical_resume_block_reason(
                safety.reason_code or "safety_condition_unresolved"
            )
            raise RecoveryResumeBlocked(reason, recovery)

        recovery = _cas_update(
            recovery,
            {
                "state": PDFPipelineRecoveryState.RESUME_REQUESTED,
                # Resume is itself an explicit acknowledgement of this popup
                # generation.  Notification read state intentionally remains separate.
                "popup_acknowledged_generation": expected_pause,
                "resume_requested_at": now,
                "resume_idempotency_key_hash": key_hash,
                "resume_predecessor_generation": expected,
                "last_outcome": "A controlled half-open recovery probe was requested.",
            },
            now=now,
        )
        _create_event(
            recovery,
            kind=PDFPipelineRecoveryEventKind.RESUME_REQUESTED,
            reason_code=recovery.reason_code,
            outcome=recovery.last_outcome,
            occurred_at=now,
        )
        result = RecoveryTransitionResult(recovery, changed=True)
    _notify_transition(result.recovery, mark_unread=False)
    return result


def recovery_scope_label(scope: str | RecoveryScope) -> str:
    """Return a stable, identifier-free label for a recovery scope."""

    canonical = canonical_recovery_scope(scope)
    if canonical.startswith("repository:"):
        return "Repository PDF indexing"
    if canonical.startswith("extraction_slot:"):
        return "PDF extraction slot"
    return {
        RecoveryScope.PIPELINE: "PDF pipeline",
        RecoveryScope.SUPERVISOR: "PDF pipeline supervisor",
        RecoveryScope.CONTROLLER: "PDF admission controller",
        RecoveryScope.EXTRACTION_POOL: "PDF extraction pool",
        RecoveryScope.PUBLISHER: "PDF publisher",
    }[RecoveryScope(canonical)]


def recovery_resume_idempotency_key(
    *,
    scope: str | RecoveryScope,
    episode_id: uuid.UUID | str,
    pause_generation: int,
) -> str:
    """Create the opaque server-owned key for exactly one paused generation."""

    canonical_scope = canonical_recovery_scope(scope)
    canonical_episode = _required_uuid(episode_id, label="episode_id")
    canonical_pause = _generation(pause_generation, label="pause_generation")
    value = f"{canonical_scope}:{canonical_episode}:{canonical_pause}"
    return salted_hmac(_RESUME_HMAC_SALT, value, algorithm="sha256").hexdigest()


def recovery_resume_key_is_valid(
    *,
    scope: str | RecoveryScope,
    episode_id: uuid.UUID | str,
    pause_generation: int,
    idempotency_key: object,
) -> bool:
    """Validate a server-produced resume key without leaking comparison timing."""

    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(
        idempotency_key
    ):
        return False
    try:
        expected = recovery_resume_idempotency_key(
            scope=scope,
            episode_id=episode_id,
            pause_generation=pause_generation,
        )
    except ValueError:
        return False
    return hmac.compare_digest(expected, idempotency_key)


def recovery_resume_action(recovery: PDFPipelineRecovery) -> dict[str, object] | None:
    """Return the only browser action accepted for a paused recovery record."""

    if recovery.state != PDFPipelineRecoveryState.PAUSED:
        return None
    return {
        "type": _RESUME_ACTION_TYPE,
        "label": "Resume",
        "method": "POST",
        "url": reverse("bitbucket_search:pdf_pipeline_recovery_resume"),
        "scope": recovery.scope,
        "episodeId": str(recovery.episode_id),
        "expectedGeneration": recovery.generation,
        "pauseGeneration": recovery.pause_generation,
        "idempotencyKey": recovery_resume_idempotency_key(
            scope=recovery.scope,
            episode_id=recovery.episode_id,
            pause_generation=recovery.pause_generation,
        ),
    }


def _popup_control_action(
    recovery: PDFPipelineRecovery,
    *,
    action_type: str,
    route_name: str,
) -> dict[str, object]:
    return {
        "type": action_type,
        "method": "POST",
        "url": reverse(route_name),
        "scope": recovery.scope,
        "episodeId": str(recovery.episode_id),
        "expectedGeneration": recovery.generation,
        "pauseGeneration": recovery.pause_generation,
    }


def recovery_popup_payload(
    recovery: PDFPipelineRecovery,
    *,
    claimed: bool,
) -> dict[str, object]:
    """Build one fixed, redacted popup contract; repository/log input never supplies text."""

    if recovery.state != PDFPipelineRecoveryState.PAUSED or recovery.paused_at is None:
        raise RecoveryTransitionRejected("The PDF recovery scope is not paused.")
    classification = classify_recovery_failure(recovery.reason_code)
    immediate = classification.immediate_pause
    attempt_summary = (
        "Paused immediately for safety."
        if immediate
        else (
            f"Paused after {recovery.consecutive_failed_attempts} failed recovery "
            f"attempt{'s' if recovery.consecutive_failed_attempts != 1 else ''}."
        )
    )
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "type": "pdf_pipeline_recovery_paused",
        "scope": recovery.scope,
        "scopeLabel": recovery_scope_label(recovery.scope),
        "episodeId": str(recovery.episode_id),
        "generation": recovery.generation,
        "pauseGeneration": recovery.pause_generation,
        "reasonCode": classification.reason_code,
        "reason": _public_pause_message(recovery),
        "pausedAt": recovery.paused_at.isoformat(),
        "attemptSummary": attempt_summary,
        "durableWorkSummary": (
            "Queued jobs and valid staged PDF output remain preserved at their last durable boundary."
        ),
        "recommendedNextStep": (
            "Review pipeline details, resolve the reported condition, then use Resume to run "
            "one controlled stability probe."
        ),
        "detailsPath": _RECOVERY_DETAILS_PATH,
    }
    if claimed:
        payload["resumeAction"] = recovery_resume_action(recovery)
        payload["acknowledgeAction"] = _popup_control_action(
            recovery,
            action_type=_POPUP_ACKNOWLEDGE_ACTION_TYPE,
            route_name="bitbucket_search:pdf_pipeline_recovery_popup_acknowledge",
        )
    else:
        payload["claimAction"] = _popup_control_action(
            recovery,
            action_type=_POPUP_CLAIM_ACTION_TYPE,
            route_name="bitbucket_search:pdf_pipeline_recovery_popup_claim",
        )
    return payload


def pending_recovery_popup_payload() -> dict[str, object] | None:
    """Return (without claiming) the oldest pause generation awaiting one popup."""

    recovery = (
        PDFPipelineRecovery.objects.filter(
            state=PDFPipelineRecoveryState.PAUSED,
            paused_at__isnull=False,
            pause_generation__gt=F("popup_claimed_generation"),
        )
        .order_by("paused_at", "id")
        .first()
    )
    return recovery_popup_payload(recovery, claimed=False) if recovery is not None else None


def recovery_notification_poll_active() -> bool:
    """Keep visible-page polling active through recovery and unacknowledged pauses."""

    active_states = {
        PDFPipelineRecoveryState.RETRY_WAIT,
        PDFPipelineRecoveryState.RECOVERING,
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }
    return PDFPipelineRecovery.objects.filter(
        Q(state__in=active_states)
        | Q(
            state=PDFPipelineRecoveryState.PAUSED,
            pause_generation__gt=F("popup_acknowledged_generation"),
        )
    ).exists()


def recovery_notification_payload(event_key: object) -> dict[str, object] | None:
    """Resolve a recovery card by its private stable key without exposing that key."""

    match = _RECOVERY_EVENT_KEY_PATTERN.fullmatch(str(event_key or "").strip().casefold())
    if match is None:
        return None
    try:
        episode_id = uuid.UUID(match.group("episode"))
    except ValueError:  # pragma: no cover - guarded by the exact expression
        return None
    recovery = PDFPipelineRecovery.objects.filter(episode_id=episode_id).first()
    if recovery is None:
        return None
    return {
        "recovery": recovery_payload(recovery),
        "action": recovery_resume_action(recovery),
    }


def claim_recovery_popup(
    scope: str | RecoveryScope,
    *,
    episode_id: uuid.UUID | str,
    expected_generation: int,
    pause_generation: int,
    occurred_at: datetime | None = None,
) -> RecoveryTransitionResult:
    """Atomically grant at most one browser tab the popup for a pause generation."""

    canonical_scope = canonical_recovery_scope(scope)
    canonical_episode = _required_uuid(episode_id, label="episode_id")
    expected = _generation(expected_generation, label="expected_generation")
    expected_pause = _generation(pause_generation, label="pause_generation")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        if recovery.episode_id != canonical_episode or recovery.pause_generation != expected_pause:
            raise RecoveryConflict("The recovery popup is stale.")
        if recovery.popup_claimed_generation >= expected_pause:
            return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
        _check_expected_generation(recovery, expected)
        if recovery.state != PDFPipelineRecoveryState.PAUSED:
            raise RecoveryTransitionRejected("The PDF recovery scope is not paused.")
        recovery = _cas_update(
            recovery,
            {"popup_claimed_generation": expected_pause},
            now=now,
        )
        return RecoveryTransitionResult(recovery, changed=True)


def acknowledge_recovery_popup(
    scope: str | RecoveryScope,
    *,
    episode_id: uuid.UUID | str,
    expected_generation: int,
    pause_generation: int,
    occurred_at: datetime | None = None,
) -> RecoveryTransitionResult:
    """Persist dismissal separately from notification read state and from resume."""

    canonical_scope = canonical_recovery_scope(scope)
    canonical_episode = _required_uuid(episode_id, label="episode_id")
    expected = _generation(expected_generation, label="expected_generation")
    expected_pause = _generation(pause_generation, label="pause_generation")
    now = _aware_timestamp(occurred_at, label="occurred_at")
    with _recovery_lock(), transaction.atomic():
        recovery = _get_recovery(canonical_scope)
        if recovery.episode_id != canonical_episode or recovery.pause_generation != expected_pause:
            raise RecoveryConflict("The recovery popup is stale.")
        if recovery.popup_acknowledged_generation >= expected_pause:
            return RecoveryTransitionResult(recovery, changed=False, duplicate=True)
        _check_expected_generation(recovery, expected)
        if recovery.state != PDFPipelineRecoveryState.PAUSED:
            raise RecoveryTransitionRejected("The PDF recovery scope is not paused.")
        if recovery.popup_claimed_generation < expected_pause:
            raise RecoveryTransitionRejected("The recovery popup was not claimed.")
        recovery = _cas_update(
            recovery,
            {"popup_acknowledged_generation": expected_pause},
            now=now,
        )
        return RecoveryTransitionResult(recovery, changed=True)


def recovery_resume_preflight(payload: dict[str, object]) -> ResumeSafetyResult:
    """Fail closed unless current local resources and the original safety class permit a probe."""

    if not getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
        return ResumeSafetyResult.blocked("automatic_recovery_disabled")
    try:
        configured_recovery_stability_seconds()
        _pause_after_attempts(None)
        _configured_backoff(1, jitter=lambda low, _high: low)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                return ResumeSafetyResult.blocked("database_check_failed")
        migration_executor = MigrationExecutor(connection)
        if migration_executor.migration_plan(migration_executor.loader.graph.leaf_nodes()):
            return ResumeSafetyResult.blocked("database_migrations_pending")
        state_root = _recovery_lock_path().parent
        if not state_root.is_dir() or not os.access(state_root, os.R_OK | os.W_OK | os.X_OK):
            return ResumeSafetyResult.blocked("state_store_unavailable")

        from bitbucket_search.services.pdf_pipeline_metrics import resource_snapshot

        resources = resource_snapshot()
        disk_available = resources.get("diskAvailableBytes")
        memory_available = resources.get("hostMemoryAvailableBytes")
        if not isinstance(disk_available, int):
            return ResumeSafetyResult.blocked("disk_check_unavailable")
        required_disk = max(
            _MINIMUM_RESUME_DISK_BYTES,
            int(getattr(settings, "PDF_MAX_FILE_BYTES", _MINIMUM_RESUME_DISK_BYTES)),
        )
        if disk_available < required_disk:
            return ResumeSafetyResult.blocked("disk_space_still_unsafe")
        if not isinstance(memory_available, int):
            return ResumeSafetyResult.blocked("memory_check_unavailable")
        required_memory = int(getattr(settings, "PDF_MAX_PROCESS_MEMORY_BYTES", 1))
        if memory_available < required_memory:
            return ResumeSafetyResult.blocked("memory_still_unsafe")

        scope = canonical_recovery_scope(str(payload.get("scope") or ""))
        repository = None
        if scope.startswith("repository:"):
            from bitbucket_search.models import BitbucketRepository
            from bitbucket_search.services.git_sync import managed_repository_path

            repository_id = int(scope.split(":", maxsplit=1)[1])
            repository = BitbucketRepository.objects.filter(pk=repository_id, enabled=True).first()
            if repository is None:
                return ResumeSafetyResult.blocked("repository_unavailable")
            expected_path = managed_repository_path(repository)
            if (
                not repository.local_path
                or expected_path != Path(repository.local_path).resolve(strict=False)
                or not expected_path.is_dir()
                or not (expected_path / ".git").is_dir()
            ):
                return ResumeSafetyResult.blocked("checkout_not_ready")

        classification = classify_recovery_failure(payload.get("reasonCode"))
        if classification.reason_code == RecoveryReasonCode.MISSING_CREDENTIALS:
            if repository is None or not repository.remote_url.startswith(("http://", "https://")):
                return ResumeSafetyResult.blocked("credential_check_required")
            from bitbucket_search.services.https_credentials import resolve_https_credential

            if resolve_https_credential(repository.remote_url) is None:
                return ResumeSafetyResult.blocked("credentials_still_unavailable")
        if classification.reason_code in {
            RecoveryReasonCode.DATA_INTEGRITY,
            RecoveryReasonCode.REPEATED_CORRUPTION,
            RecoveryReasonCode.DETERMINISTIC_VALIDATION,
            RecoveryReasonCode.UNSUPPORTED_FORMAT,
            RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE,
        }:
            return ResumeSafetyResult.blocked("integrity_check_required")
    except Exception:
        logger.warning("PDF recovery resume preflight could not prove current safety.")
        return ResumeSafetyResult.blocked("safety_check_unavailable")
    return ResumeSafetyResult.safe()


def publish_recovery_notification(
    recovery: PDFPipelineRecovery,
    *,
    mark_unread: bool,
) -> None:
    """Update one durable card per episode; pause generations control unread state."""

    if recovery.paused_at is None:
        return
    from bookmark_manager.models import Notification, NotificationKind, NotificationState
    from bookmark_manager.services.notifications import publish_notification

    scope_label = recovery_scope_label(recovery.scope)
    if recovery.state == PDFPipelineRecoveryState.PAUSED:
        state = NotificationState.ERROR
        title = f"{scope_label} recovery paused"
        message = _public_pause_message(recovery)
        occurred_at = recovery.paused_at
    elif recovery.state in {
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }:
        state = NotificationState.RUNNING
        title = f"{scope_label} recovery resumed"
        message = "OWL is running one controlled recovery stability probe."
        occurred_at = recovery.resume_requested_at or recovery.updated_at
    elif recovery.state == PDFPipelineRecoveryState.HEALTHY:
        state = NotificationState.SUCCESS
        title = f"{scope_label} recovery completed"
        message = "The affected PDF pipeline component passed its stability check."
        occurred_at = recovery.recovered_at or recovery.updated_at
    else:
        return
    event_key = f"pdf-pipeline-recovery:{recovery.episode_id}"
    if state == NotificationState.ERROR:
        existing_pause = Notification.objects.filter(
            event_key=event_key,
            state=NotificationState.ERROR,
            occurred_at=occurred_at,
        ).exists()
        # Reconciliation/startup may republish an already-visible pause. A new
        # paused_at marks a new pause generation; the same value preserves read state.
        mark_unread = mark_unread and not existing_pause
    publish_notification(
        event_key=event_key,
        kind=NotificationKind.PDF_PIPELINE_RECOVERY,
        state=state,
        title=title,
        message=message,
        target_path="/pdfs/status/",
        occurred_at=occurred_at,
        mark_unread=mark_unread,
    )


def _notify_transition(recovery: PDFPipelineRecovery, *, mark_unread: bool) -> None:
    if recovery.paused_at is None or recovery.state not in {
        PDFPipelineRecoveryState.PAUSED,
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
        PDFPipelineRecoveryState.HEALTHY,
    }:
        return
    try:
        publish_recovery_notification(recovery, mark_unread=mark_unread)
    except Exception:
        # Canonical recovery truth must survive a temporarily unavailable alert store.
        # Do not include exception text, paths, repository IDs, or scope identifiers.
        logger.warning("PDF recovery notification publication is pending.")


def _reconcile_recovery_scope(
    scope: str,
    *,
    configured_threshold: int,
) -> PDFPipelineRecovery:
    """Choose one authority under the cross-process lock before any role launch."""

    notify_after_commit = False
    checkpoint_after_commit = False
    with _recovery_lock(), transaction.atomic():
        fallback = pdf_recovery_fallback.read_fallback(scope)
        recovery, _created = PDFPipelineRecovery.objects.get_or_create(
            scope=scope,
            defaults={"pause_after_attempts": configured_threshold},
        )
        fallback_record = fallback.record
        fallback_invalid = fallback.state in {
            pdf_recovery_fallback.FallbackReadState.CORRUPT,
            pdf_recovery_fallback.FallbackReadState.UNAVAILABLE,
        }
        fallback_unorderable = bool(
            fallback_record is not None and not fallback_record.generationOrdered
        )
        if fallback_invalid or fallback_unorderable:
            already_conflict_paused = bool(
                recovery.state == PDFPipelineRecoveryState.PAUSED
                and recovery.reason_code == RecoveryReasonCode.CONTROL_STATE_CONFLICT
                and (fallback_record is None or recovery.generation > fallback_record.generation)
            )
            if not already_conflict_paused:
                recovery = _pause_for_control_state_conflict(recovery, fallback_record)
                notify_after_commit = True
            checkpoint_after_commit = True
        elif fallback_record is not None:
            if fallback_record.generation > recovery.generation:
                recovery = _apply_fallback_record(recovery, fallback_record)
                notify_after_commit = fallback_record.pendingReconciliation
                checkpoint_after_commit = True
            elif (
                fallback_record.generation == recovery.generation
                and fallback_record.clean_checkpoint() != _fallback_record_from_recovery(recovery)
            ):
                recovery = _pause_for_control_state_conflict(recovery, fallback_record)
                notify_after_commit = True
                checkpoint_after_commit = True
            elif (
                fallback_record.generation < recovery.generation
                or fallback_record.pendingReconciliation
            ):
                checkpoint_after_commit = True
            # A greater database generation wins for every state, including
            # healthy.  This deliberately prevents an older paused checkpoint
            # from resurrecting a completed recovery episode.
        else:
            checkpoint_after_commit = True
        if checkpoint_after_commit:
            _schedule_reconciled_checkpoint(
                recovery,
                publish_notification_after_commit=notify_after_commit,
            )
    return recovery


def _apply_fallback_record(
    recovery: PDFPipelineRecovery,
    record: pdf_recovery_fallback.FallbackRecoveryRecord,
) -> PDFPipelineRecovery:
    classification = classify_recovery_failure(record.reasonCode) if record.reasonCode else None
    paused_at = _fallback_timestamp(record.pausedAt)
    values: dict[str, object] = {
        "state": record.state,
        "episode_id": uuid.UUID(record.episodeId),
        "generation": record.generation,
        "pause_generation": record.pauseGeneration,
        "reason_family": classification.reason_family if classification is not None else "",
        "reason_code": classification.reason_code if classification is not None else "",
        "consecutive_failed_attempts": record.consecutiveFailedAttempts,
        "lifetime_attempts": record.lifetimeAttempts,
        "pause_after_attempts": record.pauseAfterAttempts,
        "first_failure_at": _fallback_timestamp(record.firstFailureAt),
        "last_failure_at": _fallback_timestamp(record.lastFailureAt),
        "last_attempt_at": _fallback_timestamp(record.lastAttemptAt),
        "next_retry_at": _fallback_timestamp(record.nextRetryAt),
        "current_backoff_seconds": record.currentBackoffSeconds,
        "paused_reason": (
            _pause_message(record.reasonCode, threshold=record.pauseAfterAttempts)
            if paused_at is not None and record.reasonCode
            else ""
        ),
        "paused_at": paused_at,
        "popup_acknowledged_generation": record.popupAcknowledgedGeneration,
        "popup_claimed_generation": record.popupClaimedGeneration,
        "resume_requested_at": _fallback_timestamp(record.resumeRequestedAt),
        "resume_idempotency_key_hash": record.resumeIdempotencyKeyHash,
        "resume_predecessor_generation": record.resumePredecessorGeneration,
        "recovered_at": _fallback_timestamp(record.recoveredAt),
        "last_outcome": _fallback_last_outcome(record),
        "active_attempt_id": (
            uuid.UUID(record.activeAttemptId) if record.activeAttemptId is not None else None
        ),
        "updated_at": timezone.now(),
    }
    changed = PDFPipelineRecovery.objects.filter(
        pk=recovery.pk,
        generation=recovery.generation,
    ).update(**values)
    if changed != 1:
        raise RecoveryConflict("The PDF recovery state changed during fallback reconciliation.")
    reconciled = PDFPipelineRecovery.objects.get(pk=recovery.pk)
    _create_event(
        reconciled,
        kind=(
            PDFPipelineRecoveryEventKind.PAUSED
            if reconciled.state == PDFPipelineRecoveryState.PAUSED
            else (
                PDFPipelineRecoveryEventKind.RECOVERED
                if reconciled.state == PDFPipelineRecoveryState.HEALTHY
                else PDFPipelineRecoveryEventKind.SUPERSEDED
            )
        ),
        reason_code=reconciled.reason_code,
        outcome=reconciled.last_outcome,
        occurred_at=timezone.now(),
    )
    return reconciled


def _pause_for_control_state_conflict(
    recovery: PDFPipelineRecovery,
    fallback: pdf_recovery_fallback.FallbackRecoveryRecord | None,
) -> PDFPipelineRecovery:
    fallback_generation = fallback.generation if fallback is not None else 0
    fallback_pause_generation = fallback.pauseGeneration if fallback is not None else 0
    highest_generation = max(recovery.generation, fallback_generation)
    highest_pause_generation = max(recovery.pause_generation, fallback_pause_generation)
    if (
        highest_generation >= pdf_recovery_fallback.MAX_GENERATION
        or highest_pause_generation >= pdf_recovery_fallback.MAX_GENERATION
    ):
        raise RecoveryControlUnavailable(durably_recorded=False)
    now = timezone.now()
    classification = classify_recovery_failure(RecoveryReasonCode.CONTROL_STATE_CONFLICT)
    generation = highest_generation + 1
    pause_generation = highest_pause_generation + 1
    changed = PDFPipelineRecovery.objects.filter(
        pk=recovery.pk,
        generation=recovery.generation,
    ).update(
        state=PDFPipelineRecoveryState.PAUSED,
        episode_id=uuid.uuid4(),
        generation=generation,
        pause_generation=pause_generation,
        reason_family=classification.reason_family,
        reason_code=classification.reason_code,
        consecutive_failed_attempts=recovery.consecutive_failed_attempts,
        first_failure_at=recovery.first_failure_at or now,
        last_failure_at=now,
        next_retry_at=None,
        current_backoff_seconds=0,
        paused_reason=_pause_message(
            classification.reason_code,
            threshold=recovery.pause_after_attempts,
        ),
        paused_at=now,
        active_attempt_id=None,
        resume_requested_at=None,
        resume_idempotency_key_hash="",
        resume_predecessor_generation=None,
        recovered_at=None,
        last_outcome=_reason_message(classification.reason_code),
        updated_at=now,
    )
    if changed != 1:
        raise RecoveryConflict("The PDF recovery state changed during safety reconciliation.")
    reconciled = PDFPipelineRecovery.objects.get(pk=recovery.pk)
    _create_event(
        reconciled,
        kind=PDFPipelineRecoveryEventKind.EPISODE_OPENED,
        reason_code=classification.reason_code,
        outcome=reconciled.last_outcome,
        occurred_at=now,
    )
    _create_event(
        reconciled,
        kind=PDFPipelineRecoveryEventKind.PAUSED,
        reason_code=classification.reason_code,
        outcome=reconciled.paused_reason,
        occurred_at=now,
    )
    return reconciled


def _fallback_record_from_recovery(
    recovery: PDFPipelineRecovery,
) -> pdf_recovery_fallback.FallbackRecoveryRecord:
    raw_reason = str(recovery.reason_code or "")
    reason_code = classify_recovery_failure(raw_reason).reason_code if raw_reason else ""
    return pdf_recovery_fallback.FallbackRecoveryRecord(
        schemaVersion=pdf_recovery_fallback.FALLBACK_SCHEMA_VERSION,
        scopeFingerprint=pdf_recovery_fallback.scope_fingerprint(recovery.scope),
        generation=recovery.generation,
        generationOrdered=True,
        state=recovery.state,
        episodeId=str(recovery.episode_id),
        pauseGeneration=recovery.pause_generation,
        reasonCode=reason_code,
        consecutiveFailedAttempts=recovery.consecutive_failed_attempts,
        lifetimeAttempts=recovery.lifetime_attempts,
        pauseAfterAttempts=recovery.pause_after_attempts,
        firstFailureAt=_isoformat(recovery.first_failure_at),
        lastFailureAt=_isoformat(recovery.last_failure_at),
        lastAttemptAt=_isoformat(recovery.last_attempt_at),
        nextRetryAt=_isoformat(recovery.next_retry_at),
        currentBackoffSeconds=recovery.current_backoff_seconds,
        pausedAt=_isoformat(recovery.paused_at),
        popupAcknowledgedGeneration=recovery.popup_acknowledged_generation,
        popupClaimedGeneration=recovery.popup_claimed_generation,
        resumeRequestedAt=_isoformat(recovery.resume_requested_at),
        resumeIdempotencyKeyHash=recovery.resume_idempotency_key_hash,
        resumePredecessorGeneration=recovery.resume_predecessor_generation,
        recoveredAt=_isoformat(recovery.recovered_at),
        activeAttemptId=(
            str(recovery.active_attempt_id) if recovery.active_attempt_id is not None else None
        ),
        pendingReconciliation=False,
    )


def _schedule_reconciled_checkpoint(
    recovery: PDFPipelineRecovery,
    *,
    publish_notification_after_commit: bool,
) -> None:
    checkpoint = _fallback_record_from_recovery(recovery)
    transaction.on_commit(
        lambda: _finish_reconciled_checkpoint(
            recovery,
            checkpoint,
            publish_notification_after_commit=publish_notification_after_commit,
        )
    )


def _finish_reconciled_checkpoint(
    recovery: PDFPipelineRecovery,
    checkpoint: pdf_recovery_fallback.FallbackRecoveryRecord,
    *,
    publish_notification_after_commit: bool,
) -> None:
    try:
        pdf_recovery_fallback.mark_reconciled(recovery.scope, checkpoint)
    except Exception:
        # SQLite has committed and remains authoritative. Keep an older pending
        # file intact and retry reconciliation later; log no path or exception.
        logger.warning("event=pdf_recovery_fallback_checkpoint_pending")
    if publish_notification_after_commit and recovery.paused_at is not None:
        try:
            publish_recovery_notification(
                recovery,
                mark_unread=recovery.state == PDFPipelineRecoveryState.PAUSED,
            )
        except Exception:
            logger.warning("event=pdf_recovery_notification_pending")


def _fallback_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _fallback_last_outcome(
    record: pdf_recovery_fallback.FallbackRecoveryRecord,
) -> str:
    if record.state == PDFPipelineRecoveryState.HEALTHY:
        return (
            "Recovery stability check passed."
            if record.recoveredAt is not None
            else "PDF recovery control state is healthy."
        )
    if record.state in {
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }:
        return "A controlled recovery stability probe is in progress."
    if record.state == PDFPipelineRecoveryState.RETRY_WAIT:
        return "Recovery retry scheduled."
    return _reason_message(record.reasonCode)


def _recovery_lock_path() -> Path:
    lock_root = Path(
        getattr(
            settings,
            "PDF_PIPELINE_STATE_ROOT",
            Path(settings.OWL_DATA_ROOT) / "pdf-pipeline",
        )
    )
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return lock_root / "pdf-pipeline-recovery.lock"


def _recovery_lock():
    return _file_lock(_recovery_lock_path(), blocking=True)


def _get_recovery(scope: str) -> PDFPipelineRecovery:
    try:
        return PDFPipelineRecovery.objects.get(scope=scope)
    except PDFPipelineRecovery.DoesNotExist as exc:
        raise RecoveryNotFound("The PDF recovery scope does not exist.") from exc


def _cas_update(
    recovery: PDFPipelineRecovery,
    updates: dict[str, object],
    *,
    now: datetime,
) -> PDFPipelineRecovery:
    previous_generation = recovery.generation
    next_generation = previous_generation + 1
    changed = PDFPipelineRecovery.objects.filter(
        pk=recovery.pk,
        generation=previous_generation,
    ).update(generation=next_generation, updated_at=now, **updates)
    if changed != 1:
        raise RecoveryConflict("The PDF recovery state changed concurrently.")
    updated = PDFPipelineRecovery.objects.get(pk=recovery.pk)
    _schedule_reconciled_checkpoint(
        updated,
        publish_notification_after_commit=False,
    )
    return updated


def _create_event(
    recovery: PDFPipelineRecovery,
    *,
    kind: str,
    reason_code: str,
    outcome: str,
    occurred_at: datetime,
    attempt_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
) -> PDFPipelineRecoveryEvent:
    values: dict[str, object] = {
        "recovery": recovery,
        "attempt_id": attempt_id,
        "generation": recovery.generation,
        "pause_generation": recovery.pause_generation,
        "kind": kind,
        "reason_code": reason_code,
        "outcome": outcome,
        "occurred_at": occurred_at,
    }
    if event_id is not None:
        values["event_id"] = event_id
    if correlation_id is not None:
        values["correlation_id"] = correlation_id
    return PDFPipelineRecoveryEvent.objects.create(**values)


def _event_duplicate(event_id: uuid.UUID | None) -> PDFPipelineRecoveryEvent | None:
    if event_id is None:
        return None
    event = (
        PDFPipelineRecoveryEvent.objects.select_related("recovery")
        .filter(event_id=event_id)
        .first()
    )
    return event


def _attempt_event_exists(
    recovery: PDFPipelineRecovery,
    attempt_id: uuid.UUID,
    kind: str,
) -> bool:
    return PDFPipelineRecoveryEvent.objects.filter(
        recovery=recovery,
        attempt_id=attempt_id,
        kind=kind,
    ).exists()


def _configured_backoff(failed_attempts: int, *, jitter: JitterFunction | None) -> int:
    return recovery_backoff_seconds(
        failed_attempts,
        base_seconds=_integer_setting(
            "PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS",
            DEFAULT_BACKOFF_BASE_SECONDS,
            minimum=1,
            maximum=MAX_BACKOFF_SECONDS,
        ),
        maximum_seconds=_integer_setting(
            "PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS",
            DEFAULT_BACKOFF_MAX_SECONDS,
            minimum=1,
            maximum=MAX_BACKOFF_SECONDS,
        ),
        jitter_fraction=_float_setting(
            "PDF_PIPELINE_RECOVERY_JITTER_FRACTION",
            DEFAULT_BACKOFF_JITTER_FRACTION,
            minimum=0,
            maximum=1,
        ),
        jitter=jitter,
    )


def _pause_after_attempts(value: int | None) -> int:
    if value is None:
        value = getattr(
            settings,
            "PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS",
            DEFAULT_PAUSE_AFTER_ATTEMPTS,
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pause_after_attempts must be an integer.")
    if not 0 <= value <= MAX_PAUSE_AFTER_ATTEMPTS:
        raise ValueError(f"pause_after_attempts must be between 0 and {MAX_PAUSE_AFTER_ATTEMPTS}.")
    return value


def _integer_setting(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _float_setting(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return number


def _check_expected_generation(
    recovery: PDFPipelineRecovery,
    expected_generation: int | None,
) -> None:
    if expected_generation is None:
        return
    expected = _generation(expected_generation, label="expected_generation")
    if recovery.generation != expected:
        raise RecoveryConflict("The PDF recovery generation is stale.")


def _generation(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")
    return value


def _optional_uuid(value: uuid.UUID | str | None, *, label: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _required_uuid(value, label=label)


def _required_uuid(value: uuid.UUID | str, *, label: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID.") from exc


def _aware_timestamp(value: datetime | None, *, label: str) -> datetime:
    timestamp = value or timezone.now()
    if not isinstance(timestamp, datetime) or timezone.is_naive(timestamp):
        raise ValueError(f"{label} must include a timezone.")
    return timestamp


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _idempotency_key_hash(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ValueError("The recovery idempotency key is invalid.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_resume_block_reason(value: object) -> str:
    canonical = str(value or "").strip().casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", canonical):
        return "safety_condition_unresolved"
    return canonical


def _reason_message(reason_code: str) -> str:
    try:
        reason = RecoveryReasonCode(reason_code)
    except ValueError:
        reason = RecoveryReasonCode.UNKNOWN_COMPONENT_FAILURE
    return _REASON_MESSAGES.get(reason, "OWL paused PDF recovery for safety.")


def _public_pause_message(recovery: PDFPipelineRecovery) -> str:
    """Derive popup/card copy from the closed reason vocabulary, never stored exception text."""

    half_open_repause = bool(
        recovery.pause_generation > 1
        and recovery.resume_requested_at is not None
        and recovery.paused_at is not None
        and recovery.paused_at >= recovery.resume_requested_at
    )
    return _pause_message(
        classify_recovery_failure(recovery.reason_code).reason_code,
        threshold=recovery.pause_after_attempts,
        half_open=half_open_repause,
    )


def _public_last_outcome(recovery: PDFPipelineRecovery) -> str | None:
    if not recovery.last_outcome:
        return None
    if recovery.state == PDFPipelineRecoveryState.PAUSED:
        return _public_pause_message(recovery)
    if recovery.state in {
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }:
        return "A controlled recovery stability probe is in progress."
    if recovery.state == PDFPipelineRecoveryState.HEALTHY and recovery.recovered_at is not None:
        return "Recovery stability check passed."
    return _reason_message(recovery.reason_code)


def configured_recovery_stability_seconds() -> int:
    return _integer_setting(
        "PDF_PIPELINE_RECOVERY_STABILITY_SECONDS",
        DEFAULT_STABILITY_SECONDS,
        minimum=1,
        maximum=MAX_BACKOFF_SECONDS,
    )


def _pause_message(reason_code: str, *, threshold: int, half_open: bool = False) -> str:
    classification = classify_recovery_failure(reason_code)
    if classification.immediate_pause:
        return f"PDF recovery paused immediately for safety. {_reason_message(reason_code)}"
    if half_open:
        return "PDF recovery paused again after its controlled recovery probe failed."
    if threshold == 0:
        return "PDF recovery paused before automatic relaunch because its threshold is zero."
    return f"PDF recovery paused after {threshold} failed recovery attempts."
