from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from django.utils import timezone

from bitbucket_search.models import PDFPipelineTuningAction, PDFPipelineTuningEvent
from bitbucket_search.services import pdf_pipeline_metrics as metrics
from bitbucket_search.services.pdf_pipeline_controller import (
    GATE_KIND,
    ControllerObservation,
    PDFPipelineController,
    ResourceSignals,
    adaptive_enablement,
    admission_target_from_owner_snapshot,
    config_from_settings,
    controller_snapshot,
    recommend_target,
    replay_controller_observations,
    resource_ceiling,
    worker_slot_admitted,
)

pytestmark = pytest.mark.django_db


def _config(settings, tmp_path, **overrides):
    settings.PDF_MAX_EXTRACTION_WORKERS = 8
    settings.PDF_PIPELINE_TESTED_HARD_MAX = 8
    settings.PDF_PIPELINE_INITIAL_TARGET = 4
    settings.PDF_PIPELINE_CONFIGURED_MIN_TARGET = 1
    settings.PDF_PIPELINE_CONTROLLER_MODE = "observe"
    settings.PDF_PIPELINE_ADAPTIVE_ENABLED = False
    settings.PDF_PIPELINE_CONTROLLER_KILL_SWITCH = False
    settings.PDF_PIPELINE_MANUAL_FIXED_TARGET = None
    settings.PDF_PIPELINE_ADAPTIVE_BENCHMARK_GATE_PATH = tmp_path / "gate.json"
    settings.PDF_PIPELINE_CONTROLLER_OBSERVATION_SECONDS = 60
    settings.PDF_PIPELINE_CONTROLLER_COOLDOWN_SECONDS = 60
    settings.PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES = 3
    settings.PDF_PIPELINE_CONTROLLER_MIN_DOCUMENTS = 3
    settings.PDF_PIPELINE_CONTROLLER_MIN_PAGES = 10
    settings.PDF_PIPELINE_CONTROLLER_MIN_BYTES = 1_048_576
    settings.PDF_PIPELINE_CONTROLLER_MAX_ORDINARY_DECREASE = 2
    settings.PDF_PIPELINE_CONTROLLER_MIN_THROUGHPUT_IMPROVEMENT = 0.05
    settings.PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT = 85
    settings.PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES = 8 * 1_024**3
    settings.PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES = 10 * 1_024**3
    settings.PDF_PIPELINE_CONTROLLER_MAX_FOREGROUND_P95_MS = 500
    config = config_from_settings()
    return replace(config, **overrides)


def _resources(**overrides):
    values = {
        "schedulable_cpu_count": 18,
        "host_cpu_pct": 45.0,
        "available_memory_bytes": 32 * 1_024**3,
        "available_disk_bytes": 100 * 1_024**3,
        "semantic_workers_active": 0,
        "git_workers_active": 0,
        "publisher_cpu_slots": 0.0,
        "other_cpu_heavy_slots": 0.0,
    }
    values.update(overrides)
    return ResourceSignals(**values)


def _observation(**overrides):
    values = {
        "at": timezone.now(),
        "fresh": True,
        "confidence": "high",
        "observation_seconds": 90,
        "completed_documents": 20,
        "completed_pages": 100,
        "completed_bytes": 20 * 1_048_576,
        "eligible_jobs": 20,
        "active_extractors": 4,
        "occupancy_pct": 100.0,
        "backpressure_depth": 0,
        "backpressure_threshold": 8,
        "backpressure_growth_per_second": 0.0,
        "publisher_starved_pct": 50.0,
        "extractor_outputs_per_second": 1.0,
        "writer_publications_per_second": 1.0,
        "sqlite_busy_errors": 0,
        "foreground_p95_ms": 100.0,
        "failure_rate": 0.0,
        "source_blocked": False,
        "recovery_state": "healthy",
        "recovery_scope": None,
        "resources": _resources(),
    }
    values.update(overrides)
    return ControllerObservation(**values)


def _write_passing_gate(config):
    config.benchmark_gate_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": GATE_KIND,
                "status": "passed",
                "representativeWorkload": {
                    "documentCount": 20_000,
                    "sourceBytes": 50_000_000_000,
                },
                "fixedWorkerTargets": [1, 2, 4, 6, 8],
                "minimumRepetitions": 3,
                "testedPdfHardMax": 8,
                "checks": {
                    "metricDefinitionsVerified": True,
                    "fixedTargetVarianceUnderstood": True,
                    "shadowDecisionsStable": True,
                    "resourceGuardrailsPassed": True,
                    "recoveryAndIntegrityPassed": True,
                    "adaptivePerformancePassed": True,
                    "dashboardStatesVerified": True,
                    "cpuBudgetReasonVisible": True,
                },
            }
        ),
        encoding="utf-8",
    )


