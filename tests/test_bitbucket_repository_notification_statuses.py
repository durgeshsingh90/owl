from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services.git_output import MAX_LINE_CHARACTERS
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_notifications import repository_notification_statuses
from bookmark_manager.models import BookmarkRefreshSchedule, Notification

pytestmark = pytest.mark.django_db


def _repository(name="Notes", **kwargs):
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/{name}",
        remote_url=f"https://example.invalid/{name}.git",
        **kwargs,
    )


def _job(repository, status, *, requested_at=None, **kwargs):
    job = RepositorySyncJob.objects.create(
        repository=repository,
        status=status,
        operation=kwargs.pop("operation", RepositorySyncOperation.REFRESH),
        **kwargs,
    )
    if requested_at is not None:
        RepositorySyncJob.objects.filter(pk=job.pk).update(requested_at=requested_at)
        job.requested_at = requested_at
    return job


def _document(repository, **kwargs):
    return PDFDocument.objects.create(
        repository=repository,
        filename=kwargs.pop("filename", "document.pdf"),
        relative_path=kwargs.pop("relative_path", "docs/document.pdf"),
        git_blob_id="a" * 40,
        file_size=123,
        **kwargs,
    )


def _extraction(document, status, **kwargs):
    return PDFExtractionJob.objects.create(
        document=document,
        status=status,
        target_git_blob_id=kwargs.pop("target_git_blob_id", document.git_blob_id),
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_source_commit="b" * 40,
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        **kwargs,
    )


def _item(repository):
    return next(
        item for item in repository_notification_statuses()["items"] if item["id"] == repository.pk
    )


def test_empty_snapshot_has_one_read_only_query(django_assert_num_queries):
    with django_assert_num_queries(1):
        payload = repository_notification_statuses()
    assert payload == {
        "total": 0,
        "activeCount": 0,
        "failedCount": 0,
        "activities": [],
        "items": [],
    }


def test_every_repository_is_returned_without_a_limit_or_n_plus_one(django_assert_num_queries):
    now = timezone.now()
    repositories = []
    for index in range(55):
        repository = _repository(f"Repo {index:02}")
        repositories.append(repository)
        _job(
            repository,
            RepositorySyncJobStatus.SUCCEEDED,
            completed_at=now,
            output_log=f"Repository {index} completed.",
        )
        _document(repository, index_state=PDFIndexState.READY)
    with django_assert_num_queries(3):
        payload = repository_notification_statuses()
    assert payload["total"] == 55
    assert payload["activeCount"] == payload["failedCount"] == 0
    assert [item["id"] for item in payload["items"]] == [repo.pk for repo in repositories]
    assert all(item["status"] == "ready" for item in payload["items"])
    assert [item["logPreview"] for item in payload["items"]] == [
        [f"Repository {index} completed."] for index in range(55)
    ]


@pytest.mark.parametrize(
    ("state", "enabled", "status", "tone"),
    [
        (RepositorySyncState.NOT_CLONED, True, "pending", "neutral"),
        (RepositorySyncState.NOT_CLONED, False, "disabled", "neutral"),
        (RepositorySyncState.QUEUED, True, "queued", "progress"),
        (RepositorySyncState.CLONING, True, "cloning", "progress"),
        (RepositorySyncState.FETCHING, True, "refreshing", "progress"),
        (RepositorySyncState.UPDATING, True, "refreshing", "progress"),
        (RepositorySyncState.READY, True, "ready", "success"),
        (RepositorySyncState.FAILED, True, "failed", "error"),
        (RepositorySyncState.INTERRUPTED, True, "failed", "error"),
        (RepositorySyncState.BLOCKED_DIRTY, True, "failed", "error"),
    ],
)
def test_repositories_without_jobs_use_persisted_state(state, enabled, status, tone):
    repository = _repository(sync_state=state, enabled=enabled)
    item = _item(repository)
    assert item["status"] == status
    assert item["statusTone"] == tone
    assert item["lastOutcome"] is None
    assert item["lastOutcomeAt"] is None
    assert item["lastSuccessAt"] is None
    assert item["logPreview"] == []


