from __future__ import annotations

import logging
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, transaction
from django.utils import timezone

from bookmark_manager.management.commands import (
    bookmark_refresh_scheduler,
    bookmark_refresh_worker,
    run_owl,
)
from bookmark_manager.models import (
    BookmarkRefreshRun,
    BookmarkRefreshSchedule,
    BookmarkRefreshStatus,
)
from bookmark_manager.services import bookmark_refresh as refresh
from bookmark_manager.services.bookmark_domain import ConfluencePageSnapshot, upsert_bookmark
from bookmark_manager.services.configuration import (
    ActiveConfluenceProfile,
    ConfigurationUnavailable,
)
from bookmark_manager.services.confluence_adapter import ConfluenceResult, ConfluenceResultCode
from bookmark_manager.services.confluence_validation import CanonicalOrigin
from bookmark_manager.services.logging_events import log_event

pytestmark = pytest.mark.django_db(transaction=True)
_PRIVATE = "synthetic-private-refresh-value-never-log"


@pytest.fixture
def captured_logs(caplog):
    logger = logging.getLogger("owl.bookmarks")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="owl.bookmarks")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _event(capture, name, level=None):
    records = [
        record for record in capture.records if f"event={name} " in f"{record.getMessage()} "
    ]
    assert records, name
    if level is not None:
        assert all(record.levelno == level for record in records)
    return records


def _private(capture):
    assert _PRIVATE not in capture.text
    assert "https://" not in capture.text
    assert "Traceback" not in capture.text
    assert all(record.exc_info is None and record.stack_info is None for record in capture.records)


def _profile():
    return ActiveConfluenceProfile(
        origin=CanonicalOrigin(
            scheme="https",
            host="refresh.example.invalid",
            port=443,
            context_path="/wiki",
            is_test_target=True,
        ),
        token=_PRIVATE,
        auth_mode="bearer",
        source="environment",
    )


def _bookmark(page_id="80001"):
    return upsert_bookmark(_snapshot(page_id)).bookmark


def _snapshot(page_id):
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=_PRIVATE,
        url=f"https://refresh.example.invalid/wiki/{page_id}/{_PRIVATE}",
        space_name=_PRIVATE,
        space_key="TEST",
        version=2,
        created_at=timezone.now(),
        updated_at=timezone.now(),
        created_by_name=_PRIVATE,
        modified_by_name=_PRIVATE,
        author_name=_PRIVATE,
        page_text=_PRIVATE,
        ancestors=(),
    )


def test_refresh_queue_info_is_committed_and_not_emitted_for_rollback(captured_logs):
    with pytest.raises(RuntimeError), transaction.atomic():
        refresh.create_or_get_refresh_run()
        assert "event=refresh_run_queued" not in captured_logs.text
        raise RuntimeError(_PRIVATE)
    assert "event=refresh_run_queued" not in captured_logs.text
    run, created = refresh.create_or_get_refresh_run()
    assert created
    record = _event(captured_logs, "refresh_run_queued", logging.INFO)[0]
    assert f"run_id={run.pk}" in record.getMessage()
    refresh.create_or_get_refresh_run()
    _event(captured_logs, "refresh_active_run_reused", logging.DEBUG)
    _private(captured_logs)


def test_idle_schedule_and_missing_claim_do_not_log_each_poll(captured_logs):
    refresh.get_refresh_schedule()
    captured_logs.clear()
    for _ in range(3):
        assert refresh.queue_due_scheduled_refresh() == (None, False)
        assert refresh.execute_refresh_run(987654) is None
    assert captured_logs.records == []


def test_refresh_success_logs_thread_correlations_progress_and_publication(captured_logs):
    for page_id in ("80001", "80002"):
        _bookmark(page_id)
    run = BookmarkRefreshRun.objects.create()

    def fetcher(_profile, bookmark_id, page_id):
        log_event(refresh.logger, logging.DEBUG, "test_fetch_thread", bookmark_id=bookmark_id)
        return refresh.RefreshFetchResult(bookmark_id, page_id, snapshot=_snapshot(page_id))

    completed = refresh.execute_refresh_run(
        run.pk, profile=_profile(), fetcher=fetcher, max_workers=2
    )

    assert completed.status == BookmarkRefreshStatus.SUCCEEDED
    records = _event(captured_logs, "test_fetch_thread", logging.DEBUG)
    assert len(records) == 2
    assert all(f"run_id={run.pk}" in record.getMessage() for record in records)
    assert all("page_id=8000" in record.getMessage() for record in records)
    assert len(_event(captured_logs, "refresh_page_published", logging.INFO)) == 2
    assert len(_event(captured_logs, "refresh_progress_updated", logging.DEBUG)) == 2
    _event(captured_logs, "refresh_run_claimed", logging.INFO)
    _event(captured_logs, "refresh_run_started", logging.INFO)
    _event(captured_logs, "refresh_run_completed", logging.INFO)
    _private(captured_logs)


