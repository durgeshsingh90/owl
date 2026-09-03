from __future__ import annotations

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
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_activity import (
    repository_work_summary,
    with_repository_activity,
)
from bitbucket_search.views import _repository_payload, _with_repository_work_status

pytestmark = pytest.mark.django_db


def _repository(name="Synthetic repository", **values):
    number = BitbucketRepository.objects.count() + 1
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/repository-{number}",
        remote_url=f"https://example.invalid/team/repository-{number}.git",
        sync_state=RepositorySyncState.READY,
        **values,
    )


def _pdf_job(repository, status=PDFExtractionJobStatus.QUEUED, *, current=True, **values):
    number = PDFDocument.objects.count() + 1
    document = PDFDocument.objects.create(
        repository=repository,
        filename=f"Private filename {number}.pdf",
        relative_path=f"private/folder/Private filename {number}.pdf",
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
        file_size=123,
    )
    return PDFExtractionJob.objects.create(
        document=document,
        status=status,
        started_at=timezone.now() - timedelta(seconds=30)
        if status == PDFExtractionJobStatus.RUNNING
        else None,
        target_git_blob_id=document.git_blob_id if current else "c" * 40,
        target_source_commit=document.last_seen_commit,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        **values,
    )


def _sync_job(repository, *, status=RepositorySyncJobStatus.RUNNING, **values):
    now = timezone.now()
    defaults = {
        "operation": RepositorySyncOperation.REFRESH,
        "phase": RepositorySyncPhase.FETCHING,
        "started_at": now - timedelta(seconds=45)
        if status == RepositorySyncJobStatus.RUNNING
        else None,
        "heartbeat_at": now,
        "status_message": "Private diagnostic must not be exposed",
        "output_log": "Private Git transport output",
    }
    defaults.update(values)
    return RepositorySyncJob.objects.create(repository=repository, status=status, **defaults)


@pytest.mark.parametrize("queued,running", [(3, 0), (0, 2), (3, 2)])
def test_git_ready_repository_exposes_pdf_work_instead_of_idle(queued, running):
    repository = _repository()
    for _ in range(queued):
        _pdf_job(repository)
    for _ in range(running):
        _pdf_job(repository, PDFExtractionJobStatus.RUNNING)

    _with_repository_work_status((repository,))
    payload = _repository_payload(repository)

    assert payload["state"] == RepositorySyncState.READY
    assert payload["active"] is False  # Legacy Git-only state remains unchanged.
    assert payload["hasActiveWork"] is payload["activity"]["active"] is True
    assert payload["activity"]["kind"] == "indexing"
    assert payload["activity"]["phase"] == ("extracting" if running else "pdf_queued")
    assert payload["activity"]["queuedPdfs"] == queued
    assert payload["activity"]["runningPdfs"] == running
    if running:
        assert f"{running} PDFs extracting" in payload["activity"]["detail"]
    else:
        assert payload["activity"]["detail"] == ""
    assert payload["activity"]["pendingCleanupJobs"] == 0


@pytest.mark.parametrize(
    "operation,phase,expected_phase,label",
    [
        ("refresh", "validating", "validating", "Preparing sync"),
        ("clone", "checking_connection", "checking_connection", "Checking connection"),
        ("refresh", "checking_connection", "checking_connection", "Checking connection"),
        ("clone", "cloning", "cloning", "Cloning repository"),
        ("refresh", "fetching", "pulling", "Pulling updates"),
        ("refresh", "updating", "pulling", "Pulling updates"),
        ("clone", "discovering", "cataloguing", "Updating catalogue"),
        ("refresh", "finalizing", "cataloguing", "Updating catalogue"),
    ],
)
def test_active_sync_phase_is_reported_without_loading_output(
    operation, phase, expected_phase, label
):
    repository = _repository()
    _sync_job(repository, operation=operation, phase=phase)

    with_repository_activity((repository,))

    assert repository.activity["active"] == repository.has_active_work
    assert repository.activity["phase"] == expected_phase
    assert repository.activity["label"] == label
    assert repository.activity["detail"] == label
    assert repository.activity["kind"] == "sync"
    assert repository.activity["operation"] == (
        "clone" if operation == RepositorySyncOperation.CLONE else "pull"
    )
    assert repository.activity["progress"] == 0
    assert repository.activity["operations"][0]["phaseLabel"] == label
    assert repository.activity["runningSyncJobs"] == 1
    summary = repository_work_summary((repository,))
    assert summary["label"] == label
    assert f"{repository.display_name}: {label}" in summary["detail"]
    assert "Private" not in str(summary)
    assert "https://" not in str(summary)