@pytest.mark.parametrize(
    "status",
    [
        RepositorySyncJobStatus.RUNNING,
        RepositorySyncJobStatus.SUCCEEDED,
        RepositorySyncJobStatus.FAILED,
        RepositorySyncJobStatus.INTERRUPTED,
        RepositorySyncJobStatus.CANCELLED,
    ],
)
def test_log_preview_shows_only_the_current_jobs_last_two_nonempty_lines(status):
    repository = _repository()
    requested_at = timezone.now()
    _job(
        repository,
        RepositorySyncJobStatus.SUCCEEDED,
        requested_at=requested_at,
        output_log="Previous job must not appear.",
    )
    current = _job(
        repository,
        status,
        requested_at=requested_at,
        output_log="Connection passed.\n\nReceiving objects: 50%\nReceiving objects: 100%\n",
    )
    item = _item(repository)
    assert item["logPreview"] == ["Receiving objects: 50%", "Receiving objects: 100%"]

    updated_at = timezone.now() + timedelta(seconds=5)
    RepositorySyncJob.objects.filter(pk=current.pk).update(
        output_log="Receiving objects: 100%\nAlready up to date.\n",
        output_log_updated_at=updated_at,
    )
    item = _item(repository)
    assert item["logPreview"] == ["Receiving objects: 100%", "Already up to date."]
    assert item["updatedAt"] == updated_at.isoformat()


@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_new_job_without_output_does_not_display_previous_jobs_log(status):
    repository = _repository()
    _job(
        repository,
        RepositorySyncJobStatus.FAILED,
        requested_at=timezone.now() - timedelta(minutes=1),
        output_log="fatal: Previous failure must not appear in a new run.",
    )
    _job(repository, status)
    assert _item(repository)["logPreview"] == []


def test_log_preview_uses_the_active_job_matching_the_displayed_status():
    repository = _repository()
    _job(
        repository,
        RepositorySyncJobStatus.RUNNING,
        requested_at=timezone.now() - timedelta(minutes=1),
        output_log="Receiving objects: 25%",
    )
    _job(
        repository,
        RepositorySyncJobStatus.SUCCEEDED,
        output_log="Inconsistent newer terminal record.",
    )
    item = _item(repository)
    assert item["active"] is True
    assert item["logPreview"] == ["Receiving objects: 25%"]


def test_log_preview_sanitizes_all_stored_lines_before_taking_the_tail():
    repository = _repository()
    key_label = "PRIVATE KEY"
    secret_value = "synthetic-secret-body"
    _job(
        repository,
        RepositorySyncJobStatus.FAILED,
        output_log=(
            "Connection started.\n"
            f"-----BEGIN {key_label}-----\n"
            f"{secret_value}\n"
            f"-----END {key_label}-----\n"
        ),
    )
    item = _item(repository)
    assert len(item["logPreview"]) == 2
    assert all("hidden" in line for line in item["logPreview"])
    assert secret_value not in json.dumps(item)


def test_log_preview_redacts_credentials_and_urls_from_legacy_saved_output():
    repository = _repository()
    userinfo = "preview-reader" + ":" + "placeholder"
    credential_key = "Authorization"
    _job(
        repository,
        RepositorySyncJobStatus.FAILED,
        output_log=(
            "Connection started.\n"
            f"fatal: Could not access https://{userinfo}@private.invalid/repository.git\n"
            f"{credential_key}: Bearer synthetic-credential\n"
        ),
    )
    item = _item(repository)
    assert len(item["logPreview"]) == 2
    assert "[remote URL]" in item["logPreview"][0]
    assert "hidden" in item["logPreview"][1]
    serialized = json.dumps(item)
    for private_value in (userinfo, "private.invalid", "synthetic-credential"):
        assert private_value not in serialized


def test_log_preview_remains_bounded_for_oversized_legacy_saved_output():
    repository = _repository()
    _job(
        repository,
        RepositorySyncJobStatus.RUNNING,
        output_log="\n".join(f"Progress {index}: " + ("x " * 1_000) for index in range(500)),
    )
    preview = _item(repository)["logPreview"]
    assert len(preview) == 2
    assert preview[0].startswith("Progress 498:")
    assert preview[1].startswith("Progress 499:")
    assert all(len(line) <= MAX_LINE_CHARACTERS for line in preview)


