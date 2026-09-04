from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from bookmark_manager.models import (
    BookmarkRefreshSchedule,
    Notification,
    NotificationKind,
    NotificationState,
)
from bookmark_manager.services.notifications import publish_notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def test_notification_status_is_read_only_and_includes_exact_schedule_state(loopback_client):
    occurred_at = datetime(2026, 8, 29, 14, 30, 45, tzinfo=UTC)
    publish_notification(
        event_key="bookmark-export:status-test",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="Export ready",
        occurred_at=occurred_at,
    )
    next_run = occurred_at + timedelta(days=7)
    BookmarkRefreshSchedule.objects.create(next_run_at=next_run)

    response = loopback_client.get(reverse("bookmark_manager:notifications"))

    assert response.status_code == 200
    assert Notification.objects.count() == 1
    payload = response.json()
    assert payload["unread_count"] == 1
    assert payload["notifications"][0]["occurredAt"] == occurred_at.isoformat()
    assert payload["schedule"]["next_run_at"] == next_run.isoformat()
    assert payload["schedule"]["retrying"] is False


def test_notification_read_actions_update_unread_badge(loopback_client):
    first, _ = publish_notification(
        event_key="bookmark-export:read-1",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="First export",
    )
    publish_notification(
        event_key="bookmark-export:read-2",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="Second export",
    )

    one = loopback_client.post(
        reverse("bookmark_manager:notifications_read"),
        {"notification_id": first.pk},
    )
    all_read = loopback_client.post(reverse("bookmark_manager:notifications_read_all"))

    assert one.status_code == 200
    assert one.json()["unread_count"] == 1
    assert all_read.status_code == 200
    assert all_read.json()["marked"] == 1
    assert Notification.objects.filter(read_at__isnull=True).count() == 0


def test_notification_state_changes_require_csrf():
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )

    assert csrf_client.post(reverse("bookmark_manager:notifications_read_all")).status_code == 403
    assert csrf_client.post(reverse("bookmark_manager:refresh_schedule_tick")).status_code == 403


def test_export_post_downloads_backup_and_publishes_notification(loopback_client):
    response = loopback_client.post(reverse("bookmark_manager:export"))

    assert response.status_code == 200
    assert json.loads(response.content)["document_type"] == "owl.bookmark-export"
    notification = Notification.objects.get(kind=NotificationKind.BOOKMARK_EXPORT)
    assert notification.state == NotificationState.SUCCESS
    assert notification.read_at is None


def test_import_completion_publishes_warning_card_with_result_link(loopback_client):
    upload = SimpleUploadedFile(
        "mixed.json",
        json.dumps(["not a bookmark record"]).encode(),
        content_type="application/json",
    )

    response = loopback_client.post(
        reverse("bookmark_manager:import"),
        {"import_file": upload},
    )

    assert response.status_code == 302
    notification = Notification.objects.get(kind=NotificationKind.BOOKMARK_IMPORT)
    assert notification.state == NotificationState.WARNING
    assert notification.target_path.startswith("/bookmarks/?import_run=")
