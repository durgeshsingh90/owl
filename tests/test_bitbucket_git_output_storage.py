from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncTrigger,
)
from bitbucket_search.services import git_output, repository_sync
from bitbucket_search.services.git_output import RepositoryGitLog, emit_git_output
from bitbucket_search.services.git_sync import RepositorySyncError
from bookmark_manager.models import Notification, NotificationState

pytestmark = pytest.mark.django_db


@pytest.fixture
def running_job():
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic documents",
        canonical_remote_key="example.invalid/workspace/docs",
        remote_url="https://example.invalid/workspace/docs.git",
    )
    return RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.RUNNING,
        worker_pid=12345,
        started_at=timezone.now(),
    )


def test_log_is_live_redacted_bounded_and_not_printed(running_job, caplog):
    with RepositoryGitLog(running_job) as output:
        emit_git_output("Receiving objects: 10% (2/20)", operation="clone")
        running_job.refresh_from_db()
        assert "Receiving objects: 10%" in running_job.output_log
        synthetic_userinfo = "user:do-not-store"
        synthetic_key = "token"
        emit_git_output(
            f"fatal: https://{synthetic_userinfo}@example.invalid/repo?{synthetic_key}=placeholder"
        )
        for index in range(500):
            emit_git_output(f"Receiving objects: {index} " + "x" * 300, operation="clone")
        output.flush(force=True)
    running_job.refresh_from_db()
    assert running_job.output_log_truncated
    assert len(running_job.output_log) <= git_output.MAX_LOG_CHARACTERS
    assert len(running_job.output_log.splitlines()) <= git_output.MAX_LOG_LINES
    assert "do-not-store" not in running_job.output_log
    assert "Receiving objects: 499" in running_job.output_log
    assert "Receiving objects" not in caplog.text
    assert "do-not-store" not in caplog.text


def test_sink_is_isolated_and_resets_after_exception(running_job):
    with pytest.raises(ValueError, match="synthetic"), RepositoryGitLog(running_job):
        emit_git_output("First worker output")
        raise ValueError("synthetic")
    emit_git_output("Not inside a job")
    running_job.refresh_from_db()
    assert "First worker output" in running_job.output_log
    assert "Not inside a job" not in running_job.output_log


def test_sink_preserves_warning_and_error_levels(running_job):
    with RepositoryGitLog(running_job):
        emit_git_output("warning: synthetic retry", level="warning", operation="fetch")
        emit_git_output("fatal: synthetic failure", level="error", operation="fetch")
    running_job.refresh_from_db()
    assert "WARNING [fetch]" in running_job.output_log
    assert "ERROR [fetch]" in running_job.output_log


def test_sink_hides_key_blocks_and_credential_continuations_across_calls(running_job):
    with RepositoryGitLog(running_job):
        key_label = "OPENSSH PRIVATE KEY"
        for line in (
            f"-----BEGIN {key_label}-----",
            "privatebody123",
            "secondprivatebody456",
            f"-----END {key_label}-----",
            "password:",
            "unlabelled-value",
            "Receiving objects: 100%",
        ):
            emit_git_output(line)
    running_job.refresh_from_db()
    assert "privatebody" not in running_job.output_log
    assert "unlabelled-value" not in running_job.output_log
    assert "Receiving objects: 100%" in running_job.output_log


def test_cancelled_or_replaced_worker_cannot_overwrite_log(running_job):
    with RepositoryGitLog(running_job):
        RepositorySyncJob.objects.filter(pk=running_job.pk).update(worker_pid=9876)
        emit_git_output("Old worker output")
    running_job.refresh_from_db()
    assert not running_job.output_log
    with RepositoryGitLog(running_job):
        RepositorySyncJob.objects.filter(pk=running_job.pk).update(status="cancelled")
        emit_git_output("Cancelled worker output")
    running_job.refresh_from_db()
    assert not running_job.output_log