@pytest.mark.parametrize(
    ("job_status", "operation", "phase", "status"),
    [
        (RepositorySyncJobStatus.QUEUED, "clone", "queued", "queued"),
        (RepositorySyncJobStatus.RUNNING, "clone", "validating", "cloning"),
        (RepositorySyncJobStatus.RUNNING, "clone", "cloning", "cloning"),
        (RepositorySyncJobStatus.RUNNING, "refresh", "validating", "refreshing"),
        (RepositorySyncJobStatus.RUNNING, "refresh", "fetching", "refreshing"),
        (RepositorySyncJobStatus.RUNNING, "refresh", "updating", "refreshing"),
        (RepositorySyncJobStatus.RUNNING, "refresh", "discovering", "cataloging"),
        (RepositorySyncJobStatus.RUNNING, "refresh", "finalizing", "cataloging"),
    ],
)
def test_active_durable_job_overrides_stale_repository_state(job_status, operation, phase, status):
    repository = _repository(sync_state=RepositorySyncState.READY, enabled=False)
    _job(repository, job_status, operation=operation, phase=phase)
    payload = repository_notification_statuses()
    assert payload["activeCount"] == 1
    assert payload["items"][0]["status"] == status
    assert payload["items"][0]["active"] is True


def test_payload_distinguishes_clone_pull_and_indexing_with_progress_and_timing():
    observed_at = timezone.now()
    clone = _repository("Clone")
    pull = _repository("Pull")
    indexing = _repository("Indexing", sync_state=RepositorySyncState.READY)
    _job(
        clone,
        RepositorySyncJobStatus.RUNNING,
        operation=RepositorySyncOperation.CLONE,
        phase=RepositorySyncPhase.FINALIZING,
        progress=88,
        started_at=observed_at - timedelta(minutes=3),
        heartbeat_at=observed_at,
    )
    _job(
        pull,
        RepositorySyncJobStatus.RUNNING,
        operation=RepositorySyncOperation.REFRESH,
        phase=RepositorySyncPhase.DISCOVERING,
        progress=64,
        started_at=observed_at - timedelta(minutes=2),
        heartbeat_at=observed_at,
    )
    first = _document(indexing, filename="first.pdf", relative_path="docs/first.pdf")
    second = _document(indexing, filename="second.pdf", relative_path="docs/second.pdf")
    queued = _document(indexing, filename="queued.pdf", relative_path="docs/queued.pdf")
    _extraction(
        first,
        PDFExtractionJobStatus.RUNNING,
        progress=25,
        started_at=observed_at - timedelta(minutes=5),
        heartbeat_at=observed_at,
    )
    _extraction(
        second,
        PDFExtractionJobStatus.RUNNING,
        progress=75,
        started_at=observed_at - timedelta(minutes=4),
        heartbeat_at=observed_at,
    )
    _extraction(queued, PDFExtractionJobStatus.QUEUED, progress=0)

    payload = repository_notification_statuses(at=observed_at)

    activities = {activity["operation"]: activity for activity in payload["activities"]}
    assert tuple(activities) == ("clone", "pull", "indexing")
    assert activities["clone"]["progress"] == 88
    assert activities["clone"]["label"] == "Git clone"
    assert activities["pull"]["progress"] == 64
    assert activities["pull"]["label"] == "Git pull"
    assert activities["indexing"]["progress"] == 50
    assert activities["indexing"]["queuedJobs"] == 1
    assert activities["indexing"]["runningJobs"] == 2
    items = {item["name"]: item for item in payload["items"]}
    assert items["Clone"]["operation"] == "clone"
    assert items["Clone"]["phaseLabel"] == "Updating catalogue"
    assert items["Clone"]["workerTiming"]["operation"] == "clone"
    assert items["Pull"]["operation"] == "pull"
    assert items["Pull"]["phaseLabel"] == "Updating catalogue"
    assert items["Indexing"]["activity"]["progressScope"] == ("running_workers_average")
    assert (
        items["Indexing"]["activity"]["startedAt"]
        == (observed_at - timedelta(minutes=5)).isoformat()
    )


def test_queued_operation_has_no_fabricated_progress_or_timer():
    repository = _repository("Queued clone")
    _job(
        repository,
        RepositorySyncJobStatus.QUEUED,
        operation=RepositorySyncOperation.CLONE,
        phase=RepositorySyncPhase.QUEUED,
        progress=73,
    )

    payload = repository_notification_statuses()

    item = payload["items"][0]
    assert item["operation"] == "clone"
    assert item["progress"] is None
    assert item["workerTiming"] is None
    assert item["activity"]["state"] == "queued"
    assert item["activity"]["startedAt"] is None
    assert payload["activities"][0]["progress"] is None


