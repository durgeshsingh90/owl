from __future__ import annotations

import json
from collections import deque
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPipelineCompletionKind,
    PDFPipelineRecovery,
    PDFPipelineRecoveryState,
    PDFPipelineRunRepository,
    PDFPipelineRunState,
    PDFPipelineTuningAction,
    PDFPipelineTuningEvent,
)
from bitbucket_search.services import pdf_pipeline_metrics as metrics
from bitbucket_search.services.pdf_pipeline_runs import accept_pipeline_run

pytestmark = pytest.mark.django_db


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _repository(name: str = "Metrics repository") -> BitbucketRepository:
    slug = name.casefold().replace(" ", "-")
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{slug}",
        remote_url=f"ssh://git@example.invalid/team/{slug}.git",
    )


def _eta(state="available", seconds=120):
    return {
        "state": state,
        "etaSeconds": seconds,
        "display": "ETA ~00:02:00",
        "confidence": "medium",
        "lowerSeconds": 90,
        "upperSeconds": 150,
        "reasonCode": "test_rate",
    }


def test_rate_warms_then_normalizes_partial_and_full_windows():
    now = timezone.now()

    early = metrics.rolling_rate(
        [now - timedelta(seconds=1)] * 10,
        now=now,
        history_started_at=now - timedelta(seconds=20),
    )
    partial = metrics.rolling_rate(
        [now - timedelta(seconds=5), now - timedelta(seconds=10), now - timedelta(seconds=20)],
        now=now,
        history_started_at=now - timedelta(seconds=40),
    )
    measured_zero = metrics.rolling_rate(
        [],
        now=now,
        history_started_at=now - timedelta(seconds=60),
    )
    low_sample = metrics.rolling_rate(
        [now - timedelta(seconds=5)],
        now=now,
        history_started_at=now - timedelta(seconds=61),
    )

    assert early["state"] == "warming"
    assert early["perMinute"] is None
    assert partial["state"] == "available"
    assert partial["perMinute"] == 4.5
    assert partial["confidence"] == "low"
    assert measured_zero["state"] == "available"
    assert measured_zero["perMinute"] == 0
    assert measured_zero["confidence"] == "high"
    assert low_sample["perMinute"] == 1
    assert low_sample["confidence"] == "low"


def test_throughput_includes_once_only_bytes_pages_characters_and_latencies(
    settings,
    monkeypatch,
    tmp_path,
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    now = timezone.now()
    monkeypatch.setattr(metrics, "_SERIES_STARTED_AT", now - timedelta(seconds=60))
    repository = _repository("Measured throughput")
    document = PDFDocument.objects.create(
        repository=repository,
        filename="measured.pdf",
        relative_path="documents/measured.pdf",
        git_blob_id="a" * 40,
        file_size=4_096,
    )
    published = PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id="a" * 40,
        target_source_commit="b" * 40,
        target_relative_path=document.relative_path,
        target_file_size=4_096,
        target_extractor_version="test-v1",
    )
    PDFExtractionJob.objects.filter(pk=published.pk).update(
        status=PDFExtractionJobStatus.SUCCEEDED,
        phase=PDFExtractionJobPhase.COMPLETED,
        requested_at=now - timedelta(seconds=12),
        started_at=now - timedelta(seconds=10),
        staged_at=now - timedelta(seconds=6),
        publication_started_at=now - timedelta(seconds=5),
        published_at=now - timedelta(seconds=3),
        completed_at=now - timedelta(seconds=3),
        completion_kind=PDFPipelineCompletionKind.NORMAL_PUBLICATION,
        pages_processed=2,
        characters_extracted=1_200,
    )
    failed = PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id="c" * 40,
        target_source_commit="d" * 40,
        target_relative_path=document.relative_path,
        target_file_size=2_048,
        target_extractor_version="test-v2",
    )
    PDFExtractionJob.objects.filter(pk=failed.pk).update(
        status=PDFExtractionJobStatus.FAILED,
        completed_at=now - timedelta(seconds=2),
        error_code="parser_timeout",
    )

    throughput = metrics._throughput_snapshot(now)

    assert throughput["pagesPersistedPerSecond"] == pytest.approx(2 / 60, abs=1e-6)
    assert throughput["pagesExtractedPerSecond"] == pytest.approx(2 / 60, abs=1e-6)
    assert throughput["charactersExtractedPerSecond"] == pytest.approx(1_200 / 60, abs=1e-6)
    assert throughput["charactersPersistedPerSecond"] == pytest.approx(1_200 / 60, abs=1e-6)
    assert throughput["sourceBytesProcessedPerSecond"] == pytest.approx(4_096 / 60, abs=1e-6)
    assert throughput["extractionLatencyP95Ms"] == 4_000
    assert throughput["publicationLatencyP95Ms"] == 2_000
    assert throughput["endToEndLatencyP95Ms"] == 9_000
    assert throughput["failedPerSecond"] == pytest.approx(1 / 60, abs=1e-6)
    assert throughput["timeoutPerSecond"] == pytest.approx(1 / 60, abs=1e-6)
    assert throughput["latencySampleCounts"] == {
        "extraction": 1,
        "publication": 1,
        "stagedWait": 1,
        "endToEnd": 1,
    }
    assert throughput["stagedWaitLatencyP95Ms"] == 1_000
    assert throughput["pagesPersistedPerMinute"] == pytest.approx(2, abs=1e-3)
    assert throughput["seriesTotals"]["written"] == 1


