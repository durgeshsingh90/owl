from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from bitbucket_search.models import (
    PDFPipelineRecovery,
    PDFPipelineRecoveryEvent,
    PDFPipelineRecoveryEventKind,
    PDFPipelineRecoveryState,
)
from bitbucket_search.services import pdf_recovery

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_recovery_state(tmp_path, settings, monkeypatch):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pdf-pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 25
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 2
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 16
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0.20
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 60
    publisher = Mock()
    monkeypatch.setattr(pdf_recovery, "publish_recovery_notification", publisher)
    return publisher


def _middle_jitter(lower: float, upper: float) -> float:
    return (lower + upper) / 2


def _incident(
    scope: str = pdf_recovery.RecoveryScope.PUBLISHER,
    *,
    pause_after_attempts: int = 2,
    occurred_at: datetime = NOW,
):
    return pdf_recovery.record_recovery_incident(
        scope,
        reason_code=pdf_recovery.RecoveryReasonCode.PROCESS_EXIT,
        incident_id=uuid.uuid4(),
        occurred_at=occurred_at,
        pause_after_attempts=pause_after_attempts,
        jitter=_middle_jitter,
    ).transition


def _begin_due(transition, *, attempt_id: uuid.UUID | None = None):
    assert transition is not None
    return pdf_recovery.begin_recovery_attempt(
        transition.recovery.scope,
        expected_generation=transition.recovery.generation,
        attempt_id=attempt_id,
        occurred_at=transition.recovery.next_retry_at,
    )


def test_exact_state_enum_scope_helpers_and_healthy_payload():
    assert tuple(PDFPipelineRecoveryState.values) == (
        "healthy",
        "retry_wait",
        "recovering",
        "paused",
        "resume_requested",
        "recovering_half_open",
    )
    assert pdf_recovery.extraction_slot_scope(20) == "extraction_slot:20"
    assert pdf_recovery.repository_recovery_scope(42) == "repository:42"
    recovery = pdf_recovery.ensure_recovery_scope(pdf_recovery.RecoveryScope.EXTRACTION_POOL)

    assert pdf_recovery.recovery_payload(recovery) == {
        "schemaVersion": 1,
        "state": "healthy",
        "halfOpen": False,
        "episodeId": None,
        "generation": 0,
        "pauseGeneration": 0,
        "scope": "extraction_pool",
        "reasonFamily": None,
        "reasonCode": None,
        "consecutiveFailedAttempts": 0,
        "lifetimeAttempts": 0,
        "pauseAfterAttempts": 25,
        "firstFailureAt": None,
        "lastFailureAt": None,
        "lastAttemptAt": None,
        "nextRetryAt": None,
        "currentBackoffSeconds": 0,
        "pausedReason": None,
        "pausedAt": None,
        "popupAcknowledgedGeneration": 0,
        "popupClaimedGeneration": 0,
        "resumeRequestedAt": None,
        "recoveredAt": None,
        "lastOutcome": None,
        "activeAttemptId": None,
        "resumable": False,
        "resumeSafety": "not_applicable",
        "resumeBlockedReason": None,
        "resumeAction": None,
        "stabilityWindowSeconds": 60,
    }


@pytest.mark.parametrize(
    ("reason", "disposition"),
    (
        ("sqlite_locked", pdf_recovery.RecoveryFailureDisposition.RETRY_COMPONENT),
        ("critical_disk", pdf_recovery.RecoveryFailureDisposition.PAUSE_SAFETY),
        ("corrupt_pdf", pdf_recovery.RecoveryFailureDisposition.PERMANENT_ITEM),
        ("planned_shutdown", pdf_recovery.RecoveryFailureDisposition.IGNORE),
        ("private /Users/example/file", pdf_recovery.RecoveryFailureDisposition.PAUSE_SAFETY),
    ),
)
def test_failure_classification_is_closed_and_redacted(reason, disposition):
    classification = pdf_recovery.classify_recovery_failure(reason)

    assert classification.disposition == disposition
    if reason.startswith("private"):
        assert classification.reason_code == "unknown_component_failure"


@pytest.mark.parametrize(
    "reason", ("corrupt_pdf", "encrypted_pdf", "user_cancelled", "suspend_wake")
)
def test_permanent_pdf_and_planned_outcomes_never_create_component_recovery(reason):
    result = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code=reason,
        occurred_at=NOW,
    )

    assert result.transition is None
    assert not PDFPipelineRecovery.objects.exists()
    assert not PDFPipelineRecoveryEvent.objects.exists()