def test_refresh_fetch_exception_is_error_with_no_user_content(captured_logs):
    factory = Mock(side_effect=OSError(13, _PRIVATE))
    fetched = refresh.fetch_confluence_snapshot(_profile(), 17, _PRIVATE, client_factory=factory)
    assert not fetched.ok
    record = _event(captured_logs, "refresh_fetch_failed", logging.ERROR)[0]
    assert "bookmark_id=17" in record.getMessage()
    assert "page_id=" not in record.getMessage()
    assert "error_type=PermissionError" in record.getMessage()
    assert "errno=13" in record.getMessage()
    _private(captured_logs)


def test_refresh_failed_http_result_is_error_not_warning(captured_logs):
    client = SimpleNamespace(
        get_page=lambda page_id: ConfluenceResult(
            code=ConfluenceResultCode.RATE_LIMITED,
            message=_PRIVATE,
            http_status=429,
        )
    )
    result = refresh.fetch_confluence_snapshot(
        _profile(), 17, "80001", client_factory=lambda _: client
    )
    assert not result.ok
    record = _event(captured_logs, "refresh_fetch_failed", logging.ERROR)[0]
    assert "http_status=429" in record.getMessage()
    assert "error_code=rate_limited" in record.getMessage()
    _private(captured_logs)


def test_failed_page_logs_before_failure_write_and_keeps_retry_context(monkeypatch, captured_logs):
    _bookmark()
    run = BookmarkRefreshRun.objects.create()
    monkeypatch.setattr(
        refresh, "record_refresh_failure", Mock(side_effect=OperationalError(_PRIVATE))
    )

    def fetcher(_profile, bookmark_id, page_id):
        return refresh.RefreshFetchResult(
            bookmark_id, page_id, error_code=_PRIVATE, error_message=_PRIVATE
        )

    completed = refresh.execute_refresh_run(run.pk, profile=_profile(), fetcher=fetcher)

    assert completed.status == BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS
    assert len(_event(captured_logs, "refresh_page_attempt_failed", logging.ERROR)) == 3
    assert len(_event(captured_logs, "refresh_page_retry_round_queued", logging.WARNING)) == 2
    outcome = _event(captured_logs, "refresh_page_failed_outcome", logging.ERROR)[0]
    failure = _event(captured_logs, "refresh_page_failure_record_failed", logging.ERROR)[0]
    assert "attempt=3" in outcome.getMessage()
    assert "error_code=refresh_error" in outcome.getMessage()
    assert captured_logs.records.index(outcome) < captured_logs.records.index(failure)
    _event(captured_logs, "refresh_run_failed_outcome", logging.ERROR)
    _private(captured_logs)


def test_fetch_future_exception_logs_safe_type_and_preserves_run(monkeypatch, captured_logs):
    _bookmark()
    run = BookmarkRefreshRun.objects.create()
    completed = refresh.execute_refresh_run(
        run.pk,
        profile=_profile(),
        fetcher=Mock(side_effect=TimeoutError(_PRIVATE)),
    )
    assert completed.status == BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS
    errors = _event(captured_logs, "refresh_fetch_future_failed", logging.ERROR)
    assert len(errors) == 3
    assert all("error_type=TimeoutError" in record.getMessage() for record in errors)
    _private(captured_logs)


def test_refresh_page_publish_failure_is_error_before_retry(monkeypatch, captured_logs):
    _bookmark()
    run = BookmarkRefreshRun.objects.create()
    monkeypatch.setattr(refresh, "upsert_bookmark", Mock(side_effect=OperationalError(_PRIVATE)))
    completed = refresh.execute_refresh_run(
        run.pk,
        profile=_profile(),
        fetcher=lambda profile, bookmark_id, page_id: refresh.RefreshFetchResult(
            bookmark_id, page_id, snapshot=_snapshot(page_id)
        ),
    )
    assert completed.status == BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS
    assert len(_event(captured_logs, "refresh_page_publish_failed", logging.ERROR)) == 3
    assert "event=refresh_page_published " not in captured_logs.text
    _private(captured_logs)


