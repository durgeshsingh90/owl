from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command

from bookmark_manager.management.commands import run_owl
from bookmark_manager.models import (
    BookmarkRefreshFailure,
    BookmarkRefreshRun,
    BookmarkRefreshSchedule,
    BookmarkRefreshStatus,
    BookmarkRefreshTrigger,
    Notification,
)
from bookmark_manager.services import bookmark_refresh
from bookmark_manager.services.bookmark_domain import ConfluencePageSnapshot, upsert_bookmark

pytestmark = pytest.mark.django_db(transaction=True)


def _completed_run(*, status: str, completed_at: datetime) -> BookmarkRefreshRun:
    return BookmarkRefreshRun.objects.create(
        status=status,
        trigger=BookmarkRefreshTrigger.SCHEDULED,
        total_bookmarks=1,
        processed_bookmarks=1,
        succeeded_bookmarks=0 if status != BookmarkRefreshStatus.SUCCEEDED else 1,
        failed_bookmarks=0 if status == BookmarkRefreshStatus.SUCCEEDED else 1,
        requested_at=completed_at - timedelta(minutes=2),
        started_at=completed_at - timedelta(minutes=1),
        heartbeat_at=completed_at,
        completed_at=completed_at,
        last_error_message="Temporary network failure"
        if status == BookmarkRefreshStatus.FAILED
        else "",
    )


def _bookmark(page_id: str):
    return upsert_bookmark(
        ConfluencePageSnapshot(
            page_id=page_id,
            title=f"Page {page_id}",
            url=f"https://confluence.example.invalid/spaces/ENG/pages/{page_id}/Page",
            space_name="Engineering",
            space_key="ENG",
            version=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            created_by_name="Writer",
            modified_by_name="Editor",
            author_name="Writer",
            page_text="Searchable text",
            ancestors=(),
        )
    ).bookmark


def test_schedule_starts_one_week_from_initialization(settings):
    settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
    at = datetime(2026, 8, 29, 9, 15, tzinfo=UTC)

    schedule = bookmark_refresh.get_refresh_schedule(at=at)

    assert schedule.next_run_at == at + timedelta(days=7)
    assert schedule.consecutive_failures == 0


def test_failed_run_retries_after_two_hours_then_success_returns_to_weekly(settings):
    settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
    settings.CONFLUENCE_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    failed_at = datetime(2026, 8, 29, 10, tzinfo=UTC)
    failed = _completed_run(status=BookmarkRefreshStatus.FAILED, completed_at=failed_at)

    retry_schedule = bookmark_refresh._update_refresh_schedule(failed, at=failed_at)

    assert retry_schedule.next_run_at == failed_at + timedelta(hours=2)
    assert retry_schedule.consecutive_failures == 1
    assert retry_schedule.last_success_at is None

    succeeded_at = failed_at + timedelta(hours=2, minutes=5)
    succeeded = _completed_run(
        status=BookmarkRefreshStatus.SUCCEEDED,
        completed_at=succeeded_at,
    )
    weekly_schedule = bookmark_refresh._update_refresh_schedule(succeeded, at=succeeded_at)

    assert weekly_schedule.next_run_at == succeeded_at + timedelta(days=7)
    assert weekly_schedule.consecutive_failures == 0
    assert weekly_schedule.last_success_at == succeeded_at


def test_partial_run_retries_only_temporary_failures(settings):
    settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
    settings.CONFLUENCE_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    completed_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    bookmark = _bookmark("991001")
    permanent = _completed_run(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        completed_at=completed_at,
    )
    BookmarkRefreshFailure.objects.create(
        refresh_run=permanent,
        bookmark=bookmark,
        page_id=bookmark.page_id,
        url=bookmark.url,
        error_code="not_found",
        reason="Confluence could not find this page.",
    )

    weekly = bookmark_refresh._update_refresh_schedule(permanent, at=completed_at)
    assert weekly.next_run_at == completed_at + timedelta(days=7)
    assert weekly.consecutive_failures == 0

    temporary_at = completed_at + timedelta(days=7)
    temporary = _completed_run(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        completed_at=temporary_at,
    )
    BookmarkRefreshFailure.objects.create(
        refresh_run=temporary,
        bookmark=bookmark,
        page_id=bookmark.page_id,
        url=bookmark.url,
        error_code="unreachable",
        reason="Confluence could not be reached.",
    )

    retry = bookmark_refresh._update_refresh_schedule(temporary, at=temporary_at)
    assert retry.next_run_at == temporary_at + timedelta(hours=2)
    assert retry.consecutive_failures == 1


