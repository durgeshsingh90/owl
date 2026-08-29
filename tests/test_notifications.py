from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from bookmark_manager.models import (
    Notification,
    NotificationKind,
    NotificationState,
)
from bookmark_manager.services.notifications import (
    list_notification_payloads,
    mark_all_notifications_read,
    mark_notification_read,
    publish_notification,
    unread_notification_count,
)

pytestmark = pytest.mark.django_db


def test_notification_model_sanitizes_copy_and_rejects_unsafe_identifiers_and_targets():
    notification = Notification.objects.create(
        event_key="  BOOKMARK-IMPORT:17  ",
        kind=NotificationKind.BOOKMARK_IMPORT,
        state=NotificationState.SUCCESS,
        title=" Import\ncompleted ",
        message=" Added\tthree bookmarks ",
        target_path="/bookmarks/?import_run=17",
    )

    assert notification.event_key == "bookmark-import:17"
    assert notification.title == "Import completed"
    assert notification.message == "Added three bookmarks"

    for index, target_path in enumerate(
        (
            "https://evil.example.invalid/",
            "//evil.example.invalid/path",
            r"\evil.example.invalid\path",
            "/bookmarks/\nheader",
        ),
        start=1,
    ):
        with pytest.raises(ValidationError, match="safe local OWL path"):
            Notification.objects.create(
                event_key=f"bookmark-export:unsafe-{index}",
                kind=NotificationKind.BOOKMARK_EXPORT,
                state=NotificationState.ERROR,
                title="Unsafe target",
                target_path=target_path,
            )

    with pytest.raises(ValidationError, match="stable lowercase"):
        Notification.objects.create(
            event_key="contains spaces",
            kind=NotificationKind.BOOKMARK_EXPORT,
            state=NotificationState.ERROR,
            title="Invalid event key",
        )