def test_backoff_is_exponential_capped_and_has_injectable_bounded_jitter():
    assert (
        pdf_recovery.recovery_backoff_seconds(
            1,
            base_seconds=4,
            maximum_seconds=10,
            jitter_fraction=0.25,
            jitter=lambda lower, upper: lower,
        )
        == 3
    )
    assert (
        pdf_recovery.recovery_backoff_seconds(
            1,
            base_seconds=4,
            maximum_seconds=10,
            jitter_fraction=0.25,
            jitter=lambda lower, upper: upper,
        )
        == 5
    )
    assert (
        pdf_recovery.recovery_backoff_seconds(
            8,
            base_seconds=4,
            maximum_seconds=10,
            jitter_fraction=0.25,
            jitter=lambda lower, upper: 1_000,
        )
        == 10
    )


def test_incident_is_generation_safe_persisted_and_deduplicated():
    incident_id = uuid.uuid4()
    first = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.PUBLISHER,
        reason_code="process_exit",
        incident_id=incident_id,
        expected_generation=0,
        occurred_at=NOW,
        pause_after_attempts=2,
        jitter=_middle_jitter,
    )
    recovery = first.transition.recovery

    assert recovery.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert recovery.generation == 1
    assert recovery.pause_generation == 0
    assert recovery.lifetime_attempts == 0
    assert recovery.consecutive_failed_attempts == 0
    assert recovery.current_backoff_seconds == 2
    assert recovery.next_retry_at == NOW + timedelta(seconds=2)
    assert list(recovery.events.order_by("id").values_list("kind", flat=True)) == [
        PDFPipelineRecoveryEventKind.EPISODE_OPENED,
        PDFPipelineRecoveryEventKind.RETRY_SCHEDULED,
    ]

    duplicate = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.PUBLISHER,
        reason_code="process_exit",
        incident_id=incident_id,
        expected_generation=0,
        occurred_at=NOW + timedelta(seconds=1),
    )

    assert duplicate.transition.duplicate
    assert duplicate.transition.recovery.generation == 1
    assert PDFPipelineRecoveryEvent.objects.count() == 2


def test_probe_counts_at_start_and_failure_below_threshold_reschedules():
    opened = _incident(pause_after_attempts=2)
    with pytest.raises(pdf_recovery.RecoveryNotDue):
        pdf_recovery.begin_recovery_attempt(
            pdf_recovery.RecoveryScope.PUBLISHER,
            expected_generation=opened.recovery.generation,
            occurred_at=NOW + timedelta(seconds=1),
        )
    attempt_id = uuid.uuid4()
    started = _begin_due(opened, attempt_id=attempt_id)

    assert started.recovery.state == PDFPipelineRecoveryState.RECOVERING
    assert started.recovery.lifetime_attempts == 1
    assert started.recovery.consecutive_failed_attempts == 0
    assert started.recovery.active_attempt_id == attempt_id

    failed_at = started.recovery.last_attempt_at + timedelta(seconds=1)
    failed = pdf_recovery.fail_recovery_attempt(
        pdf_recovery.RecoveryScope.PUBLISHER,
        attempt_id=attempt_id,
        expected_generation=started.recovery.generation,
        occurred_at=failed_at,
        jitter=_middle_jitter,
    )

    assert failed.recovery.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert failed.recovery.lifetime_attempts == 1
    assert failed.recovery.consecutive_failed_attempts == 1
    assert failed.recovery.next_retry_at == failed_at + timedelta(seconds=2)
    assert failed.recovery.active_attempt_id is None


