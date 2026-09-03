"""Deterministic, benchmark-gated admission control for the PDF parser pool.

The controller has one intended caller: the process holding OWL's resident
Bitbucket supervisor lock.  Web requests may render :func:`controller_snapshot`
but must never call :class:`PDFPipelineController.evaluate`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from django.conf import settings
from django.utils import timezone

from bitbucket_search.models import PDFPipelineRecoveryState, PDFPipelineTuningAction

GATE_SCHEMA_VERSION = 1
GATE_KIND = "owl.pdf-pipeline-adaptive-enablement"
METRICS_SCHEMA_VERSION = 1
METRICS_SNAPSHOT_FILENAME = "metrics-v1.json"
REQUIRED_GATE_CHECKS = (
    "metricDefinitionsVerified",
    "fixedTargetVarianceUnderstood",
    "shadowDecisionsStable",
    "resourceGuardrailsPassed",
    "recoveryAndIntegrityPassed",
    "adaptivePerformancePassed",
    "dashboardStatesVerified",
    "cpuBudgetReasonVisible",
)
SAFE_RECOVERY_STATE = PDFPipelineRecoveryState.HEALTHY


class ControllerEvaluationError(RuntimeError):
    """The owner could not evaluate a sample and must open its recovery circuit."""


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    mode: Literal["fixed", "observe", "shadow", "adaptive"]
    adaptive_enabled: bool
    kill_switch: bool
    manual_fixed_target: int | None
    configured_min: int
    configured_hard_max: int
    tested_hard_max: int
    initial_target: int
    cpu_budget_fraction: float
    observation_seconds: int
    cooldown_seconds: int
    hysteresis_samples: int
    minimum_documents: int
    minimum_pages: int
    minimum_bytes: int
    max_ordinary_decrease: int
    minimum_throughput_improvement: float
    max_host_cpu_pct: float
    min_available_memory_bytes: int
    min_available_disk_bytes: int
    max_foreground_p95_ms: int
    benchmark_gate_path: Path


@dataclass(frozen=True, slots=True)
class ResourceSignals:
    schedulable_cpu_count: int | None
    host_cpu_pct: float | None
    available_memory_bytes: int | None
    available_disk_bytes: int | None
    semantic_workers_active: int | None
    git_workers_active: int | None
    publisher_cpu_slots: float | None
    other_cpu_heavy_slots: float | None = 0.0

    @property
    def complete(self) -> bool:
        numeric = (
            self.schedulable_cpu_count,
            self.host_cpu_pct,
            self.available_memory_bytes,
            self.available_disk_bytes,
            self.semantic_workers_active,
            self.git_workers_active,
            self.publisher_cpu_slots,
            self.other_cpu_heavy_slots,
        )
        try:
            valid = all(
                value is not None
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and float(value) >= 0
                for value in numeric
            )
            cpu_valid = (
                self.schedulable_cpu_count is not None and int(self.schedulable_cpu_count) >= 1
            )
            usage_valid = self.host_cpu_pct is not None and 0 <= float(self.host_cpu_pct) <= 100
        except (TypeError, ValueError, OverflowError):
            return False
        return valid and cpu_valid and usage_valid


@dataclass(frozen=True, slots=True)
class ControllerObservation:
    at: datetime
    fresh: bool
    confidence: Literal["low", "medium", "high"]
    observation_seconds: float
    completed_documents: int
    completed_pages: int
    completed_bytes: int
    eligible_jobs: int
    active_extractors: int
    occupancy_pct: float | None
    backpressure_depth: int
    backpressure_threshold: int
    backpressure_growth_per_second: float | None
    publisher_starved_pct: float | None
    extractor_outputs_per_second: float | None
    writer_publications_per_second: float | None
    sqlite_busy_errors: int | None
    foreground_p95_ms: float | None
    failure_rate: float | None
    source_blocked: bool
    recovery_state: str
    recovery_scope: str | None
    resources: ResourceSignals

    @property
    def valid(self) -> bool:
        numeric_nonnegative = (
            self.observation_seconds,
            self.completed_documents,
            self.completed_pages,
            self.completed_bytes,
            self.eligible_jobs,
            self.active_extractors,
            self.backpressure_depth,
        )
        optional_nonnegative = (
            self.extractor_outputs_per_second,
            self.writer_publications_per_second,
            self.sqlite_busy_errors,
            self.foreground_p95_ms,
            self.failure_rate,
        )
        try:
            if timezone.is_naive(self.at) or any(
                isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0
                for value in numeric_nonnegative
            ):
                return False
            if any(
                value is not None
                and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0)
                for value in optional_nonnegative
            ):
                return False
            if self.backpressure_threshold < 1:
                return False
            if self.backpressure_growth_per_second is not None and not math.isfinite(
                float(self.backpressure_growth_per_second)
            ):
                return False
            if self.occupancy_pct is not None and not 0 <= float(self.occupancy_pct) <= 100:
                return False
            if self.failure_rate is not None and float(self.failure_rate) > 1:
                return False
            return (
                self.publisher_starved_pct is None or 0 <= float(self.publisher_starved_pct) <= 100
            )
        except (TypeError, ValueError, OverflowError):
            return False


@dataclass(frozen=True, slots=True)
class BenchmarkGate:
    passed: bool
    reason_code: str
    tested_hard_max: int | None = None


@dataclass(frozen=True, slots=True)
class ControllerDecision:
    action: Literal["hold", "increase", "decrease", "pause", "rollback"]
    proposed_target: int
    reason_code: str
    confidence: str
    evidence: dict[str, int | float | bool | None]
    safety: bool = False


@dataclass(frozen=True, slots=True)
class _PendingEvaluation:
    event_id: int | None
    previous_target: int
    proposed_target: int
    baseline_writer_rate: float | None
    evaluate_after: datetime
    shadow: bool


def config_from_settings() -> ControllerConfig:
    """Map validated Django settings without changing legacy worker defaults."""

    return ControllerConfig(
        mode=str(getattr(settings, "PDF_PIPELINE_CONTROLLER_MODE", "observe")),
        adaptive_enabled=bool(getattr(settings, "PDF_PIPELINE_ADAPTIVE_ENABLED", False)),
        kill_switch=bool(getattr(settings, "PDF_PIPELINE_CONTROLLER_KILL_SWITCH", False)),
        manual_fixed_target=getattr(settings, "PDF_PIPELINE_MANUAL_FIXED_TARGET", None),
        configured_min=int(getattr(settings, "PDF_PIPELINE_CONFIGURED_MIN_TARGET", 1)),
        configured_hard_max=int(settings.PDF_MAX_EXTRACTION_WORKERS),
        tested_hard_max=int(getattr(settings, "PDF_PIPELINE_TESTED_HARD_MAX", 8)),
        initial_target=int(
            getattr(settings, "PDF_PIPELINE_INITIAL_TARGET", settings.PDF_MAX_EXTRACTION_WORKERS)
        ),
        cpu_budget_fraction=float(
            getattr(settings, "PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION", 0.8)
        ),
        observation_seconds=int(
            getattr(settings, "PDF_PIPELINE_CONTROLLER_OBSERVATION_SECONDS", 60)
        ),
        cooldown_seconds=int(getattr(settings, "PDF_PIPELINE_CONTROLLER_COOLDOWN_SECONDS", 120)),
        hysteresis_samples=int(getattr(settings, "PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES", 3)),
        minimum_documents=int(getattr(settings, "PDF_PIPELINE_CONTROLLER_MIN_DOCUMENTS", 3)),
        minimum_pages=int(getattr(settings, "PDF_PIPELINE_CONTROLLER_MIN_PAGES", 10)),
        minimum_bytes=int(getattr(settings, "PDF_PIPELINE_CONTROLLER_MIN_BYTES", 1_048_576)),
        max_ordinary_decrease=int(
            getattr(settings, "PDF_PIPELINE_CONTROLLER_MAX_ORDINARY_DECREASE", 2)
        ),
        minimum_throughput_improvement=float(
            getattr(settings, "PDF_PIPELINE_CONTROLLER_MIN_THROUGHPUT_IMPROVEMENT", 0.05)
        ),
        max_host_cpu_pct=float(getattr(settings, "PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT", 85.0)),
        min_available_memory_bytes=int(
            getattr(
                settings,
                "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES",
                8 * 1_024**3,
            )
        ),
        min_available_disk_bytes=int(
            getattr(
                settings,
                "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES",
                10 * 1_024**3,
            )
        ),
        max_foreground_p95_ms=int(
            getattr(settings, "PDF_PIPELINE_CONTROLLER_MAX_FOREGROUND_P95_MS", 500)
        ),
        benchmark_gate_path=Path(settings.PDF_PIPELINE_ADAPTIVE_BENCHMARK_GATE_PATH),
    )


def resources_from_metrics(resources: Mapping[str, Any]) -> ResourceSignals:
    """Convert the redacted telemetry contract to strict controller inputs."""

    return ResourceSignals(
        schedulable_cpu_count=resources.get("schedulableCpuCount"),
        host_cpu_pct=resources.get("hostCpuPct"),
        available_memory_bytes=resources.get("hostMemoryAvailableBytes"),
        available_disk_bytes=resources.get("diskAvailableBytes"),
        semantic_workers_active=resources.get("semanticWorkersActive"),
        git_workers_active=resources.get("gitWorkersActive"),
        publisher_cpu_slots=resources.get("publisherCpuSlots"),
        other_cpu_heavy_slots=resources.get("otherCpuHeavySlots", 0.0),
    )


def validate_benchmark_gate(config: ControllerConfig) -> BenchmarkGate:
    """Accept only explicit, representative, complete benchmark evidence."""

    try:
        if config.benchmark_gate_path.stat().st_size > 1_048_576:
            return BenchmarkGate(False, "benchmark_gate_too_large")
        payload = json.loads(config.benchmark_gate_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BenchmarkGate(False, "benchmark_gate_missing")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return BenchmarkGate(False, "benchmark_gate_unreadable")
    if not isinstance(payload, dict):
        return BenchmarkGate(False, "benchmark_gate_invalid")
    if payload.get("schemaVersion") != GATE_SCHEMA_VERSION or payload.get("kind") != GATE_KIND:
        return BenchmarkGate(False, "benchmark_gate_incompatible")
    if payload.get("status") != "passed":
        return BenchmarkGate(False, "benchmark_gate_not_passed")
    workload = payload.get("representativeWorkload")
    checks = payload.get("checks")
    fixed_targets = payload.get("fixedWorkerTargets")
    repetitions = payload.get("minimumRepetitions")
    tested_hard_max = payload.get("testedPdfHardMax")
    if not isinstance(workload, dict) or not isinstance(checks, dict):
        return BenchmarkGate(False, "benchmark_gate_incomplete")
    if (
        type(workload.get("documentCount")) is not int
        or not 20_000 <= workload["documentCount"] <= 25_000
        or type(workload.get("sourceBytes")) is not int
        or workload["sourceBytes"] < 50_000_000_000
    ):
        return BenchmarkGate(False, "benchmark_workload_not_representative")
    if (
        not isinstance(fixed_targets, list)
        or not {1, 2, 4, 6, 8}.issubset({value for value in fixed_targets if type(value) is int})
        or type(repetitions) is not int
        or repetitions < 3
    ):
        return BenchmarkGate(False, "benchmark_matrix_incomplete")
    if any(checks.get(name) is not True for name in REQUIRED_GATE_CHECKS):
        return BenchmarkGate(False, "benchmark_checks_incomplete")
    if (
        type(tested_hard_max) is not int
        or tested_hard_max < config.tested_hard_max
        or tested_hard_max > config.configured_hard_max
    ):
        return BenchmarkGate(False, "benchmark_tested_max_mismatch")
    return BenchmarkGate(True, "benchmark_gate_passed", tested_hard_max)


def adaptive_enablement(config: ControllerConfig) -> BenchmarkGate:
    if config.mode != "adaptive":
        return BenchmarkGate(False, "adaptive_mode_not_requested")
    if config.kill_switch:
        return BenchmarkGate(False, "controller_kill_switch")
    if config.manual_fixed_target is not None:
        return BenchmarkGate(False, "manual_fixed_override")
    if not config.adaptive_enabled:
        return BenchmarkGate(False, "adaptive_not_enabled")
    return validate_benchmark_gate(config)


def configured_fixed_target(config: ControllerConfig | None = None) -> int:
    """Return the conservative fixed target after tested/configured bounds."""

    config = config or config_from_settings()
    requested = (
        config.manual_fixed_target
        if config.manual_fixed_target is not None
        else config.initial_target
    )
    return max(0, min(requested, config.configured_hard_max, config.tested_hard_max))


def admission_target_from_owner_snapshot(*, at: datetime | None = None) -> int:
    """Read the supervisor's target, falling back to the configured fixed target.

    This read-only helper is used by resident extraction slots at job boundaries.
    A missing, malformed, future, or stale owner snapshot can never expand the
    pool or preserve a previously raised adaptive target.
    """

    config = config_from_settings()
    fallback = configured_fixed_target(config)
    target = Path(settings.PDF_PIPELINE_STATE_ROOT) / METRICS_SNAPSHOT_FILENAME
    try:
        if target.stat().st_size > 4 * 1_024 * 1_024:
            return fallback
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict) or payload.get("schemaVersion") != METRICS_SCHEMA_VERSION:
        return fallback
    try:
        generated_at = datetime.fromisoformat(str(payload["generatedAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return fallback
    now = at or timezone.now()
    if (
        timezone.is_naive(generated_at)
        or generated_at > now + timedelta(seconds=5)
        or now - generated_at
        > timedelta(seconds=int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15)))
    ):
        return fallback
    controller = payload.get("controller")
    if not isinstance(controller, Mapping):
        return fallback
    effective = controller.get("effectiveAdmissionTarget")
    if type(effective) is not int:
        return fallback
    return max(0, min(effective, config.configured_hard_max, config.tested_hard_max))


def worker_slot_admitted(slot_number: int, *, at: datetime | None = None) -> bool:
    """Return whether a one-based resident slot may claim its next job."""

    if isinstance(slot_number, bool) or slot_number < 1:
        return False
    return slot_number <= admission_target_from_owner_snapshot(at=at)


def resource_ceiling(
    config: ControllerConfig,
    resources: ResourceSignals,
) -> tuple[int | None, int | None, float | None]:
    """Return resource ceiling, raw 80%-budget slots, and competing slots."""

    if not resources.complete:
        return None, None, None
    assert resources.schedulable_cpu_count is not None
    raw_slots = math.floor(resources.schedulable_cpu_count * config.cpu_budget_fraction)
    # A one-CPU host may keep one bounded slot for minimum progress; this does
    # not reinterpret the budget as permission to exceed the tested hard max.
    if resources.schedulable_cpu_count == 1:
        raw_slots = max(1, raw_slots)
    competing = math.ceil(
        float(resources.semantic_workers_active or 0)
        + float(resources.git_workers_active or 0)
        + float(resources.publisher_cpu_slots or 0)
        + float(resources.other_cpu_heavy_slots or 0)
    )
    return max(0, min(config.configured_hard_max, raw_slots - competing)), raw_slots, competing


def _safety_ceiling(
    config: ControllerConfig,
    resources: ResourceSignals,
    *,
    recovery_state: str,
    recovery_scope: str | None = None,
    foreground_p95_ms: float | None = None,
) -> tuple[int, str | None]:
    effective_hard_max = min(config.configured_hard_max, config.tested_hard_max)
    if recovery_state != SAFE_RECOVERY_STATE and not str(recovery_scope or "").startswith(
        "extraction_slot:"
    ):
        return 0, "recovery_freeze"
    if resources.available_memory_bytes is not None and (
        resources.available_memory_bytes < config.min_available_memory_bytes
    ):
        return 0, "critical_memory_headroom"
    if resources.available_disk_bytes is not None and (
        resources.available_disk_bytes < config.min_available_disk_bytes
    ):
        return 0, "critical_disk_headroom"
    if foreground_p95_ms is not None and (foreground_p95_ms > config.max_foreground_p95_ms * 4):
        return 0, "critical_foreground_latency"
    return effective_hard_max, None


def controller_snapshot(
    resources: Mapping[str, Any],
    *,
    config: ControllerConfig | None = None,
    requested_target: int | None = None,
    recovery_state: str = SAFE_RECOVERY_STATE,
    recovery_scope: str | None = None,
    foreground_p95_ms: float | None = None,
    cooldown_until: datetime | None = None,
    proposed_target: int | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Build the explicit ceiling/precedence contract used by metrics and UI."""

    config = config or config_from_settings()
    signals = resources_from_metrics(resources)
    gate = adaptive_enablement(config)
    adaptive_active = gate.passed
    fixed_target = (
        config.manual_fixed_target
        if config.manual_fixed_target is not None
        else config.initial_target
    )
    requested = fixed_target if requested_target is None else requested_target
    if not adaptive_active:
        requested = fixed_target
    effective_hard_max = min(config.configured_hard_max, config.tested_hard_max)
    resource_aware, raw_slots, competing_slots = resource_ceiling(config, signals)
    safety_ceiling, safety_reason = _safety_ceiling(
        config,
        signals,
        recovery_state=recovery_state,
        recovery_scope=recovery_scope,
        foreground_p95_ms=foreground_p95_ms,
    )
    ordinary_ceiling = (
        resource_aware if adaptive_active and resource_aware is not None else effective_hard_max
    )
    effective = max(
        0,
        min(requested, effective_hard_max, ordinary_ceiling, safety_ceiling),
    )
    if safety_reason is not None:
        limiting_reason = safety_reason
    elif requested > effective_hard_max:
        limiting_reason = "tested_hard_max"
    elif adaptive_active and resource_aware is None:
        limiting_reason = "resource_metrics_unavailable"
    elif adaptive_active and resource_aware < min(requested, effective_hard_max):
        limiting_reason = "shared_cpu_budget"
    elif config.manual_fixed_target is not None:
        limiting_reason = "manual_fixed_override"
    elif config.kill_switch:
        limiting_reason = "controller_kill_switch"
    elif config.mode == "adaptive" and not adaptive_active:
        limiting_reason = gate.reason_code
    else:
        limiting_reason = "requested_target"
    return {
        "mode": config.mode,
        "operatingMode": "adaptive"
        if adaptive_active
        else config.mode
        if config.mode != "adaptive"
        else "fixed",
        "adaptiveEnabled": adaptive_active,
        "adaptiveEnablementReason": gate.reason_code,
        "killSwitch": config.kill_switch,
        "manualFixedOverride": config.manual_fixed_target,
        "configuredMin": config.configured_min,
        "configuredPdfHardMax": config.configured_hard_max,
        "testedPdfHardMax": config.tested_hard_max,
        "effectivePdfHardMax": effective_hard_max,
        "fixedTarget": fixed_target,
        "requestedTarget": requested,
        "proposedTarget": proposed_target,
        "backgroundCpuBudgetFraction": config.cpu_budget_fraction,
        "backgroundCpuSlotBudget": raw_slots,
        "otherActiveOrReservedCpuHeavyOwlSlots": competing_slots,
        "resourceReservationFreshAt": (at or timezone.now()).isoformat()
        if signals.complete
        else None,
        "resourceAwarePdfCeiling": resource_aware,
        "safetyCeiling": safety_ceiling,
        "effectiveAdmissionTarget": effective,
        "limitingReason": limiting_reason,
        "tuningFrozen": recovery_state != SAFE_RECOVERY_STATE,
        "cooldownUntil": cooldown_until.isoformat() if cooldown_until else None,
    }


