from __future__ import annotations

import logging
import os
import subprocess
import threading
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.db import OperationalError

from bitbucket_search.management.commands import (
    bitbucket_index_worker,
    bitbucket_pdf_writer,
    bitbucket_sync_worker,
)
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPipelineRecovery,
    PDFPipelineRecoveryState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
)
from bookmark_manager.management.commands import run_owl
from bookmark_manager.models import Bookmark, ConfluencePageNode
from core.process_supervision import RESIDENT_SUPERVISOR_PID_ENV
from semantic_search.management.commands import semantic_index_worker
from semantic_search.models import (
    SemanticIndexJob,
    SemanticIndexJobStatus,
    SemanticSourceType,
)

pytestmark = pytest.mark.django_db


class _ResidentProcess:
    def __init__(self, role: str, pid: int | None = None) -> None:
        self.role = role
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def _repository() -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name="Synthetic repository",
        canonical_remote_key="example.invalid/team/keep-awake",
        remote_url="https://example.invalid/team/keep-awake.git",
    )


def _pdf_job(*, status: str) -> PDFExtractionJob:
    repository = _repository()
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Synthetic.pdf",
        relative_path="docs/Synthetic.pdf",
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
        file_size=128,
    )
    return PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id=document.git_blob_id,
        target_source_commit=document.last_seen_commit,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version="synthetic-v1",
        status=status,
    )


def _semantic_job(*, status: str) -> SemanticIndexJob:
    node = ConfluencePageNode.objects.create(
        page_id="keep-awake-page",
        title="Synthetic page",
        url="https://example.invalid/pages/keep-awake",
    )
    bookmark = Bookmark.objects.create(
        page_id=node.page_id,
        tree_node=node,
        title=node.title,
        url=node.url,
    )
    return SemanticIndexJob.objects.create(
        source_type=SemanticSourceType.BOOKMARK,
        bookmark=bookmark,
        target_content_hash="c" * 64,
        target_model_version="synthetic-model-v1",
        target_chunker_version="synthetic-chunker-v1",
        status=status,
    )


@pytest.mark.parametrize(
    ("queue_name", "status"),
    (
        ("repository", RepositorySyncJobStatus.QUEUED),
        ("repository", RepositorySyncJobStatus.RUNNING),
        ("pdf", PDFExtractionJobStatus.QUEUED),
        ("pdf", PDFExtractionJobStatus.RUNNING),
        ("semantic", SemanticIndexJobStatus.QUEUED),
        ("semantic", SemanticIndexJobStatus.RUNNING),
    ),
)
def test_background_work_active_for_every_durable_queue(settings, queue_name, status):
    settings.SEMANTIC_SEARCH_ENABLED = True
    if queue_name == "repository":
        RepositorySyncJob.objects.create(
            repository=_repository(),
            operation=RepositorySyncOperation.CLONE,
            status=status,
        )
    elif queue_name == "pdf":
        _pdf_job(status=status)
    else:
        _semantic_job(status=status)

    assert run_owl._background_work_active() is True


def test_background_work_active_ignores_terminal_jobs(settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    repository = _repository()
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.SUCCEEDED,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Terminal.pdf",
        relative_path="docs/Terminal.pdf",
    )
    PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id="a" * 40,
        target_source_commit="b" * 40,
        target_relative_path=document.relative_path,
        target_extractor_version="synthetic-v1",
        status=PDFExtractionJobStatus.SUCCEEDED,
    )
    _semantic_job(status=SemanticIndexJobStatus.SUCCEEDED)

    assert run_owl._background_work_active() is False


def test_background_work_active_ignores_semantic_queue_when_disabled(settings):
    settings.SEMANTIC_SEARCH_ENABLED = False
    _semantic_job(status=SemanticIndexJobStatus.RUNNING)

    assert run_owl._background_work_active() is False


def test_display_awake_launch_uses_mac_display_and_idle_assertions(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    executable = tmp_path / "caffeinate"
    executable.touch()
    process = Mock(pid=321)
    popen = Mock(return_value=process)
    monkeypatch.setattr(run_owl, "CAFFEINATE_PATH", executable)
    monkeypatch.setattr(run_owl.sys, "platform", "darwin")
    monkeypatch.setattr(run_owl.subprocess, "Popen", popen)

    assertion = run_owl._launch_display_awake_assertion()

    assert assertion is not None
    assert assertion.backend == "macos"
    assert assertion.owner_thread_id == threading.get_ident()
    assert assertion.process is process
    assert popen.call_args.args[0] == [
        str(executable),
        "-di",
        "-w",
        str(os.getpid()),
    ]
    assert popen.call_args.kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "start_new_session": True,
    }


