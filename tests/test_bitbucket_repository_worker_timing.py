from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

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
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_notifications import repository_notification_statuses
from bitbucket_search.services.repository_sync import repository_status_snapshot
from bitbucket_search.services.repository_worker_timing import worker_timing

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 30, 14, 30, tzinfo=UTC)


def _repository(name="Notes", **values):
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{name}",
        remote_url=f"https://example.invalid/team/{name}.git",
        sync_state=values.pop("sync_state", RepositorySyncState.READY),
        **values,
    )


def _sync(repository, **values):
    return RepositorySyncJob.objects.create(
        repository=repository,
        operation=values.pop("operation", RepositorySyncOperation.CLONE),
        status=values.pop("status", RepositorySyncJobStatus.RUNNING),
        phase=values.pop("phase", RepositorySyncPhase.CLONING),
        started_at=values.pop("started_at", NOW - timedelta(minutes=4)),
        heartbeat_at=NOW,
        **values,
    )


def _pdf_job(repository, name="document", **values):
    document = PDFDocument.objects.create(
        repository=repository,
        filename=f"{name}.pdf",
        relative_path=f"docs/{name}.pdf",
        git_blob_id="a" * 40,
        file_size=100,
        index_state=PDFIndexState.READY,
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
    )
    return PDFExtractionJob.objects.create(
        document=document,
        status=values.pop("status", PDFExtractionJobStatus.RUNNING),
        started_at=values.pop("started_at", NOW - timedelta(minutes=2)),
        heartbeat_at=NOW,
        target_git_blob_id=values.pop("target_git_blob_id", document.git_blob_id),
        target_relative_path=values.pop("target_relative_path", document.relative_path),
        target_file_size=values.pop("target_file_size", document.file_size),
        target_extractor_version=values.pop("target_extractor_version", PDF_EXTRACTOR_VERSION),
        target_source_commit="b" * 40,
        **values,
    )


def _both_timings(repository):
    notification = next(
        item
        for item in repository_notification_statuses(at=NOW)["items"]
        if item["id"] == repository.pk
    )
    sidebar_repository = next(
        item for item in repository_status_snapshot(at=NOW) if item.pk == repository.pk
    )
    assert sidebar_repository.worker_timing == notification["workerTiming"]
    return notification["workerTiming"]


@pytest.mark.parametrize("state", list(RepositorySyncState))
def test_repository_state_and_last_sync_time_never_fabricate_a_worker_start(state):
    repository = _repository(sync_state=state, last_sync_started_at=NOW - timedelta(minutes=5))
    assert _both_timings(repository) is None


@pytest.mark.parametrize(
    "status",
    [value for value in RepositorySyncJobStatus if value != RepositorySyncJobStatus.RUNNING],
)
def test_queued_and_terminal_sync_records_do_not_have_a_running_timer(status):
    repository = _repository()
    _sync(repository, status=status)
    assert _both_timings(repository) is None


@pytest.mark.parametrize("start", [None, NOW + timedelta(microseconds=1)])
def test_missing_or_future_sync_start_is_not_replaced_by_queue_or_index_start(start):
    repository = _repository()
    _sync(repository, started_at=start)
    _pdf_job(repository)
    assert _both_timings(repository) is None


def test_sync_timer_keeps_its_start_across_download_and_catalogue_phases():
    repository = _repository()
    job = _sync(repository)
    for phase, label in (
        (RepositorySyncPhase.VALIDATING, "Downloading"),
        (RepositorySyncPhase.CLONING, "Downloading"),
        (RepositorySyncPhase.UPDATING, "Downloading"),
        (RepositorySyncPhase.DISCOVERING, "Updating catalogue"),
        (RepositorySyncPhase.FINALIZING, "Updating catalogue"),
    ):
        job.phase = phase
        job.save(update_fields=("phase",))
        assert _both_timings(repository) == {
            "startedAt": job.started_at.isoformat(),
            "observedAt": NOW.isoformat(),
            "label": label,
            "kind": "sync",
            "operation": "clone",
            "phase": phase,
            "phaseLabel": label,
            "progress": 0,
            "progressScope": "job",
        }


def test_finished_sync_clears_timer_and_next_refresh_uses_a_new_start():
    repository = _repository()
    original = _sync(repository)
    assert _both_timings(repository)["label"] == "Downloading"
    original.status = RepositorySyncJobStatus.SUCCEEDED
    original.completed_at = NOW
    original.save(update_fields=("status", "completed_at"))
    assert _both_timings(repository) is None
    next_job = _sync(
        repository,
        operation=RepositorySyncOperation.REFRESH,
        phase=RepositorySyncPhase.FETCHING,
        started_at=NOW - timedelta(seconds=10),
    )
    assert _both_timings(repository) == {
        "startedAt": next_job.started_at.isoformat(),
        "observedAt": NOW.isoformat(),
        "label": "Refreshing",
        "kind": "sync",
        "operation": "pull",
        "phase": "fetching",
        "phaseLabel": "Refreshing",
        "progress": 0,
        "progressScope": "job",
    }