def _evidence(observation: ControllerObservation) -> dict[str, int | float | bool | None]:
    return {
        "observation_seconds": observation.observation_seconds,
        "completed_documents": observation.completed_documents,
        "completed_pages": observation.completed_pages,
        "completed_bytes": observation.completed_bytes,
        "eligible_jobs": observation.eligible_jobs,
        "active_extractors": observation.active_extractors,
        "occupancy_pct": observation.occupancy_pct,
        "backpressure_depth": observation.backpressure_depth,
        "backpressure_growth_per_second": observation.backpressure_growth_per_second,
        "publisher_starved_pct": observation.publisher_starved_pct,
        "extractor_outputs_per_second": observation.extractor_outputs_per_second,
        "writer_publications_per_second": observation.writer_publications_per_second,
        "sqlite_busy_errors": observation.sqlite_busy_errors,
        "foreground_p95_ms": observation.foreground_p95_ms,
        "failure_rate": observation.failure_rate,
        "host_cpu_pct": observation.resources.host_cpu_pct,
        "available_memory_bytes": observation.resources.available_memory_bytes,
        "available_disk_bytes": observation.resources.available_disk_bytes,
        "semantic_workers_active": observation.resources.semantic_workers_active,
        "git_workers_active": observation.resources.git_workers_active,
    }


