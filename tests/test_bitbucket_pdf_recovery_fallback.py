from __future__ import annotations

import stat
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from django.db import DatabaseError

from bitbucket_search.models import PDFPipelineRecovery, PDFPipelineRecoveryState
from bitbucket_search.services import pdf_recovery, pdf_recovery_fallback

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_fallback_state(tmp_path, settings, monkeypatch):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pdf-pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 25
    pdf_recovery_fallback.reset_process_state_for_tests()
    monkeypatch.setattr(pdf_recovery, "publish_recovery_notification", Mock())
    yield
    pdf_recovery_fallback.reset_process_state_for_tests()


def _healthy_recovery(*, generation: int) -> PDFPipelineRecovery:
    return PDFPipelineRecovery.objects.create(
        scope=pdf_recovery.RecoveryScope.PUBLISHER,
        generation=generation,
        state=PDFPipelineRecoveryState.HEALTHY,
        pause_after_attempts=25,
    )


def _paused_record(
    recovery: PDFPipelineRecovery,
    *,
    generation: int,
    pause_generation: int = 1,
    pending_reconciliation: bool = True,
) -> pdf_recovery_fallback.FallbackRecoveryRecord:
    timestamp = NOW.isoformat()
    return replace(
        pdf_recovery._fallback_record_from_recovery(recovery),
        generation=generation,
        generationOrdered=True,
        state=PDFPipelineRecoveryState.PAUSED,
        pauseGeneration=pause_generation,
        reasonCode=pdf_recovery.RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE,
        firstFailureAt=timestamp,
        lastFailureAt=timestamp,
        pausedAt=timestamp,
        activeAttemptId=None,
        nextRetryAt=None,
        pendingReconciliation=pending_reconciliation,
    )


def test_database_outage_checkpoint_is_private_bounded_and_redacted():
    scope = pdf_recovery.extraction_slot_scope(3)

    assert pdf_recovery_fallback.persist_database_unavailable_pause(scope, now=NOW)

    result = pdf_recovery_fallback.read_fallback(scope)
    assert result.state == pdf_recovery_fallback.FallbackReadState.VALID
    assert result.record is not None
    assert result.record.state == PDFPipelineRecoveryState.PAUSED
    assert result.record.reasonCode == pdf_recovery.RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE
    assert result.record.pendingReconciliation is True
    checkpoint = pdf_recovery_fallback.fallback_path(scope)
    payload = checkpoint.read_bytes()
    assert len(payload) <= pdf_recovery_fallback.MAX_FALLBACK_BYTES
    assert scope.encode() not in payload
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert stat.S_IMODE(checkpoint.parent.stat().st_mode) == 0o700


def test_database_failure_blocks_launch_and_records_durable_fallback(monkeypatch):
    monkeypatch.setattr(
        pdf_recovery,
        "_reconcile_recovery_scope",
        Mock(side_effect=DatabaseError("synthetic database outage")),
    )

    with pytest.raises(pdf_recovery.RecoveryControlUnavailable) as unavailable:
        pdf_recovery.ensure_recovery_scope(pdf_recovery.RecoveryScope.PUBLISHER)

    assert unavailable.value.durably_recorded is True
    result = pdf_recovery_fallback.read_fallback(pdf_recovery.RecoveryScope.PUBLISHER)
    assert result.state == pdf_recovery_fallback.FallbackReadState.VALID
    assert result.record is not None
    assert result.record.state == PDFPipelineRecoveryState.PAUSED


def test_unwritable_fallback_retains_in_memory_pause_and_bounds_retries(monkeypatch):
    scope = pdf_recovery.RecoveryScope.PUBLISHER
    writer = Mock(side_effect=OSError("synthetic read-only disk"))
    monkeypatch.setattr(pdf_recovery_fallback, "write_fallback", writer)

    assert not pdf_recovery_fallback.persist_database_unavailable_pause(
        scope,
        now=NOW,
        monotonic_now=10.0,
    )
    assert pdf_recovery_fallback.is_in_memory_fail_closed(scope)
    assert not pdf_recovery_fallback.persist_database_unavailable_pause(
        scope,
        now=NOW,
        monotonic_now=10.5,
    )
    assert writer.call_count == 1
    assert pdf_recovery_fallback.emergency_log_due(scope, monotonic_now=10.0)
    assert not pdf_recovery_fallback.emergency_log_due(scope, monotonic_now=69.9)
    assert pdf_recovery_fallback.emergency_log_due(scope, monotonic_now=70.0)