def test_waiting_sync_has_queue_label_without_inventing_running_worker():
    repository = _repository()
    _sync_job(repository, status=RepositorySyncJobStatus.QUEUED, phase=RepositorySyncPhase.QUEUED)
    with_repository_activity((repository,))
    assert repository.activity["phase"] == "sync_queued"
    assert repository.activity["queuedSyncJobs"] == 1
    assert repository.activity["runningSyncJobs"] == 0
    assert repository.activity["detail"] == "Git sync queued"
    assert repository.activity["operation"] == "pull"
    assert repository.activity["progress"] is None
    assert repository.activity["operations"][0]["phaseLabel"] == "Git pull queued"


@pytest.mark.parametrize("status", [PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING])
def test_obsolete_pdf_jobs_remain_safety_active_and_have_explicit_cleanup_label(status):
    repository = _repository()
    job = _pdf_job(repository, status, current=False)
    original = PDFExtractionJob.objects.values().get(pk=job.pk)

    with_repository_activity((repository,))

    assert repository.has_active_work
    assert repository.activity["phase"] == "cleanup_pending"
    assert repository.activity["pendingCleanupJobs"] == 1
    assert "earlier PDF job" in repository.activity["detail"]
    assert PDFExtractionJob.objects.values().get(pk=job.pk) == original


def test_disabled_or_removed_pdf_jobs_remain_visible_while_their_cleanup_is_pending():
    disabled = _repository("Disabled", enabled=False)
    removed = _repository("Removed PDF")
    _pdf_job(disabled)
    job = _pdf_job(removed)
    PDFDocument.objects.filter(pk=job.document_id).update(
        lifecycle_state=PDFDocumentLifecycle.REMOVED
    )

    with_repository_activity((disabled, removed))

    for repository in (disabled, removed):
        assert repository.has_active_work
        assert repository.activity["pendingCleanupJobs"] == 1
        assert repository.activity["phase"] == "cleanup_pending"


def test_mixed_current_and_obsolete_pdf_jobs_report_counts_without_losing_cleanup_warning():
    repository = _repository()
    _pdf_job(repository)
    _pdf_job(repository, PDFExtractionJobStatus.RUNNING)
    _pdf_job(repository, current=False)
    with_repository_activity((repository,))
    assert repository.activity["phase"] == "extracting"
    assert repository.activity["queuedPdfs"] == 2
    assert repository.activity["runningPdfs"] == 1
    assert repository.activity["pendingCleanupJobs"] == 1
    assert repository.activity["detail"] == (
        "1 PDF extracting · 1 earlier PDF job awaiting cleanup"
    )


def test_sync_and_pdf_jobs_report_all_work_even_when_one_stage_has_precedence():
    repository = _repository()
    _sync_job(repository, status=RepositorySyncJobStatus.QUEUED)
    _pdf_job(repository, PDFExtractionJobStatus.RUNNING)
    with_repository_activity((repository,))
    assert repository.activity["phase"] == "extracting"
    assert repository.activity["detail"] == "1 PDF extracting · Git sync queued"


def test_long_running_pdf_uses_oldest_heartbeat_to_distinguish_health_from_stall():
    observed_at = timezone.now()
    repository = _repository()
    first = _pdf_job(repository, PDFExtractionJobStatus.RUNNING)
    PDFExtractionJob.objects.filter(pk=first.pk).update(
        started_at=observed_at - timedelta(hours=2),
        heartbeat_at=observed_at - timedelta(seconds=5),
    )

    with_repository_activity((repository,), at=observed_at)

    assert repository.activity["healthState"] == "healthy"
    assert repository.activity["healthLabel"] == "Worker responding"
    assert repository.activity["heartbeatAt"] == (observed_at - timedelta(seconds=5)).isoformat()
    assert repository.activity["detail"] == "1 PDF extracting"

    second = _pdf_job(repository, PDFExtractionJobStatus.RUNNING)
    PDFExtractionJob.objects.filter(pk=second.pk).update(
        started_at=observed_at - timedelta(minutes=2),
        heartbeat_at=observed_at - timedelta(seconds=45),
    )
    with_repository_activity((repository,), at=observed_at)

    assert repository.activity["healthState"] == "delayed"
    assert repository.activity["healthLabel"] == "Worker response delayed"
    assert repository.activity["heartbeatAt"] == (observed_at - timedelta(seconds=45)).isoformat()
    assert repository.activity["detail"] == "2 PDFs extracting"

    PDFExtractionJob.objects.filter(pk=second.pk).update(
        heartbeat_at=observed_at - timedelta(minutes=11)
    )
    with_repository_activity((repository,), at=observed_at)

    assert repository.activity["healthState"] == "stalled"
    assert repository.activity["healthLabel"] == "Worker stalled · restarting automatically"
    assert repository.activity["heartbeatAt"] == (observed_at - timedelta(minutes=11)).isoformat()
    assert repository.activity["detail"] == "2 PDFs extracting"