@pytest.mark.parametrize(
    ("job_status", "repo_state", "expected"),
    [
        (RepositorySyncJobStatus.SUCCEEDED, RepositorySyncState.FAILED, "ready"),
        (RepositorySyncJobStatus.SUCCEEDED, RepositorySyncState.CLONING, "ready"),
        (RepositorySyncJobStatus.FAILED, RepositorySyncState.READY, "failed"),
        (RepositorySyncJobStatus.INTERRUPTED, RepositorySyncState.READY, "failed"),
        (RepositorySyncJobStatus.CANCELLED, RepositorySyncState.READY, "cancelled"),
    ],
)
def test_latest_terminal_job_overrides_stale_repository_state(job_status, repo_state, expected):
    repository = _repository(sync_state=repo_state)
    _job(repository, job_status, completed_at=timezone.now())
    assert _item(repository)["status"] == expected


def test_latest_job_and_previous_outcomes_use_deterministic_timestamps():
    now = timezone.now()
    repository = _repository(sync_state=RepositorySyncState.FAILED)
    success_at = now - timedelta(days=3)
    failure_at = now - timedelta(days=1)
    _job(
        repository,
        RepositorySyncJobStatus.SUCCEEDED,
        requested_at=success_at - timedelta(minutes=5),
        completed_at=success_at,
    )
    _job(
        repository,
        RepositorySyncJobStatus.FAILED,
        requested_at=failure_at - timedelta(minutes=5),
        completed_at=failure_at,
    )
    _job(
        repository,
        RepositorySyncJobStatus.RUNNING,
        requested_at=now - timedelta(minutes=2),
        started_at=now - timedelta(minutes=1),
        heartbeat_at=now,
        phase=RepositorySyncPhase.FETCHING,
    )
    item = _item(repository)
    assert item["status"] == "refreshing"
    assert item["lastSuccessAt"] == success_at.isoformat()
    assert item["lastOutcome"] == "failed"
    assert item["lastOutcomeAt"] == failure_at.isoformat()
    assert item["updatedAt"] == now.isoformat()


def test_active_job_takes_precedence_even_over_inconsistent_newer_terminal_record():
    now = timezone.now()
    repository = _repository(sync_state=RepositorySyncState.READY)
    _job(repository, RepositorySyncJobStatus.RUNNING, requested_at=now - timedelta(minutes=2))
    _job(repository, RepositorySyncJobStatus.SUCCEEDED, completed_at=now)
    assert _item(repository)["active"] is True


@pytest.mark.parametrize(
    "job_status", [PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING]
)
def test_pdf_text_work_remains_busy_after_git_success(job_status):
    repository = _repository(sync_state=RepositorySyncState.READY)
    _job(repository, RepositorySyncJobStatus.SUCCEEDED, completed_at=timezone.now())
    document = _document(repository)
    _extraction(document, job_status)
    payload = repository_notification_statuses()
    assert payload["activeCount"] == 1
    assert payload["items"][0]["status"] == "indexing"
    assert payload["items"][0]["lastOutcome"] == "succeeded"


@pytest.mark.parametrize(
    ("job_status", "expected_status"),
    [
        (PDFExtractionJobStatus.QUEUED, "disabled"),
        (PDFExtractionJobStatus.RUNNING, "indexing"),
    ],
)
def test_disabled_repository_ignores_unclaimable_pdf_queue_but_retains_running_work(
    job_status, expected_status
):
    repository = _repository(sync_state=RepositorySyncState.READY, enabled=False)
    document = _document(repository)
    _extraction(document, job_status)
    assert _item(repository)["status"] == expected_status


@pytest.mark.parametrize(
    "index_state", [PDFIndexState.FAILED, PDFIndexState.STALE_ERROR, PDFIndexState.PARTIAL]
)
def test_pdf_index_failure_is_visible_even_after_successful_git_sync(index_state):
    repository = _repository(sync_state=RepositorySyncState.READY)
    _document(repository, index_state=index_state, last_index_attempt_at=timezone.now())
    payload = repository_notification_statuses()
    assert payload["failedCount"] == 1
    assert payload["items"][0]["statusLabel"] == "PDF indexing needs attention"


def test_pending_document_without_queue_is_not_falsely_ready():
    repository = _repository(sync_state=RepositorySyncState.READY)
    _document(repository)
    assert _item(repository)["status"] == "indexing"


def test_stale_extraction_for_previous_revision_is_ignored():
    repository = _repository(sync_state=RepositorySyncState.READY)
    document = _document(repository, index_state=PDFIndexState.READY)
    _extraction(document, PDFExtractionJobStatus.RUNNING, target_git_blob_id="c" * 40)
    item = _item(repository)
    assert item["status"] == "ready"
    assert item["active"] is False