def test_fixed_observe_and_shadow_keep_resource_ceiling_advisory(settings, tmp_path):
    resources = {
        "schedulableCpuCount": 4,
        "hostCpuPct": 30,
        "hostMemoryAvailableBytes": 32 * 1_024**3,
        "diskAvailableBytes": 100 * 1_024**3,
        "semanticWorkersActive": 1,
        "gitWorkersActive": 1,
        "publisherCpuSlots": 1,
    }
    for mode in ("fixed", "observe", "shadow"):
        config = _config(settings, tmp_path, mode=mode, initial_target=4)
        snapshot = controller_snapshot(resources, config=config)

        assert snapshot["resourceAwarePdfCeiling"] == 0
        assert snapshot["effectiveAdmissionTarget"] == 4
        assert snapshot["limitingReason"] == "requested_target"


def test_resource_budget_math_accounts_for_competing_work_and_one_cpu(settings, tmp_path):
    config = replace(
        _config(settings, tmp_path),
        configured_hard_max=20,
        tested_hard_max=20,
    )

    ceiling_20, raw_20, competing = resource_ceiling(
        config,
        _resources(schedulable_cpu_count=20, semantic_workers_active=1, git_workers_active=1),
    )
    ceiling_18, raw_18, _ = resource_ceiling(
        config,
        _resources(schedulable_cpu_count=18),
    )
    ceiling_1, raw_1, _ = resource_ceiling(
        config,
        _resources(schedulable_cpu_count=1),
    )

    assert (raw_20, competing, ceiling_20) == (16, 2, 14)
    assert (raw_18, ceiling_18) == (14, 14)
    assert (raw_1, ceiling_1) == (1, 1)


def test_adaptive_requires_both_explicit_opt_in_and_representative_gate(settings, tmp_path):
    config = _config(settings, tmp_path, mode="adaptive", adaptive_enabled=False)
    assert adaptive_enablement(config).reason_code == "adaptive_not_enabled"

    enabled = replace(config, adaptive_enabled=True)
    assert adaptive_enablement(enabled).reason_code == "benchmark_gate_missing"

    _write_passing_gate(enabled)
    assert adaptive_enablement(enabled).passed is True


def test_synthetic_or_incomplete_gate_cannot_enable_adaptive(settings, tmp_path):
    config = _config(settings, tmp_path, mode="adaptive", adaptive_enabled=True)
    config.benchmark_gate_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": GATE_KIND,
                "status": "passed",
                "representativeWorkload": {"documentCount": 8, "sourceBytes": 1_000},
                "fixedWorkerTargets": [4, 6, 8],
                "minimumRepetitions": 1,
                "testedPdfHardMax": 8,
                "checks": {},
            }
        ),
        encoding="utf-8",
    )

    result = adaptive_enablement(config)

    assert result.passed is False
    assert result.reason_code == "benchmark_workload_not_representative"


def test_kill_switch_and_manual_override_win_over_passing_gate(settings, tmp_path):
    config = _config(settings, tmp_path, mode="adaptive", adaptive_enabled=True)
    _write_passing_gate(config)

    assert adaptive_enablement(replace(config, kill_switch=True)).reason_code == (
        "controller_kill_switch"
    )
    assert adaptive_enablement(replace(config, manual_fixed_target=2)).reason_code == (
        "manual_fixed_override"
    )
    snapshot = controller_snapshot(
        {
            "schedulableCpuCount": 18,
            "hostCpuPct": 30,
            "hostMemoryAvailableBytes": 32 * 1_024**3,
            "diskAvailableBytes": 100 * 1_024**3,
            "semanticWorkersActive": 0,
            "gitWorkersActive": 0,
            "publisherCpuSlots": 0,
        },
        config=replace(config, manual_fixed_target=2),
        requested_target=8,
    )
    assert snapshot["operatingMode"] == "fixed"
    assert snapshot["effectiveAdmissionTarget"] == 2


def test_missing_or_stale_metrics_hold_and_adaptive_falls_back_to_fixed(settings, tmp_path):
    config = _config(settings, tmp_path, mode="adaptive", adaptive_enabled=True)
    _write_passing_gate(config)
    missing = _observation(resources=_resources(host_cpu_pct=None))
    stale = _observation(fresh=False)

    assert recommend_target(config, missing, current_target=4).reason_code == (
        "critical_metrics_unavailable"
    )
    assert recommend_target(config, stale, current_target=4).reason_code == (
        "critical_metrics_unavailable"
    )