def test_display_awake_reconcile_starts_once_and_releases_when_idle(
    monkeypatch,
    settings,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    monkeypatch.setattr(run_owl.sys, "platform", "darwin")
    process = Mock(pid=321)
    process.poll.return_value = None
    assertion = run_owl._DisplayAwakeAssertion(
        backend="macos",
        owner_thread_id=threading.get_ident(),
        process=process,
    )
    launch = Mock(return_value=assertion)
    stop = Mock(return_value=True)
    monkeypatch.setattr(run_owl, "_launch_display_awake_assertion", launch)
    monkeypatch.setattr(run_owl, "_stop_display_awake_assertion", stop)

    active = run_owl._reconcile_display_awake_assertion(None, work_active=True)
    retained = run_owl._reconcile_display_awake_assertion(active, work_active=True)
    released = run_owl._reconcile_display_awake_assertion(retained, work_active=False)

    assert active is assertion
    assert retained is assertion
    assert released is None
    launch.assert_called_once_with()
    stop.assert_called_once_with(assertion)


def test_display_awake_reconcile_relaunches_an_exited_helper(
    monkeypatch,
    settings,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    monkeypatch.setattr(run_owl.sys, "platform", "darwin")
    exited = Mock(pid=321)
    exited.poll.return_value = 9
    exited_assertion = run_owl._DisplayAwakeAssertion(
        backend="macos",
        owner_thread_id=threading.get_ident(),
        process=exited,
    )
    replacement = run_owl._DisplayAwakeAssertion(
        backend="macos",
        owner_thread_id=threading.get_ident(),
        process=Mock(pid=654),
    )
    launch = Mock(return_value=replacement)
    monkeypatch.setattr(run_owl, "_launch_display_awake_assertion", launch)

    result = run_owl._reconcile_display_awake_assertion(exited_assertion, work_active=True)

    assert result is replacement
    launch.assert_called_once_with()


def test_display_awake_launch_failure_is_nonfatal_and_retryable(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    executable = tmp_path / "caffeinate"
    executable.touch()
    process = Mock(pid=321)
    popen = Mock(side_effect=[OSError("synthetic launch failure"), process])
    monkeypatch.setattr(run_owl, "CAFFEINATE_PATH", executable)
    monkeypatch.setattr(run_owl.sys, "platform", "darwin")
    monkeypatch.setattr(run_owl.subprocess, "Popen", popen)

    assert run_owl._reconcile_display_awake_assertion(None, work_active=True) is None
    assertion = run_owl._reconcile_display_awake_assertion(None, work_active=True)

    assert assertion is not None
    assert assertion.backend == "macos"
    assert assertion.process is process
    assert popen.call_count == 2


def test_display_awake_is_a_noop_on_unsupported_platform(monkeypatch, settings):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    popen = Mock()
    monkeypatch.setattr(run_owl.sys, "platform", "linux")
    monkeypatch.setattr(run_owl.subprocess, "Popen", popen)

    assert run_owl._reconcile_display_awake_assertion(None, work_active=True) is None
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("feature_enabled", "platform"),
    ((False, "darwin"), (True, "linux")),
)
def test_display_awake_skips_queue_queries_when_unsupported(
    monkeypatch,
    settings,
    feature_enabled,
    platform,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = feature_enabled
    queue_check = Mock()
    monkeypatch.setattr(run_owl.sys, "platform", platform)
    monkeypatch.setattr(run_owl, "_background_work_active", queue_check)

    assert run_owl._reconcile_display_awake_for_current_queues(None) is None
    queue_check.assert_not_called()


def test_display_awake_does_not_spawn_when_caffeinate_is_missing(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    popen = Mock()
    monkeypatch.setattr(run_owl.sys, "platform", "darwin")
    monkeypatch.setattr(run_owl, "CAFFEINATE_PATH", tmp_path / "missing-caffeinate")
    monkeypatch.setattr(run_owl.subprocess, "Popen", popen)

    assert run_owl._launch_display_awake_assertion() is None
    popen.assert_not_called()


def test_windows_execution_state_wrapper_uses_kernel32_and_32_bit_flags(monkeypatch):
    import ctypes

    calls: list[int] = []

    class Operation:
        argtypes = None
        restype = None

        def __call__(self, flags):
            calls.append(flags)
            return run_owl.WINDOWS_ES_CONTINUOUS

    operation = Operation()
    kernel = type("Kernel", (), {"SetThreadExecutionState": operation})()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)

    run_owl._set_windows_execution_state(run_owl.WINDOWS_AWAKE_EXECUTION_STATE)

    assert calls == [0x80000003]
    assert operation.argtypes == (ctypes.c_uint32,)
    assert operation.restype is ctypes.c_uint32


def test_windows_execution_state_wrapper_reports_native_failure(monkeypatch):
    import ctypes

    class Operation:
        def __call__(self, _flags):
            return 0

    kernel = type("Kernel", (), {"SetThreadExecutionState": Operation()})()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    with pytest.raises(OSError) as error:
        run_owl._set_windows_execution_state(run_owl.WINDOWS_AWAKE_EXECUTION_STATE)

    assert error.value.errno == 5


def test_windows_display_awake_sets_once_and_clears_on_idle(monkeypatch, settings):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    native_call = Mock()
    monkeypatch.setattr(run_owl.sys, "platform", "win32")
    monkeypatch.setattr(run_owl, "_set_windows_execution_state", native_call)

    active = run_owl._reconcile_display_awake_assertion(None, work_active=True)
    retained = run_owl._reconcile_display_awake_assertion(active, work_active=True)
    released = run_owl._reconcile_display_awake_assertion(retained, work_active=False)

    assert active is not None
    assert active.backend == "windows"
    assert active.owner_thread_id == threading.get_ident()
    assert active.process is None
    assert retained is active
    assert released is None
    assert [item.args[0] for item in native_call.call_args_list] == [
        run_owl.WINDOWS_AWAKE_EXECUTION_STATE,
        run_owl.WINDOWS_ES_CONTINUOUS,
    ]
    assert run_owl.WINDOWS_AWAKE_EXECUTION_STATE == 0x80000003


def test_windows_display_awake_activation_failure_is_retried(monkeypatch, settings):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    native_call = Mock(side_effect=[OSError("synthetic activation failure"), None])
    monkeypatch.setattr(run_owl.sys, "platform", "win32")
    monkeypatch.setattr(run_owl, "_set_windows_execution_state", native_call)

    first = run_owl._reconcile_display_awake_assertion(None, work_active=True)
    second = run_owl._reconcile_display_awake_assertion(first, work_active=True)

    assert first is None
    assert second is not None
    assert second.backend == "windows"
    assert [item.args[0] for item in native_call.call_args_list] == [
        run_owl.WINDOWS_AWAKE_EXECUTION_STATE,
        run_owl.WINDOWS_AWAKE_EXECUTION_STATE,
    ]


def test_windows_display_awake_release_failure_is_retried(monkeypatch, settings):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    native_call = Mock(side_effect=[None, OSError("synthetic release failure"), None])
    monkeypatch.setattr(run_owl.sys, "platform", "win32")
    monkeypatch.setattr(run_owl, "_set_windows_execution_state", native_call)

    active = run_owl._reconcile_display_awake_assertion(None, work_active=True)
    retained = run_owl._reconcile_display_awake_assertion(active, work_active=False)
    released = run_owl._reconcile_display_awake_assertion(retained, work_active=False)

    assert active is not None
    assert retained is active
    assert released is None
    assert [item.args[0] for item in native_call.call_args_list] == [
        run_owl.WINDOWS_AWAKE_EXECUTION_STATE,
        run_owl.WINDOWS_ES_CONTINUOUS,
        run_owl.WINDOWS_ES_CONTINUOUS,
    ]


def test_windows_display_awake_refuses_release_from_another_thread(
    monkeypatch,
    settings,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    native_call = Mock()
    launch = Mock()
    monkeypatch.setattr(run_owl.sys, "platform", "win32")
    monkeypatch.setattr(run_owl, "_set_windows_execution_state", native_call)
    monkeypatch.setattr(run_owl, "_launch_display_awake_assertion", launch)
    assertion = run_owl._DisplayAwakeAssertion(
        backend="windows",
        owner_thread_id=threading.get_ident() + 1,
    )

    assert run_owl._stop_display_awake_assertion(assertion) is False
    assert run_owl._reconcile_display_awake_assertion(assertion, work_active=True) is assertion
    native_call.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize(
    ("platform", "backend"),
    (("darwin", "macos"), ("win32", "windows")),
)
def test_display_awake_starts_conservatively_when_queue_state_is_unavailable(
    monkeypatch,
    settings,
    platform,
    backend,
):
    settings.OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = True
    monkeypatch.setattr(run_owl.sys, "platform", platform)
    monkeypatch.setattr(
        run_owl,
        "_background_work_active",
        Mock(side_effect=RuntimeError("synthetic database failure")),
    )
    assertion = run_owl._DisplayAwakeAssertion(
        backend=backend,
        owner_thread_id=threading.get_ident(),
        process=Mock(pid=321) if backend == "macos" else None,
    )
    launch = Mock(return_value=assertion)
    monkeypatch.setattr(run_owl, "_launch_display_awake_assertion", launch)

    assert run_owl._reconcile_display_awake_for_current_queues(None) is assertion
    launch.assert_called_once_with()


def test_display_awake_stop_kills_only_helper_that_will_not_terminate():
    process = Mock(pid=321)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 2), 0]
    assertion = run_owl._DisplayAwakeAssertion(
        backend="macos",
        owner_thread_id=threading.get_ident(),
        process=process,
    )

    assert run_owl._stop_display_awake_assertion(assertion) is True

    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2


def test_supervisor_releases_display_assertion_even_when_worker_shutdown_fails(
    monkeypatch,
    settings,
):
    settings.SEMANTIC_SEARCH_ENABLED = False
    stop_event = threading.Event()
    worker = _ResidentProcess("bitbucket_sync_worker", pid=123)
    awake = Mock(pid=321)
    reconcile_awake = Mock(return_value=awake)
    stop_awake = Mock()

    monkeypatch.setattr(run_owl, "resident_repository_workers_active", Mock(return_value=False))
    monkeypatch.setattr(run_owl, "set_resident_repository_workers_active", Mock())
    monkeypatch.setattr(
        run_owl,
        "_resident_worker_specs",
        Mock(return_value=(("repository-sync-1", "bitbucket_sync_worker"),)),
    )
    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", Mock())
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "stale_repository_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_extraction_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_semantic_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(
        run_owl,
        "sweep_pdf_extraction_queue",
        Mock(return_value=type("Recovery", (), {"interrupted_worker_pids": ()})()),
    )
    monkeypatch.setattr(run_owl, "sweep_semantic_index_queue", Mock())

    def launch(_command, *, role=None):
        stop_event.set()
        return worker

    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)
    monkeypatch.setattr(
        run_owl,
        "_stop_resident_bitbucket_worker",
        Mock(side_effect=RuntimeError("synthetic worker shutdown failure")),
    )
    monkeypatch.setattr(
        run_owl,
        "_reconcile_display_awake_for_current_queues",
        reconcile_awake,
    )
    monkeypatch.setattr(run_owl, "_stop_display_awake_assertion", stop_awake)

    with pytest.raises(RuntimeError, match="synthetic worker shutdown failure"):
        run_owl._run_bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert reconcile_awake.call_count == 2
    stop_awake.assert_called_once_with(awake)


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
        ("pdf-writer-1", "bitbucket_pdf_writer"),
    )


