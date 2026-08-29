from __future__ import annotations

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

    def poll(self):
        return None


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


def test_resident_supervisor_recovers_leases_before_launch_and_stops_owned_workers(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 2
    settings.PDF_MAX_EXTRACTION_WORKERS = 3
    stop_event = threading.Event()
    events: list[tuple[str, object]] = []
    launched: list[_ResidentProcess] = []
    stopped: list[_ResidentProcess] = []
    extraction_sweeps = 0

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

    assert events[:3] == [
        ("daily-schedule", None),
        ("repository-sweep", None),
        ("extraction-sweep", True),
    ]
    assert [process.role for process in launched] == [
        "bitbucket_sync_worker",
        "bitbucket_sync_worker",
        "bitbucket_index_worker",
        "bitbucket_index_worker",
        "bitbucket_index_worker",
    ]
    assert [event[1] for event in events if event[0] == "extraction-sweep"] == [
        True,
        False,
    ]
    assert [event for event in events if event[0] == "daily-schedule"] == [
        ("daily-schedule", None),
        ("daily-schedule", None),
    ]
    assert stopped == launched


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

    assert sync_arguments[-4:] == [
        "bitbucket_sync_worker",
        "--repository-only",
        "--no-spawn-index-workers",
        "--no-startup-index-sweep",
    ]
    assert index_arguments[-2:] == ["bitbucket_index_worker", "--no-startup-sweep"]
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
