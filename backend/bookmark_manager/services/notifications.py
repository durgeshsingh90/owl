"""Durable, compact notification-center state for local bookmark operations."""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from bookmark_manager.models import Notification, NotificationState

DEFAULT_NOTIFICATION_LIMIT = 20
MAX_NOTIFICATION_LIMIT = 100
TERMINAL_NOTIFICATION_STATES = frozenset(
    {
        NotificationState.SUCCESS,
        NotificationState.WARNING,
        NotificationState.ERROR,
    }
)


def _aware_timestamp(value: datetime | None, *, label: str) -> datetime:
    timestamp = value or timezone.now()
    if not isinstance(timestamp, datetime) or timezone.is_naive(timestamp):
        raise ValueError(f"{label} must include a timezone.")
    return timestamp


def _notification_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("The notification limit must be an integer.")
    if not 1 <= value <= MAX_NOTIFICATION_LIMIT:
        raise ValueError(f"The notification limit must be between 1 and {MAX_NOTIFICATION_LIMIT}.")
    return value


def publish_notification(
    *,
    event_key: str,
    kind: str,
    state: str,
    title: str,
    message: str = "",
    target_path: str = "",
    occurred_at: datetime | None = None,
    mark_unread: bool | None = None,
) -> tuple[Notification, bool]:
    """Create or update one event without duplicating cards for the same operation.

    New events are unread. By default, a meaningful transition into a terminal state
    marks an already-read running card unread again, while repeated publication of the
    same terminal state preserves the reader's choice. Callers may override that rule
    explicitly with ``mark_unread``.
    """

    if mark_unread is not None and not isinstance(mark_unread, bool):
        raise ValueError("mark_unread must be a boolean when provided.")
    event_time = _aware_timestamp(occurred_at, label="The notification occurrence time")
    canonical_event_key = str(event_key or "").strip().casefold()
    with transaction.atomic():
        notification, created = Notification.objects.select_for_update().get_or_create(
            event_key=canonical_event_key,
            defaults={
                "kind": kind,
                "state": state,
                "title": title,
                "message": message,
                "target_path": target_path,
                "occurred_at": event_time,
            },
        )
        if created:
            return notification, True

        previous_state = notification.state
        notification.kind = kind
        notification.state = state
        notification.title = title
        notification.message = message
        notification.target_path = target_path
        notification.occurred_at = event_time
        should_mark_unread = (
            mark_unread
            if mark_unread is not None
            else state in TERMINAL_NOTIFICATION_STATES and state != previous_state
        )
        if should_mark_unread:
            notification.read_at = None
        notification.save(
            update_fields=(
                "kind",
                "state",
                "title",
                "message",
                "target_path",
                "occurred_at",
                "read_at",
            )
        )
        return notification, False


def _notification_payload(notification: Notification) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": notification.pk,
        "kind": notification.kind,
        "kindLabel": notification.get_kind_display(),
        "state": notification.state,
        "stateLabel": notification.get_state_display(),
        "title": notification.title,
        "message": notification.message,
        "targetPath": notification.target_path,
        "occurredAt": notification.occurred_at.isoformat(),
        "read": notification.read_at is not None,
    }
    return payload


def list_notification_payloads(
    *,
    limit: int = DEFAULT_NOTIFICATION_LIMIT,
    unread_only: bool = False,
) -> tuple[dict[str, object], ...]:
    """Return newest-first, bounded notification cards with no internal event keys."""

    if not isinstance(unread_only, bool):
        raise ValueError("unread_only must be a boolean.")
    queryset = Notification.objects.all()
    if unread_only:
        queryset = queryset.filter(read_at__isnull=True)
    notifications = queryset.only(
        "id",
        "event_key",
        "kind",
        "state",
        "title",
        "message",
        "target_path",
        "occurred_at",
        "read_at",
    )[: _notification_limit(limit)]
    return tuple(_notification_payload(notification) for notification in notifications)


def unread_notification_count() -> int:
    return Notification.objects.filter(read_at__isnull=True).count()


def mark_notification_read(
    notification_id: int,
    *,
    read_at: datetime | None = None,
) -> bool:
    """Mark one unread card read and report whether its state changed."""

    if isinstance(notification_id, bool) or not isinstance(notification_id, int):
        raise ValueError("A notification ID must be a positive integer.")
    if notification_id < 1:
        raise ValueError("A notification ID must be a positive integer.")
    timestamp = _aware_timestamp(read_at, label="The notification read time")
    return bool(
        Notification.objects.filter(pk=notification_id, read_at__isnull=True).update(
            read_at=timestamp,
            updated_at=timestamp,
        )
    )


def mark_all_notifications_read(*, read_at: datetime | None = None) -> int:
    """Mark every currently unread card read and return the affected count."""

    timestamp = _aware_timestamp(read_at, label="The notification read time")
    return Notification.objects.filter(read_at__isnull=True).update(
        read_at=timestamp,
        updated_at=timestamp,
    )