def test_default_pdf_pipeline_uses_four_extractors_and_one_writer(settings):
    specs = run_owl._resident_bitbucket_worker_specs()
    repository_specs = [spec for spec in specs if spec[1] == "bitbucket_sync_worker"]
    pdf_specs = [spec for spec in specs if spec[1] == "bitbucket_index_worker"]

    assert settings.BITBUCKET_MAX_REPO_WORKERS == 4
    assert len(repository_specs) == 4
    assert settings.PDF_MAX_EXTRACTION_WORKERS == 4
    assert settings.PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY == 4
    assert pdf_specs == [
        (f"pdf-index-{worker_number}", "bitbucket_index_worker") for worker_number in range(1, 5)
    ]
    assert ("pdf-writer-1", "bitbucket_pdf_writer") in specs


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

    def launch(command, *, role=None):
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
        "bitbucket_pdf_writer",
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

    assert resident_states == [False, True, False]
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

    def launch(command, *, role=None):
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
        "bitbucket_pdf_writer",
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

    def launch(command, *, role=None):
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
    run_owl._launch_resident_bitbucket_worker(
        "bitbucket_index_worker",
        role="pdf-index-3",
    )
    index_arguments = popen.call_args.args[0]
    run_owl._launch_resident_bitbucket_worker("semantic_index_worker")
    semantic_arguments = popen.call_args.args[0]

    assert sync_arguments[-4:] == [
        "bitbucket_sync_worker",
        "--repository-only",
        "--no-spawn-index-workers",
        "--no-startup-index-sweep",
    ]
    assert index_arguments[-4:] == [
        "bitbucket_index_worker",
        "--no-startup-sweep",
        "--slot-number",
        "3",
    ]
    assert semantic_arguments[-2:] == ["semantic_index_worker", "--no-startup-sweep"]
    assert "--no-spawn-index-workers" not in index_arguments
    assert popen.call_args.kwargs["cwd"] == settings.BASE_DIR
    assert popen.call_args.kwargs["start_new_session"] is (os.name != "nt")
    assert popen.call_args.kwargs["env"][RESIDENT_SUPERVISOR_PID_ENV] == str(os.getpid())