def test_notification_model_enforces_supported_choices_timestamps_and_unique_event_keys():
    Notification.objects.create(
        event_key="bookmark-export:19",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="Export ready",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(
            event_key="BOOKMARK-EXPORT:19",
            kind=NotificationKind.BOOKMARK_EXPORT,
            state=NotificationState.SUCCESS,
            title="Duplicate export",
        )
    with pytest.raises(ValidationError, match="kind is not supported"):
        Notification.objects.create(
            event_key="unsupported-kind:1",
            kind="repository_sync",
            state=NotificationState.SUCCESS,
            title="Unsupported",
        )
    with pytest.raises(ValidationError, match="state is not supported"):
        Notification.objects.create(
            event_key="unsupported-state:1",
            kind=NotificationKind.CONFLUENCE_REFRESH,
            state="completed",
            title="Unsupported",
        )
    with pytest.raises(ValidationError, match="include a timezone"):
        Notification.objects.create(
            event_key="naive-time:1",
            kind=NotificationKind.CONFLUENCE_REFRESH,
            state=NotificationState.RUNNING,
            title="Refresh running",
            occurred_at=datetime(2026, 8, 29, 12, 0),
        )
    with pytest.raises(ValueError, match="mark_unread"):
        publish_notification(
            event_key="invalid-unread-flag:1",
            kind=NotificationKind.BOOKMARK_EXPORT,
            state=NotificationState.SUCCESS,
            title="Invalid unread flag",
            mark_unread=1,
        )


def test_publish_is_idempotent_and_terminal_transition_marks_a_read_card_unread_again():
    started_at = datetime(2026, 8, 29, 9, tzinfo=UTC)
    completed_at = started_at + timedelta(minutes=4)
    notification, created = publish_notification(
        event_key="confluence-refresh:41",
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=NotificationState.RUNNING,
        title="Refreshing Confluence",
        message="0 of 12 processed",
        target_path="/bookmarks/",
        occurred_at=started_at,
    )

    assert created is True
    assert unread_notification_count() == 1
    assert (
        mark_notification_read(notification.pk, read_at=started_at + timedelta(minutes=1)) is True
    )

    same, created_again = publish_notification(
        event_key="CONFLUENCE-REFRESH:41",
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=NotificationState.RUNNING,
        title="Refreshing Confluence",
        message="6 of 12 processed",
        target_path="/bookmarks/",
        occurred_at=started_at + timedelta(minutes=2),
    )
    same.refresh_from_db()
    assert created_again is False
    assert same.pk == notification.pk
    assert same.read_at is not None
    assert same.message == "6 of 12 processed"

    completed, completion_created = publish_notification(
        event_key="confluence-refresh:41",
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=NotificationState.WARNING,
        title="Refresh completed with issues",
        message="11 refreshed and 1 failed",
        target_path="/bookmarks/",
        occurred_at=completed_at,
    )
    assert completion_created is False
    assert completed.read_at is None
    assert completed.occurred_at == completed_at
    assert Notification.objects.count() == 1
    assert unread_notification_count() == 1

    assert mark_notification_read(completed.pk) is True
    repeated, _ = publish_notification(
        event_key="confluence-refresh:41",
        kind=NotificationKind.CONFLUENCE_REFRESH,
        state=NotificationState.WARNING,
        title="Refresh completed with issues",
        message="11 refreshed and 1 failed",
        target_path="/bookmarks/",
        occurred_at=completed_at,
    )
    repeated.refresh_from_db()
    assert repeated.read_at is not None


def test_compact_payloads_are_newest_first_bounded_and_hide_internal_event_keys():
    first_at = datetime(2026, 8, 29, 8, tzinfo=UTC)
    first, _ = publish_notification(
        event_key="bookmark-import:1",
        kind=NotificationKind.BOOKMARK_IMPORT,
        state=NotificationState.SUCCESS,
        title="Import completed",
        message="Added 3 bookmarks",
        target_path="/bookmarks/?import_run=1",
        occurred_at=first_at,
    )
    publish_notification(
        event_key="bookmark-export:2",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.ERROR,
        title="Export failed",
        message="OWL could not prepare the export safely.",
        occurred_at=first_at + timedelta(hours=1),
    )
    mark_notification_read(first.pk, read_at=first_at + timedelta(minutes=10))

    payloads = list_notification_payloads(limit=2)
    assert [item["title"] for item in payloads] == ["Export failed", "Import completed"]
    assert payloads[0] == {
        "id": payloads[0]["id"],
        "kind": "bookmark_export",
        "kindLabel": "Bookmark export",
        "state": "error",
        "stateLabel": "Error",
        "title": "Export failed",
        "message": "OWL could not prepare the export safely.",
        "targetPath": "",
        "occurredAt": "2026-08-29T09:00:00+00:00",
        "read": False,
    }
    assert all("event_key" not in item and "eventKey" not in item for item in payloads)
    assert [item["title"] for item in list_notification_payloads(unread_only=True)] == [
        "Export failed"
    ]

    for invalid_limit in (0, 101, True, "20"):
        with pytest.raises(ValueError, match="notification limit"):
            list_notification_payloads(limit=invalid_limit)
    with pytest.raises(ValueError, match="must be a boolean"):
        list_notification_payloads(unread_only=1)


def test_mark_one_and_all_read_are_idempotent_and_validate_inputs():
    first, _ = publish_notification(
        event_key="bookmark-import:50",
        kind=NotificationKind.BOOKMARK_IMPORT,
        state=NotificationState.SUCCESS,
        title="Import complete",
    )
    second, _ = publish_notification(
        event_key="bookmark-export:51",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="Export complete",
    )
    read_at = datetime(2026, 8, 29, 14, 30, tzinfo=UTC)

    assert unread_notification_count() == 2
    assert mark_notification_read(first.pk, read_at=read_at) is True
    assert mark_notification_read(first.pk, read_at=read_at) is False
    assert unread_notification_count() == 1
    assert mark_all_notifications_read(read_at=read_at + timedelta(minutes=1)) == 1
    assert mark_all_notifications_read(read_at=read_at + timedelta(minutes=2)) == 0
    assert unread_notification_count() == 0
    second.refresh_from_db()
    assert second.read_at == read_at + timedelta(minutes=1)

    for invalid_id in (0, -1, True, "1"):
        with pytest.raises(ValueError, match="positive integer"):
            mark_notification_read(invalid_id)
    with pytest.raises(ValueError, match="include a timezone"):
        mark_all_notifications_read(read_at=datetime(2026, 8, 29, 14, 30))