def test_refresh_configuration_failure_is_error(monkeypatch, captured_logs):
    _bookmark()
    run = BookmarkRefreshRun.objects.create()
    monkeypatch.setattr(
        refresh, "get_active_profile", Mock(side_effect=ConfigurationUnavailable(_PRIVATE))
    )
    assert refresh.execute_refresh_run(run.pk).status == BookmarkRefreshStatus.FAILED
    _event(captured_logs, "refresh_configuration_unavailable", logging.ERROR)
    _event(captured_logs, "refresh_run_failed_outcome", logging.ERROR)
    _private(captured_logs)


def test_refresh_failed_outcome_is_logged_even_when_completion_write_fails(
    monkeypatch, captured_logs
):
    run = BookmarkRefreshRun.objects.create()
    monkeypatch.setattr(run, "save", Mock(side_effect=OperationalError(_PRIVATE)))
    with pytest.raises(OperationalError):
        refresh._finish_run(run, fatal_message=_PRIVATE)
    _event(captured_logs, "refresh_run_failed_outcome", logging.ERROR)
    record = _event(captured_logs, "refresh_operation_failed", logging.ERROR)[0]
    assert "stage=completion" in record.getMessage()
    assert "event=refresh_run_completed " not in captured_logs.text
    _private(captured_logs)


def test_heartbeat_only_writes_do_not_emit_progress(monkeypatch, captured_logs):
    run = BookmarkRefreshRun.objects.create(total_bookmarks=1)
    for _ in range(3):
        refresh._save_run_progress(run)
    assert captured_logs.records == []
    refresh._save_run_progress(run, outcome="succeeded")
    _event(captured_logs, "refresh_progress_updated", logging.DEBUG)


@pytest.mark.parametrize("stage", ["progress", "notification"])
def test_refresh_persistence_failures_include_the_stage(monkeypatch, captured_logs, stage):
    run = BookmarkRefreshRun.objects.create()
    if stage == "progress":
        monkeypatch.setattr(run, "save", Mock(side_effect=OperationalError(_PRIVATE)))

        def operation():
            refresh._save_run_progress(run)

        expected_stage = "progress_persistence"
    else:
        monkeypatch.setattr(
            refresh, "publish_notification", Mock(side_effect=OperationalError(_PRIVATE))
        )

        def operation():
            refresh.publish_refresh_notification(run)

        expected_stage = "notification"
    with pytest.raises(OperationalError):
        operation()
    record = _event(captured_logs, "refresh_operation_failed", logging.ERROR)[0]
    assert f"stage={expected_stage}" in record.getMessage()
    assert f"run_id={run.pk}" in record.getMessage()
    _private(captured_logs)


def test_completion_success_log_is_discarded_if_outer_transaction_rolls_back(captured_logs):
    run = BookmarkRefreshRun.objects.create()
    with pytest.raises(RuntimeError), transaction.atomic():
        refresh._finish_run(run)
        assert "event=refresh_run_completed " not in captured_logs.text
        raise RuntimeError(_PRIVATE)
    assert "event=refresh_run_completed " not in captured_logs.text
    _private(captured_logs)


def test_stale_refresh_records_interruption_before_recovery(monkeypatch, captured_logs):
    run = BookmarkRefreshRun.objects.create(status=BookmarkRefreshStatus.RUNNING)
    BookmarkRefreshRun.objects.filter(pk=run.pk).update(
        heartbeat_at=timezone.now() - timedelta(minutes=8)
    )
    monkeypatch.setattr(
        refresh, "_update_refresh_schedule", Mock(side_effect=OperationalError(_PRIVATE))
    )
    with pytest.raises(OperationalError):
        refresh._interrupt_stale_runs()
    _event(captured_logs, "refresh_run_interrupted", logging.ERROR)
    record = _event(captured_logs, "refresh_operation_failed", logging.ERROR)[0]
    assert "stage=stale_recovery" in record.getMessage()
    _private(captured_logs)


