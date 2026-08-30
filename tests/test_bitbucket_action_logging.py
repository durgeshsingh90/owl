from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import DatabaseError, OperationalError
from django.test import RequestFactory

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncState,
)
from bitbucket_search.services import (
    document_actions,
    git_sync,
    pdf_local_policy,
    pdf_search,
    people,
)
from bitbucket_search.services.document_actions import DocumentActionError
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.pdf_search_query import PDFSearchQuery

pytestmark = pytest.mark.django_db
PRIVATE_TEXT = "PRIVATE_PDF_CONTENT_TOKEN_AND_SEARCH_TERM"


@pytest.fixture
def bitbucket_logs(caplog):
    logger = logging.getLogger("owl.bitbucket")
    caplog.set_level(logging.DEBUG, logger="owl.bitbucket")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


@pytest.fixture
def logging_pdf(tmp_path: Path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    repository = BitbucketRepository.objects.create(
        display_name=PRIVATE_TEXT,
        canonical_remote_key="example.invalid/logging-fixture",
        remote_url=f"https://example.invalid/{PRIVATE_TEXT}.git",
        sync_state=RepositorySyncState.READY,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    path = checkout / f"{PRIVATE_TEXT}.pdf"
    path.write_bytes(b"synthetic PDF bytes for logging tests")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=path.name,
        relative_path=path.name,
        file_size=path.stat().st_size,
    )
    return document


def _events(capture, event: str | None = None):
    return [
        record
        for record in capture.records
        if record.name.startswith("owl.bitbucket.")
        and (event is None or f"event={event} " in f"{record.getMessage()} ")
    ]


def _assert_private(capture):
    for record in _events(capture):
        assert PRIVATE_TEXT not in record.getMessage()
        assert "https://" not in record.getMessage()
        assert "synthetic PDF bytes" not in record.getMessage()
        assert record.exc_info is None


def test_direct_open_logs_request_completion_and_safe_ids(logging_pdf, bitbucket_logs, monkeypatch):
    monkeypatch.setattr(document_actions, "open_pdf_native", Mock())

    result = document_actions.open_registered_pdf(logging_pdf.pk)

    assert result.open_count == 1
    requested = _events(bitbucket_logs, "pdf_action_requested")
    completed = _events(bitbucket_logs, "pdf_action_completed")
    assert len(requested) == len(completed) == 1
    assert requested[0].levelno == completed[0].levelno == logging.INFO
    assert f"document_id={logging_pdf.pk}" in completed[0].getMessage()
    assert f"repository_id={logging_pdf.repository_id}" in completed[0].getMessage()
    assert "elapsed_ms=" in completed[0].getMessage()
    _assert_private(bitbucket_logs)


def test_native_failure_is_error_without_exception_message(
    logging_pdf, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(
        document_actions,
        "open_pdf_native",
        Mock(side_effect=DocumentActionError("native_action_failed", PRIVATE_TEXT)),
    )

    with pytest.raises(DocumentActionError):
        document_actions.open_registered_pdf(logging_pdf.pk)

    failed = _events(bitbucket_logs, "pdf_action_failed")
    assert len(failed) == 1
    assert failed[0].levelno == logging.ERROR
    assert "stage=native" in failed[0].getMessage()
    assert "error_code=native_action_failed" in failed[0].getMessage()
    assert "frames=" in failed[0].getMessage()
    assert not _events(bitbucket_logs, "pdf_action_completed")
    _assert_private(bitbucket_logs)


def test_bulk_usage_failure_is_error_without_raw_traceback(
    logging_pdf, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(document_actions, "open_pdf_native", Mock())
    monkeypatch.setattr(
        document_actions, "record_successful_open", Mock(side_effect=DatabaseError(PRIVATE_TEXT))
    )

    result = document_actions.open_registered_pdfs((logging_pdf.pk,))

    assert result.opened_count == 1
    assert result.usage_failure_count == 1
    failure = _events(bitbucket_logs, "pdf_usage_update_failed")[0]
    assert failure.levelno == logging.ERROR
    assert f"document_id={logging_pdf.pk}" in failure.getMessage()
    assert "error_type=DatabaseError" in failure.getMessage()
    _assert_private(bitbucket_logs)


def test_expected_action_validation_is_warning_not_error(bitbucket_logs):
    with pytest.raises(DocumentActionError):
        document_actions.open_registered_pdfs(())

    failure = _events(bitbucket_logs, "pdf_action_failed")[0]
    assert failure.levelno == logging.WARNING
    assert "error_code=invalid_document_selection" in failure.getMessage()
    assert not any(record.levelno >= logging.ERROR for record in _events(bitbucket_logs))


def test_policy_confirmation_rejection_is_warning(bitbucket_logs):
    with pytest.raises(DocumentActionError):
        pdf_local_policy.delete_registered_pdf(99, confirmed=False)

    failure = _events(bitbucket_logs, "pdf_policy_failed")[0]
    assert failure.levelno == logging.WARNING
    assert "operation=delete" in failure.getMessage()
    assert "document_id=99" in failure.getMessage()
    assert "error_code=pdf_delete_confirmation_required" in failure.getMessage()


def test_policy_operational_failure_is_error(bitbucket_logs, monkeypatch):
    monkeypatch.setattr(pdf_local_policy, "_change_pdf", Mock(side_effect=OSError(PRIVATE_TEXT)))

    with pytest.raises(OSError):
        pdf_local_policy.exclude_registered_pdf(87)

    failure = _events(bitbucket_logs, "pdf_policy_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "document_id=87" in failure.getMessage()
    _assert_private(bitbucket_logs)


def test_caught_policy_rollback_failure_is_separately_error_logged(
    logging_pdf, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(
        git_sync,
        "require_clean_document_checkout",
        Mock(side_effect=RepositorySyncError("status_failed", PRIVATE_TEXT)),
    )
    monkeypatch.setattr(
        pdf_local_policy._FileChanges,
        "restore",
        Mock(side_effect=PermissionError(PRIVATE_TEXT)),
    )

    with pytest.raises(DocumentActionError) as captured:
        pdf_local_policy.exclude_registered_pdf(logging_pdf.pk)

    assert captured.value.code == "pdf_policy_rollback_failed"
    failure = _events(bitbucket_logs, "pdf_policy_rollback_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "stage=file_restore" in failure.getMessage()
    assert f"document_id={logging_pdf.pk}" in failure.getMessage()
    _assert_private(bitbucket_logs)


def test_post_commit_snapshot_cleanup_failure_is_error_not_warning(
    logging_pdf, bitbucket_logs, monkeypatch, django_capture_on_commit_callbacks
):
    PDFLocalPolicy.objects.create(
        repository=logging_pdf.repository,
        document=logging_pdf,
        relative_path=logging_pdf.relative_path,
        state=PDFLocalPolicyState.RESUMING,
    )
    snapshot = Mock()
    snapshot.unlink.side_effect = OSError(PRIVATE_TEXT)
    monkeypatch.setattr(pdf_local_policy, "policy_snapshot_path", Mock(return_value=snapshot))

    with django_capture_on_commit_callbacks(execute=True):
        pdf_local_policy.complete_resumed_policies(
            logging_pdf.repository_id, (logging_pdf.relative_path,)
        )

    failure = _events(bitbucket_logs, "pdf_snapshot_cleanup_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "stage=post_commit_cleanup" in failure.getMessage()
    assert f"repository_id={logging_pdf.repository_id}" in failure.getMessage()
    _assert_private(bitbucket_logs)


def test_search_debug_logs_only_timing_counts_not_terms(bitbucket_logs):
    result = pdf_search.search_documents(PDFSearchQuery(chips=(PRIVATE_TEXT,)))

    assert result.total == 0
    requested = _events(bitbucket_logs, "pdf_search_requested")[0]
    completed = _events(bitbucket_logs, "pdf_search_completed")[0]
    assert requested.levelno == completed.levelno == logging.DEBUG
    assert "count=1" in requested.getMessage()
    assert "result_count=0" in completed.getMessage()
    assert "elapsed_ms=" in completed.getMessage()
    _assert_private(bitbucket_logs)


def test_missing_search_index_is_error_logged(bitbucket_logs, monkeypatch):
    cursor = Mock()
    cursor.fetchone.return_value = (0,)
    context = Mock()
    context.__enter__ = Mock(return_value=cursor)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(pdf_search.connection, "cursor", Mock(return_value=context))

    assert pdf_search.search_index_available() is False
    assert _events(bitbucket_logs, "pdf_search_index_missing")[0].levelno == logging.ERROR


@pytest.mark.parametrize("stage", ["index", "candidate"])
def test_caught_search_sql_errors_are_logged_without_sql_or_terms(
    stage, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(
        pdf_search.connection, "cursor", Mock(side_effect=DatabaseError(PRIVATE_TEXT))
    )

    if stage == "index":
        assert pdf_search.search_index_available() is False
        failure = _events(bitbucket_logs, "pdf_search_index_check_failed")[0]
    else:
        assert pdf_search._query_candidate_page(PDFSearchQuery(chips=(PRIVATE_TEXT,))) == ((), 0)
        failure = _events(bitbucket_logs, "pdf_search_sql_failed")[0]
    assert failure.levelno == logging.ERROR
    _assert_private(bitbucket_logs)


def test_worker_wakeup_exception_is_error_logged_with_job_id(bitbucket_logs, monkeypatch):
    monkeypatch.setattr(views, "resident_repository_workers_active", Mock(return_value=False))
    monkeypatch.setattr(
        views,
        "reserve_queued_repository_worker_wakeups",
        Mock(return_value=SimpleNamespace(job_ids=(71, 72))),
    )
    monkeypatch.setattr(views, "launch_sync_worker", Mock(side_effect=OSError(PRIVATE_TEXT)))
    release = Mock()
    monkeypatch.setattr(views, "release_repository_worker_wakeups", release)

    assert views._wake_queued_repository_workers() == (0, True)
    failure = _events(bitbucket_logs, "repository_worker_wakeup_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "job_id=71" in failure.getMessage()
    assert "failed_count=2" in failure.getMessage()
    release.assert_called_once()
    _assert_private(bitbucket_logs)


def test_swallowed_schedule_database_failure_is_error_logged(bitbucket_logs, monkeypatch):
    request = RequestFactory().post("/pdfs/repositories/schedule/tick/", REMOTE_ADDR="127.0.0.1")
    request._dont_enforce_csrf_checks = True
    monkeypatch.setattr(
        views,
        "queue_due_daily_repository_refreshes",
        Mock(side_effect=OperationalError(PRIVATE_TEXT)),
    )

    assert views.tick_repository_schedule(request).status_code == 202
    failure = _events(bitbucket_logs, "repository_schedule_request_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "stage=schedule_tick" in failure.getMessage()
    _assert_private(bitbucket_logs)


@pytest.mark.parametrize(
    ("view_name", "dependency"),
    [
        ("index", "_index_context"),
        ("repositories", "_index_context"),
        ("document_page", "_index_context"),
        ("repository_status", "repository_status_snapshot"),
        ("index_status", "repository_status_snapshot"),
    ],
)
def test_read_view_failures_are_logged_in_bitbucket_namespace(
    view_name, dependency, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(views, dependency, Mock(side_effect=OperationalError(PRIVATE_TEXT)))
    request = RequestFactory().get("/pdfs/", REMOTE_ADDR="127.0.0.1")

    with pytest.raises(OperationalError):
        getattr(views, view_name)(request)

    failure = _events(bitbucket_logs, "web_action_failed")[0]
    assert failure.levelno == logging.ERROR
    assert not _events(bitbucket_logs, "web_action_requested")
    _assert_private(bitbucket_logs)


def test_empty_status_and_schedule_polls_do_not_emit_logs(bitbucket_logs, monkeypatch):
    factory = RequestFactory()
    status = factory.get("/pdfs/repositories/status/", REMOTE_ADDR="127.0.0.1")
    assert views.repository_status(status).status_code == 200
    monkeypatch.setattr(views, "queue_due_daily_repository_refreshes", Mock(return_value=()))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", Mock(return_value=(0, False)))
    tick = factory.post("/pdfs/repositories/schedule/tick/", REMOTE_ADDR="127.0.0.1")
    tick._dont_enforce_csrf_checks = True
    assert views.tick_repository_schedule(tick).status_code == 200
    assert not _events(bitbucket_logs)


def test_people_group_validation_logs_without_names(bitbucket_logs):
    with pytest.raises(people.PeopleGroupValidationError):
        people.create_people_group(PRIVATE_TEXT, (PRIVATE_TEXT,))

    failure = _events(bitbucket_logs, "people_group_create_rejected")[0]
    assert failure.levelno == logging.WARNING
    _assert_private(bitbucket_logs)


@pytest.mark.parametrize(
    ("view_name", "dependency"),
    [
        ("add_people_group", "create_people_group"),
        ("tick_repository_schedule", "queue_due_daily_repository_refreshes"),
    ],
)
def test_remaining_mutation_failures_are_logged_without_poll_success_noise(
    view_name, dependency, bitbucket_logs, monkeypatch
):
    monkeypatch.setattr(views, dependency, Mock(side_effect=RuntimeError(PRIVATE_TEXT)))
    request = RequestFactory().post("/pdfs/", REMOTE_ADDR="127.0.0.1")
    request._dont_enforce_csrf_checks = True

    with pytest.raises(RuntimeError):
        getattr(views, view_name)(request)

    failure = _events(bitbucket_logs, "web_action_failed")[0]
    assert failure.levelno == logging.ERROR
    assert not _events(bitbucket_logs, "web_action_requested")
    _assert_private(bitbucket_logs)


def test_generic_exception_code_attribute_is_not_logged(bitbucket_logs, monkeypatch):
    error = RuntimeError(PRIVATE_TEXT)
    error.code = PRIVATE_TEXT
    monkeypatch.setattr(pdf_local_policy, "_change_pdf", Mock(side_effect=error))

    with pytest.raises(RuntimeError):
        pdf_local_policy.exclude_registered_pdf(87)

    failure = _events(bitbucket_logs, "pdf_policy_failed")[0]
    assert "error_code=pdf_policy_failed" in failure.getMessage()
    _assert_private(bitbucket_logs)