def test_eta_uses_overlapped_critical_path_not_phase_sum():
    result = metrics.estimate_pipeline_eta(
        remaining_extraction=8,
        remaining_publication=10,
        extracted_rate_per_second=2,
        written_rate_per_second=1,
        inventory_final=True,
        now=timezone.now(),
    )

    assert result["state"] == "available"
    assert result["etaSeconds"] == 10
    assert result["etaSeconds"] != 14
    assert result["limitingPhase"] == "publication"


def test_eta_does_not_require_extraction_rate_when_only_staged_work_remains():
    result = metrics.estimate_pipeline_eta(
        remaining_extraction=0,
        remaining_publication=4,
        extracted_rate_per_second=None,
        written_rate_per_second=2,
        inventory_final=True,
        now=timezone.now(),
    )

    assert result["state"] == "available"
    assert result["etaSeconds"] == 2


def test_cancelled_eta_never_manufactures_zero_duration():
    result = metrics._terminal_eta(PDFPipelineRunState.CANCELLED, timezone.now())

    assert result["state"] == "cancelled"
    assert result["etaSeconds"] is None
    assert result["display"] == "Cancelled"


def test_worker_state_buckets_form_exact_live_and_expected_partitions(settings):
    settings.PDF_MAX_EXTRACTION_WORKERS = 4
    settings.PDF_PIPELINE_INITIAL_TARGET = 3
    queue = {
        "activeExtractionJobs": 1,
        "eligibleInputJobs": 0,
        "inputQueuedJobs": 0,
        "backpressureDepthJobs": 0,
        "backpressureThresholdJobs": 2,
    }
    roles = {f"pdf-index-{number}": _Process() for number in range(1, 5)}

    result = metrics._worker_snapshot(queue, resident_roles=roles)

    assert result["live"] == 4
    assert result["active"] == 1
    assert result["idleNoDemand"] == 2
    assert result["pausedByController"] == 1
    assert (
        result["active"]
        + result["idleNoDemand"]
        + result["waitingForEligibleInput"]
        + result["backpressured"]
        + result["pausedByController"]
        == result["live"]
    )
    assert result["live"] + result["pausedByRecovery"] + result["unavailable"] == 4


def test_recovery_paused_pool_is_not_reported_as_unavailable(settings):
    settings.PDF_MAX_EXTRACTION_WORKERS = 4
    settings.PDF_PIPELINE_INITIAL_TARGET = 4
    PDFPipelineRecovery.objects.create(
        scope="extraction_pool",
        state=PDFPipelineRecoveryState.PAUSED,
    )
    roles = {"pdf-index-1": _Process()}
    queue = {
        "activeExtractionJobs": 1,
        "eligibleInputJobs": 1,
        "inputQueuedJobs": 1,
        "backpressureDepthJobs": 0,
        "backpressureThresholdJobs": 2,
    }

    result = metrics._worker_snapshot(queue, resident_roles=roles)

    assert result["active"] == 1
    assert result["pausedByRecovery"] == 3
    assert result["unavailable"] == 0
    assert result["live"] + result["pausedByRecovery"] + result["unavailable"] == 4