def test_threshold_opens_exactly_at_limit_and_duplicate_failure_cannot_exceed_it(
    isolated_recovery_state,
):
    first = _incident(pause_after_attempts=2)
    first_started = _begin_due(first)
    first_failed = pdf_recovery.fail_recovery_attempt(
        first.recovery.scope,
        attempt_id=first_started.recovery.active_attempt_id,
        expected_generation=first_started.recovery.generation,
        occurred_at=first_started.recovery.last_attempt_at + timedelta(seconds=1),
        jitter=_middle_jitter,
    )
    second_started = _begin_due(first_failed)
    paused = pdf_recovery.fail_recovery_attempt(
        first.recovery.scope,
        attempt_id=second_started.recovery.active_attempt_id,
        expected_generation=second_started.recovery.generation,
        occurred_at=second_started.recovery.last_attempt_at + timedelta(seconds=1),
        jitter=_middle_jitter,
    )

    assert paused.recovery.state == PDFPipelineRecoveryState.PAUSED
    assert paused.recovery.consecutive_failed_attempts == 2
    assert paused.recovery.lifetime_attempts == 2
    assert paused.recovery.pause_generation == 1
    isolated_recovery_state.assert_called_once_with(paused.recovery, mark_unread=True)

    duplicate = pdf_recovery.fail_recovery_attempt(
        first.recovery.scope,
        attempt_id=second_started.recovery.active_attempt_id,
        expected_generation=second_started.recovery.generation,
        occurred_at=second_started.recovery.last_attempt_at + timedelta(seconds=2),
    )

    assert duplicate.duplicate
    assert duplicate.recovery.generation == paused.recovery.generation
    assert duplicate.recovery.consecutive_failed_attempts == 2
    assert duplicate.recovery.lifetime_attempts == 2
    assert isolated_recovery_state.call_count == 1


def test_default_threshold_pauses_on_twenty_fifth_failed_probe():
    transition = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.PUBLISHER,
        reason_code="process_exit",
        incident_id=uuid.uuid4(),
        occurred_at=NOW,
        jitter=_middle_jitter,
    ).transition

    for attempt_number in range(1, 26):
        started = _begin_due(transition)
        transition = pdf_recovery.fail_recovery_attempt(
            transition.recovery.scope,
            attempt_id=started.recovery.active_attempt_id,
            expected_generation=started.recovery.generation,
            occurred_at=started.recovery.last_attempt_at + timedelta(seconds=1),
            jitter=_middle_jitter,
        )
        expected_state = (
            PDFPipelineRecoveryState.PAUSED
            if attempt_number == 25
            else PDFPipelineRecoveryState.RETRY_WAIT
        )
        assert transition.recovery.state == expected_state
        assert transition.recovery.consecutive_failed_attempts == attempt_number

    assert transition.recovery.pause_after_attempts == 25
    assert transition.recovery.lifetime_attempts == 25
    with pytest.raises(pdf_recovery.RecoveryTransitionRejected):
        pdf_recovery.begin_recovery_attempt(
            transition.recovery.scope,
            expected_generation=transition.recovery.generation,
            occurred_at=transition.recovery.paused_at + timedelta(seconds=1),
        )


def test_new_detector_incident_is_coalesced_into_current_episode():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    first = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="stale_heartbeat",
        incident_id=first_id,
        occurred_at=NOW,
        jitter=_middle_jitter,
    ).transition

    coalesced = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="stale_heartbeat",
        incident_id=second_id,
        expected_generation=first.recovery.generation,
        occurred_at=NOW + timedelta(seconds=1),
    ).transition

    assert not coalesced.changed
    assert coalesced.recovery.generation == first.recovery.generation
    assert coalesced.recovery.lifetime_attempts == 0
    event = PDFPipelineRecoveryEvent.objects.get(event_id=second_id)
    assert event.kind == PDFPipelineRecoveryEventKind.SUPERSEDED
    assert event.generation == first.recovery.generation

    duplicate = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="stale_heartbeat",
        incident_id=second_id,
        expected_generation=0,
        occurred_at=NOW + timedelta(seconds=2),
    ).transition
    assert duplicate.duplicate
    assert duplicate.recovery.generation == first.recovery.generation


def _failed_slot_probe(scope: str, *, attempt_id: uuid.UUID):
    opened = pdf_recovery.record_recovery_incident(
        scope,
        reason_code="process_exit",
        incident_id=uuid.uuid4(),
        occurred_at=NOW,
        pause_after_attempts=25,
        jitter=_middle_jitter,
    ).transition
    started = pdf_recovery.begin_recovery_attempt(
        scope,
        expected_generation=opened.recovery.generation,
        attempt_id=attempt_id,
        occurred_at=opened.recovery.next_retry_at,
    )
    return pdf_recovery.fail_recovery_attempt(
        scope,
        attempt_id=attempt_id,
        expected_generation=started.recovery.generation,
        occurred_at=started.recovery.last_attempt_at + timedelta(seconds=1),
        jitter=_middle_jitter,
    ).recovery