def test_optional_log_storage_failure_does_not_hide_primary_error(running_job, monkeypatch):
    with RepositoryGitLog(running_job) as output:
        original = RepositorySyncJob.objects.filter
        with monkeypatch.context() as patch:
            patch.setattr(
                RepositorySyncJob.objects, "filter", Mock(side_effect=OperationalError("locked"))
            )
            emit_git_output("Still downloading")
        assert RepositorySyncJob.objects.filter == original
        output.flush(force=True)
    running_job.refresh_from_db()
    assert "Still downloading" in running_job.output_log


def test_endpoint_exposes_only_latest_job_and_sanitizes_stored_values(client, running_job):
    url = reverse("bitbucket_search:repository_logs", args=(running_job.repository_id,))
    running_job.status = RepositorySyncJobStatus.FAILED
    running_job.output_log = "old log must not appear"
    running_job.save()
    synthetic_userinfo = "login:secret"
    latest = RepositorySyncJob.objects.create(
        repository=running_job.repository,
        operation=RepositorySyncOperation.CLONE,
        output_log=f"Receiving objects: 5%\nfatal: https://{synthetic_userinfo}@host.invalid/path?key=value",
    )
    response = client.get(url, REMOTE_ADDR="127.0.0.1")
    assert response.status_code == 200
    body = response.json()
    assert body["jobId"] == latest.pk
    assert body["repositoryId"] == running_job.repository_id
    assert "Receiving objects: 5%" in body["log"]
    assert "secret" not in body["log"]
    assert "old log" not in body["log"]
    assert "remote_url" not in body
    assert "no-store" in response.headers["Cache-Control"]
    assert client.post(url, REMOTE_ADDR="127.0.0.1").status_code == 405
    assert client.get(url, REMOTE_ADDR="203.0.113.8").status_code == 403


def test_endpoint_empty_repo_and_unknown_repo(client, running_job):
    repository_id = running_job.repository_id
    running_job.delete()
    url = reverse("bitbucket_search:repository_logs", args=(repository_id,))
    body = client.get(url, REMOTE_ADDR="127.0.0.1").json()
    assert body["jobId"] is None and body["status"] == "not_started" and body["log"] == ""
    unknown = reverse("bitbucket_search:repository_logs", args=(repository_id + 1000,))
    assert client.get(unknown, REMOTE_ADDR="127.0.0.1").status_code == 404


@pytest.mark.parametrize("trigger", (RepositorySyncTrigger.MANUAL, RepositorySyncTrigger.DAILY))
def test_connection_failure_stops_cataloguing_and_notifies_for_manual_and_daily(
    running_job, monkeypatch, trigger
):
    if trigger == RepositorySyncTrigger.DAILY:
        running_job.trigger = trigger
        running_job.scheduled_day = timezone.localdate()
        running_job.save()

    def fail_connection(repository, *, operation, progress_callback):
        progress_callback(RepositorySyncPhase.CHECKING_CONNECTION, 3, "Checking connection…")
        emit_git_output("Could not resolve host", operation="connection", level="error")
        raise RepositorySyncError(
            "connection_unreachable", "Cannot reach Git. Connect to VPN if required."
        )

    monkeypatch.setattr(repository_sync, "synchronize_repository", fail_connection)
    catalogue = Mock(side_effect=AssertionError("Must not scan after connection failure"))
    monkeypatch.setattr(repository_sync, "build_repository_pdf_catalog", catalogue)
    result = repository_sync.execute_claimed_job(running_job.pk)
    assert result.status == RepositorySyncJobStatus.FAILED
    catalogue.assert_not_called()
    notice = Notification.objects.get()
    assert "VPN" in notice.message
    assert notice.read_at is None
    result.refresh_from_db()
    assert "Could not resolve host" in result.output_log
    assert "Connect to VPN" in result.output_log


def test_manual_connection_notifications_deduplicate_and_recover(running_job):
    now = timezone.now()
    for _ in range(2):
        repository_sync._publish_manual_connection_status(
            running_job, occurred_at=now, failure_summary="Check VPN access."
        )
    assert Notification.objects.count() == 1
    assert Notification.objects.get().state == NotificationState.ERROR
    repository_sync._publish_manual_connection_status(running_job, occurred_at=now)
    assert Notification.objects.count() == 1
    assert Notification.objects.get().state == NotificationState.SUCCESS