def _expected_effect_code(decision: ControllerDecision) -> str:
    return {
        "increase": "improve_durable_throughput",
        "decrease": "relieve_measured_pressure",
        "pause": "protect_integrity_and_foreground_work",
        "rollback": "restore_last_effective_target",
        "hold": "preserve_target_pending_evidence",
    }[decision.action]


def observation_from_metrics(
    payload: Mapping[str, Any],
    *,
    at: datetime | None = None,
) -> ControllerObservation:
    """Strictly derive controller evidence from one supervisor-owned payload.

    Missing fields stay missing and therefore force a hold/fixed fallback; this
    """

    observed_at = at or timezone.now()
    throughput = payload.get("throughput")
    queues = payload.get("queues")
    workers = payload.get("workers")
    publisher = payload.get("publisher")
    foreground = payload.get("foreground")
    recovery = payload.get("recovery")
    resources = payload.get("resources")
    if not all(
        isinstance(value, Mapping)
        for value in (throughput, queues, workers, publisher, foreground, recovery, resources)
    ):
        raise ValueError("The controller metrics payload is incomplete.")
    assert isinstance(throughput, Mapping)
    assert isinstance(queues, Mapping)
    assert isinstance(workers, Mapping)
    assert isinstance(publisher, Mapping)
    assert isinstance(foreground, Mapping)
    assert isinstance(recovery, Mapping)
    assert isinstance(resources, Mapping)
    rate_window = float(throughput.get("rateWindowSeconds") or 0)
    written_rate = throughput.get("writtenRate")
    written_rate = written_rate if isinstance(written_rate, Mapping) else {}
    written_events = int(written_rate.get("eventCount") or 0)
    cache_rate = float(throughput.get("cacheReuseCompletionsPerSecond") or 0)
    completed_documents = written_events + max(0, round(cache_rate * rate_window))
    pages_rate = throughput.get("pagesPersistedPerSecond")
    bytes_rate = throughput.get("sourceBytesProcessedPerSecond")
    generated_at = payload.get("generatedAt")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        generated = None
    stale_seconds = int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15))
    fresh = bool(
        generated is not None
        and not timezone.is_naive(generated)
        and timedelta(0) <= observed_at - generated <= timedelta(seconds=stale_seconds)
        and not payload.get("snapshotStale", False)
    )
    confidence = written_rate.get("confidence")
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    extracted_rate = throughput.get("extractedRate")
    extracted_rate = extracted_rate if isinstance(extracted_rate, Mapping) else {}
    failure_rate = throughput.get("failedPerSecond")
    document_rate = throughput.get("documentsCompletedPerSecond")
    denominator = float(document_rate or 0) + float(failure_rate or 0)
    normalized_failure_rate = float(failure_rate or 0) / denominator if denominator > 0 else None
    activity = payload.get("activity")
    activity = activity if isinstance(activity, Mapping) else {}
    return ControllerObservation(
        at=observed_at,
        fresh=fresh,
        confidence=confidence,
        observation_seconds=rate_window,
        completed_documents=completed_documents,
        completed_pages=(
            max(0, round(float(pages_rate) * rate_window)) if pages_rate is not None else 0
        ),
        completed_bytes=(
            max(0, round(float(bytes_rate) * rate_window)) if bytes_rate is not None else 0
        ),
        eligible_jobs=int(queues.get("eligibleInputJobs") or 0),
        active_extractors=int(workers.get("active") or 0),
        occupancy_pct=(
            float(workers["occupancyPct"]) if workers.get("occupancyPct") is not None else None
        ),
        backpressure_depth=int(queues.get("backpressureDepthJobs") or 0),
        backpressure_threshold=max(
            1,
            int(queues.get("backpressureThresholdJobs") or 1),
        ),
        backpressure_growth_per_second=(
            float(queues["backpressureDepthGrowthPerSecond"])
            if queues.get("backpressureDepthGrowthPerSecond") is not None
            else None
        ),
        publisher_starved_pct=(
            float(publisher["starvedPct"]) if publisher.get("starvedPct") is not None else None
        ),
        extractor_outputs_per_second=(
            float(extracted_rate["perSecond"])
            if extracted_rate.get("perSecond") is not None
            else None
        ),
        writer_publications_per_second=(
            float(written_rate["perSecond"]) if written_rate.get("perSecond") is not None else None
        ),
        sqlite_busy_errors=(
            int(publisher["sqliteBusyErrors"])
            if publisher.get("sqliteBusyErrors") is not None
            else None
        ),
        foreground_p95_ms=(
            max(
                float(foreground.get("exactSearchP95Ms") or 0),
                float(foreground.get("dashboardP95Ms") or 0),
            )
            if foreground.get("exactSearchP95Ms") is not None
            and foreground.get("dashboardP95Ms") is not None
            else None
        ),
        failure_rate=normalized_failure_rate,
        source_blocked=activity.get("code") == "source_blocked",
        recovery_state=str(recovery.get("state") or "unknown"),
        recovery_scope=(str(recovery["scope"]) if recovery.get("scope") else None),
        resources=resources_from_metrics(resources),
    )


