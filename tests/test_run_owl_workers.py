from __future__ import annotations

import logging
import os
import threading
from unittest.mock import Mock

import pytest
from django.core.management import call_command

from bitbucket_search.management.commands import bitbucket_index_worker, bitbucket_sync_worker
from bookmark_manager.management.commands import run_owl

pytestmark = pytest.mark.django_db


class _ResidentProcess:
    def __init__(self, role: str) -> None:
        self.role = role
        self.returncode = None

    def poll(self):
        return self.returncode


def test_resident_worker_specs_preserve_configured_pdf_concurrency(settings):
    settings.BITBUCKET_MAX_REPO_WORKERS = 3
    settings.PDF_MAX_EXTRACTION_WORKERS = 2

    specs = run_owl._resident_bitbucket_worker_specs()

    assert specs == (
        ("repository-sync-1", "bitbucket_sync_worker"),
        ("repository-sync-2", "bitbucket_sync_worker"),
        ("repository-sync-3", "bitbucket_sync_worker"),
        ("pdf-index-1", "bitbucket_index_worker"),
        ("pdf-index-2", "bitbucket_index_worker"),
    )


def test_resident_semantic_worker_specs_preserve_configured_concurrency(settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    settings.SEMANTIC_MAX_WORKERS = 2

    assert run_owl._resident_semantic_worker_specs() == (
        ("semantic-index-1", "semantic_index_worker"),
        ("semantic-index-2", "semantic_index_worker"),
    )


def test_resident_supervisor_recovers_leases_before_launch_and_stops_owned_workers(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 2
    settings.PDF_MAX_EXTRACTION_WORKERS = 3
    settings.SEMANTIC_MAX_WORKERS = 2
    stop_event = threading.Event()
    events: list[tuple[str, object]] = []
    launched: list[_ResidentProcess] = []
    stopped: list[_ResidentProcess] = []
    resident_states: list[bool] = []
    extraction_sweeps = 0

    monkeypatch.setattr(
        run_owl,
        "set_resident_repository_workers_active",
        resident_states.append,
    )

    monkeypatch.setattr(
        run_owl,
        "repository_status_snapshot",
        lambda: events.append(("repository-sweep", None)),
    )
    monkeypatch.setattr(
        run_owl,
        "queue_due_daily_repository_refreshes",
        lambda: events.append(("daily-schedule", None)),
    )

    def extraction_sweep(*, interrupt_running=False):
        nonlocal extraction_sweeps
        extraction_sweeps += 1
        events.append(("extraction-sweep", interrupt_running))
        if extraction_sweeps == 2:
            stop_event.set()

    monkeypatch.setattr(run_owl, "sweep_pdf_extraction_queue", extraction_sweep)
    monkeypatch.setattr(
        run_owl,
        "sweep_semantic_index_queue",
        lambda *, interrupt_running=False: events.append(("semantic-sweep", interrupt_running)),
    )

    def launch(command):
        process = _ResidentProcess(command)
        launched.append(process)
        events.append(("launch", command))
        return process

    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)
    monkeypatch.setattr(
        run_owl,
        "_stop_resident_bitbucket_worker",
        lambda process: stopped.append(process),
    )

    run_owl._bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert events[:4] == [
        ("daily-schedule", None),
        ("repository-sweep", None),
        ("extraction-sweep", True),
        ("semantic-sweep", True),
    ]
    assert [process.role for process in launched] == [
        "bitbucket_sync_worker",
        "bitbucket_sync_worker",
        "bitbucket_index_worker",
        "bitbucket_index_worker",
        "bitbucket_index_worker",
        "semantic_index_worker",
        "semantic_index_worker",
    ]
    assert [event[1] for event in events if event[0] == "extraction-sweep"] == [
        True,
        False,
    ]
    assert [event[1] for event in events if event[0] == "semantic-sweep"] == [
        True,
    ]
    assert [event for event in events if event[0] == "daily-schedule"] == [
        ("daily-schedule", None),
        ("daily-schedule", None),
    ]
    assert stopped == launched
    assert resident_states == [True, False]


def test_resident_supervisor_clears_stale_marker_when_sweep_fails_before_launch(
    monkeypatch,
):
    stop_event = threading.Event()
    resident_states: list[bool] = []
    launch = Mock()

    monkeypatch.setattr(
        run_owl,
        "resident_repository_workers_active",
        Mock(return_value=True),
    )
    monkeypatch.setattr(
        run_owl,
        "set_resident_repository_workers_active",
        resident_states.append,
    )

    def fail_schedule():
        stop_event.set()
        raise RuntimeError("synthetic schedule failure")

    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", fail_schedule)
    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)

    run_owl._bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert resident_states == [False]
    launch.assert_not_called()