def test_scope_escalation_transfers_one_shared_attempt_once_and_is_idempotent():
    shared_attempt_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    slots = (pdf_recovery.extraction_slot_scope(1), pdf_recovery.extraction_slot_scope(2))
    for scope in slots:
        _failed_slot_probe(scope, attempt_id=shared_attempt_id)

    escalated = pdf_recovery.escalate_correlated_recovery(
        slots,
        target_scope=pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="process_exit",
        correlation_id=correlation_id,
        occurred_at=NOW + timedelta(seconds=5),
        pause_after_attempts=2,
        jitter=_middle_jitter,
    )

    assert escalated is not None
    assert escalated.transferred_attempt_ids == (shared_attempt_id,)
    owner = escalated.transition.recovery
    assert owner.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert owner.consecutive_failed_attempts == 1
    assert owner.lifetime_attempts == 1
    assert (
        owner.events.filter(
            kind=PDFPipelineRecoveryEventKind.ATTEMPT_FAILED,
            attempt_id=shared_attempt_id,
            correlation_id=correlation_id,
        ).count()
        == 1
    )
    assert set(
        PDFPipelineRecovery.objects.filter(scope__in=slots).values_list("state", flat=True)
    ) == {PDFPipelineRecoveryState.HEALTHY}
    assert (
        PDFPipelineRecoveryEvent.objects.filter(
            recovery__scope__in=slots,
            kind=PDFPipelineRecoveryEventKind.SUPERSEDED,
            correlation_id=correlation_id,
        ).count()
        == 2
    )

    duplicate = pdf_recovery.escalate_correlated_recovery(
        slots,
        target_scope=pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="process_exit",
        correlation_id=correlation_id,
        occurred_at=NOW + timedelta(seconds=6),
        pause_after_attempts=2,
    )
    assert duplicate is not None
    assert duplicate.transition.duplicate
    owner.refresh_from_db()
    assert owner.consecutive_failed_attempts == 1
    assert owner.lifetime_attempts == 1


def test_scope_escalation_opens_owner_at_distinct_probe_threshold():
    slots = (pdf_recovery.extraction_slot_scope(3), pdf_recovery.extraction_slot_scope(4))
    for scope in slots:
        _failed_slot_probe(scope, attempt_id=uuid.uuid4())

    escalated = pdf_recovery.escalate_correlated_recovery(
        slots,
        target_scope=pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="process_exit",
        occurred_at=NOW + timedelta(seconds=5),
        pause_after_attempts=2,
        jitter=_middle_jitter,
    )

    assert escalated is not None
    owner = escalated.transition.recovery
    assert owner.state == PDFPipelineRecoveryState.PAUSED
    assert owner.consecutive_failed_attempts == 2
    assert owner.lifetime_attempts == 2
    assert owner.pause_generation == 1


def test_scope_escalation_without_probes_moves_incidents_without_inventing_attempts():
    slots = (pdf_recovery.extraction_slot_scope(5), pdf_recovery.extraction_slot_scope(6))
    for scope in slots:
        pdf_recovery.record_recovery_incident(
            scope,
            reason_code="stale_heartbeat",
            incident_id=uuid.uuid4(),
            occurred_at=NOW,
            jitter=_middle_jitter,
        )

    escalated = pdf_recovery.escalate_correlated_recovery(
        slots,
        target_scope=pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="stale_heartbeat",
        occurred_at=NOW + timedelta(seconds=1),
        jitter=_middle_jitter,
    )

    assert escalated is not None
    assert escalated.transferred_attempt_ids == ()
    assert escalated.transition.recovery.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert escalated.transition.recovery.consecutive_failed_attempts == 0
    assert escalated.transition.recovery.lifetime_attempts == 0


def test_zero_threshold_pauses_on_detection_without_inventing_an_attempt(
    isolated_recovery_state,
):
    paused = _incident(pause_after_attempts=0)

    assert paused.recovery.state == PDFPipelineRecoveryState.PAUSED
    assert paused.recovery.pause_after_attempts == 0
    assert paused.recovery.lifetime_attempts == 0
    assert paused.recovery.consecutive_failed_attempts == 0
    assert paused.recovery.next_retry_at is None
    assert "threshold is zero" in paused.recovery.paused_reason
    isolated_recovery_state.assert_called_once_with(paused.recovery, mark_unread=True)


