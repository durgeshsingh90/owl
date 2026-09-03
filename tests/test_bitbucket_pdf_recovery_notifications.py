from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from bitbucket_search.models import PDFPipelineRecovery, PDFPipelineRecoveryState
from bitbucket_search.services import pdf_recovery
from bookmark_manager.models import Notification, NotificationKind
from bookmark_manager.services.notifications import (
    list_notification_payloads,
    mark_notification_read,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_recovery_state(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pdf-pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 25
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 16
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0.0
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 60


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.fixture
def csrf_loopback_client():
    client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    response = client.get(reverse("core:dashboard"))
    assert response.status_code == 200
    assert "csrftoken" in client.cookies
    return client


def _pause(*, scope: str = "publisher", at: datetime = NOW):
    transition = pdf_recovery.record_recovery_incident(
        scope,
        reason_code="process_exit",
        incident_id=uuid.uuid4(),
        occurred_at=at,
        pause_after_attempts=0,
        jitter=lambda low, _high: low,
    ).transition
    assert transition is not None
    assert transition.recovery.state == PDFPipelineRecoveryState.PAUSED
    return transition.recovery


def _action_form(action: dict[str, object]) -> dict[str, object]:
    return {
        "scope": action["scope"],
        "episodeId": action["episodeId"],
        "expectedGeneration": action["expectedGeneration"],
        "pauseGeneration": action["pauseGeneration"],
        **({"idempotencyKey": action["idempotencyKey"]} if "idempotencyKey" in action else {}),
    }


def _csrf_post(client: Client, url: str, data: dict[str, object], **extra):
    return client.post(
        url,
        data,
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        **extra,
    )


def test_recovery_notification_has_dedicated_kind_and_only_server_action():
    recovery = _pause()

    notification = Notification.objects.get()
    assert notification.kind == NotificationKind.PDF_PIPELINE_RECOVERY
    assert notification.event_key == f"pdf-pipeline-recovery:{recovery.episode_id}"
    payload = list_notification_payloads()[0]
    assert payload["kind"] == "pdf_pipeline_recovery"
    assert payload["targetPath"] == "/pdfs/status/"
    assert "eventKey" not in payload and "event_key" not in payload
    assert payload["action"] == pdf_recovery.recovery_resume_action(recovery)
    assert payload["action"]["url"] == reverse("bitbucket_search:pdf_pipeline_recovery_resume")
    assert payload["action"]["method"] == "POST"
    assert len(payload["action"]["idempotencyKey"]) == 64


def test_popup_and_notification_never_emit_stored_exception_or_path_text():
    private_text = "/Users/private/repository.pdf token=do-not-render"
    recovery = PDFPipelineRecovery.objects.create(
        scope="pipeline",
        state=PDFPipelineRecoveryState.PAUSED,
        pause_generation=1,
        reason_family="private family",
        reason_code=private_text[:64],
        paused_reason=private_text,
        last_outcome=private_text,
        paused_at=NOW,
    )

    popup = pdf_recovery.pending_recovery_popup_payload()
    pdf_recovery.publish_recovery_notification(recovery, mark_unread=True)
    serialized = str(popup) + str(list_notification_payloads())

    assert private_text not in serialized
    assert "/Users/private" not in serialized
    assert popup["reasonCode"] == "unknown_component_failure"


def test_default_resume_preflight_rechecks_resources_and_fails_closed(monkeypatch):
    from bitbucket_search.services import pdf_pipeline_metrics

    payload = {"scope": "publisher", "reasonCode": "process_exit"}
    monkeypatch.setattr(
        pdf_pipeline_metrics,
        "resource_snapshot",
        lambda: {
            "diskAvailableBytes": 10_000_000_000,
            "hostMemoryAvailableBytes": 10_000_000_000,
        },
    )
    assert pdf_recovery.recovery_resume_preflight(payload).state == "safe"

    monkeypatch.setattr(
        pdf_pipeline_metrics,
        "resource_snapshot",
        lambda: {
            "diskAvailableBytes": 1,
            "hostMemoryAvailableBytes": 10_000_000_000,
        },
    )
    disk_blocked = pdf_recovery.recovery_resume_preflight(payload)
    assert disk_blocked.state == "blocked"
    assert disk_blocked.reason_code == "disk_space_still_unsafe"

    monkeypatch.setattr(
        pdf_pipeline_metrics,
        "resource_snapshot",
        lambda: {
            "diskAvailableBytes": 10_000_000_000,
            "hostMemoryAvailableBytes": 10_000_000_000,
        },
    )
    integrity_blocked = pdf_recovery.recovery_resume_preflight(
        {"scope": "pipeline", "reasonCode": "data_integrity"}
    )
    assert integrity_blocked.state == "blocked"
    assert integrity_blocked.reason_code == "integrity_check_required"


def test_republishing_same_pause_preserves_read_but_reopen_marks_unread():
    recovery = _pause()
    notification = Notification.objects.get()
    assert mark_notification_read(notification.pk)

    pdf_recovery.publish_recovery_notification(recovery, mark_unread=True)
    notification.refresh_from_db()
    assert notification.read_at is not None

    requested = pdf_recovery.request_recovery_resume(
        recovery.scope,
        episode_id=recovery.episode_id,
        expected_generation=recovery.generation,
        pause_generation=recovery.pause_generation,
        idempotency_key="direct-test-resume-key",
        safety_check=lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
        occurred_at=NOW + timedelta(seconds=1),
    )
    notification.refresh_from_db()
    assert notification.state == "running"
    assert notification.read_at is not None
    started = pdf_recovery.begin_recovery_attempt(
        recovery.scope,
        expected_generation=requested.recovery.generation,
        occurred_at=NOW + timedelta(seconds=2),
    )
    reopened = pdf_recovery.fail_recovery_attempt(
        recovery.scope,
        attempt_id=started.recovery.active_attempt_id,
        expected_generation=started.recovery.generation,
        occurred_at=NOW + timedelta(seconds=3),
    )

    notification.refresh_from_db()
    assert reopened.recovery.episode_id == recovery.episode_id
    assert reopened.recovery.pause_generation == 2
    assert notification.read_at is None
    assert Notification.objects.count() == 1


def test_notification_get_is_read_only_and_exposes_unclaimed_popup(loopback_client):
    recovery = _pause()

    response = loopback_client.get(reverse("bookmark_manager:notifications"))

    assert response.status_code == 200
    popup = response.json()["recoveryPopup"]
    recovery.refresh_from_db()
    assert recovery.popup_claimed_generation == 0
    assert popup["scopeLabel"] == "PDF publisher"
    assert popup["attemptSummary"] == "Paused after 0 failed recovery attempts."
    assert popup["detailsPath"] == "/pdfs/status/"
    assert popup["claimAction"]["url"] == reverse(
        "bitbucket_search:pdf_pipeline_recovery_popup_claim"
    )
    assert response.json()["recoveryActive"] is True


def test_shared_shell_mounts_accessible_recovery_dialog(loopback_client):
    response = loopback_client.get(reverse("core:dashboard"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'role="alertdialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="owl-recovery-popup-heading"' in html
    assert "data-notification-recovery-resume" in html
    assert "View pipeline details/logs" in html
    assert "data-notification-recovery-dismiss" in html


def test_popup_claim_and_acknowledgement_are_generation_safe_and_separate_from_read():
    recovery = _pause()
    notification = Notification.objects.get()
    candidate = pdf_recovery.pending_recovery_popup_payload()

    claimed = pdf_recovery.claim_recovery_popup(
        **{
            "scope": candidate["claimAction"]["scope"],
            "episode_id": candidate["claimAction"]["episodeId"],
            "expected_generation": candidate["claimAction"]["expectedGeneration"],
            "pause_generation": candidate["claimAction"]["pauseGeneration"],
        }
    )
    duplicate_claim = pdf_recovery.claim_recovery_popup(
        **{
            "scope": candidate["claimAction"]["scope"],
            "episode_id": candidate["claimAction"]["episodeId"],
            "expected_generation": candidate["claimAction"]["expectedGeneration"],
            "pause_generation": candidate["claimAction"]["pauseGeneration"],
        }
    )

    assert claimed.changed is True
    assert duplicate_claim.duplicate is True
    assert pdf_recovery.pending_recovery_popup_payload() is None
    popup = pdf_recovery.recovery_popup_payload(claimed.recovery, claimed=True)
    acknowledged = pdf_recovery.acknowledge_recovery_popup(
        popup["acknowledgeAction"]["scope"],
        episode_id=popup["acknowledgeAction"]["episodeId"],
        expected_generation=popup["acknowledgeAction"]["expectedGeneration"],
        pause_generation=popup["acknowledgeAction"]["pauseGeneration"],
    )
    duplicate_ack = pdf_recovery.acknowledge_recovery_popup(
        popup["acknowledgeAction"]["scope"],
        episode_id=popup["acknowledgeAction"]["episodeId"],
        expected_generation=popup["acknowledgeAction"]["expectedGeneration"],
        pause_generation=popup["acknowledgeAction"]["pauseGeneration"],
    )
    notification.refresh_from_db()

    assert acknowledged.recovery.popup_acknowledged_generation == recovery.pause_generation
    assert duplicate_ack.duplicate is True
    assert notification.read_at is None
    assert pdf_recovery.recovery_notification_poll_active() is False


def test_popup_http_claim_is_single_winner_and_ack_is_idempotent(loopback_client):
    _pause()
    candidate = loopback_client.get(reverse("bookmark_manager:notifications")).json()[
        "recoveryPopup"
    ]
    claim_url = reverse("bitbucket_search:pdf_pipeline_recovery_popup_claim")

    first = loopback_client.post(claim_url, _action_form(candidate["claimAction"]))
    second = loopback_client.post(claim_url, _action_form(candidate["claimAction"]))

    assert first.status_code == 200
    assert first.json()["claimed"] is True
    assert second.status_code == 200
    assert second.json() == {"state": "already_claimed", "claimed": False, "popup": None}
    popup = first.json()["popup"]
    ack_url = reverse("bitbucket_search:pdf_pipeline_recovery_popup_acknowledge")
    acknowledged = loopback_client.post(
        ack_url,
        _action_form(popup["acknowledgeAction"]),
    )
    duplicate = loopback_client.post(
        ack_url,
        _action_form(popup["acknowledgeAction"]),
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True


def test_recovery_mutations_require_post_csrf_loopback_peer_and_loopback_host(
    csrf_loopback_client,
    settings,
):
    recovery = _pause()
    action = pdf_recovery.recovery_resume_action(recovery)
    url = action["url"]

    assert csrf_loopback_client.get(url).status_code == 405
    missing_csrf = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert missing_csrf.post(url, _action_form(action)).status_code == 403

    settings.OWL_ALLOW_NON_LOOPBACK = True
    remote = Client(enforce_csrf_checks=False, HTTP_HOST="127.0.0.1", REMOTE_ADDR="10.0.0.8")
    assert remote.post(url, _action_form(action)).status_code == 403
    with override_settings(ALLOWED_HOSTS=["127.0.0.1", "example.test"]):
        spoofed = Client(
            enforce_csrf_checks=False,
            HTTP_HOST="example.test",
            REMOTE_ADDR="127.0.0.1",
        )
        assert spoofed.post(url, _action_form(action)).status_code == 403


def test_opaque_origin_still_requires_csrf_token(csrf_loopback_client, monkeypatch):
    recovery = _pause()
    action = pdf_recovery.recovery_resume_action(recovery)
    monkeypatch.setattr(
        pdf_recovery,
        "recovery_resume_preflight",
        lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
    )

    accepted = _csrf_post(
        csrf_loopback_client,
        action["url"],
        _action_form(action),
        HTTP_ORIGIN="null",
    )
    missing = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    ).post(action["url"], _action_form(action), HTTP_ORIGIN="null")

    assert accepted.status_code == 202
    assert missing.status_code == 403


def test_resume_http_is_blocked_then_exactly_idempotent(csrf_loopback_client, monkeypatch):
    recovery = _pause()
    action = pdf_recovery.recovery_resume_action(recovery)
    monkeypatch.setattr(
        pdf_recovery,
        "recovery_resume_preflight",
        lambda _payload: pdf_recovery.ResumeSafetyResult.blocked("disk_space_still_unsafe"),
    )

    blocked = _csrf_post(csrf_loopback_client, action["url"], _action_form(action))
    recovery.refresh_from_db()
    assert blocked.status_code == 409
    assert blocked.json()["reasonCode"] == "disk_space_still_unsafe"
    assert recovery.state == PDFPipelineRecoveryState.PAUSED
    assert recovery.generation == action["expectedGeneration"]

    monkeypatch.setattr(
        pdf_recovery,
        "recovery_resume_preflight",
        lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
    )
    accepted = _csrf_post(csrf_loopback_client, action["url"], _action_form(action))
    duplicate = _csrf_post(csrf_loopback_client, action["url"], _action_form(action))

    assert accepted.status_code == 202
    assert accepted.json()["state"] == "resume_requested"
    assert accepted.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    recovery.refresh_from_db()
    assert recovery.popup_acknowledged_generation == recovery.pause_generation


def test_resume_rejects_forged_key_and_stale_generation_without_state_change(
    loopback_client,
    monkeypatch,
):
    recovery = _pause()
    action = pdf_recovery.recovery_resume_action(recovery)
    monkeypatch.setattr(
        pdf_recovery,
        "recovery_resume_preflight",
        lambda _payload: pdf_recovery.ResumeSafetyResult.safe(),
    )
    forged = dict(action)
    forged["idempotencyKey"] = "0" * 64
    stale = dict(action)
    stale["expectedGeneration"] += 1

    forged_response = loopback_client.post(action["url"], _action_form(forged))
    stale_response = loopback_client.post(action["url"], _action_form(stale))
    recovery.refresh_from_db()

    assert forged_response.status_code == 409
    assert stale_response.status_code == 409
    assert recovery.state == PDFPipelineRecoveryState.PAUSED
    assert recovery.generation == action["expectedGeneration"]
