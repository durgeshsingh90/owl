from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncPhase,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import git_sync, pdf_catalog, repository_sync
from bitbucket_search.services.git_sync import (
    DocumentStats,
    RepositorySyncError,
    RepositorySyncResult,
)
from bitbucket_search.services.pdf_catalog import CatalogBuild

pytestmark = pytest.mark.django_db
_SYNTHETIC_USERNAME = "synthetic-log-user-never-valid"
_SYNTHETIC_PASSWORD = "not-a-real-secret"
PRIVATE_VALUE = (
    f"https://{_SYNTHETIC_USERNAME}:{_SYNTHETIC_PASSWORD}@private.invalid/private-repository.pdf"
)


@pytest.fixture
def events(caplog, monkeypatch):
    boundary = logging.getLogger("owl.bitbucket")
    caplog.set_level(logging.DEBUG, logger=boundary.name)
    monkeypatch.setattr(boundary, "propagate", False)
    boundary.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        boundary.removeHandler(caplog.handler)


def _repository():
    return BitbucketRepository.objects.create(
        display_name="private-repository.pdf",
        canonical_remote_key="private.invalid/private-repository",
        remote_url=PRIVATE_VALUE,
        sync_state=RepositorySyncState.READY,
    )


def _running_job(repository, **kwargs):
    return RepositorySyncJob.objects.create(
        repository=repository,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
        **kwargs,
    )


def _event(events, name):
    records = [
        record
        for record in events.records
        if f"event={name} " in record.getMessage() or record.getMessage() == f"event={name}"
    ]
    assert records, f"No {name} in {events.text}"
    return records[-1]


def _assert_redacted(events):
    assert PRIVATE_VALUE not in events.text
    assert _SYNTHETIC_PASSWORD not in events.text
    assert "private-repository.pdf" not in events.text
    assert "private-branch" not in events.text
    assert "Traceback" not in events.text
    assert all(record.exc_info is None for record in events.records)


def test_queue_claim_progress_and_success_have_safe_committed_events(
    events, monkeypatch, django_capture_on_commit_callbacks
):
    repository = _repository()
    result = RepositorySyncResult(
        branch="private-branch",
        source_commit="",
        result_commit="a" * 40,
        documents=DocumentStats(0, 0, 0),
    )

    def fake_sync(_repository, *, operation, progress_callback):
        progress_callback(RepositorySyncPhase.UPDATING, 75, PRIVATE_VALUE)
        progress_callback(RepositorySyncPhase.UPDATING, 75, PRIVATE_VALUE)
        git_sync._run_capture(
            ("git", "-C", PRIVATE_VALUE, "status"),
            failure_code="status_failed",
            failure_summary=PRIVATE_VALUE,
        )
        return result

    process = Mock(returncode=0)
    process.communicate.return_value = ("", "")
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(repository_sync, "synchronize_repository", fake_sync)
    monkeypatch.setattr(
        repository_sync, "build_repository_pdf_catalog", Mock(return_value=CatalogBuild((), False))
    )
    with django_capture_on_commit_callbacks(execute=True):
        queued = repository_sync.queue_repository_refresh(repository.pk)
        claimed = repository_sync.claim_next_job()
        assert claimed.pk == queued.job.pk
        completed = repository_sync.execute_claimed_job(claimed.pk)

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    for name in (
        "repository_sync_queued",
        "repository_sync_claimed",
        "repository_sync_started",
        "repository_sync_completed",
    ):
        record = _event(events, name)
        assert record.levelno == logging.INFO
        assert f"repository_id={repository.pk}" in record.getMessage()
        assert f"job_id={claimed.pk}" in record.getMessage()
    completion = _event(events, "repository_sync_completed").getMessage()
    assert "pdf_count=0" in completion
    assert "elapsed_ms=" in completion
    progress = [
        record
        for record in events.records
        if "event=repository_sync_progress " in record.getMessage()
        and "progress=75" in record.getMessage()
    ]
    assert len(progress) == 1
    assert progress[0].levelno == logging.DEBUG
    git_event = _event(events, "git_command_completed").getMessage()
    assert "operation=status" in git_event
    assert f"repository_id={repository.pk}" in git_event
    assert f"job_id={claimed.pk}" in git_event
    _assert_redacted(events)


@pytest.mark.parametrize(
    ("failure", "event_name"),
    [
        (RepositorySyncError("checkout_failed", PRIVATE_VALUE), "repository_sync_failed"),
        (RuntimeError(PRIVATE_VALUE), "repository_sync_unexpected_failure"),
    ],
)
def test_known_and_unexpected_worker_failures_are_error_and_redacted(
    events, monkeypatch, failure, event_name
):
    repository = _repository()
    job = _running_job(repository)
    monkeypatch.setattr(repository_sync, "synchronize_repository", Mock(side_effect=failure))

    completed = repository_sync.execute_claimed_job(job.pk)

    assert completed.status in {RepositorySyncJobStatus.FAILED, RepositorySyncJobStatus.INTERRUPTED}
    record = _event(events, event_name)
    assert record.levelno == logging.ERROR
    assert "stage=git_sync" in record.getMessage()
    assert "error_type=" in record.getMessage()
    assert "elapsed_ms=" in record.getMessage()
    _assert_redacted(events)