def recommend_target(
    config: ControllerConfig,
    observation: ControllerObservation,
    *,
    current_target: int,
    cooldown_until: datetime | None = None,
) -> ControllerDecision:
    """Return one deterministic job-boundary decision without side effects."""

    evidence = _evidence(observation)
    hard_max = min(config.configured_hard_max, config.tested_hard_max)
    safety_ceiling, safety_reason = _safety_ceiling(
        config,
        observation.resources,
        recovery_state=observation.recovery_state,
        recovery_scope=observation.recovery_scope,
        foreground_p95_ms=observation.foreground_p95_ms,
    )
    if safety_reason is not None:
        return ControllerDecision(
            "pause",
            min(current_target, safety_ceiling),
            safety_reason,
            "high",
            evidence,
            safety=True,
        )
    if observation.recovery_state != SAFE_RECOVERY_STATE:
        return ControllerDecision(
            "hold",
            current_target,
            "recovery_tuning_frozen",
            "low",
            evidence,
        )
    if not observation.fresh or not observation.valid or not observation.resources.complete:
        return ControllerDecision(
            "hold", current_target, "critical_metrics_unavailable", "low", evidence
        )
    if observation.confidence == "low":
        return ControllerDecision("hold", current_target, "low_confidence", "low", evidence)
    if observation.observation_seconds < config.observation_seconds:
        return ControllerDecision("hold", current_target, "warming_up", "low", evidence)
    if (
        observation.completed_documents < config.minimum_documents
        or observation.completed_pages < config.minimum_pages
        or observation.completed_bytes < config.minimum_bytes
    ):
        return ControllerDecision("hold", current_target, "insufficient_work", "low", evidence)
    if observation.source_blocked:
        return ControllerDecision(
            "hold", current_target, "source_blocked", observation.confidence, evidence
        )
    if observation.eligible_jobs <= 0:
        return ControllerDecision("hold", current_target, "no_eligible_demand", "high", evidence)
    if cooldown_until is not None and observation.at < cooldown_until:
        return ControllerDecision(
            "hold", current_target, "cooldown_active", observation.confidence, evidence
        )

    resource_aware, _raw, _competing = resource_ceiling(config, observation.resources)
    if resource_aware is None:
        return ControllerDecision(
            "hold", current_target, "critical_metrics_unavailable", "low", evidence
        )
    pressure_reasons: list[str] = []
    if observation.resources.host_cpu_pct is not None and (
        observation.resources.host_cpu_pct > config.max_host_cpu_pct
    ):
        pressure_reasons.append("host_cpu_pressure")
    if observation.foreground_p95_ms is not None and (
        observation.foreground_p95_ms > config.max_foreground_p95_ms
    ):
        pressure_reasons.append("foreground_latency_pressure")
    if observation.sqlite_busy_errors is not None and observation.sqlite_busy_errors > 0:
        pressure_reasons.append("sqlite_contention")
    if observation.failure_rate is not None and observation.failure_rate > 0.05:
        pressure_reasons.append("failure_rate_pressure")
    backlog_rising = (
        observation.backpressure_growth_per_second is not None
        and observation.backpressure_growth_per_second > 0
        and observation.backpressure_depth >= observation.backpressure_threshold
    )
    if backlog_rising:
        pressure_reasons.append("publication_backlog_rising")
    if pressure_reasons:
        proposed = min(
            current_target,
            max(
                config.configured_min,
                current_target - min(config.max_ordinary_decrease, max(1, current_target)),
            ),
        )
        return ControllerDecision(
            "decrease",
            proposed,
            pressure_reasons[0],
            observation.confidence,
            evidence,
        )

    backlog_low = observation.backpressure_depth <= max(
        1,
        observation.backpressure_threshold // 4,
    )
    publisher_awaiting_input = (
        observation.publisher_starved_pct is not None and observation.publisher_starved_pct >= 25.0
    )
    occupied = observation.occupancy_pct is not None and observation.occupancy_pct >= 80.0
    continuing_demand = observation.eligible_jobs >= max(2, current_target)
    writer_not_falling_behind = (
        observation.backpressure_growth_per_second is not None
        and observation.backpressure_growth_per_second <= 0
        and (
            observation.extractor_outputs_per_second is None
            or observation.writer_publications_per_second is None
            or observation.writer_publications_per_second
            >= observation.extractor_outputs_per_second * 0.95
        )
    )
    maximum = min(hard_max, resource_aware)
    if (
        current_target < maximum
        and continuing_demand
        and occupied
        and backlog_low
        and publisher_awaiting_input
        and writer_not_falling_behind
    ):
        return ControllerDecision(
            "increase",
            current_target + 1,
            "measured_extraction_headroom",
            observation.confidence,
            evidence,
        )
    return ControllerDecision(
        "hold", current_target, "no_measured_benefit", observation.confidence, evidence
    )