def test_immediate_safety_pause_does_not_burn_or_replace_existing_attempt_threshold():
    pdf_recovery.ensure_recovery_scope(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        pause_after_attempts=3,
    )

    paused = pdf_recovery.pause_recovery(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="data_integrity",
        occurred_at=NOW,
    )

    assert paused.recovery.state == PDFPipelineRecoveryState.PAUSED
    assert paused.recovery.pause_after_attempts == 3
    assert paused.recovery.lifetime_attempts == 0
    assert paused.recovery.consecutive_failed_attempts == 0
    assert "paused immediately for safety" in paused.recovery.paused_reason.lower()

    another_hazard = pdf_recovery.pause_recovery(
        pdf_recovery.RecoveryScope.EXTRACTION_POOL,
        reason_code="critical_disk",
        incident_id=uuid.uuid4(),
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert not another_hazard.changed
    assert another_hazard.recovery.pause_generation == paused.recovery.pause_generation


def test_unknown_private_reason_is_never_persisted_verbatim():
    private_value = "/Users/person/private/repository.pdf token=secret"
    paused = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.PIPELINE,
        reason_code=private_value,
        incident_id=uuid.uuid4(),
        occurred_at=NOW,
    ).transition

    assert paused.recovery.reason_code == "unknown_component_failure"
    assert private_value not in paused.recovery.last_outcome
    assert private_value not in paused.recovery.paused_reason
    assert all(private_value not in event.outcome for event in paused.recovery.events.all())