def test_credential_failure_retries_in_two_hours_without_marking_success(settings):
    settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
    settings.CONFLUENCE_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    completed_at = datetime(2026, 8, 29, 12, 30, tzinfo=UTC)
    bookmark = _bookmark("991002")
    run = _completed_run(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        completed_at=completed_at,
    )
    BookmarkRefreshFailure.objects.create(
        refresh_run=run,
        bookmark=bookmark,
        page_id=bookmark.page_id,
        url=bookmark.url,
        error_code="invalid_credential",
        reason="Confluence rejected the configured credential.",
    )

    schedule = bookmark_refresh._update_refresh_schedule(run, at=completed_at)

    assert schedule.next_run_at == completed_at + timedelta(hours=2)
    assert schedule.consecutive_failures == 1
    assert schedule.last_success_at is None


def test_schedule_initialization_backfills_existing_refresh_history(settings):
    settings.CONFLUENCE_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
    completed_at = datetime(2026, 8, 28, 9, 45, 12, tzinfo=UTC)
    completed = _completed_run(
        status=BookmarkRefreshStatus.SUCCEEDED,
        completed_at=completed_at,
    )
    observed_at = completed_at + timedelta(hours=1)

    schedule = bookmark_refresh.get_refresh_schedule(at=observed_at)

    assert schedule.last_run_id == completed.pk
    assert schedule.last_attempt_at == completed_at
    assert schedule.last_success_at == completed_at
    assert schedule.next_run_at == completed_at + timedelta(days=7)


def test_due_schedule_queues_one_detached_run_and_publishes_running_card(
    monkeypatch,
    settings,
):
    settings.CONFLUENCE_REFRESH_RETRY_SECONDS = 2 * 60 * 60
    due_at = datetime(2026, 8, 29, 13, tzinfo=UTC)
    BookmarkRefreshSchedule.objects.create(next_run_at=due_at - timedelta(seconds=1))
    launched: list[int] = []
    monkeypatch.setattr(
        bookmark_refresh,
        "launch_refresh_worker",
        lambda run_id: launched.append(run_id),
    )

    run, queued = bookmark_refresh.queue_due_scheduled_refresh(at=due_at)

    assert queued is True
    assert run is not None
    assert run.trigger == BookmarkRefreshTrigger.SCHEDULED
    assert launched == [run.pk]
    assert Notification.objects.get(event_key=f"confluence-refresh:{run.pk}").state == "running"

    same, queued_again = bookmark_refresh.queue_due_scheduled_refresh(at=due_at)
    assert same is None
    assert queued_again is False
    assert BookmarkRefreshRun.objects.count() == 1


def test_scheduler_once_reports_future_schedule(capsys):
    at = datetime.now(tz=UTC)
    BookmarkRefreshSchedule.objects.create(next_run_at=at + timedelta(days=1))

    call_command("bookmark_refresh_scheduler", once=True)

    assert "No refresh is due" in capsys.readouterr().out


def test_resident_scheduler_recovers_and_publishes_notification(monkeypatch):
    stop_event = threading.Event()
    checks = 0

    def check_schedule():
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("synthetic scheduler failure")
        stop_event.set()

    monkeypatch.setattr(run_owl, "queue_due_scheduled_refresh", check_schedule)

    run_owl._scheduler_loop(stop_event, poll_seconds=0)

    assert checks == 2
    notification = Notification.objects.get(event_key=run_owl.SCHEDULER_EVENT_KEY)
    assert notification.state == "success"
    assert notification.title == "Weekly refresh scheduler resumed"
    assert notification.read_at is None