class PDFPipelineController:
    """One supervisor-owned controller with bounded hysteresis and cooldown."""

    def __init__(self, config: ControllerConfig | None = None):
        self.config = config or config_from_settings()
        self.current_target = (
            self.config.manual_fixed_target
            if self.config.manual_fixed_target is not None
            else self.config.initial_target
        )
        self.cooldown_until: datetime | None = None
        self._pending_key: tuple[str, int] | None = None
        self._pending_count = 0
        self._last_recorded_key: tuple[str, int] | None = None
        self._pending_evaluation: _PendingEvaluation | None = None
        self._recovery_frozen = False

    def _reset_hysteresis(self) -> None:
        self._pending_key = None
        self._pending_count = 0

    def _record(
        self,
        decision: ControllerDecision,
        *,
        previous_target: int,
        action: str,
        now: datetime,
    ) -> int:
        from bitbucket_search.models import PDFPipelineTuningEvent

        event = PDFPipelineTuningEvent.objects.create(
            occurred_at=now,
            mode=self.config.mode,
            action=action,
            previous_target=previous_target,
            proposed_target=decision.proposed_target,
            reason_code=decision.reason_code,
            reason="Measured PDF pipeline guardrails produced this bounded decision.",
            expected_effect_code=_expected_effect_code(decision),
            confidence=decision.confidence,
            observation_window_seconds=self.config.observation_seconds,
            evidence=decision.evidence,
            cooldown_until=self.cooldown_until,
        )
        return event.pk

    def _evaluate_previous_change(
        self,
        observation: ControllerObservation,
        *,
        persist: bool,
    ) -> ControllerDecision | None:
        pending = self._pending_evaluation
        if pending is None or observation.at < pending.evaluate_after:
            return None
        outcome = "shadow_not_applied" if pending.shadow else "inconclusive"
        rollback: ControllerDecision | None = None
        current_rate = observation.writer_publications_per_second
        if not pending.shadow and pending.baseline_writer_rate and current_rate is not None:
            improvement = (
                current_rate - pending.baseline_writer_rate
            ) / pending.baseline_writer_rate
            if improvement >= self.config.minimum_throughput_improvement:
                outcome = "helped"
            else:
                outcome = "hurt"
                rollback = ControllerDecision(
                    "rollback",
                    pending.previous_target,
                    "increase_no_measured_benefit",
                    observation.confidence,
                    {
                        **_evidence(observation),
                        "baseline_writer_rate": pending.baseline_writer_rate,
                        "observed_writer_rate": current_rate,
                        "minimum_improvement": self.config.minimum_throughput_improvement,
                    },
                )
        if persist and pending.event_id is not None:
            from bitbucket_search.models import PDFPipelineTuningEvent

            PDFPipelineTuningEvent.objects.filter(pk=pending.event_id).update(outcome=outcome)
        self._pending_evaluation = None
        if rollback is not None:
            previous_target = self.current_target
            self.current_target = rollback.proposed_target
            self.cooldown_until = observation.at + timedelta(seconds=self.config.cooldown_seconds)
            if persist:
                self._record(
                    rollback,
                    previous_target=previous_target,
                    action=PDFPipelineTuningAction.ROLLBACK,
                    now=observation.at,
                )
            self._last_recorded_key = (rollback.reason_code, rollback.proposed_target)
            return rollback
        return None

    def evaluate(
        self,
        observation: ControllerObservation,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Evaluate one sample; only shadow/adaptive produce tuning events."""

        fixed_target = (
            self.config.manual_fixed_target
            if self.config.manual_fixed_target is not None
            else self.config.initial_target
        )
        gate = adaptive_enablement(self.config)
        if (
            self.config.mode in {"fixed", "observe"}
            or self.config.kill_switch
            or self.config.manual_fixed_target is not None
        ):
            self.current_target = fixed_target
            self._reset_hysteresis()
            return controller_snapshot(
                _resource_mapping(observation.resources),
                config=self.config,
                requested_target=fixed_target,
                recovery_state=observation.recovery_state,
                recovery_scope=observation.recovery_scope,
                foreground_p95_ms=observation.foreground_p95_ms,
                at=observation.at,
            )
        if self.config.mode == "adaptive" and not gate.passed:
            self.current_target = fixed_target
            self._reset_hysteresis()
            return controller_snapshot(
                _resource_mapping(observation.resources),
                config=self.config,
                requested_target=fixed_target,
                recovery_state=observation.recovery_state,
                recovery_scope=observation.recovery_scope,
                foreground_p95_ms=observation.foreground_p95_ms,
                at=observation.at,
            )

        if self.config.mode == "adaptive" and (
            not observation.fresh or not observation.valid or not observation.resources.complete
        ):
            # Never keep a prior adaptive increase when the owner loses the
            # critical evidence needed to justify it.
            self.current_target = fixed_target
            self._reset_hysteresis()
            return controller_snapshot(
                _resource_mapping(observation.resources),
                config=self.config,
                requested_target=fixed_target,
                recovery_state=observation.recovery_state,
                recovery_scope=observation.recovery_scope,
                foreground_p95_ms=observation.foreground_p95_ms,
                at=observation.at,
            )

        if observation.recovery_state != SAFE_RECOVERY_STATE:
            self._recovery_frozen = True
        elif self._recovery_frozen:
            self._recovery_frozen = False
            self.cooldown_until = observation.at + timedelta(
                seconds=self.config.observation_seconds
            )
            self._reset_hysteresis()

        rollback = self._evaluate_previous_change(observation, persist=persist)
        if rollback is not None:
            return controller_snapshot(
                _resource_mapping(observation.resources),
                config=self.config,
                requested_target=self.current_target,
                recovery_state=observation.recovery_state,
                recovery_scope=observation.recovery_scope,
                foreground_p95_ms=observation.foreground_p95_ms,
                cooldown_until=self.cooldown_until,
                at=observation.at,
            )

        decision = recommend_target(
            self.config,
            observation,
            current_target=self.current_target,
            cooldown_until=self.cooldown_until,
        )
        proposed = decision.proposed_target
        actionable = decision.action != "hold" and proposed != self.current_target
        if actionable and not decision.safety:
            key = (decision.reason_code, proposed)
            if key == self._pending_key:
                self._pending_count += 1
            else:
                self._pending_key = key
                self._pending_count = 1
            if self._pending_count < self.config.hysteresis_samples:
                actionable = False
        elif decision.safety:
            event_key = (decision.reason_code, proposed)
            if (
                event_key == self._last_recorded_key
                and self.cooldown_until is not None
                and observation.at < self.cooldown_until
            ):
                actionable = False
            self._reset_hysteresis()
        else:
            self._reset_hysteresis()

        previous_target = self.current_target
        if actionable:
            self.cooldown_until = observation.at + timedelta(seconds=self.config.cooldown_seconds)
            action = (
                PDFPipelineTuningAction.SAFETY_OVERRIDE
                if decision.safety
                else PDFPipelineTuningAction.RECOMMEND
                if self.config.mode == "shadow"
                else PDFPipelineTuningAction.APPLY
            )
            event_id = None
            if persist:
                event_id = self._record(
                    decision,
                    previous_target=previous_target,
                    action=action,
                    now=observation.at,
                )
            if self.config.mode == "adaptive":
                self.current_target = proposed
            if decision.action == "increase":
                self._pending_evaluation = _PendingEvaluation(
                    event_id=event_id,
                    previous_target=previous_target,
                    proposed_target=proposed,
                    baseline_writer_rate=observation.writer_publications_per_second,
                    evaluate_after=self.cooldown_until,
                    shadow=self.config.mode == "shadow",
                )
            self._last_recorded_key = (decision.reason_code, proposed)
            self._reset_hysteresis()
        return controller_snapshot(
            _resource_mapping(observation.resources),
            config=self.config,
            requested_target=self.current_target,
            recovery_state=observation.recovery_state,
            recovery_scope=observation.recovery_scope,
            foreground_p95_ms=observation.foreground_p95_ms,
            cooldown_until=self.cooldown_until,
            proposed_target=proposed if self.config.mode == "shadow" else None,
            at=observation.at,
        )


def replay_controller_observations(
    config: ControllerConfig,
    observations: Iterable[ControllerObservation],
) -> tuple[dict[str, Any], ...]:
    """Replay a bounded chronological trace without writes or admission changes."""

    controller = PDFPipelineController(config)
    rows: list[dict[str, Any]] = []
    previous_at: datetime | None = None
    for index, observation in enumerate(observations):
        if index >= 10_000:
            raise ValueError("Controller replay is limited to 10000 observations.")
        if not isinstance(observation, ControllerObservation):
            raise ValueError("Controller replay entries must be controller observations.")
        if timezone.is_naive(observation.at):
            raise ValueError("Controller replay timestamps must be timezone-aware.")
        if previous_at is not None and observation.at <= previous_at:
            raise ValueError("Controller replay timestamps must be strictly increasing.")
        snapshot = controller.evaluate(observation, persist=False)
        rows.append(
            {
                "index": index,
                "at": observation.at.isoformat(),
                "effectiveAdmissionTarget": snapshot["effectiveAdmissionTarget"],
                "proposedTarget": snapshot["proposedTarget"],
                "limitingReason": snapshot["limitingReason"],
                "tuningFrozen": snapshot["tuningFrozen"],
                "cooldownUntil": snapshot["cooldownUntil"],
                "resourceReservationFreshAt": snapshot["resourceReservationFreshAt"],
            }
        )
        previous_at = observation.at
    return tuple(rows)


def _resource_mapping(resources: ResourceSignals) -> dict[str, int | float | None]:
    return {
        "schedulableCpuCount": resources.schedulable_cpu_count,
        "hostCpuPct": resources.host_cpu_pct,
        "hostMemoryAvailableBytes": resources.available_memory_bytes,
        "diskAvailableBytes": resources.available_disk_bytes,
        "semanticWorkersActive": resources.semantic_workers_active,
        "gitWorkersActive": resources.git_workers_active,
        "publisherCpuSlots": resources.publisher_cpu_slots,
        "otherCpuHeavySlots": resources.other_cpu_heavy_slots,
    }