def test_worker_launch_failure_is_error_and_does_not_log_arguments(monkeypatch, captured_logs):
    monkeypatch.setattr(refresh.subprocess, "Popen", Mock(side_effect=OSError(13, _PRIVATE)))
    with pytest.raises(OSError):
        refresh.launch_refresh_worker(41)
    record = _event(captured_logs, "refresh_operation_failed", logging.ERROR)[0]
    assert "stage=worker_launch" in record.getMessage()
    assert "run_id=41" in record.getMessage()
    _private(captured_logs)


def test_scheduled_launch_failure_logs_before_failed_state_write(monkeypatch, captured_logs):
    BookmarkRefreshSchedule.objects.create(pk=1, next_run_at=timezone.now() - timedelta(seconds=1))
    monkeypatch.setattr(refresh, "launch_refresh_worker", Mock(side_effect=OSError(13, _PRIVATE)))
    monkeypatch.setattr(
        refresh, "mark_refresh_launch_failed", Mock(side_effect=OperationalError(_PRIVATE))
    )
    with pytest.raises(OperationalError):
        refresh.queue_due_scheduled_refresh()
    _event(captured_logs, "refresh_scheduled_launch_failed", logging.ERROR)
    _event(captured_logs, "refresh_operation_failed", logging.ERROR)
    _private(captured_logs)


def test_worker_crash_is_critical_even_when_failure_persistence_also_fails(
    monkeypatch, captured_logs
):
    monkeypatch.setattr(
        bookmark_refresh_worker, "execute_refresh_run", Mock(side_effect=RuntimeError(_PRIVATE))
    )
    recovery_error = OperationalError(_PRIVATE)
    monkeypatch.setattr(
        bookmark_refresh_worker, "mark_refresh_worker_failed", Mock(side_effect=recovery_error)
    )
    with pytest.raises(OperationalError) as caught:
        call_command("bookmark_refresh_worker", run_id=42)
    assert caught.value is recovery_error
    _event(captured_logs, "refresh_worker_crashed", logging.CRITICAL)
    _event(captured_logs, "refresh_worker_recovery_failed", logging.ERROR)
    _event(captured_logs, "refresh_worker_stopped", logging.INFO)
    _private(captured_logs)


def test_worker_preserves_existing_command_error_and_logs_original_crash(
    monkeypatch, captured_logs
):
    failure = RuntimeError(_PRIVATE)
    monkeypatch.setattr(bookmark_refresh_worker, "execute_refresh_run", Mock(side_effect=failure))
    recover = Mock()
    monkeypatch.setattr(bookmark_refresh_worker, "mark_refresh_worker_failed", recover)
    with pytest.raises(CommandError) as caught:
        call_command("bookmark_refresh_worker", run_id=43)
    assert caught.value.__cause__ is failure
    recover.assert_called_once_with(43)
    _event(captured_logs, "refresh_worker_crashed", logging.CRITICAL)
    _private(captured_logs)


def test_scheduler_database_failure_is_error_and_fatal_once_is_critical(monkeypatch, captured_logs):
    monkeypatch.setattr(
        bookmark_refresh_scheduler,
        "queue_due_scheduled_refresh",
        Mock(side_effect=OperationalError(_PRIVATE)),
    )
    with pytest.raises(CommandError):
        call_command("bookmark_refresh_scheduler", once=True)
    _event(captured_logs, "refresh_scheduler_check_failed", logging.ERROR)
    _event(captured_logs, "refresh_scheduler_terminated", logging.CRITICAL)
    _private(captured_logs)


def test_scheduler_unexpected_fatal_error_is_reraised_unchanged(monkeypatch, captured_logs):
    failure = RuntimeError(_PRIVATE)
    monkeypatch.setattr(
        bookmark_refresh_scheduler, "queue_due_scheduled_refresh", Mock(side_effect=failure)
    )
    with pytest.raises(RuntimeError) as caught:
        call_command("bookmark_refresh_scheduler", once=True)
    assert caught.value is failure
    _event(captured_logs, "refresh_scheduler_terminated", logging.CRITICAL)
    _private(captured_logs)


