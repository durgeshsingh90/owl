"""Small, redacted, same-disk recovery checkpoint used during database outages.

The fallback is deliberately not a second operational database.  It contains one
bounded control record per recovery scope and is consulted only to prevent an
affected component from launching while canonical SQLite state is unavailable or
while a newer fallback decision is waiting to be reconciled.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

FALLBACK_SCHEMA_VERSION: Final = 1
MAX_FALLBACK_BYTES: Final = 8 * 1024
MAX_FALLBACK_FILES: Final = 4_096
MAX_GENERATION: Final = (2**63) - 1
MAX_PAUSE_AFTER_ATTEMPTS: Final = 10_000
MAX_BACKOFF_SECONDS: Final = 86_400

_FALLBACK_DIRECTORY_NAME: Final = "recovery-fallback-v1"
_SCOPE_HMAC_SALT: Final = "owl.pdf-pipeline-recovery.fallback-scope.v1"
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDEMPOTENCY_HASH_PATTERN = re.compile(r"(?:[0-9a-f]{64})?")
_TIMESTAMP_MAX_LENGTH: Final = 48
_RETRY_BACKOFF_MAX_SECONDS: Final = 60.0

_ALLOWED_STATES: Final = frozenset(
    {
        "healthy",
        "retry_wait",
        "recovering",
        "paused",
        "resume_requested",
        "recovering_half_open",
    }
)
_ALLOWED_REASON_CODES: Final = frozenset(
    {
        "",
        "process_exit",
        "stale_heartbeat",
        "launch_failed",
        "sqlite_busy",
        "sqlite_locked",
        "temporary_io",
        "temporary_resource",
        "error_loop",
        "no_forward_progress",
        "publisher_failed",
        "supervisor_loop_failed",
        "critical_disk",
        "data_integrity",
        "migration_schema",
        "unsafe_configuration",
        "repeated_corruption",
        "missing_credentials",
        "invalid_configuration",
        "unsupported_format",
        "deterministic_validation",
        "corrupt_pdf",
        "encrypted_pdf",
        "unsupported_pdf",
        "pdf_too_large",
        "pdf_changed",
        "user_cancelled",
        "planned_shutdown",
        "suspend_wake",
        "unknown_component_failure",
        "control_state_unavailable",
        "control_state_conflict",
    }
)


class FallbackReadState(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    CORRUPT = "corrupt"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FallbackRecoveryRecord:
    """The exact bounded schema stored on disk; scope itself is never persisted."""

    schemaVersion: int
    scopeFingerprint: str
    generation: int
    generationOrdered: bool
    state: str
    episodeId: str
    pauseGeneration: int
    reasonCode: str
    consecutiveFailedAttempts: int
    lifetimeAttempts: int
    pauseAfterAttempts: int
    firstFailureAt: str | None
    lastFailureAt: str | None
    lastAttemptAt: str | None
    nextRetryAt: str | None
    currentBackoffSeconds: int
    pausedAt: str | None
    popupAcknowledgedGeneration: int
    popupClaimedGeneration: int
    resumeRequestedAt: str | None
    resumeIdempotencyKeyHash: str
    resumePredecessorGeneration: int | None
    recoveredAt: str | None
    activeAttemptId: str | None
    pendingReconciliation: bool

    def as_json(self) -> bytes:
        payload = json.dumps(
            asdict(self),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(payload) > MAX_FALLBACK_BYTES:
            raise ValueError("The recovery fallback record exceeds its fixed size limit.")
        return payload

    def clean_checkpoint(self) -> FallbackRecoveryRecord:
        return replace(self, pendingReconciliation=False)


@dataclass(frozen=True, slots=True)
class FallbackReadResult:
    state: FallbackReadState
    record: FallbackRecoveryRecord | None = None


@dataclass(slots=True)
class _RetryState:
    failures: int
    next_retry_monotonic: float


_in_memory_fail_closed: set[str] = set()
_write_retry_state: dict[str, _RetryState] = {}
_emergency_log_next: dict[str, float] = {}


def scope_fingerprint(scope: str) -> str:
    """Return a secret-keyed stable identifier without persisting a private scope."""

    return salted_hmac(_SCOPE_HMAC_SALT, scope, algorithm="sha256").hexdigest()


def fallback_path(scope: str) -> Path:
    return fallback_directory() / f"{scope_fingerprint(scope)}.json"


def fallback_directory() -> Path:
    state_root = Path(
        getattr(
            settings,
            "PDF_PIPELINE_STATE_ROOT",
            Path(settings.OWL_DATA_ROOT) / "pdf-pipeline",
        )
    )
    return state_root / _FALLBACK_DIRECTORY_NAME


def read_fallback(scope: str) -> FallbackReadResult:
    """Read and strictly validate one checkpoint without returning I/O details."""

    fingerprint = scope_fingerprint(scope)
    path = fallback_path(scope)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return FallbackReadResult(FallbackReadState.MISSING)
    except OSError:
        return FallbackReadResult(FallbackReadState.UNAVAILABLE)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FALLBACK_BYTES:
        return FallbackReadResult(FallbackReadState.CORRUPT)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_size > MAX_FALLBACK_BYTES
            ):
                return FallbackReadResult(FallbackReadState.CORRUPT)
            payload = b""
            while len(payload) <= MAX_FALLBACK_BYTES:
                chunk = os.read(descriptor, min(4096, MAX_FALLBACK_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
        finally:
            os.close(descriptor)
    except OSError:
        return FallbackReadResult(FallbackReadState.UNAVAILABLE)
    if len(payload) > MAX_FALLBACK_BYTES:
        return FallbackReadResult(FallbackReadState.CORRUPT)

    try:
        decoded = json.loads(payload.decode("ascii"))
        record = _validated_record(decoded, expected_fingerprint=fingerprint)
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return FallbackReadResult(FallbackReadState.CORRUPT)
    return FallbackReadResult(FallbackReadState.VALID, record)


def write_fallback(record: FallbackRecoveryRecord) -> None:
    """Durably replace one record on the same filesystem, including directory fsync."""

    _validated_record(asdict(record), expected_fingerprint=record.scopeFingerprint)
    payload = record.as_json()
    directory = fallback_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("Recovery fallback directory is unavailable.")
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    destination = directory / f"{record.scopeFingerprint}.json"
    if not destination.exists() and _bounded_file_count(directory) >= MAX_FALLBACK_FILES:
        raise OSError("Recovery fallback file limit reached.")
    temporary = directory / f".{record.scopeFingerprint}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("Recovery fallback write did not advance.")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_descriptor = os.open(directory, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def persist_database_unavailable_pause(
    scope: str,
    *,
    now: datetime | None = None,
    monotonic_now: float | None = None,
) -> bool:
    """Persist a deduplicated fail-closed pause, or retain it only in this process."""

    fingerprint = scope_fingerprint(scope)
    _in_memory_fail_closed.add(fingerprint)
    clock = time.monotonic() if monotonic_now is None else monotonic_now
    retry = _write_retry_state.get(fingerprint)
    if retry is not None and clock < retry.next_retry_monotonic:
        return False

    existing = read_fallback(scope)
    if (
        existing.state == FallbackReadState.VALID
        and existing.record is not None
        and existing.record.state == "paused"
    ):
        _in_memory_fail_closed.discard(fingerprint)
        _write_retry_state.pop(fingerprint, None)
        return True

    timestamp = now or timezone.now()
    if timezone.is_naive(timestamp):
        raise ValueError("Fallback timestamps must include a timezone.")
    prior = existing.record if existing.state == FallbackReadState.VALID else None
    ordered = bool(prior is not None and prior.generationOrdered)
    generation = prior.generation + 1 if ordered and prior.generation < MAX_GENERATION else 0
    if ordered and generation == 0:
        ordered = False
    pause_generation = min(
        MAX_GENERATION,
        (prior.pauseGeneration if prior is not None else 0) + 1,
    )
    iso_timestamp = timestamp.isoformat()
    record = FallbackRecoveryRecord(
        schemaVersion=FALLBACK_SCHEMA_VERSION,
        scopeFingerprint=fingerprint,
        generation=generation,
        generationOrdered=ordered,
        state="paused",
        episodeId=(prior.episodeId if prior is not None else str(uuid.uuid4())),
        pauseGeneration=pause_generation,
        reasonCode="control_state_unavailable",
        consecutiveFailedAttempts=(prior.consecutiveFailedAttempts if prior is not None else 0),
        lifetimeAttempts=(prior.lifetimeAttempts if prior is not None else 0),
        pauseAfterAttempts=(prior.pauseAfterAttempts if prior is not None else 25),
        firstFailureAt=(prior.firstFailureAt if prior is not None else iso_timestamp),
        lastFailureAt=iso_timestamp,
        lastAttemptAt=(prior.lastAttemptAt if prior is not None else None),
        nextRetryAt=None,
        currentBackoffSeconds=0,
        pausedAt=iso_timestamp,
        popupAcknowledgedGeneration=(
            min(prior.popupAcknowledgedGeneration, pause_generation) if prior is not None else 0
        ),
        popupClaimedGeneration=(
            min(prior.popupClaimedGeneration, pause_generation) if prior is not None else 0
        ),
        resumeRequestedAt=None,
        resumeIdempotencyKeyHash="",
        resumePredecessorGeneration=None,
        recoveredAt=None,
        activeAttemptId=None,
        pendingReconciliation=True,
    )
    try:
        write_fallback(record)
    except OSError:
        failures = min(16, (retry.failures if retry is not None else 0) + 1)
        _write_retry_state[fingerprint] = _RetryState(
            failures=failures,
            next_retry_monotonic=clock + min(
                _RETRY_BACKOFF_MAX_SECONDS,
                float(2 ** min(failures - 1, 6)),
            ),
        )
        return False
    _in_memory_fail_closed.discard(fingerprint)
    _write_retry_state.pop(fingerprint, None)
    return True


def mark_reconciled(scope: str, record: FallbackRecoveryRecord) -> None:
    """Replace pending state with a clean generation checkpoint after DB commit."""

    write_fallback(record.clean_checkpoint())
    fingerprint = scope_fingerprint(scope)
    _in_memory_fail_closed.discard(fingerprint)
    _write_retry_state.pop(fingerprint, None)
    _emergency_log_next.pop(fingerprint, None)


def is_in_memory_fail_closed(scope: str) -> bool:
    return scope_fingerprint(scope) in _in_memory_fail_closed


def emergency_log_due(scope: str, *, monotonic_now: float | None = None) -> bool:
    """Rate-limit the one fixed emergency event without persisting private data."""

    fingerprint = scope_fingerprint(scope)
    clock = time.monotonic() if monotonic_now is None else monotonic_now
    next_log = _emergency_log_next.get(fingerprint, 0.0)
    if clock < next_log:
        return False
    _emergency_log_next[fingerprint] = clock + _RETRY_BACKOFF_MAX_SECONDS
    return True


def reset_process_state_for_tests() -> None:
    _in_memory_fail_closed.clear()
    _write_retry_state.clear()
    _emergency_log_next.clear()


def _bounded_file_count(directory: Path) -> int:
    count = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                count += 1
                if count >= MAX_FALLBACK_FILES:
                    break
    return count


def _validated_record(
    value: object,
    *,
    expected_fingerprint: str,
) -> FallbackRecoveryRecord:
    if not isinstance(value, dict) or set(value) != set(FallbackRecoveryRecord.__annotations__):
        raise ValueError("The recovery fallback schema is invalid.")
    record = FallbackRecoveryRecord(**value)
    if record.schemaVersion != FALLBACK_SCHEMA_VERSION:
        raise ValueError("The recovery fallback version is invalid.")
    if (
        not isinstance(record.scopeFingerprint, str)
        or not _HASH_PATTERN.fullmatch(record.scopeFingerprint)
        or record.scopeFingerprint != expected_fingerprint
    ):
        raise ValueError("The recovery fallback scope is invalid.")
    _bounded_integer(record.generation, maximum=MAX_GENERATION)
    _bounded_integer(record.pauseGeneration, maximum=MAX_GENERATION)
    _bounded_integer(record.consecutiveFailedAttempts, maximum=MAX_GENERATION)
    _bounded_integer(record.lifetimeAttempts, maximum=MAX_GENERATION)
    _bounded_integer(
        record.pauseAfterAttempts,
        maximum=MAX_PAUSE_AFTER_ATTEMPTS,
    )
    _bounded_integer(record.currentBackoffSeconds, maximum=MAX_BACKOFF_SECONDS)
    _bounded_integer(record.popupAcknowledgedGeneration, maximum=MAX_GENERATION)
    _bounded_integer(record.popupClaimedGeneration, maximum=MAX_GENERATION)
    if record.resumePredecessorGeneration is not None:
        _bounded_integer(record.resumePredecessorGeneration, maximum=MAX_GENERATION)
    if not isinstance(record.generationOrdered, bool) or not isinstance(
        record.pendingReconciliation, bool
    ):
        raise ValueError("The recovery fallback flags are invalid.")
    if record.state not in _ALLOWED_STATES or record.reasonCode not in _ALLOWED_REASON_CODES:
        raise ValueError("The recovery fallback state is invalid.")
    _canonical_uuid(record.episodeId)
    if record.activeAttemptId is not None:
        _canonical_uuid(record.activeAttemptId)
    if (
        not isinstance(record.resumeIdempotencyKeyHash, str)
        or not _IDEMPOTENCY_HASH_PATTERN.fullmatch(record.resumeIdempotencyKeyHash)
    ):
        raise ValueError("The recovery fallback resume key is invalid.")
    for timestamp in (
        record.firstFailureAt,
        record.lastFailureAt,
        record.lastAttemptAt,
        record.nextRetryAt,
        record.pausedAt,
        record.resumeRequestedAt,
        record.recoveredAt,
    ):
        _validated_timestamp(timestamp)
    if record.popupAcknowledgedGeneration > record.pauseGeneration:
        raise ValueError("The recovery fallback acknowledgement is invalid.")
    if record.popupClaimedGeneration > record.pauseGeneration:
        raise ValueError("The recovery fallback popup claim is invalid.")
    if record.state == "paused" and (
        record.pausedAt is None
        or record.pauseGeneration == 0
        or record.activeAttemptId is not None
        or not record.reasonCode
    ):
        raise ValueError("The paused recovery fallback is incomplete.")
    if record.state == "retry_wait" and record.nextRetryAt is None:
        raise ValueError("The retry recovery fallback is incomplete.")
    if record.state in {"recovering", "recovering_half_open"} and (
        record.activeAttemptId is None
    ):
        raise ValueError("The active recovery fallback is incomplete.")
    if record.state == "resume_requested" and record.resumeRequestedAt is None:
        raise ValueError("The resumed recovery fallback is incomplete.")
    if record.state == "healthy" and (
        record.activeAttemptId is not None or record.nextRetryAt is not None
    ):
        raise ValueError("The healthy recovery fallback is inconsistent.")
    return record


def _bounded_integer(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError("The recovery fallback number is invalid.")
    return value


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("The recovery fallback identifier is invalid.")
    canonical = str(uuid.UUID(value))
    if canonical != value:
        raise ValueError("The recovery fallback identifier is invalid.")
    return canonical


def _validated_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= _TIMESTAMP_MAX_LENGTH:
        raise ValueError("The recovery fallback timestamp is invalid.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("The recovery fallback timestamp is invalid.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("The recovery fallback timestamp must include a timezone.")
    return timestamp