def test_resume_is_fail_closed_generation_safe_and_exactly_idempotent(
    isolated_recovery_state,
):
    paused = _incident(pause_after_attempts=0).recovery
    original_generation = paused.generation
    key = "resume-key-00000001"

    with pytest.raises(pdf_recovery.RecoveryResumeBlocked) as blocked:
        pdf_recovery.request_recovery_resume(
            paused.scope,
            episode_id=paused.episode_id,
            expected_generation=original_generation,
            pause_generation=paused.pause_generation,
            idempotency_key=key,
            safety_check=lambda _payload: pdf_recovery.ResumeSafetyResult.blocked("disk_low"),
            occurred_at=NOW + timedelta(seconds=1),
        )
    assert blocked.value.reason_code == "disk_low"
    paused.refresh_from_db()
    assert paused.generation == original_generation
    assert paused.state == PDFPipelineRecoveryState.PAUSED

    requested = pdf_recovery.request_recovery_resume(
        paused.scope,
        episode_id=paused.episode_id,
        expected_generation=original_generation,
        pause_generation=paused.pause_generation,
        idempotency_key=key,
        safety_check=lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert requested.recovery.state == PDFPipelineRecoveryState.RESUME_REQUESTED
    assert requested.recovery.generation == original_generation + 1
    assert requested.recovery.resume_idempotency_key_hash != key
    assert len(requested.recovery.resume_idempotency_key_hash) == 64

    duplicate = pdf_recovery.request_recovery_resume(
        paused.scope,
        episode_id=paused.episode_id,
        expected_generation=original_generation,
        pause_generation=paused.pause_generation,
        idempotency_key=key,
        safety_check=lambda _payload: pytest.fail("duplicate resume re-ran its safety check"),
        occurred_at=NOW + timedelta(seconds=3),
    )
    assert duplicate.duplicate
    assert duplicate.recovery.generation == requested.recovery.generation

    with pytest.raises(pdf_recovery.RecoveryConflict):
        pdf_recovery.request_recovery_resume(
            paused.scope,
            episode_id=paused.episode_id,
            expected_generation=original_generation,
            pause_generation=paused.pause_generation,
            idempotency_key="different-key-000001",
            safety_check=lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
            occurred_at=NOW + timedelta(seconds=4),
        )
    assert [call.kwargs["mark_unread"] for call in isolated_recovery_state.call_args_list] == [
        True,
        False,
    ]


def test_half_open_failure_reopens_same_episode_without_double_counting_attempt():
    paused = _incident(pause_after_attempts=2).recovery
    # Open the normal circuit at 2 of 2 first.
    for offset in range(2):
        started = pdf_recovery.begin_recovery_attempt(
            paused.scope,
            expected_generation=paused.generation,
            occurred_at=paused.next_retry_at,
        )
        paused = pdf_recovery.fail_recovery_attempt(
            paused.scope,
            attempt_id=started.recovery.active_attempt_id,
            expected_generation=started.recovery.generation,
            occurred_at=started.recovery.last_attempt_at + timedelta(seconds=offset + 1),
            jitter=_middle_jitter,
        ).recovery
    episode_id = paused.episode_id
    requested = pdf_recovery.request_recovery_resume(
        paused.scope,
        episode_id=episode_id,
        expected_generation=paused.generation,
        pause_generation=paused.pause_generation,
        idempotency_key="half-open-key-000001",
        safety_check=lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
        occurred_at=paused.paused_at + timedelta(seconds=1),
    )
    started = pdf_recovery.begin_recovery_attempt(
        paused.scope,
        expected_generation=requested.recovery.generation,
        occurred_at=requested.recovery.resume_requested_at + timedelta(seconds=1),
    )
    assert started.recovery.state == PDFPipelineRecoveryState.RECOVERING_HALF_OPEN
    assert started.recovery.lifetime_attempts == 3

    reopened = pdf_recovery.fail_recovery_attempt(
        paused.scope,
        attempt_id=started.recovery.active_attempt_id,
        expected_generation=started.recovery.generation,
        occurred_at=started.recovery.last_attempt_at + timedelta(seconds=1),
    )

    assert reopened.recovery.state == PDFPipelineRecoveryState.PAUSED
    assert reopened.recovery.episode_id == episode_id
    assert reopened.recovery.pause_generation == paused.pause_generation + 1
    assert reopened.recovery.consecutive_failed_attempts == 2
    assert reopened.recovery.lifetime_attempts == 3
    assert "controlled recovery probe failed" in reopened.recovery.paused_reason

    duplicate = pdf_recovery.fail_recovery_attempt(
        paused.scope,
        attempt_id=started.recovery.active_attempt_id,
        expected_generation=started.recovery.generation,
        occurred_at=started.recovery.last_attempt_at + timedelta(seconds=2),
    )
    assert duplicate.duplicate
    assert duplicate.recovery.lifetime_attempts == 3


def test_success_requires_stability_then_resets_only_consecutive_streak():
    opened = _incident(pause_after_attempts=2)
    started = _begin_due(opened)
    attempt_id = started.recovery.active_attempt_id

    with pytest.raises(pdf_recovery.RecoveryTransitionRejected):
        pdf_recovery.succeed_recovery_attempt(
            opened.recovery.scope,
            attempt_id=attempt_id,
            stability_confirmed=False,
            expected_generation=started.recovery.generation,
            occurred_at=started.recovery.last_attempt_at + timedelta(seconds=59),
        )

    recovered = pdf_recovery.succeed_recovery_attempt(
        opened.recovery.scope,
        attempt_id=attempt_id,
        stability_confirmed=True,
        expected_generation=started.recovery.generation,
        occurred_at=started.recovery.last_attempt_at + timedelta(seconds=60),
    )

    assert recovered.recovery.state == PDFPipelineRecoveryState.HEALTHY
    assert recovered.recovery.consecutive_failed_attempts == 0
    assert recovered.recovery.lifetime_attempts == 1
    assert recovered.recovery.active_attempt_id is None
    assert recovered.payload["episodeId"] is None

    duplicate = pdf_recovery.succeed_recovery_attempt(
        opened.recovery.scope,
        attempt_id=attempt_id,
        stability_confirmed=True,
        expected_generation=started.recovery.generation,
        occurred_at=started.recovery.last_attempt_at + timedelta(seconds=61),
    )
    assert duplicate.duplicate
    assert duplicate.recovery.lifetime_attempts == 1


def test_stale_generation_and_cross_scope_incident_reuse_are_rejected():
    incident_id = uuid.uuid4()
    opened = pdf_recovery.record_recovery_incident(
        pdf_recovery.RecoveryScope.PUBLISHER,
        reason_code="process_exit",
        incident_id=incident_id,
        occurred_at=NOW,
        jitter=_middle_jitter,
    ).transition

    with pytest.raises(pdf_recovery.RecoveryConflict):
        pdf_recovery.begin_recovery_attempt(
            opened.recovery.scope,
            expected_generation=0,
            occurred_at=opened.recovery.next_retry_at,
        )
    with pytest.raises(pdf_recovery.RecoveryConflict):
        pdf_recovery.record_recovery_incident(
            pdf_recovery.RecoveryScope.SUPERVISOR,
            reason_code="process_exit",
            incident_id=incident_id,
            occurred_at=NOW,
        )


def test_notification_failure_never_rolls_back_canonical_pause(monkeypatch):
    monkeypatch.setattr(
        pdf_recovery,
        "publish_recovery_notification",
        Mock(side_effect=RuntimeError("synthetic notification failure")),
    )

    result = _incident(pause_after_attempts=0)

    persisted = PDFPipelineRecovery.objects.get(pk=result.recovery.pk)
    assert persisted.state == PDFPipelineRecoveryState.PAUSED
    assert persisted.generation == 1