def test_running_git_and_queued_only_work_have_truthful_health_states():
    observed_at = timezone.now()
    running = _repository("Running Git")
    sync_job = _sync_job(running)
    RepositorySyncJob.objects.filter(pk=sync_job.pk).update(
        started_at=observed_at - timedelta(hours=1),
        heartbeat_at=observed_at - timedelta(seconds=2),
    )
    queued = _repository("Queued PDF")
    _pdf_job(queued)

    with_repository_activity((running, queued), at=observed_at)

    assert running.activity["healthState"] == "healthy"
    assert running.activity["healthLabel"] == "Worker responding"
    assert running.activity["heartbeatAt"] == (observed_at - timedelta(seconds=2)).isoformat()
    assert queued.activity["healthState"] is None
    assert queued.activity["healthLabel"] == ""
    assert queued.activity["heartbeatAt"] is None


def test_running_git_stage_remains_visible_beside_pdf_counts_without_duplicate_summary_label():
    repository = _repository()
    _sync_job(repository, phase=RepositorySyncPhase.FINALIZING)
    _pdf_job(repository)
    with_repository_activity((repository,))
    assert repository.activity["detail"] == "Updating catalogue"
    assert repository_work_summary((repository,))["detail"] == (
        f"{repository.display_name}: Updating catalogue"
    )


def test_busy_sync_state_without_job_is_explicit_and_does_not_unlock_refresh():
    repository = _repository()
    repository.sync_state = RepositorySyncState.FETCHING
    with_repository_activity((repository,))
    assert repository.has_active_work
    assert repository.activity["phase"] == "sync_pending"
    assert repository.activity["runningSyncJobs"] == 0
    assert "waiting for a worker job" in repository.activity["detail"]


def test_completed_failed_and_cancelled_jobs_do_not_keep_work_spinner_active():
    repository = _repository()
    _sync_job(repository, status=RepositorySyncJobStatus.SUCCEEDED)
    for status in (
        PDFExtractionJobStatus.SUCCEEDED,
        PDFExtractionJobStatus.CANCELLED,
        PDFExtractionJobStatus.FAILED,
        PDFExtractionJobStatus.INTERRUPTED,
    ):
        _pdf_job(repository, status)
    with_repository_activity((repository,))
    assert not repository.has_active_work
    assert repository.activity["phase"] == "idle"
    assert repository_work_summary((repository,)) == {
        "active": False,
        "label": "Idle",
        "detail": "Repository and PDF workers are idle.",
        "activeRepositories": 0,
        "queuedPdfs": 0,
        "runningPdfs": 0,
        "activities": [],
    }


def test_summary_is_bounded_to_two_repositories_and_two_labels_but_counts_all_jobs():
    repositories = tuple(_repository(name) for name in ("Alpha", "Beta", "Gamma", "Delta"))
    _sync_job(repositories[0], phase=RepositorySyncPhase.CHECKING_CONNECTION)
    _sync_job(
        repositories[1], operation=RepositorySyncOperation.CLONE, phase=RepositorySyncPhase.CLONING
    )
    _pdf_job(repositories[2], PDFExtractionJobStatus.RUNNING)
    _pdf_job(repositories[3])
    with_repository_activity(repositories)
    summary = repository_work_summary(repositories)
    assert summary["active"]
    assert summary["label"] == "Checking connection · Cloning repository · +2 stages"
    assert summary["detail"] == (
        "Alpha: Checking connection · Beta: Cloning repository · +2 more repositories"
    )
    assert summary["activeRepositories"] == 4
    assert summary["queuedPdfs"] == summary["runningPdfs"] == 1
    assert [activity["operation"] for activity in summary["activities"]] == [
        "clone",
        "pull",
        "indexing",
    ]
    assert summary["activities"][0]["progress"] == 0
    assert summary["activities"][2]["queuedJobs"] == 1
    assert summary["activities"][2]["runningJobs"] == 1