def test_semantic_reconciliation_failure_does_not_block_other_recovery_or_workers(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    settings.PDF_MAX_EXTRACTION_WORKERS = 1
    settings.SEMANTIC_SEARCH_ENABLED = True
    settings.SEMANTIC_MAX_WORKERS = 1
    stop_event = threading.Event()
    events: list[tuple[str, object]] = []
    launched: list[_ResidentProcess] = []
    semantic_error = RuntimeError("synthetic semantic reconciliation failure")

    monkeypatch.setattr(
        run_owl,
        "queue_due_daily_repository_refreshes",
        lambda: events.append(("daily-schedule", None)),
    )
    monkeypatch.setattr(
        run_owl,
        "repository_status_snapshot",
        lambda: events.append(("repository-sweep", None)),
    )
    monkeypatch.setattr(
        run_owl,
        "sweep_pdf_extraction_queue",
        lambda *, interrupt_running=False: events.append(("extraction-sweep", interrupt_running)),
    )

    def fail_semantic_reconciliation(*, interrupt_running=False):
        events.append(("semantic-sweep", interrupt_running))
        raise semantic_error

    monkeypatch.setattr(
        run_owl,
        "sweep_semantic_index_queue",
        fail_semantic_reconciliation,
    )
    semantic_logs: list[tuple[object, int, str, dict[str, object]]] = []
    monkeypatch.setattr(
        run_owl,
        "log_semantic_event",
        lambda logger, level, event, **fields: semantic_logs.append((logger, level, event, fields)),
    )

    def launch(command):
        process = _ResidentProcess(command)
        launched.append(process)
        events.append(("launch", command))
        if len(launched) == 3:
            stop_event.set()
        return process

    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", Mock())

    run_owl._bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert events[:4] == [
        ("daily-schedule", None),
        ("repository-sweep", None),
        ("extraction-sweep", True),
        ("semantic-sweep", True),
    ]
    assert [process.role for process in launched] == [
        "bitbucket_sync_worker",
        "bitbucket_index_worker",
        "semantic_index_worker",
    ]
    assert semantic_logs == [
        (
            run_owl.semantic_logger,
            logging.ERROR,
            "semantic_reconciliation_failed",
            {"error": semantic_error, "stage": "lease_recovery"},
        )
    ]


def test_resident_supervisor_clears_marker_when_live_worker_dies_during_exception(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    settings.PDF_MAX_EXTRACTION_WORKERS = 1
    stop_event = threading.Event()
    resident_states: list[bool] = []
    repository_worker = _ResidentProcess("bitbucket_sync_worker")
    schedule_checks = 0

    monkeypatch.setattr(
        run_owl,
        "set_resident_repository_workers_active",
        resident_states.append,
    )
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "sweep_pdf_extraction_queue", Mock())

    def check_schedule():
        nonlocal schedule_checks
        schedule_checks += 1
        if schedule_checks == 2:
            repository_worker.returncode = 1
            stop_event.set()
            raise RuntimeError("synthetic supervisor failure")

    def launch(command):
        if command == "bitbucket_sync_worker":
            return repository_worker
        return _ResidentProcess(command)

    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", check_schedule)
    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", Mock())

    run_owl._bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert resident_states == [True, False]


def test_resident_launcher_disables_detached_helpers_only_for_sync_worker(
    monkeypatch,
    settings,
):
    popen = Mock(return_value=object())
    monkeypatch.setattr(run_owl.subprocess, "Popen", popen)

    run_owl._launch_resident_bitbucket_worker("bitbucket_sync_worker")
    sync_arguments = popen.call_args.args[0]
    run_owl._launch_resident_bitbucket_worker("bitbucket_index_worker")
    index_arguments = popen.call_args.args[0]
    run_owl._launch_resident_bitbucket_worker("semantic_index_worker")
    semantic_arguments = popen.call_args.args[0]

    assert sync_arguments[-4:] == [
        "bitbucket_sync_worker",
        "--repository-only",
        "--no-spawn-index-workers",
        "--no-startup-index-sweep",
    ]
    assert index_arguments[-2:] == ["bitbucket_index_worker", "--no-startup-sweep"]
    assert semantic_arguments[-2:] == ["semantic_index_worker", "--no-startup-sweep"]
    assert "--no-spawn-index-workers" not in index_arguments
    assert popen.call_args.kwargs["cwd"] == settings.BASE_DIR
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")


def test_standalone_index_worker_reconciles_once_before_cheap_claims(monkeypatch):
    reconcile = Mock()
    work_one = Mock(return_value=None)
    monkeypatch.setattr(bitbucket_index_worker, "sweep_pdf_extraction_queue", reconcile)
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", work_one)

    call_command("bitbucket_index_worker", "--once")

    reconcile.assert_called_once_with()
    work_one.assert_called_once_with()


def test_repository_only_sync_worker_never_claims_pdf_extraction(monkeypatch):
    repository_work = Mock(return_value=None)
    extraction_work = Mock()
    extraction_sweep = Mock()
    monkeypatch.setattr(bitbucket_sync_worker, "work_one_job", repository_work)
    monkeypatch.setattr(bitbucket_sync_worker, "work_one_extraction_job", extraction_work)
    monkeypatch.setattr(bitbucket_sync_worker, "sweep_pdf_extraction_queue", extraction_sweep)

    call_command(
        "bitbucket_sync_worker",
        "--once",
        "--repository-only",
    )

    repository_work.assert_called_once_with()
    extraction_sweep.assert_not_called()
    extraction_work.assert_not_called()