def test_increase_is_plus_one_and_requires_deterministic_hysteresis(settings, tmp_path):
    config = _config(
        settings,
        tmp_path,
        mode="shadow",
        hysteresis_samples=3,
    )
    controller = PDFPipelineController(config)
    observation = _observation()

    first = controller.evaluate(observation)
    second = controller.evaluate(replace(observation, at=observation.at + timedelta(seconds=1)))
    third = controller.evaluate(replace(observation, at=observation.at + timedelta(seconds=2)))

    assert first["effectiveAdmissionTarget"] == 4
    assert second["effectiveAdmissionTarget"] == 4
    assert third["effectiveAdmissionTarget"] == 4
    assert third["proposedTarget"] == 5
    event = PDFPipelineTuningEvent.objects.get()
    assert event.action == PDFPipelineTuningAction.RECOMMEND
    assert (event.previous_target, event.proposed_target) == (4, 5)
    assert event.expected_effect_code == "improve_durable_throughput"
    assert event.evidence["completed_documents"] == 20
    assert event.evidence["host_cpu_pct"] == 45.0


def test_adaptive_applies_only_after_gate_and_cooldown_prevents_oscillation(
    settings,
    tmp_path,
):
    config = _config(
        settings,
        tmp_path,
        mode="adaptive",
        adaptive_enabled=True,
        hysteresis_samples=2,
    )
    _write_passing_gate(config)
    controller = PDFPipelineController(config)
    observation = _observation()

    controller.evaluate(observation)
    applied = controller.evaluate(replace(observation, at=observation.at + timedelta(seconds=1)))
    cooling = controller.evaluate(replace(observation, at=observation.at + timedelta(seconds=2)))

    assert applied["adaptiveEnabled"] is True
    assert applied["effectiveAdmissionTarget"] == 5
    assert cooling["effectiveAdmissionTarget"] == 5
    assert PDFPipelineTuningEvent.objects.get().action == PDFPipelineTuningAction.APPLY


def test_sqlite_backlog_never_reduces_extractors_and_critical_pressure_is_immediate(
    settings,
    tmp_path,
):
    config = _config(settings, tmp_path, max_ordinary_decrease=2)
    ordinary = recommend_target(
        config,
        _observation(
            backpressure_depth=8,
            backpressure_growth_per_second=0.5,
        ),
        current_target=6,
    )
    critical = recommend_target(
        config,
        _observation(resources=_resources(available_disk_bytes=1)),
        current_target=6,
    )

    assert (ordinary.action, ordinary.proposed_target) == ("hold", 6)
    assert ordinary.reason_code == "sqlite_backlog_allowed"
    assert ordinary.safety is False
    assert (critical.action, critical.proposed_target) == ("pause", 0)
    assert critical.safety is True


def test_recovery_freezes_controller_and_observe_never_writes_tuning_event(settings, tmp_path):
    observe = PDFPipelineController(_config(settings, tmp_path, mode="observe"))
    result = observe.evaluate(
        _observation(recovery_state="retry_wait", recovery_scope="extraction_pool")
    )

    assert result["safetyCeiling"] == 0
    assert result["effectiveAdmissionTarget"] == 0
    assert result["limitingReason"] == "recovery_freeze"
    assert PDFPipelineTuningEvent.objects.count() == 0


def test_single_slot_recovery_freezes_tuning_without_pausing_healthy_slots(settings, tmp_path):
    controller = PDFPipelineController(_config(settings, tmp_path, mode="shadow"))

    snapshot = controller.evaluate(
        _observation(
            recovery_state="retry_wait",
            recovery_scope="extraction_slot:3",
        )
    )

    assert snapshot["tuningFrozen"] is True
    assert snapshot["safetyCeiling"] == 8
    assert snapshot["effectiveAdmissionTarget"] == 4
    assert PDFPipelineTuningEvent.objects.count() == 0


def test_worker_slots_use_fresh_owner_target_and_stale_snapshot_falls_back(
    settings,
    tmp_path,
):
    _config(settings, tmp_path)
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path
    now = timezone.now()
    snapshot_path = tmp_path / "metrics-v1.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAt": now.isoformat(),
                "controller": {"effectiveAdmissionTarget": 2},
            }
        ),
        encoding="utf-8",
    )

    assert admission_target_from_owner_snapshot(at=now) == 2
    assert worker_slot_admitted(2, at=now) is True
    assert worker_slot_admitted(3, at=now) is False

    snapshot_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAt": (now - timedelta(minutes=1)).isoformat(),
                "controller": {"effectiveAdmissionTarget": 8},
            }
        ),
        encoding="utf-8",
    )
    assert admission_target_from_owner_snapshot(at=now) == 4


def test_adaptive_drops_a_prior_increase_when_critical_metrics_disappear(settings, tmp_path):
    config = _config(
        settings,
        tmp_path,
        mode="adaptive",
        adaptive_enabled=True,
        hysteresis_samples=1,
    )
    _write_passing_gate(config)
    controller = PDFPipelineController(config)
    increased = controller.evaluate(_observation(), persist=False)
    fallback = controller.evaluate(
        _observation(
            at=timezone.now() + timedelta(seconds=1),
            resources=_resources(host_cpu_pct=None),
        ),
        persist=False,
    )

    assert increased["effectiveAdmissionTarget"] == 5
    assert fallback["effectiveAdmissionTarget"] == 4
    assert fallback["limitingReason"] == "resource_metrics_unavailable"