def test_classifier_safety_precedence_is_fail_closed():
    healthy = {"state": PDFPipelineRecoveryState.HEALTHY}
    activity = {"code": "extracting"}
    queues = {
        "backpressureDepthJobs": 4,
        "backpressureThresholdJobs": 4,
        "inputQueuedJobs": 1,
        "jsonl": {"queuedChunks": 1},
    }

    publisher_failure = metrics._state_snapshot(
        activity,
        queues,
        {"unavailable": 0},
        {"state": "unavailable"},
        healthy,
    )
    paused = metrics._state_snapshot(
        {"code": "source_blocked"},
        queues,
        {"unavailable": 4},
        {"state": "unavailable"},
        {"state": PDFPipelineRecoveryState.PAUSED},
    )
    source_blocked = metrics._state_snapshot(
        {"code": "source_blocked"},
        {**queues, "backpressureDepthJobs": 0},
        {"unavailable": 0},
        {"state": "starved"},
        healthy,
    )

    assert publisher_failure["code"] == "degraded"
    assert publisher_failure["reasonCode"] == "publisher_unavailable_with_backlog"
    assert paused["code"] == "recovery_paused"
    assert source_blocked["code"] == "source_blocked"


@pytest.mark.parametrize(
    (
        "expected",
        "activity_code",
        "queue_updates",
        "worker_updates",
        "publisher_updates",
        "resource_updates",
    ),
    [
        ("idle", "idle", {}, {}, {"state": "idle_no_demand"}, {}),
        ("balanced", "extracting", {}, {}, {"state": "busy"}, {}),
        ("extraction_limited", "extracting", {}, {}, {"state": "starved"}, {}),
        (
            "publication_limited",
            "extracting",
            {
                "backpressureDepthJobs": 1,
                "backpressureDepthGrowthPerSecond": 0.5,
                "jsonl": {"queuedChunks": 1},
            },
            {},
            {"state": "busy"},
            {},
        ),
        (
            "sqlite_contended",
            "extracting",
            {"backpressureDepthJobs": 1},
            {},
            {"state": "blocked", "sqliteBusyErrors": 1},
            {},
        ),
        (
            "balanced",
            "extracting",
            {"backpressureDepthJobs": 4},
            {},
            {"state": "busy"},
            {},
        ),
        (
            "source_blocked",
            "source_blocked",
            {"inputQueuedJobs": 2},
            {},
            {"state": "starved"},
            {},
        ),
        (
            "degraded",
            "extracting",
            {"inputQueuedJobs": 2},
            {"unavailable": 1},
            {"state": "unavailable"},
            {},
        ),
        ("cpu_limited", "extracting", {}, {}, {"state": "busy"}, {"hostCpuPct": 95}),
        (
            "memory_limited",
            "extracting",
            {},
            {},
            {"state": "busy"},
            {"hostMemoryAvailableBytes": 1},
        ),
        (
            "disk_limited",
            "extracting",
            {},
            {},
            {"state": "busy"},
            {"diskAvailableBytes": 1},
        ),
    ],
)
def test_classifier_covers_stable_primary_states(
    settings,
    expected,
    activity_code,
    queue_updates,
    worker_updates,
    publisher_updates,
    resource_updates,
):
    queues = {
        "backpressureDepthJobs": 0,
        "backpressureThresholdJobs": 4,
        "backpressureDepthGrowthPerSecond": 0,
        "inputQueuedJobs": 0,
        **queue_updates,
    }
    publisher = {
        "state": "busy",
        "sqliteBusyErrors": 0,
        "sqliteLockWaitP95Ms": 0,
        "sqliteLockBlockedThresholdMs": 250,
        **publisher_updates,
    }
    resources = {
        "hostCpuPct": 10,
        "hostMemoryAvailableBytes": settings.PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES,
        "diskAvailableBytes": settings.PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES,
        **resource_updates,
    }
    throughput = {
        "rateWindowSeconds": 60,
        "extractedRate": {"state": "available"},
        "writtenRate": {"state": "available"},
    }

    result = metrics._state_snapshot(
        {"code": activity_code},
        queues,
        {"unavailable": 0, **worker_updates},
        publisher,
        {"state": PDFPipelineRecoveryState.HEALTHY},
        resources,
        throughput,
    )

    assert result["code"] == expected
    assert result["reasonCode"]
    assert result["reason"]
    assert result["confidence"] in {"warming", "low", "medium", "high"}
    assert result["window"]["durationSeconds"] == 60