def test_orphaned_resident_workers_exit_before_claiming(monkeypatch):
    index_work = Mock()
    writer_work = Mock()
    repository_work = Mock()
    semantic_work = Mock()
    monkeypatch.setattr(
        bitbucket_index_worker, "resident_supervisor_is_alive", Mock(return_value=False)
    )
    monkeypatch.setattr(
        bitbucket_pdf_writer, "resident_supervisor_is_alive", Mock(return_value=False)
    )
    monkeypatch.setattr(
        bitbucket_sync_worker, "resident_supervisor_is_alive", Mock(return_value=False)
    )
    monkeypatch.setattr(
        semantic_index_worker, "resident_supervisor_is_alive", Mock(return_value=False)
    )
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", index_work)
    monkeypatch.setattr(bitbucket_pdf_writer, "work_one_publication_job", writer_work)
    monkeypatch.setattr(bitbucket_sync_worker, "work_one_job", repository_work)
    monkeypatch.setattr(semantic_index_worker, "work_one_semantic_job", semantic_work)

    call_command("bitbucket_index_worker", "--once", "--no-startup-sweep")
    call_command("bitbucket_pdf_writer", "--once")
    call_command("bitbucket_sync_worker", "--once", "--repository-only")
    call_command("semantic_index_worker", "--once", "--no-startup-sweep")

    index_work.assert_not_called()
    writer_work.assert_not_called()
    repository_work.assert_not_called()
    semantic_work.assert_not_called()