def test_supervisor_sampling_uses_the_single_controller_instance(settings, tmp_path):
    _config(settings, tmp_path)
    settings.PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED = False
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"

    class RecordingController:
        def __init__(self):
            self.observations = []

        def evaluate(self, observation):
            self.observations.append(observation)
            return {
                "requestedTarget": 2,
                "effectiveAdmissionTarget": 2,
            }

    controller = RecordingController()
    payload = metrics.sample_pipeline_metrics(
        at=timezone.now(),
        resident_roles={},
        controller=controller,
    )

    assert len(controller.observations) == 1
    assert payload["controller"]["effectiveAdmissionTarget"] == 2
    assert payload["samples"][-1]["workers"]["effectiveAdmissionTarget"] == 2


def test_adaptive_rolls_back_increase_without_measured_throughput_benefit(
    settings,
    tmp_path,
):
    config = _config(
        settings,
        tmp_path,
        mode="adaptive",
        adaptive_enabled=True,
        hysteresis_samples=1,
        cooldown_seconds=60,
    )
    _write_passing_gate(config)
    controller = PDFPipelineController(config)
    observation = _observation(writer_publications_per_second=1.0)

    controller.evaluate(observation)
    rolled_back = controller.evaluate(
        _observation(
            at=observation.at + timedelta(seconds=61),
            writer_publications_per_second=1.02,
        )
    )

    assert rolled_back["effectiveAdmissionTarget"] == 4
    assert list(PDFPipelineTuningEvent.objects.values_list("action", "outcome")) == [
        (PDFPipelineTuningAction.ROLLBACK, ""),
        (PDFPipelineTuningAction.APPLY, "hurt"),
    ]


def test_kill_switch_stops_shadow_recommendations(settings, tmp_path):
    controller = PDFPipelineController(_config(settings, tmp_path, mode="shadow", kill_switch=True))

    snapshot = controller.evaluate(_observation())

    assert snapshot["effectiveAdmissionTarget"] == 4
    assert snapshot["limitingReason"] == "controller_kill_switch"
    assert PDFPipelineTuningEvent.objects.count() == 0


def test_controller_failure_publishes_fixed_fallback_before_supervisor_error(
    settings,
    tmp_path,
):
    _config(settings, tmp_path)
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED = True
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"

    class FailedController:
        def evaluate(self, observation):
            raise ValueError("synthetic controller failure")

    with pytest.raises(RuntimeError, match="controller evaluation failed"):
        metrics.sample_pipeline_metrics(
            at=timezone.now(),
            resident_roles={},
            controller=FailedController(),
        )

    snapshot = json.loads(
        (settings.PDF_PIPELINE_STATE_ROOT / "metrics-v1.json").read_text(encoding="utf-8")
    )
    assert snapshot["controller"]["operatingMode"] == "observe"
    assert snapshot["controller"]["effectiveAdmissionTarget"] == 4


def test_shadow_trace_replay_is_deterministic_and_never_changes_admission(
    settings,
    tmp_path,
):
    config = _config(settings, tmp_path, mode="shadow", hysteresis_samples=3)
    started = timezone.now()
    trace = tuple(_observation(at=started + timedelta(seconds=index)) for index in range(3))

    first = replay_controller_observations(config, trace)
    second = replay_controller_observations(config, trace)

    assert first == second
    assert [row["effectiveAdmissionTarget"] for row in first] == [4, 4, 4]
    assert first[-1]["proposedTarget"] == 5
    assert first[-1]["resourceReservationFreshAt"] == trace[-1].at.isoformat()
    assert PDFPipelineTuningEvent.objects.count() == 0


def test_shadow_trace_replay_rejects_nonchronological_input(settings, tmp_path):
    config = _config(settings, tmp_path, mode="shadow")
    observed = _observation()

    with pytest.raises(ValueError, match="strictly increasing"):
        replay_controller_observations(config, (observed, observed))


def test_shadow_recommendation_records_truthful_nonapplied_outcome(settings, tmp_path):
    config = _config(
        settings,
        tmp_path,
        mode="shadow",
        hysteresis_samples=1,
        cooldown_seconds=60,
    )
    controller = PDFPipelineController(config)
    observed = _observation()

    controller.evaluate(observed)
    first_event = PDFPipelineTuningEvent.objects.get()
    controller.evaluate(replace(observed, at=observed.at + timedelta(seconds=61)))

    first_event.refresh_from_db()
    assert first_event.outcome == "shadow_not_applied"