def test_metrics_build_is_read_only_for_run_summary(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    repository = _repository("Read only")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        lifecycle_state=PDFPipelineRunState.ACTIVE,
        inventory_final=True,
        total_pdfs=1,
        successful_pdfs=1,
        remaining_pdfs=0,
    )

    metrics.build_pipeline_metrics_payload(at=timezone.now(), resident_roles={})

    membership.refresh_from_db()
    assert membership.successful_pdfs == 1
    assert membership.remaining_pdfs == 0


def test_repository_progress_separates_staged_waiting_from_active_publication(
    settings,
    tmp_path,
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    now = timezone.now()
    repository = _repository("Publication boundaries")
    run = accept_pipeline_run([repository.pk])
    membership = run.repository_memberships.get()
    PDFPipelineRunRepository.objects.filter(pk=membership.pk).update(
        lifecycle_state=PDFPipelineRunState.ACTIVE,
        inventory_final=True,
        total_pdfs=2,
        remaining_pdfs=2,
    )
    documents = [
        PDFDocument.objects.create(
            repository=repository,
            filename=f"document-{index}.pdf",
            relative_path=f"documents/document-{index}.pdf",
            git_blob_id=str(index) * 40,
        )
        for index in (1, 2)
    ]
    for index, document in enumerate(documents):
        PDFExtractionJob.objects.create(
            document=document,
            run_repository=membership,
            run_id=run.pk,
            target_git_blob_id=document.git_blob_id,
            target_source_commit="a" * 40,
            target_relative_path=document.relative_path,
            target_file_size=100,
            target_extractor_version="test-v1",
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
            worker_pid=1234 if index else None,
            heartbeat_at=now if index else None,
        )

    payload = metrics.build_pipeline_metrics_payload(at=now, resident_roles={})
    progress = payload["run"]["repositoryProgress"][0]

    assert progress["stagedPdfs"] == 1
    assert progress["publishingPdfs"] == 1
    assert progress["remainingPdfs"] == 2


def test_idle_indicator_uses_saved_repository_not_only_current_run(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    _repository("Saved and idle")

    payload = metrics.build_pipeline_metrics_payload(at=timezone.now(), resident_roles={})

    assert payload["run"] is None
    assert payload["activity"]["code"] == "idle"
    assert payload["topBarActivityIndicator"]["state"] == "idle_actionable"
    assert payload["topBarActivityIndicator"]["hasFreshRunningWork"] is False


def test_waiting_and_completing_states_never_claim_fresh_running_artwork():
    now = timezone.now()
    recovery = {"activeAttemptId": None}
    for code in ("backpressured", "source_blocked", "completing"):
        indicator = metrics._indicator_snapshot(
            now,
            {"code": code, "reasonCode": f"activity_{code}", "evidenceAt": None},
            None,
            1,
            1,
            recovery,
        )

        assert indicator["state"] == "queued"
        assert indicator["hasFreshRunningWork"] is False
        assert indicator["evidenceAt"] is None


def test_indicator_uses_authoritative_heartbeat_time_instead_of_sample_time():
    now = timezone.now()
    heartbeat = now - timedelta(seconds=4)
    indicator = metrics._indicator_snapshot(
        now,
        {
            "code": "extracting",
            "reasonCode": "activity_extracting",
            "evidenceAt": heartbeat.isoformat(),
        },
        None,
        1,
        1,
        {"activeAttemptId": None},
    )

    assert indicator["state"] == "running"
    assert indicator["hasFreshRunningWork"] is True
    assert indicator["evidenceAt"] == heartbeat.isoformat()


def test_stale_snapshot_removes_nonterminal_eta_and_running_evidence(settings, tmp_path):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_METRICS_STALE_SECONDS = 15
    now = timezone.now()
    stale_at = now - timedelta(seconds=60)
    snapshot = {
        "schemaVersion": metrics.SCHEMA_VERSION,
        "generatedAt": stale_at.isoformat(),
        "state": {"code": "balanced", "label": "Balanced"},
        "activity": {"code": "extracting"},
        "topBarActivityIndicator": {
            "state": "running",
            "hasFreshRunningWork": True,
        },
        "run": {
            "state": PDFPipelineRunState.ACTIVE,
            "totalEta": _eta(),
            "repositoryProgress": [
                {
                    "lifecycleState": PDFPipelineRunState.ACTIVE,
                    "eta": _eta(seconds=30),
                }
            ],
        },
    }
    metrics.write_metrics_snapshot(snapshot)

    payload = metrics.metrics_payload_for_request(at=now)

    assert payload["snapshotStale"] is True
    assert payload["topBarActivityIndicator"]["state"] == "unknown"
    assert payload["topBarActivityIndicator"]["hasFreshRunningWork"] is False
    assert payload["run"]["totalEta"]["state"] == "stale"
    assert payload["run"]["totalEta"]["etaSeconds"] is None
    assert payload["run"]["repositoryProgress"][0]["eta"]["etaSeconds"] is None


def test_payload_omits_repository_and_document_secrets_and_sanitizes_tuning(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    private_remote = "ssh://git@example.invalid/private/secret-repository.git"
    private_filename = "customer-passwords.pdf"
    private_path = "/Users/private"
    private_marker = "ABC123SECRET"
    repository = BitbucketRepository.objects.create(
        display_name="Sensitive display name",
        canonical_remote_key="example.invalid/private/secret-repository",
        remote_url=private_remote,
    )
    PDFDocument.objects.create(
        repository=repository,
        filename=private_filename,
        relative_path=f"private/{private_filename}",
        git_blob_id="a" * 40,
    )
    accept_pipeline_run([repository.pk])
    PDFPipelineTuningEvent.objects.create(
        mode="shadow",
        action=PDFPipelineTuningAction.RECOMMEND,
        previous_target=2,
        proposed_target=3,
        reason_code="throughput_improved",
        reason=f"Read {private_path}/secret-repository and token {private_marker}",
        expected_effect_code="improve_durable_throughput",
        confidence="medium",
        observation_window_seconds=60,
        evidence={"throughput": 1.25, "path": private_path, "token": private_marker},
    )

    payload = metrics.build_pipeline_metrics_payload(at=timezone.now(), resident_roles={})
    serialized = json.dumps(payload)

    assert private_remote not in serialized
    assert private_filename not in serialized
    assert private_path not in serialized
    assert private_marker not in serialized
    assert payload["tuningEvents"][0]["evidence"] == {"throughput": 1.25}
    assert payload["tuningEvents"][0]["expectedEffectCode"] == ("improve_durable_throughput")
    assert "additional admitted parser" in payload["tuningEvents"][0]["expectedEffect"]


def test_sample_ring_is_bounded_and_reports_history_truncation(
    settings,
    monkeypatch,
    tmp_path,
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    settings.PDF_PIPELINE_METRICS_SAMPLE_SECONDS = 10
    settings.PDF_PIPELINE_METRICS_RETENTION_SECONDS = 60
    settings.PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED = False
    base = timezone.now() - timedelta(minutes=5)
    monkeypatch.setattr(metrics, "_SERIES_STARTED_AT", base)
    monkeypatch.setattr(metrics, "_SAMPLES", deque())
    monkeypatch.setattr(metrics, "_HISTORY_TRUNCATED", False)

    payload = None
    for index in range(14):
        payload = metrics.sample_pipeline_metrics(
            at=base + timedelta(seconds=index * 10),
            resident_roles={},
        )

    assert payload is not None
    assert len(payload["samples"]) == 12
    assert payload["historyComplete"] is False
    assert payload["historyStartedAt"] == (base + timedelta(seconds=20)).isoformat()


def test_resource_snapshot_does_not_mislabel_current_process_as_process_tree():
    payload = metrics.resource_snapshot()

    assert payload["owlProcessTreeRssBytes"] is None
    assert payload["availability"]["owlProcessTreeRssBytes"] == ("process_tree_sampler_unavailable")
    assert "owlCurrentProcessPeakRssBytes" in payload


def test_darwin_memory_fallback_parses_vm_stat(monkeypatch):
    def fake_sysconf(name):
        if name == "SC_AVPHYS_PAGES":
            raise ValueError("unsupported")
        return {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 100}[name]

    output = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free: 10.
Pages active: 50.
Pages inactive: 20.
Pages speculative: 5.
"""
    monkeypatch.setattr(metrics.sys, "platform", "darwin")
    monkeypatch.setattr(metrics.os, "sysconf", fake_sysconf)
    monkeypatch.setattr(
        metrics.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    monkeypatch.setattr(metrics, "_DARWIN_MEMORY_CACHE", None)

    available, source = metrics._memory_available_bytes()

    assert available == 35 * 4096
    assert source == "darwin_vm_stat"


def test_queue_growth_and_publisher_duty_cycle_use_bounded_sample_history(monkeypatch):
    now = timezone.now()
    monkeypatch.setattr(
        metrics,
        "_SAMPLES",
        deque(
            (
                {
                    "at": (now - timedelta(seconds=20)).isoformat(),
                    "backpressureDepthJobs": 1,
                    "stagedBytes": 100,
                    "publisherState": "starved",
                },
                {
                    "at": (now - timedelta(seconds=10)).isoformat(),
                    "backpressureDepthJobs": 3,
                    "stagedBytes": 300,
                    "publisherState": "busy",
                },
            )
        ),
    )

    assert metrics._gauge_growth_rate(
        "backpressureDepthJobs", 5, now=now, window_seconds=60
    ) == pytest.approx(0.2)
    duty = metrics._publisher_duty_cycle("blocked", now=now, window_seconds=60)

    assert duty["dutyCycleMeasuredSeconds"] == 20
    assert duty["starvedPct"] == 50
    assert duty["busyPct"] == 50
    assert duty["blockedPct"] == 0
    assert sum(duty[key] for key in ("busyPct", "starvedPct", "noDemandPct", "blockedPct")) == 100


def test_process_table_snapshot_normalizes_host_and_descendant_cpu(monkeypatch):
    monkeypatch.setattr(metrics.os, "getpid", lambda: 100)
    monkeypatch.setattr(metrics, "_schedulable_cpu_count", lambda: 4)
    monkeypatch.setattr(
        metrics.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="100 1 40.0 100\n101 100 80.0 200\n102 101 20.0 300\n200 1 60.0 400\n",
        ),
    )

    host_cpu, tree_cpu, tree_rss = metrics._process_table_snapshot()

    assert host_cpu == 50
    assert tree_cpu == 35
    assert tree_rss == 600 * 1_024


def test_completed_run_eta_accuracy_scores_shadow_forecasts(monkeypatch):
    completed_at = timezone.now()
    run_id = "00000000-0000-0000-0000-000000000011"
    monkeypatch.setattr(
        metrics,
        "_SAMPLES",
        deque(
            (
                {
                    "at": (completed_at - timedelta(seconds=100)).isoformat(),
                    "run": {
                        "id": run_id,
                        "totalEta": {"state": "available", "etaSeconds": 120},
                        "repositories": [
                            {
                                "repositoryId": 7,
                                "eta": {"state": "available", "etaSeconds": 80},
                            }
                        ],
                    },
                },
                {
                    "at": (completed_at - timedelta(seconds=50)).isoformat(),
                    "run": {
                        "id": run_id,
                        "totalEta": {"state": "available", "etaSeconds": 40},
                        "repositories": [
                            {
                                "repositoryId": 7,
                                "eta": {"state": "available", "etaSeconds": 40},
                            }
                        ],
                    },
                },
            )
        ),
    )
    run = {
        "id": run_id,
        "completedAt": completed_at.isoformat(),
        "pdfs": {"total": 10},
        "repositoryProgress": [{"repositoryId": 7, "completedAt": completed_at.isoformat()}],
    }

    accuracy = metrics._eta_accuracy_snapshot(run)

    assert accuracy["state"] == "available"
    assert accuracy["checkpointCount"] == 2
    assert accuracy["medianAbsolutePercentageError"] == 20
    assert accuracy["meanBiasPercentage"] == 0
    assert accuracy["repositorySummaries"][0]["checkpointCount"] == 2
