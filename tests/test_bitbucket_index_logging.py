from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, transaction

from bitbucket_search.management.commands import (
    bitbucket_index_worker,
    bitbucket_rebuild_search_index,
    bitbucket_reindex_pdfs,
    bitbucket_sync_worker,
)
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFPageExtractionState,
    PDFTextRevisionState,
    RepositoryOperationLogChannel,
    RepositorySyncState,
)
from bitbucket_search.services import pdf_indexing
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.logging_events import log_event
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_lock import repository_checkout_lock
from bookmark_manager.management.commands import run_owl

pytestmark = pytest.mark.django_db
_PRIVATE = "synthetic-private-value-not-for-logs"


@pytest.fixture
def captured_logs(caplog):
    logger = logging.getLogger("owl.bitbucket")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="owl.bitbucket")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def pdf_target(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "private-checkouts"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "private-parser-staging"
    repository = BitbucketRepository.objects.create(
        display_name=_PRIVATE,
        canonical_remote_key="example.invalid/team/private-logging-fixture",
        remote_url="https://example.invalid/team/private-logging-fixture.git",
        sync_state=RepositorySyncState.READY,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    path = checkout / f"{_PRIVATE}.pdf"
    path.write_bytes(f"%PDF {_PRIVATE}".encode())
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=path.name,
        relative_path=path.name,
        file_size=path.stat().st_size,
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )
    return repository, document, path


def _claimed(repository):
    pdf_indexing.queue_repository_pdf_extractions(repository)
    return pdf_indexing.claim_next_extraction_job()


def _stage(path: Path, state=PDFTextRevisionState.READY):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pages = ()
    if state in {PDFTextRevisionState.READY, PDFTextRevisionState.PARTIAL}:
        pages = (
            pdf_indexing.StagedPDFPage(1, _PRIVATE, len(_PRIVATE), PDFPageExtractionState.READY),
        )
        if state == PDFTextRevisionState.PARTIAL:
            pages += (
                pdf_indexing.StagedPDFPage(2, "", 0, PDFPageExtractionState.FAILED, _PRIVATE),
            )
    return pdf_indexing.StagedPDFExtraction(
        state=state,
        pages=pages,
        page_count=len(pages),
        extracted_character_count=sum(page.character_count for page in pages),
        source_size_bytes=path.stat().st_size,
        content_sha256_before=digest,
        content_sha256_after=digest,
        extractor_version=PDF_EXTRACTOR_VERSION,
        error_code=_PRIVATE,
        error_summary=_PRIVATE,
    )


def _event(capture, event, *, level=None):
    matches = [record for record in capture.records if f"event={event}" in record.getMessage()]
    assert matches, event
    if level is not None:
        assert all(record.levelno == level for record in matches)
    return matches


def _assert_private(capture, path=None):
    assert _PRIVATE not in capture.text
    assert "https://" not in capture.text
    assert "Traceback (most recent call last)" not in capture.text
    if path is not None:
        assert str(path) not in capture.text
    assert all(record.exc_info is None for record in capture.records)


def test_queue_claim_progress_and_publication_log_counts_ids_and_duration(
    pdf_target, captured_logs, django_capture_on_commit_callbacks
):
    repository, document, path = pdf_target
    with django_capture_on_commit_callbacks(execute=True):
        job = _claimed(repository)
    completed = pdf_indexing.execute_claimed_extraction_job(
        job.pk, extraction_runner=lambda *_args: _stage(path)
    )

    assert completed.status == PDFExtractionJobStatus.SUCCEEDED
    _event(captured_logs, "pdf_extraction_queue_reconciled", level=logging.INFO)
    _event(captured_logs, "pdf_extraction_candidate_selected", level=logging.DEBUG)
    _event(captured_logs, "pdf_extraction_claimed", level=logging.INFO)
    progress = _event(captured_logs, "pdf_extraction_progress", level=logging.DEBUG)
    assert all(f"repository_id={repository.pk}" in record.getMessage() for record in progress)
    assert all(f"document_id={document.pk}" in record.getMessage() for record in progress)
    published = _event(captured_logs, "pdf_text_published", level=logging.INFO)[0].getMessage()
    assert "page_count=1" in published
    assert "indexed_count=1" in published
    assert "elapsed_ms=" in _event(captured_logs, "pdf_extraction_finished")[0].getMessage()
    log_event(pdf_indexing.logger, logging.INFO, "after_extraction_context")
    assert "job_id=" not in _event(captured_logs, "after_extraction_context")[0].getMessage()
    _assert_private(captured_logs, path)


def test_rolled_back_queue_does_not_emit_success_event(
    pdf_target, captured_logs, django_capture_on_commit_callbacks
):
    repository, _document, _path = pdf_target
    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        pdf_indexing.queue_repository_pdf_extractions(repository)
        raise RuntimeError("synthetic publication rollback")

    assert not PDFExtractionJob.objects.exists()
    assert "event=pdf_extraction_queue_reconciled" not in captured_logs.text


def test_repeated_identical_heartbeat_progress_is_logged_once(pdf_target, captured_logs):
    repository, _document, path = pdf_target
    job = _claimed(repository)

    def extract(_path, heartbeat):
        heartbeat()
        heartbeat()
        heartbeat()
        return _stage(path)

    pdf_indexing.execute_claimed_extraction_job(job.pk, extraction_runner=extract)

    repeated = [
        record
        for record in _event(captured_logs, "pdf_extraction_progress")
        if "phase=extracting" in record.getMessage() and "progress=60" in record.getMessage()
    ]
    assert len(repeated) == 1
    entries = list(job.operation_log_entries.order_by("id"))
    assert [(entry.event, entry.phase, entry.progress) for entry in entries] == [
        ("indexing_claimed", "validating", 1),
        ("indexing_phase_changed", "validating", 5),
        ("indexing_phase_changed", "hashing", 10),
        ("indexing_phase_changed", "extracting", 20),
        ("indexing_phase_changed", "publishing", 95),
        ("indexing_completed", "completed", 100),
    ]
    phase_entries = [entry for entry in entries if entry.event == "indexing_phase_changed"]
    assert [entry.phase for entry in phase_entries] == [
        "validating",
        "hashing",
        "extracting",
        "publishing",
    ]
    assert sum(entry.phase == "extracting" for entry in phase_entries) == 1
    assert entries[-1].event == "indexing_completed"
    assert entries[-1].progress == 100
    assert all(entry.channel == RepositoryOperationLogChannel.INDEXING for entry in entries)
    assert _PRIVATE not in "\n".join(entry.message for entry in entries)


def test_progress_and_completion_events_share_their_state_transactions(pdf_target, monkeypatch):
    repository, _document, path = pdf_target
    observed: list[tuple[str, bool]] = []
    original_append = pdf_indexing.append_operation_log_entry_safely

    def record_transaction(**values):
        observed.append((values["event"], connection.in_atomic_block))
        return original_append(**values)

    monkeypatch.setattr(
        pdf_indexing,
        "append_operation_log_entry_safely",
        record_transaction,
    )
    job = _claimed(repository)

    completed = pdf_indexing.execute_claimed_extraction_job(
        job.pk, extraction_runner=lambda *_args: _stage(path)
    )

    assert completed.status == PDFExtractionJobStatus.SUCCEEDED
    durable_transitions = [
        inside_transaction
        for event, inside_transaction in observed
        if event in {"indexing_phase_changed", "indexing_completed"}
    ]
    assert durable_transitions
    assert all(durable_transitions)


def test_failure_event_shares_the_terminal_state_transaction(pdf_target, monkeypatch):
    repository, _document, path = pdf_target
    observed: list[tuple[str, bool]] = []
    original_append = pdf_indexing.append_operation_log_entry_safely

    def record_transaction(**values):
        observed.append((values["event"], connection.in_atomic_block))
        return original_append(**values)

    monkeypatch.setattr(
        pdf_indexing,
        "append_operation_log_entry_safely",
        record_transaction,
    )
    job = _claimed(repository)

    completed = pdf_indexing.execute_claimed_extraction_job(
        job.pk, extraction_runner=lambda *_args: _stage(path, "corrupt")
    )

    assert completed.status == PDFExtractionJobStatus.FAILED
    assert ("indexing_failed", True) in observed


@pytest.mark.parametrize("state", ["corrupt", "encrypted", "resource_limit"])
def test_parser_failures_are_error_events_without_payload_text(pdf_target, captured_logs, state):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    completed = pdf_indexing.execute_claimed_extraction_job(
        job.pk, extraction_runner=lambda *_args: _stage(path, state)
    )

    assert completed.status == PDFExtractionJobStatus.FAILED
    event = _event(captured_logs, "pdf_extraction_failed", level=logging.ERROR)[0].getMessage()
    assert f"repository_id={repository.pk}" in event
    assert f"job_id={job.pk}" in event
    assert "error_type=PDFIndexingError" in event
    _assert_private(captured_logs, path)


def test_partial_publication_and_failed_pages_are_errors_not_warnings(pdf_target, captured_logs):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    completed = pdf_indexing.execute_claimed_extraction_job(
        job.pk, extraction_runner=lambda *_args: _stage(path, PDFTextRevisionState.PARTIAL)
    )

    assert completed.status == PDFExtractionJobStatus.SUCCEEDED
    page_event = _event(captured_logs, "pdf_page_extraction_failed", level=logging.ERROR)[0]
    assert "failed_count=1" in page_event.getMessage()
    assert "page_count=2" in page_event.getMessage()
    _event(captured_logs, "pdf_partial_publication", level=logging.ERROR)
    _assert_private(captured_logs, path)


@pytest.mark.parametrize("code", ["pdf_extraction_timeout", _PRIVATE])
def test_failure_code_is_known_and_exception_message_is_never_logged(
    pdf_target, captured_logs, code
):
    repository, _document, path = pdf_target
    job = _claimed(repository)

    def fail(*_args):
        raise pdf_indexing.PDFIndexingError(code, _PRIVATE)

    pdf_indexing.execute_claimed_extraction_job(job.pk, extraction_runner=fail)

    message = _event(captured_logs, "pdf_extraction_failed", level=logging.ERROR)[0].getMessage()
    assert f"error_code={'pdf_indexing_error' if code == _PRIVATE else code}" in message
    operation_entry = job.operation_log_entries.order_by("id").last()
    assert operation_entry.event == "indexing_failed"
    assert operation_entry.severity == "error"
    assert _PRIVATE not in operation_entry.message
    _assert_private(captured_logs, path)


def test_original_failure_is_logged_even_if_failure_state_write_also_fails(
    pdf_target, captured_logs, monkeypatch
):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    monkeypatch.setattr(PDFExtractionJob, "save", Mock(side_effect=OperationalError(_PRIVATE)))

    def fail(*_args):
        raise pdf_indexing.PDFIndexingError("corrupt_pdf", _PRIVATE)

    with pytest.raises(OperationalError):
        pdf_indexing.execute_claimed_extraction_job(job.pk, extraction_runner=fail)

    _event(captured_logs, "pdf_extraction_failed", level=logging.ERROR)
    persistence = _event(captured_logs, "pdf_indexing_operation_failed", level=logging.ERROR)[0]
    assert "error_type=OperationalError" in persistence.getMessage()
    assert f"document_id={job.document_id}" in persistence.getMessage()
    _assert_private(captured_logs, path)


def test_unexpected_parser_failure_has_safe_error_type(pdf_target, captured_logs):
    repository, _document, path = pdf_target
    job = _claimed(repository)

    def fail(*_args):
        raise RuntimeError(_PRIVATE)

    pdf_indexing.execute_claimed_extraction_job(job.pk, extraction_runner=fail)

    message = _event(captured_logs, "pdf_extraction_unexpected_error", level=logging.ERROR)[
        0
    ].getMessage()
    assert "error_type=RuntimeError" in message
    _assert_private(captured_logs, path)


def test_intentional_checkout_deferral_is_warning_without_error(pdf_target, captured_logs):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    captured_logs.clear()
    with repository_checkout_lock(repository.pk, blocking=False):
        deferred = pdf_indexing.execute_claimed_extraction_job(job.pk)

    assert deferred.status == PDFExtractionJobStatus.QUEUED
    _event(captured_logs, "pdf_extraction_deferred", level=logging.WARNING)
    operation_entry = job.operation_log_entries.order_by("id").last()
    assert operation_entry.event == "indexing_deferred"
    assert operation_entry.phase == "queued"
    assert operation_entry.progress == 0
    assert not [record for record in captured_logs.records if record.levelno >= logging.ERROR]
    _assert_private(captured_logs, path)


def test_empty_claim_and_sweep_do_not_emit_poll_noise(captured_logs):
    assert pdf_indexing.claim_next_extraction_job() is None
    assert pdf_indexing.sweep_pdf_extraction_queue().queued_job_ids == ()
    assert not captured_logs.records


def test_stale_lease_interruption_is_logged_before_document_failure_write(
    pdf_target, captured_logs, monkeypatch
):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    monkeypatch.setattr(PDFDocument, "save", Mock(side_effect=OperationalError(_PRIVATE)))

    with pytest.raises(OperationalError):
        pdf_indexing.sweep_pdf_extraction_queue(interrupt_running=True)

    message = _event(captured_logs, "pdf_extraction_worker_interrupted", level=logging.ERROR)[
        0
    ].getMessage()
    assert f"job_id={job.pk}" in message
    _event(captured_logs, "pdf_indexing_operation_failed", level=logging.ERROR)
    _assert_private(captured_logs, path)


@pytest.mark.parametrize(
    "failure_target", ["sweep_pdf_extraction_queue", "work_one_extraction_job"]
)
def test_index_worker_logs_swallowed_database_failures(monkeypatch, captured_logs, failure_target):
    monkeypatch.setattr(bitbucket_index_worker, "sweep_pdf_extraction_queue", Mock())
    monkeypatch.setattr(bitbucket_index_worker, "work_one_extraction_job", Mock(return_value=None))
    monkeypatch.setattr(
        bitbucket_index_worker, failure_target, Mock(side_effect=OperationalError(_PRIVATE))
    )

    call_command("bitbucket_index_worker", "--once")

    event = (
        "pdf_worker_queue_recovery_failed"
        if failure_target == "sweep_pdf_extraction_queue"
        else "pdf_worker_database_failed"
    )
    _event(captured_logs, event, level=logging.ERROR)
    _assert_private(captured_logs)


def test_repository_worker_logs_swallowed_database_failure(monkeypatch, captured_logs):
    monkeypatch.setattr(
        bitbucket_sync_worker, "work_one_job", Mock(side_effect=OperationalError(_PRIVATE))
    )
    call_command("bitbucket_sync_worker", "--once", "--repository-only")
    _event(captured_logs, "repository_worker_database_failed", level=logging.ERROR)
    _assert_private(captured_logs)


@pytest.mark.parametrize(
    "command,module,function,event",
    [
        (
            "bitbucket_index_worker",
            bitbucket_index_worker,
            "work_one_extraction_job",
            "pdf_index_worker_crashed",
        ),
        (
            "bitbucket_sync_worker",
            bitbucket_sync_worker,
            "work_one_job",
            "repository_worker_crashed",
        ),
    ],
)
def test_unhandled_worker_failure_is_critical_and_still_raised(
    monkeypatch, captured_logs, command, module, function, event
):
    monkeypatch.setattr(module, "sweep_pdf_extraction_queue", Mock())
    monkeypatch.setattr(module, function, Mock(side_effect=RuntimeError(_PRIVATE)))
    with pytest.raises(RuntimeError):
        call_command(command, "--once")
    _event(captured_logs, event, level=logging.CRITICAL)
    _assert_private(captured_logs)


def test_reindex_spawn_failure_is_error_without_subprocess_data(
    pdf_target, captured_logs, monkeypatch
):
    monkeypatch.setattr(
        bitbucket_reindex_pdfs,
        "queue_repository_pdf_extractions",
        Mock(return_value=pdf_indexing.ExtractionQueueResult((42,), (), ())),
    )
    launch = Mock(side_effect=OSError(13, _PRIVATE, f"/private/{_PRIVATE}"))
    monkeypatch.setattr(bitbucket_reindex_pdfs, "launch_index_worker", launch)

    call_command("bitbucket_reindex_pdfs")

    message = _event(captured_logs, "pdf_reindex_worker_spawn_failed", level=logging.ERROR)[
        0
    ].getMessage()
    assert "errno=13" in message
    _assert_private(captured_logs)


@pytest.mark.parametrize("available", [False, True])
def test_search_rebuild_configuration_and_runtime_failures_are_errors(
    monkeypatch, captured_logs, available
):
    monkeypatch.setattr(
        bitbucket_rebuild_search_index, "search_index_available", Mock(return_value=available)
    )
    monkeypatch.setattr(
        bitbucket_rebuild_search_index,
        "rebuild_search_index",
        Mock(side_effect=OperationalError(_PRIVATE)),
    )
    with pytest.raises(OperationalError if available else CommandError):
        call_command("bitbucket_rebuild_search_index")
    _event(captured_logs, "pdf_search_rebuild_failed", level=logging.ERROR)
    _assert_private(captured_logs)


def test_detached_launch_failure_logs_safe_os_code(monkeypatch, captured_logs):
    monkeypatch.setattr(
        pdf_indexing.subprocess,
        "Popen",
        Mock(side_effect=OSError(13, _PRIVATE, f"/private/{_PRIVATE}")),
    )
    with pytest.raises(OSError):
        pdf_indexing.launch_index_worker()
    message = _event(captured_logs, "pdf_index_worker_spawn_failed", level=logging.ERROR)[
        0
    ].getMessage()
    assert "errno=13" in message
    _assert_private(captured_logs)


def test_isolated_parser_spawn_failure_retains_ids_and_os_code(
    pdf_target, captured_logs, monkeypatch
):
    repository, document, path = pdf_target
    job = _claimed(repository)
    monkeypatch.setattr(
        pdf_indexing.subprocess, "Popen", Mock(side_effect=OSError(13, _PRIVATE, str(path)))
    )

    completed = pdf_indexing.execute_claimed_extraction_job(job.pk)

    assert completed.status == PDFExtractionJobStatus.FAILED
    message = _event(captured_logs, "pdf_parser_spawn_failed", level=logging.ERROR)[0].getMessage()
    assert "errno=13" in message
    assert f"repository_id={repository.pk}" in message
    assert f"document_id={document.pk}" in message
    assert f"job_id={job.pk}" in message
    _assert_private(captured_logs, path)


def test_isolated_parser_timeout_is_error_and_process_is_reaped(
    pdf_target, captured_logs, monkeypatch, settings
):
    repository, _document, path = pdf_target
    job = _claimed(repository)
    settings.PDF_EXTRACTION_TIMEOUT_SECONDS = 0
    process = Mock(args=[_PRIVATE])
    process.poll.return_value = None
    monkeypatch.setattr(pdf_indexing.subprocess, "Popen", Mock(return_value=process))

    completed = pdf_indexing.execute_claimed_extraction_job(job.pk)

    assert completed.error_code == "pdf_extraction_timeout"
    _event(captured_logs, "pdf_parser_timeout", level=logging.ERROR)
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()
    _assert_private(captured_logs, path)


def test_supervisor_recovery_failure_is_error_without_raw_traceback(monkeypatch, captured_logs):
    stop = threading.Event()

    def fail():
        stop.set()
        raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", fail)
    run_owl._bitbucket_queue_loop(stop, poll_seconds=0)
    message = _event(captured_logs, "bitbucket_supervisor_pass_failed", level=logging.ERROR)[
        0
    ].getMessage()
    assert "stage=daily_schedule" in message
    assert "error_type=RuntimeError" in message
    _assert_private(captured_logs)


def test_supervisor_logs_abnormal_child_exit(monkeypatch, captured_logs):
    stop = threading.Event()
    checks = 0

    def schedule():
        nonlocal checks
        checks += 1
        if checks == 2:
            stop.set()

    monkeypatch.setattr(run_owl, "queue_due_daily_repository_refreshes", schedule)
    monkeypatch.setattr(run_owl, "repository_status_snapshot", Mock())
    monkeypatch.setattr(run_owl, "sweep_pdf_extraction_queue", Mock())
    monkeypatch.setattr(
        run_owl,
        "_resident_bitbucket_worker_specs",
        lambda: (("repository-sync-1", "bitbucket_sync_worker"),),
    )
    monkeypatch.setattr(
        run_owl,
        "_launch_resident_bitbucket_worker",
        Mock(return_value=SimpleNamespace(pid=123, poll=lambda: 7)),
    )
    monkeypatch.setattr(run_owl, "_stop_resident_bitbucket_worker", Mock())

    run_owl._bitbucket_queue_loop(stop, poll_seconds=0)

    message = _event(captured_logs, "resident_worker_exited", level=logging.ERROR)[0].getMessage()
    assert "return_code=7" in message
    assert "worker_pid=123" in message
    _assert_private(captured_logs)


def test_supervisor_force_stop_failure_is_error(monkeypatch, captured_logs):
    process = Mock(pid=123)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(_PRIVATE, 5)
    if os.name != "nt":
        monkeypatch.setattr(run_owl.os, "killpg", Mock())

    run_owl._stop_resident_bitbucket_worker(process)

    _event(captured_logs, "resident_worker_stop_timeout", level=logging.ERROR)
    _event(captured_logs, "resident_worker_force_stop_failed", level=logging.ERROR)
    _assert_private(captured_logs)


def test_unrecoverable_supervisor_exit_is_critical_and_reraised(monkeypatch, captured_logs):
    monkeypatch.setattr(
        run_owl, "_run_bitbucket_queue_loop", Mock(side_effect=RuntimeError(_PRIVATE))
    )
    with pytest.raises(RuntimeError):
        run_owl._bitbucket_queue_loop(threading.Event(), poll_seconds=0)
    _event(captured_logs, "bitbucket_supervisor_crashed", level=logging.CRITICAL)
    _assert_private(captured_logs)