def test_expired_worker_lease_is_error_not_a_warning(events):
    repository = _repository()
    job = _running_job(repository)
    RepositorySyncJob.objects.filter(pk=job.pk).update(
        heartbeat_at=timezone.now() - timedelta(minutes=20)
    )

    repository_sync._interrupt_stale_jobs()

    job.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.INTERRUPTED
    assert _event(events, "repository_worker_lease_expired").levelno == logging.ERROR
    _assert_redacted(events)


def test_worker_launch_failure_is_error_with_no_command_or_exception_content(events, monkeypatch):
    monkeypatch.setattr(
        repository_sync.subprocess, "Popen", Mock(side_effect=PermissionError(13, PRIVATE_VALUE))
    )

    with pytest.raises(PermissionError):
        repository_sync.launch_sync_worker()

    record = _event(events, "repository_worker_launch_failed")
    assert record.levelno == logging.ERROR
    assert "errno=13" in record.getMessage()
    _assert_redacted(events)


@pytest.mark.parametrize("recovery", (False, True))
def test_notification_write_failures_are_error_without_tracebacks(events, monkeypatch, recovery):
    repository = _repository()
    job = _running_job(
        repository, trigger=RepositorySyncTrigger.DAILY, scheduled_day=timezone.localdate()
    )
    monkeypatch.setattr(
        repository_sync, "publish_notification", Mock(side_effect=RuntimeError(PRIVATE_VALUE))
    )
    if recovery:
        monkeypatch.setattr(
            repository_sync.Notification.objects,
            "filter",
            Mock(return_value=Mock(exists=Mock(return_value=True))),
        )
        repository_sync._publish_automatic_refresh_recovery(job, occurred_at=timezone.now())
        event_name = "repository_recovery_notification_failed"
    else:
        repository_sync._publish_automatic_refresh_failure(
            job, summary=PRIVATE_VALUE, occurred_at=timezone.now()
        )
        event_name = "repository_failure_notification_failed"

    assert _event(events, event_name).levelno == logging.ERROR
    _assert_redacted(events)


def test_nonzero_git_command_logs_error_without_output_arguments_or_paths(events, monkeypatch):
    process = Mock(returncode=1)
    process.communicate.return_value = (PRIVATE_VALUE, PRIVATE_VALUE)
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))

    with pytest.raises(RepositorySyncError):
        git_sync._run_capture(
            ("git", "clone", PRIVATE_VALUE),
            failure_code="clone_failed",
            failure_summary=PRIVATE_VALUE,
        )

    record = _event(events, "git_process_failed")
    assert record.levelno == logging.ERROR
    assert "return_code=1" in record.getMessage()
    _assert_redacted(events)


def test_catalogue_failure_logs_stage_and_safe_error_category(events, monkeypatch):
    repository = _repository()
    monkeypatch.setattr(
        pdf_catalog, "_read_current_pdfs", Mock(side_effect=PermissionError(13, PRIVATE_VALUE))
    )

    with pytest.raises(PermissionError):
        pdf_catalog.build_repository_pdf_catalog(
            repository, result_commit="a" * 40, progress_callback=lambda *_args: None
        )

    record = _event(events, "catalog_build_failed")
    assert record.levelno == logging.ERROR
    assert "error_type=PermissionError" in record.getMessage()
    assert "errno=13" in record.getMessage()
    _assert_redacted(events)


def test_idle_worker_polling_does_not_emit_repetitive_events(events, settings):
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = False
    for _ in range(3):
        assert repository_sync.claim_next_job() is None
        assert repository_sync.queue_due_daily_repository_refreshes() == ()
    assert events.records == []


def test_filtered_clone_fallback_is_warning_but_final_failure_is_error(
    events, monkeypatch, tmp_path, settings
):
    repository = _repository()
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "staging"
    monkeypatch.setattr(git_sync, "_check_connection", Mock())
    monkeypatch.setattr(
        git_sync,
        "_run_streaming",
        Mock(
            side_effect=[
                RepositorySyncError("clone_failed", PRIVATE_VALUE),
                RepositorySyncError("clone_failed", PRIVATE_VALUE),
            ]
        ),
    )

    with pytest.raises(RepositorySyncError):
        git_sync.synchronize_repository(
            repository, operation="clone", progress_callback=lambda *_args: None
        )

    assert _event(events, "git_clone_compatibility_fallback").levelno == logging.WARNING
    assert _event(events, "git_repository_sync_failed").levelno == logging.ERROR
    _assert_redacted(events)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("git", "-C", PRIVATE_VALUE, "ls-files"), "ls-files"),
        (("git", "clone", PRIVATE_VALUE), "clone"),
        (("git", PRIVATE_VALUE, "status"), "git_command"),
    ],
)
def test_git_command_labels_are_selected_without_interpreting_private_arguments(
    arguments, expected
):
    assert git_sync._git_command_operation(arguments) == expected