def test_active_summary_skips_idle_repositories_and_deduplicates_phase_labels():
    idle, first, second, third = tuple(
        _repository(name) for name in ("Idle", "One", "Two", "Three")
    )
    for repository in (first, second, third):
        _pdf_job(repository)
    with_repository_activity((idle, first, second, third))
    summary = repository_work_summary((idle, first, second, third))
    assert summary["label"] == "PDF extraction queued"
    assert summary["detail"].endswith("+1 more repository")
    assert "Idle:" not in summary["detail"]


def test_activity_reads_are_batched_and_do_not_change_jobs_or_contact_git(monkeypatch):
    repositories = tuple(_repository(f"Repository {number}") for number in range(20))
    for repository in repositories:
        _pdf_job(repository, current=False)
        _pdf_job(repository, PDFExtractionJobStatus.RUNNING)
    before = tuple(PDFExtractionJob.objects.order_by("pk").values())

    def fail(*args, **kwargs):
        pytest.fail("Activity reads must not run Git, workers, or network requests")

    monkeypatch.setattr("subprocess.Popen", fail)
    monkeypatch.setattr("socket.create_connection", fail)
    with CaptureQueriesContext(connection) as queries:
        with_repository_activity(repositories)
        summary = repository_work_summary(repositories)
    assert len(queries) == 4
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    assert tuple(PDFExtractionJob.objects.order_by("pk").values()) == before
    assert summary["activeRepositories"] == 20


def test_initial_context_and_status_poll_agree_for_git_ready_with_pdf_work(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    repository = _repository()
    _pdf_job(repository)
    _pdf_job(repository, PDFExtractionJobStatus.RUNNING)

    initial = client.get(reverse("bitbucket_search:index"))
    response = client.get(reverse("bitbucket_search:repository_status"))
    payload = response.json()

    assert initial.status_code == response.status_code == 200
    initial_work = initial.context["repository_work_summary"]
    assert initial_work["label"] == payload["work"]["label"]
    assert initial_work["activeRepositories"] == payload["work"]["activeRepositories"]
    for work in (initial_work, payload["work"]):
        assert work["activities"][0]["operation"] == "indexing"
        assert work["activities"][0]["progress"] == 0
        assert work["activities"][0]["startedAt"]
    assert payload["repositories"][0]["activity"]["phase"] == "extracting"
    assert payload["repositories"][0]["state"] == RepositorySyncState.READY
    assert payload["work"]["label"] == "Extracting PDF text"
    assert payload["repositories"][0]["workerTiming"]["kind"] == "indexing"
    assert payload["work"]["queuedPdfs"] == payload["work"]["runningPdfs"] == 1


def test_empty_work_summary_is_read_only_and_idle(django_assert_num_queries):
    with django_assert_num_queries(0):
        with_repository_activity(())
        summary = repository_work_summary(())
    assert not summary["active"]
    assert summary["label"] == "Idle"


def test_parallel_pdf_progress_uses_stable_current_run_completion():
    repository = _repository()
    _pdf_job(repository, PDFExtractionJobStatus.RUNNING, progress=25)
    _pdf_job(repository, PDFExtractionJobStatus.RUNNING, progress=75)
    _pdf_job(repository, PDFExtractionJobStatus.QUEUED, progress=0)

    with_repository_activity((repository,))

    activity = repository.activity
    assert activity["operation"] == "indexing"
    assert activity["progress"] == 0
    assert activity["progressScope"] == "current_run_completion"
    assert activity["operations"][0]["queuedJobs"] == 1
    assert activity["operations"][0]["runningJobs"] == 2
    summary = repository_work_summary((repository,))["activities"][0]
    assert summary["progress"] == 0
    assert summary["progressScope"] == "running_jobs_average"


def test_latest_run_counts_ignore_attempts_for_an_old_document_revision():
    repository = _repository()
    stale = _pdf_job(repository, PDFExtractionJobStatus.FAILED, current=False)
    current = _pdf_job(repository, PDFExtractionJobStatus.QUEUED)
    run_id = "90f5b36e-3347-4f73-9643-4784ecca90ba"
    stale.run_id = run_id
    current.run_id = run_id
    stale.save(update_fields=("run_id",))
    current.save(update_fields=("run_id",))

    with_repository_activity((repository,))

    assert repository.activity["pdfCounts"]["queued"] == 1
    assert repository.activity["pdfCounts"]["failed"] == 0