def test_newer_database_generation_wins_and_cleans_older_paused_checkpoint(
    django_capture_on_commit_callbacks,
):
    recovery = _healthy_recovery(generation=5)
    pdf_recovery_fallback.write_fallback(
        _paused_record(recovery, generation=4, pending_reconciliation=True)
    )

    with django_capture_on_commit_callbacks(execute=True):
        reconciled = pdf_recovery.ensure_recovery_scope(recovery.scope)

    assert reconciled.state == PDFPipelineRecoveryState.HEALTHY
    assert reconciled.generation == 5
    checkpoint = pdf_recovery_fallback.read_fallback(recovery.scope).record
    assert checkpoint is not None
    assert checkpoint.state == PDFPipelineRecoveryState.HEALTHY
    assert checkpoint.generation == 5
    assert checkpoint.pendingReconciliation is False


def test_newer_fallback_generation_becomes_canonical_and_is_checkpointed(
    django_capture_on_commit_callbacks,
):
    recovery = _healthy_recovery(generation=2)
    pdf_recovery_fallback.write_fallback(_paused_record(recovery, generation=3))

    with django_capture_on_commit_callbacks(execute=True):
        reconciled = pdf_recovery.ensure_recovery_scope(recovery.scope)

    assert reconciled.state == PDFPipelineRecoveryState.PAUSED
    assert reconciled.generation == 3
    assert reconciled.reason_code == pdf_recovery.RecoveryReasonCode.CONTROL_STATE_UNAVAILABLE
    checkpoint = pdf_recovery_fallback.read_fallback(recovery.scope).record
    assert checkpoint is not None
    assert checkpoint.generation == 3
    assert checkpoint.pendingReconciliation is False


def test_equal_generation_disagreement_pauses_with_new_ordered_generation(
    django_capture_on_commit_callbacks,
):
    recovery = _healthy_recovery(generation=2)
    pdf_recovery_fallback.write_fallback(_paused_record(recovery, generation=2))

    with django_capture_on_commit_callbacks(execute=True):
        reconciled = pdf_recovery.ensure_recovery_scope(recovery.scope)

    assert reconciled.state == PDFPipelineRecoveryState.PAUSED
    assert reconciled.generation == 3
    assert reconciled.reason_code == pdf_recovery.RecoveryReasonCode.CONTROL_STATE_CONFLICT
    assert reconciled.pause_generation == 2
    checkpoint = pdf_recovery_fallback.read_fallback(recovery.scope).record
    assert checkpoint is not None
    assert checkpoint.generation == 3
    assert checkpoint.reasonCode == pdf_recovery.RecoveryReasonCode.CONTROL_STATE_CONFLICT


def test_corrupt_fallback_fails_closed_instead_of_launching(
    django_capture_on_commit_callbacks,
):
    recovery = _healthy_recovery(generation=7)
    checkpoint = pdf_recovery_fallback.fallback_path(recovery.scope)
    checkpoint.parent.mkdir(mode=0o700, parents=True)
    checkpoint.write_bytes(b'{"schemaVersion":1,"unexpected":"record"}')
    checkpoint.chmod(0o600)

    with django_capture_on_commit_callbacks(execute=True):
        reconciled = pdf_recovery.ensure_recovery_scope(recovery.scope)

    assert reconciled.state == PDFPipelineRecoveryState.PAUSED
    assert reconciled.generation == 8
    assert reconciled.reason_code == pdf_recovery.RecoveryReasonCode.CONTROL_STATE_CONFLICT
    assert pdf_recovery_fallback.read_fallback(recovery.scope).state == (
        pdf_recovery_fallback.FallbackReadState.VALID
    )