def test_supervisor_stops_only_tracked_stale_children(monkeypatch):
    healthy = Mock(pid=101)
    healthy.poll.return_value = None
    stale = Mock(pid=202)
    stale.poll.return_value = None
    stopped = Mock()
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", stopped)

    run_owl._stop_stale_owned_workers(
        {"healthy": healthy, "stale": stale},
        {202, 303},
    )

    stopped.assert_called_once_with(stale)


def test_supervisor_watchdog_restarts_an_escaped_controller(monkeypatch, settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = False
    stop_event = threading.Event()
    calls = 0

    def controller(_stop_event, *, poll_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic controller escape")
        stop_event.set()

    waits: list[float] = []

    def immediate_wait(delay):
        waits.append(delay)
        return stop_event.is_set()

    monkeypatch.setattr(run_owl, "_bitbucket_queue_loop", controller)
    monkeypatch.setattr(stop_event, "wait", immediate_wait)

    run_owl._bitbucket_supervisor_watchdog_loop(stop_event, poll_seconds=5)

    assert calls == 2
    assert waits == [5.0]


def test_supervisor_watchdog_does_not_enter_controller_when_circuit_is_paused(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 0
    run_owl._record_recovery_scope_failure(
        "supervisor",
        reason_code=run_owl.RecoveryReasonCode.SUPERVISOR_LOOP_FAILED,
        probe=None,
    )
    stop_event = threading.Event()
    controller = Mock()

    def stop_after_wait(_delay):
        stop_event.set()
        return True

    monkeypatch.setattr(run_owl, "_bitbucket_queue_loop", controller)
    monkeypatch.setattr(stop_event, "wait", stop_after_wait)

    run_owl._bitbucket_supervisor_watchdog_loop(stop_event, poll_seconds=5)

    controller.assert_not_called()
    recovery = PDFPipelineRecovery.objects.get(scope="supervisor")
    assert recovery.state == PDFPipelineRecoveryState.PAUSED


def test_exited_pdf_controller_lease_is_recovered_before_replacement(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    settings.PDF_MAX_EXTRACTION_WORKERS = 1
    settings.SEMANTIC_SEARCH_ENABLED = False
    # This legacy test isolates lease ordering. Component-circuit timing has
    # dedicated deterministic coverage below.
    settings.PDF_PIPELINE_RECOVERY_ENABLED = False
    stop_event = threading.Event()
    launched: list[_ResidentProcess] = []
    sweep_options: list[dict[str, object]] = []
    interrupted_repository_pids: list[tuple[int, ...]] = []
    schedule_pass = 0

    def schedule():
        nonlocal schedule_pass
        schedule_pass += 1
        if schedule_pass == 2:
            pdf_worker = next(
                worker for worker in launched if worker.role == "bitbucket_index_worker"
            )
            pdf_worker.returncode = 9

    def launch(command, *, role=None):
        process = _ResidentProcess(command, 1000 + len(launched))
        launched.append(process)
        if (
            command == "bitbucket_index_worker"
            and sum(worker.role == command for worker in launched) == 2
        ):
            stop_event.set()
        return process

    def sweep(**options):
        sweep_options.append(options)
        interrupted = tuple(options.get("interrupt_worker_pids", ()))
        return type("Recovery", (), {"interrupted_worker_pids": interrupted})()

    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", schedule)
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "stale_repository_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_extraction_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(
        run_owl,
        "interrupt_repository_worker_leases",
        lambda pids: interrupted_repository_pids.append(pids),
    )
    monkeypatch.setattr(run_owl, "sweep_pdf_extraction_queue", sweep)
    monkeypatch.setattr(run_owl, "_launch_resident_bitbucket_worker", launch)
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", Mock())

    run_owl._bitbucket_queue_loop(stop_event, poll_seconds=0)

    first_pdf_pid = next(
        worker.pid for worker in launched if worker.role == "bitbucket_index_worker"
    )
    assert interrupted_repository_pids == [(first_pdf_pid,)]
    assert sweep_options == [
        {"interrupt_running": True},
        {"interrupt_running": False, "interrupt_worker_pids": (first_pdf_pid,)},
    ]
    assert sum(worker.role == "bitbucket_index_worker" for worker in launched) == 2


def test_standalone_index_worker_reconciles_once_before_cheap_claims(monkeypatch):
    reconcile = Mock()
    work_one = Mock(return_value=None)
    monkeypatch.setattr(bitbucket_index_worker, "sweep_pdf_extraction_queue", reconcile)
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", work_one)

    call_command("bitbucket_index_worker", "--once")

    reconcile.assert_called_once_with()
    work_one.assert_called_once_with()


def test_index_worker_controller_pauses_only_new_job_claims(monkeypatch):
    work_one = Mock(return_value=None)
    monkeypatch.setattr(bitbucket_index_worker, "worker_slot_admitted", Mock(return_value=False))
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", work_one)

    call_command(
        "bitbucket_index_worker",
        "--once",
        "--no-startup-sweep",
        "--slot-number",
        "3",
    )

    bitbucket_index_worker.worker_slot_admitted.assert_called_once_with(3)
    work_one.assert_not_called()


def test_supervisor_records_controller_evaluation_failure_in_controller_scope(
    monkeypatch,
    settings,
):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.SEMANTIC_SEARCH_ENABLED = False
    stop_event = threading.Event()
    record_failure = Mock()
    monkeypatch.setattr(run_owl, "resident_repository_workers_active", Mock(return_value=False))
    monkeypatch.setattr(run_owl, "set_resident_repository_workers_active", Mock())
    monkeypatch.setattr(run_owl, "_resident_worker_specs", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "_resident_bitbucket_worker_specs", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "ensure_recovery_scope", Mock())
    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", Mock())
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "stale_repository_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_extraction_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_semantic_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(
        run_owl,
        "sweep_pdf_extraction_queue",
        Mock(return_value=type("Recovery", (), {"interrupted_worker_pids": ()})()),
    )
    monkeypatch.setattr(run_owl, "reconcile_open_pipeline_runs", Mock())
    monkeypatch.setattr(
        run_owl,
        "_prepare_recovery_scope_launch",
        Mock(return_value=run_owl._WorkerLaunchPermission(allowed=True)),
    )
    monkeypatch.setattr(run_owl, "_record_recovery_scope_failure", record_failure)
    monkeypatch.setattr(
        run_owl,
        "_reconcile_display_awake_for_current_queues",
        Mock(return_value=None),
    )
    monkeypatch.setattr(run_owl, "_stop_display_awake_assertion", Mock())

    def fail_controller_sample(**kwargs):
        stop_event.set()
        raise run_owl.ControllerEvaluationError("synthetic evaluation failure")

    monkeypatch.setattr(run_owl, "sample_pipeline_metrics", fail_controller_sample)

    run_owl._run_bitbucket_queue_loop(stop_event, poll_seconds=0)

    record_failure.assert_called_once_with(
        run_owl.RecoveryScope.CONTROLLER.value,
        reason_code=run_owl.RecoveryReasonCode.ERROR_LOOP,
        probe=None,
    )


def test_paused_controller_scope_samples_fixed_fallback_without_evaluating(
    monkeypatch,
    settings,
):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.SEMANTIC_SEARCH_ENABLED = False
    stop_event = threading.Event()
    sampled_controllers = []
    monkeypatch.setattr(run_owl, "resident_repository_workers_active", Mock(return_value=False))
    monkeypatch.setattr(run_owl, "set_resident_repository_workers_active", Mock())
    monkeypatch.setattr(run_owl, "_resident_worker_specs", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "_resident_bitbucket_worker_specs", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "ensure_recovery_scope", Mock())
    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", Mock())
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "stale_repository_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_extraction_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(run_owl, "stale_semantic_worker_pids", Mock(return_value=()))
    monkeypatch.setattr(
        run_owl,
        "sweep_pdf_extraction_queue",
        Mock(return_value=type("Recovery", (), {"interrupted_worker_pids": ()})()),
    )
    monkeypatch.setattr(run_owl, "reconcile_open_pipeline_runs", Mock())
    monkeypatch.setattr(
        run_owl,
        "_prepare_recovery_scope_launch",
        Mock(return_value=run_owl._WorkerLaunchPermission(allowed=False)),
    )
    monkeypatch.setattr(
        run_owl,
        "_reconcile_display_awake_for_current_queues",
        Mock(return_value=None),
    )
    monkeypatch.setattr(run_owl, "_stop_display_awake_assertion", Mock())

    def sample_fixed_fallback(**kwargs):
        sampled_controllers.append(kwargs["controller"])
        stop_event.set()

    monkeypatch.setattr(run_owl, "sample_pipeline_metrics", sample_fixed_fallback)

    run_owl._run_bitbucket_queue_loop(stop_event, poll_seconds=0)

    assert sampled_controllers == [None]


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


def test_paused_pdf_slot_is_not_relaunched(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 0

    run_owl._record_pdf_worker_failure(
        "pdf-index-1",
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
        probe=None,
    )

    recovery = PDFPipelineRecovery.objects.get(scope="extraction_slot:1")
    assert recovery.state == PDFPipelineRecoveryState.PAUSED
    assert run_owl._prepare_pdf_worker_launch("pdf-index-1").allowed is False


def test_due_pdf_recovery_launch_closes_only_after_stability(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 2
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 5
    run_owl._record_pdf_worker_failure(
        "pdf-writer-1",
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
        probe=None,
    )
    PDFPipelineRecovery.objects.filter(scope="publisher").update(
        next_retry_at=run_owl.timezone.now(),
    )

    permission = run_owl._prepare_pdf_worker_launch(
        "pdf-writer-1",
        monotonic_now=10,
    )
    assert permission.allowed is True
    assert permission.probe is not None
    recovery = PDFPipelineRecovery.objects.get(scope="publisher")
    assert recovery.state == PDFPipelineRecoveryState.RECOVERING
    assert recovery.lifetime_attempts == 1

    process = _ResidentProcess("bitbucket_pdf_writer", 808)
    probes = {"pdf-writer-1": permission.probe}
    run_owl._complete_stable_pdf_worker_probes(
        {"pdf-writer-1": process},
        probes,
        monotonic_now=14.9,
    )
    recovery.refresh_from_db()
    assert recovery.state == PDFPipelineRecoveryState.RECOVERING
    assert "pdf-writer-1" in probes

    run_owl._complete_stable_pdf_worker_probes(
        {"pdf-writer-1": process},
        probes,
        monotonic_now=15,
    )
    recovery.refresh_from_db()
    assert recovery.state == PDFPipelineRecoveryState.HEALTHY
    assert recovery.lifetime_attempts == 1
    assert recovery.consecutive_failed_attempts == 0
    assert probes == {}


def test_live_replacement_with_demand_needs_progress_before_recovery(
    tmp_path, settings, monkeypatch
):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 2
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 5
    job = _pdf_job(status=PDFExtractionJobStatus.RUNNING)
    PDFExtractionJob.objects.filter(pk=job.pk).update(
        phase=PDFExtractionJobPhase.PUBLISHING,
        worker_pid=None,
    )
    run_owl._record_pdf_worker_failure(
        "pdf-writer-1",
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
        probe=None,
    )
    PDFPipelineRecovery.objects.filter(scope="publisher").update(
        next_retry_at=run_owl.timezone.now(),
    )
    permission = run_owl._prepare_pdf_worker_launch("pdf-writer-1", monotonic_now=10)
    process = _ResidentProcess("bitbucket_pdf_writer", 808)
    probes = {"pdf-writer-1": permission.probe}
    stop_worker = Mock()
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", stop_worker)

    run_owl._complete_stable_pdf_worker_probes({"pdf-writer-1": process}, probes, monotonic_now=15)
    recovery = PDFPipelineRecovery.objects.get(scope="publisher")
    assert recovery.state == PDFPipelineRecoveryState.RECOVERING
    assert "pdf-writer-1" in probes

    run_owl._complete_stable_pdf_worker_probes({"pdf-writer-1": process}, probes, monotonic_now=20)
    recovery.refresh_from_db()
    assert recovery.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert recovery.reason_code == "no_forward_progress"
    assert recovery.lifetime_attempts == 1
    assert recovery.consecutive_failed_attempts == 1
    assert probes == {}
    stop_worker.assert_called_once_with(process)


def test_fresh_owned_publisher_heartbeat_satisfies_demand_stability(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 2
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 5
    job = _pdf_job(status=PDFExtractionJobStatus.RUNNING)
    PDFExtractionJob.objects.filter(pk=job.pk).update(
        phase=PDFExtractionJobPhase.PUBLISHING,
        worker_pid=808,
        heartbeat_at=run_owl.timezone.now(),
    )
    run_owl._record_pdf_worker_failure(
        "pdf-writer-1",
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
        probe=None,
    )
    PDFPipelineRecovery.objects.filter(scope="publisher").update(
        next_retry_at=run_owl.timezone.now(),
    )
    permission = run_owl._prepare_pdf_worker_launch("pdf-writer-1", monotonic_now=10)
    process = _ResidentProcess("bitbucket_pdf_writer", 808)
    probes = {"pdf-writer-1": permission.probe}

    run_owl._complete_stable_pdf_worker_probes({"pdf-writer-1": process}, probes, monotonic_now=15)

    recovery = PDFPipelineRecovery.objects.get(scope="publisher")
    assert recovery.state == PDFPipelineRecoveryState.HEALTHY
    assert probes == {}


def test_failed_replacement_probe_opens_circuit_at_threshold(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0
    run_owl._record_pdf_worker_failure(
        "pdf-index-2",
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
        probe=None,
    )
    PDFPipelineRecovery.objects.filter(scope="extraction_slot:2").update(
        next_retry_at=run_owl.timezone.now(),
    )
    permission = run_owl._prepare_pdf_worker_launch("pdf-index-2")
    assert permission.probe is not None

    run_owl._record_pdf_worker_failure(
        "pdf-index-2",
        reason_code=run_owl.RecoveryReasonCode.LAUNCH_FAILED,
        probe=permission.probe,
    )

    recovery = PDFPipelineRecovery.objects.get(scope="extraction_slot:2")
    assert recovery.state == PDFPipelineRecoveryState.PAUSED
    assert recovery.consecutive_failed_attempts == 1
    assert recovery.lifetime_attempts == 1
    assert run_owl._prepare_pdf_worker_launch("pdf-index-2").allowed is False


def test_correlated_slot_failures_escalate_once_to_extraction_pool(tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 25
    settings.PDF_PIPELINE_RECOVERY_CORRELATION_WINDOW_SECONDS = 10
    settings.PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT = 2

    run_owl._record_pdf_worker_failure(
        "pdf-index-1",
        reason_code=run_owl.RecoveryReasonCode.STALE_HEARTBEAT,
        probe=None,
    )
    assert PDFPipelineRecovery.objects.get(scope="extraction_pool").state == (
        PDFPipelineRecoveryState.HEALTHY
    )

    run_owl._record_pdf_worker_failure(
        "pdf-index-2",
        reason_code=run_owl.RecoveryReasonCode.STALE_HEARTBEAT,
        probe=None,
    )

    owner = PDFPipelineRecovery.objects.get(scope="extraction_pool")
    assert owner.state == PDFPipelineRecoveryState.RETRY_WAIT
    assert owner.lifetime_attempts == 0
    assert owner.consecutive_failed_attempts == 0
    assert set(
        PDFPipelineRecovery.objects.filter(
            scope__in=("extraction_slot:1", "extraction_slot:2")
        ).values_list("state", flat=True)
    ) == {PDFPipelineRecoveryState.HEALTHY}
    assert run_owl._prepare_pdf_worker_launch("pdf-index-1").allowed is False

    run_owl._record_pdf_worker_failure(
        "pdf-index-3",
        reason_code=run_owl.RecoveryReasonCode.STALE_HEARTBEAT,
        probe=None,
    )
    owner.refresh_from_db()
    assert owner.generation == 1
    assert not PDFPipelineRecovery.objects.filter(scope="extraction_slot:3").exists()


def test_extraction_pool_probe_allows_children_and_closes_after_idle_stability(
    tmp_path,
    settings,
):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    settings.PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = 25
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = 1
    settings.PDF_PIPELINE_RECOVERY_JITTER_FRACTION = 0
    settings.PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = 5
    incident = run_owl.record_recovery_incident(
        run_owl.RecoveryScope.EXTRACTION_POOL,
        reason_code=run_owl.RecoveryReasonCode.PROCESS_EXIT,
    ).transition
    PDFPipelineRecovery.objects.filter(pk=incident.recovery.pk).update(
        next_retry_at=run_owl.timezone.now()
    )

    permission = run_owl._maintain_parent_recovery_probe(
        run_owl.RecoveryScope.EXTRACTION_POOL.value,
        None,
        monotonic_now=10,
    )
    assert permission.allowed is True
    assert permission.probe is not None
    child_permission = run_owl._prepare_pdf_worker_launch(
        "pdf-index-1",
        active_parent_scopes=frozenset({run_owl.RecoveryScope.EXTRACTION_POOL.value}),
    )
    assert child_permission.allowed is True

    process = _ResidentProcess("bitbucket_index_worker", 808)
    remaining = run_owl._complete_parent_recovery_probe(
        run_owl.RecoveryScope.EXTRACTION_POOL.value,
        {"pdf-index-1": process},
        permission.probe,
        monotonic_now=15,
    )

    assert remaining is None
    recovery = PDFPipelineRecovery.objects.get(scope="extraction_pool")
    assert recovery.state == PDFPipelineRecoveryState.HEALTHY
    assert recovery.lifetime_attempts == 1


def test_index_worker_exits_a_caught_database_error_loop_for_supervisor_recovery(
    monkeypatch,
    settings,
):
    settings.PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD = 2
    monkeypatch.setattr(
        bitbucket_index_worker,
        "resident_supervisor_is_alive",
        Mock(return_value=True),
    )
    work = Mock(side_effect=OperationalError("synthetic locked loop"))
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", work)
    monkeypatch.setattr(bitbucket_index_worker.time, "sleep", Mock())

    with pytest.raises(OperationalError):
        call_command("bitbucket_index_worker", "--no-startup-sweep")

    assert work.call_count == 2