def test_resident_scheduler_logs_recovery_and_notification_failures(monkeypatch, captured_logs):
    stop_event = threading.Event()
    calls = 0

    def queue():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError(_PRIVATE)
        stop_event.set()
        return None, False

    monkeypatch.setattr(run_owl, "queue_due_scheduled_refresh", queue)
    monkeypatch.setattr(
        run_owl, "_publish_scheduler_state", Mock(side_effect=OperationalError(_PRIVATE))
    )
    run_owl._scheduler_loop(stop_event, poll_seconds=0)
    _event(captured_logs, "resident_refresh_scheduler_check_failed", logging.ERROR)
    assert (
        len(_event(captured_logs, "resident_refresh_scheduler_notification_failed", logging.ERROR))
        == 2
    )
    _event(captured_logs, "resident_refresh_scheduler_recovered", logging.INFO)
    _private(captured_logs)


def test_resident_scheduler_unhandled_termination_logs_critical(monkeypatch, captured_logs):
    failure = SystemExit(_PRIVATE)
    monkeypatch.setattr(run_owl, "queue_due_scheduled_refresh", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as caught:
        run_owl._scheduler_loop(threading.Event(), poll_seconds=0)
    assert caught.value is failure
    _event(captured_logs, "resident_refresh_scheduler_terminated", logging.CRITICAL)
    _private(captured_logs)


def test_worker_spawn_success_logs_only_internal_ids(monkeypatch, captured_logs):
    process = SimpleNamespace(pid=12345)
    monkeypatch.setattr(refresh.subprocess, "Popen", Mock(return_value=process))
    assert refresh.launch_refresh_worker(41) is process
    record = _event(captured_logs, "refresh_worker_spawned", logging.INFO)[0]
    assert "worker_pid=12345" in record.getMessage()
    assert "manage.py" not in captured_logs.text
    _private(captured_logs)


def test_worker_success_lifecycle_and_unavailable_run_warning(monkeypatch, captured_logs):
    run = SimpleNamespace(
        pk=44,
        status=BookmarkRefreshStatus.SUCCEEDED,
        succeeded_bookmarks=2,
        failed_bookmarks=0,
    )
    monkeypatch.setattr(bookmark_refresh_worker, "execute_refresh_run", lambda run_id: run)
    call_command("bookmark_refresh_worker", run_id=44)
    _event(captured_logs, "refresh_worker_started", logging.INFO)
    _event(captured_logs, "refresh_worker_completed", logging.INFO)
    captured_logs.clear()
    monkeypatch.setattr(bookmark_refresh_worker, "execute_refresh_run", lambda run_id: None)
    with pytest.raises(CommandError):
        call_command("bookmark_refresh_worker", run_id=44)
    _event(captured_logs, "refresh_worker_run_unavailable", logging.WARNING)
    assert not any(record.levelno >= logging.ERROR for record in captured_logs.records)


def test_worker_unhandled_termination_is_critical_and_reraised(monkeypatch, captured_logs):
    failure = SystemExit(_PRIVATE)
    monkeypatch.setattr(bookmark_refresh_worker, "execute_refresh_run", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as caught:
        call_command("bookmark_refresh_worker", run_id=45)
    assert caught.value is failure
    _event(captured_logs, "refresh_worker_terminated", logging.CRITICAL)
    _private(captured_logs)


def test_scheduler_expected_retry_wait_warning_and_stop_are_not_fatal(monkeypatch, captured_logs):
    monkeypatch.setattr(
        bookmark_refresh_scheduler,
        "queue_due_scheduled_refresh",
        Mock(side_effect=OperationalError(_PRIVATE)),
    )
    monkeypatch.setattr(
        bookmark_refresh_scheduler.time, "sleep", Mock(side_effect=KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        call_command("bookmark_refresh_scheduler", poll_seconds=5)
    _event(captured_logs, "refresh_scheduler_check_failed", logging.ERROR)
    _event(captured_logs, "refresh_scheduler_retry_wait", logging.WARNING)
    _event(captured_logs, "refresh_scheduler_stop_requested", logging.INFO)
    assert not any(record.levelno == logging.CRITICAL for record in captured_logs.records)
    _private(captured_logs)


def test_scheduler_invalid_poll_options_are_warning_not_worker_crash(captured_logs):
    with pytest.raises(CommandError):
        call_command("bookmark_refresh_scheduler", once=True, poll_seconds=1)
    _event(captured_logs, "refresh_scheduler_options_rejected", logging.WARNING)
    assert not any(record.levelno >= logging.ERROR for record in captured_logs.records)