@pytest.mark.parametrize("policy_state", list(PDFLocalPolicyState))
def test_frozen_deleted_and_resuming_documents_do_not_claim_extraction_is_running(policy_state):
    repository = _repository(sync_state=RepositorySyncState.READY)
    document = _document(repository, index_state=PDFIndexState.FAILED)
    PDFLocalPolicy.objects.create(
        repository=repository,
        document=document,
        relative_path=document.relative_path,
        state=policy_state,
    )
    _extraction(document, PDFExtractionJobStatus.RUNNING)
    assert _item(repository)["status"] == "ready"


def test_removed_document_failures_do_not_make_repository_failed():
    repository = _repository(sync_state=RepositorySyncState.READY)
    _document(
        repository,
        lifecycle_state=PDFDocumentLifecycle.REMOVED,
        index_state=PDFIndexState.FAILED,
    )
    assert _item(repository)["status"] == "ready"


@pytest.mark.parametrize(
    ("error_code", "expected_detail"),
    [
        ("missing_pdf_file", "missing from the managed checkout"),
        ("pdf_path_too_long", "path is too long"),
        ("pdf_file_access_denied", "permissions or file locks"),
        ("database_busy", "database was busy"),
        ("worker_error", "unexpected error"),
    ],
)
def test_known_error_categories_have_safe_actionable_details(error_code, expected_detail):
    repository = _repository()
    _job(repository, RepositorySyncJobStatus.FAILED, error_code=error_code)
    assert expected_detail in _item(repository)["detail"]


@pytest.mark.parametrize(
    ("error_code", "expected_detail"),
    [
        ("history_diverged", "history diverged"),
        ("local_commits_detected", "Local commits prevented"),
    ],
)
def test_blocked_history_keeps_specific_safe_failure_context(error_code, expected_detail):
    repository = _repository(sync_state=RepositorySyncState.BLOCKED_DIRTY)
    _job(repository, RepositorySyncJobStatus.FAILED, error_code=error_code)
    item = _item(repository)
    assert expected_detail in item["detail"]
    assert item["statusLabel"] == "Blocked by repository history"


@pytest.mark.parametrize(
    "repo_state", [RepositorySyncState.INTERRUPTED, RepositorySyncState.BLOCKED_DIRTY]
)
def test_failed_durable_job_detail_overrides_stale_repository_failure_label(repo_state):
    repository = _repository(sync_state=repo_state, last_error_code="dirty_working_tree")
    _job(repository, RepositorySyncJobStatus.FAILED, error_code="fetch_failed")
    item = _item(repository)
    assert item["statusLabel"] == "Refresh failed"
    assert "could not be fetched" in item["detail"]


def test_endpoint_is_read_only_even_with_stale_jobs_and_does_not_leak_diagnostics(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    private_path = r"C:\private\documents\confidential.pdf"
    private_url = "https://private.invalid/private-repository.git"
    private_message = "private-sensitive-diagnostic-content"
    repository = _repository(
        "Visible repository",
        local_path=private_path,
        status_message=private_message,
        last_error_summary=private_url,
        last_error_code=private_message,
    )
    BitbucketRepository.objects.filter(pk=repository.pk).update(remote_url=private_url)
    stale_at = timezone.now() - timedelta(days=2)
    job = _job(
        repository,
        RepositorySyncJobStatus.RUNNING,
        started_at=stale_at,
        heartbeat_at=stale_at,
        status_message=private_path,
        error_summary=private_url,
    )
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("bookmark_manager:notifications"))
    assert response.status_code == 200
    payload = response.json()["repositoryStatuses"]
    assert payload["items"][0]["name"] == "Visible repository"
    assert payload["items"][0]["targetPath"] == f"/pdfs/?repository={repository.pk}"
    assert payload["items"][0]["statusTargetPath"] == (f"/pdfs/status/?repository={repository.pk}")
    assert payload["items"][0]["cancelIndexingUrl"] == (
        f"/pdfs/repositories/{repository.pk}/indexing/cancel/"
    )
    serialized = json.dumps(payload)
    for private_value in (private_path, private_url, private_message, "remote_url", "local_path"):
        assert private_value not in serialized
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert Notification.objects.count() == 0
    assert BookmarkRefreshSchedule.objects.count() == 0
    assert RepositorySyncJob.objects.count() == 1
    job.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.RUNNING


def test_unknown_persisted_outcome_is_not_echoed_to_payload():
    repository = _repository()
    _job(repository, "private-untrusted-outcome", error_code="private-untrusted-code")
    payload = repository_notification_statuses()
    assert payload["items"][0]["lastOutcome"] is None
    assert "private-untrusted" not in json.dumps(payload)