def test_longest_running_current_pdf_worker_is_used_after_sync_and_stops_when_finished():
    repository = _repository()
    first = _pdf_job(repository, "first", started_at=NOW - timedelta(minutes=7))
    second = _pdf_job(repository, "second", started_at=NOW - timedelta(minutes=3))
    _pdf_job(
        repository,
        "queued",
        status=PDFExtractionJobStatus.QUEUED,
        started_at=NOW - timedelta(hours=1),
    )
    _pdf_job(repository, "future", started_at=NOW + timedelta(minutes=1))
    _pdf_job(repository, "missing-start", started_at=None)
    assert _both_timings(repository) == {
        "startedAt": first.started_at.isoformat(),
        "observedAt": NOW.isoformat(),
        "label": "Indexing PDFs",
        "kind": "indexing",
        "operation": "indexing",
        "phase": "extracting",
        "phaseLabel": "Indexing PDFs",
        "progress": 0,
        "progressScope": "running_workers_average",
    }
    first.status = PDFExtractionJobStatus.SUCCEEDED
    first.save(update_fields=("status",))
    assert _both_timings(repository)["startedAt"] == second.started_at.isoformat()
    second.status = PDFExtractionJobStatus.FAILED
    second.save(update_fields=("status",))
    assert _both_timings(repository) is None


def test_running_sync_takes_precedence_over_older_pdf_worker_and_queued_sync_does_not():
    repository = _repository()
    pdf = _pdf_job(repository, started_at=NOW - timedelta(minutes=9))
    sync = _sync(repository)
    assert _both_timings(repository)["kind"] == "sync"
    sync.status = RepositorySyncJobStatus.QUEUED
    sync.save(update_fields=("status",))
    assert _both_timings(repository)["startedAt"] == pdf.started_at.isoformat()
    assert _both_timings(repository)["kind"] == "indexing"


@pytest.mark.parametrize(
    "status", [value for value in PDFExtractionJobStatus if value != PDFExtractionJobStatus.RUNNING]
)
def test_only_running_pdf_jobs_get_a_worker_timer(status):
    repository = _repository()
    _pdf_job(repository, status=status)
    assert _both_timings(repository) is None


@pytest.mark.parametrize(
    "exclusion", ["removed", "excluded", "deleted", "resuming", "blob", "path", "size", "extractor"]
)
def test_non_current_or_locally_ineligible_pdf_jobs_do_not_contribute_timing(exclusion):
    repository = _repository()
    stale = _pdf_job(repository, "ineligible", started_at=NOW - timedelta(hours=1))
    if exclusion == "removed":
        stale.document.lifecycle_state = PDFDocumentLifecycle.REMOVED
        stale.document.save(update_fields=("lifecycle_state",))
    elif exclusion in PDFLocalPolicyState.values:
        PDFLocalPolicy.objects.create(
            repository=repository,
            document=stale.document,
            relative_path=stale.document.relative_path,
            state=exclusion,
        )
    else:
        field, value = {
            "blob": ("target_git_blob_id", "f" * 40),
            "path": ("target_relative_path", "old/document.pdf"),
            "size": ("target_file_size", 99),
            "extractor": ("target_extractor_version", "outdated-extractor"),
        }[exclusion]
        setattr(stale, field, value)
        stale.save(update_fields=(field,))
    assert _both_timings(repository) is None
    current = _pdf_job(repository, "current")
    assert _both_timings(repository)["startedAt"] == current.started_at.isoformat()


def test_disabled_repository_still_shows_a_genuine_already_running_pdf_worker():
    repository = _repository(enabled=False, sync_state=RepositorySyncState.DISABLED)
    current = _pdf_job(repository)
    assert _both_timings(repository)["startedAt"] == current.started_at.isoformat()


@pytest.mark.parametrize(
    "start", [None, "not-a-time", datetime(2026, 8, 30, 13, 30), NOW + timedelta(seconds=1)]
)
def test_timing_helper_rejects_invalid_sync_and_index_starts(start):
    assert (
        worker_timing(
            observed_at=NOW, sync_status=RepositorySyncJobStatus.RUNNING, sync_started_at=start
        )
        is None
    )
    assert worker_timing(observed_at=NOW, indexing_started_at=start) is None


def test_timing_queries_are_read_only_bounded_and_observed_time_is_shared(
    django_assert_num_queries,
):
    for index in range(30):
        repository = _repository(f"Repository {index:02}")
        _sync(repository) if index % 2 else _pdf_job(repository)
    with CaptureQueriesContext(connection) as queries, django_assert_num_queries(3):
        notification = repository_notification_statuses(at=NOW)
    assert len(notification["items"]) == 30
    assert {item["workerTiming"]["observedAt"] for item in notification["items"]} == {
        NOW.isoformat()
    }
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    with CaptureQueriesContext(connection) as queries, django_assert_num_queries(4):
        repositories = repository_status_snapshot(at=NOW)
    assert len(repositories) == 30
    assert all(repository.worker_timing is not None for repository in repositories)
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)


def test_timer_contract_is_published_in_rail_endpoint_notification_endpoint_and_ssr(
    client, monkeypatch
):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    repository = _repository()
    job = _sync(repository)
    expected = {
        "startedAt": job.started_at.isoformat(),
        "observedAt": NOW.isoformat(),
        "label": "Downloading",
        "kind": "sync",
        "operation": "clone",
        "phase": "cloning",
        "phaseLabel": "Downloading",
        "progress": 0,
        "progressScope": "job",
    }
    response = client.get(reverse("bitbucket_search:repository_status"))
    assert response.status_code == 200
    assert response.json()["repositories"][0]["workerTiming"] == expected
    response = client.get(reverse("bookmark_manager:notifications"))
    assert response.status_code == 200
    assert response.json()["repositoryStatuses"]["items"][0]["workerTiming"] == expected
    response = client.get(reverse("bitbucket_search:index"))
    assert response.status_code == 200
    assert response.context["repositories"][0].worker_timing == expected
