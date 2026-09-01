from __future__ import annotations

import importlib
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import OperationalError, connection
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketPeopleGroupMember,
    BitbucketRepository,
    GitCommit,
    GitCommitFolder,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import pdf_indexing, repository_lifecycle, repository_sync
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.repository_lifecycle import (
    RepositoryLifecycleError,
    remove_repository,
    set_repository_refresh_excluded,
)
from bitbucket_search.services.repository_lock import repository_checkout_lock
from bookmark_manager.models import Notification, NotificationKind, NotificationState

pytestmark = pytest.mark.django_db


def _repository(name="synthetic"):
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{name}",
        remote_url=f"https://example.invalid/team/{name}.git",
        sync_state=RepositorySyncState.READY,
    )


@pytest.fixture
def stored_repository(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path.resolve() / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = settings.MEDIA_ROOT / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = settings.MEDIA_ROOT / "bitbucket" / "tmp"
    repository = _repository()
    checkout = managed_repository_path(repository)
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / ".git" / "readonly").write_bytes(b"synthetic Git object")
    (checkout / ".git" / "readonly").chmod(stat.S_IRUSR)
    (checkout / "Test.pdf").write_bytes(b"synthetic PDF")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path",))
    snapshot = settings.MEDIA_ROOT / "bitbucket" / "excluded" / str(repository.pk)
    snapshot.mkdir(parents=True)
    (snapshot / "99.pdf").write_bytes(b"synthetic frozen PDF")
    staging = settings.BITBUCKET_TEMP_ROOT / f"repository-{repository.pk}-{'a' * 32}"
    staging.mkdir(parents=True)
    (staging / "Test.pdf").write_bytes(b"synthetic incomplete clone")
    return repository, checkout, snapshot, staging


def _revision(token):
    revision = PDFTextRevision.objects.create(
        content_sha256=token * 64,
        extractor_version="synthetic-test-v1",
        state=PDFTextRevisionState.READY,
        page_count=1,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text=f"UniqueToken{token}",
        extraction_state=PDFPageExtractionState.READY,
    )
    return revision


def _pdf(repository, name="Test.pdf", revision=None):
    return PDFDocument.objects.create(
        repository=repository,
        filename=name,
        relative_path=name,
        indexed_revision=revision,
    )


def _extraction(document, status):
    return PDFExtractionJob.objects.create(
        document=document,
        status=status,
        target_git_blob_id="a" * 40,
        target_source_commit="b" * 40,
        target_relative_path=document.relative_path,
        target_extractor_version="synthetic-test-v1",
    )


def test_exclusion_skips_bulk_but_keeps_repository_and_pdf_visible():
    included = _repository("included")
    excluded = _repository("excluded")
    document = _pdf(excluded)

    changed = set_repository_refresh_excluded(excluded.pk, excluded=True)
    result = repository_sync.queue_all_repository_refreshes()

    assert changed.enabled and changed.exclude_from_refresh
    assert {queued.repository.pk for queued in result.results} == {included.pk}
    assert PDFDocument.objects.filter(pk=document.pk, repository__enabled=True).exists()
    explicit = repository_sync.queue_repository_refresh(excluded.pk)
    assert explicit.job_created
    assert explicit.job.repository_id == excluded.pk


def test_exclusion_does_not_cancel_existing_jobs_and_include_resumes_bulk():
    repository = _repository()
    queued = repository_sync.queue_repository_refresh(repository.pk)
    set_repository_refresh_excluded(repository.pk, excluded=True)
    queued.job.refresh_from_db()
    assert queued.job.status == RepositorySyncJobStatus.QUEUED
    set_repository_refresh_excluded(repository.pk, excluded=False)
    assert repository_sync.queue_all_repository_refreshes().eligible_total == 1


@pytest.mark.parametrize("has_failure", [False, True])
def test_exclusion_skips_daily_and_delayed_retry(settings, has_failure):
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = True
    repository = _repository()
    observed = datetime(2026, 8, 30, 16, tzinfo=UTC)
    if has_failure:
        RepositorySyncJob.objects.create(
            repository=repository,
            operation=RepositorySyncOperation.REFRESH,
            trigger=RepositorySyncTrigger.DAILY,
            scheduled_day=observed.date(),
            status=RepositorySyncJobStatus.FAILED,
            completed_at=observed - timedelta(hours=3),
        )
    set_repository_refresh_excluded(repository.pk, excluded=True)
    assert repository_sync.queue_due_daily_repository_refreshes(at=observed) == ()
    status = repository_sync.repository_status_snapshot(at=observed)[0].automatic_refresh
    assert status.state == "excluded"
    assert status.next_action_at is None


def test_removal_cleans_owned_files_records_and_fts_but_keeps_shared_data(
    stored_repository, settings
):
    repository, checkout, snapshots, staging = stored_repository
    other = _repository("other")
    shared = _revision("a")
    unique = _revision("b")
    _revision("c")  # An obsolete, unreferenced historical cache version.
    _pdf(repository, "Shared.pdf", shared)
    document = _pdf(repository, revision=unique)
    other_document = _pdf(other, revision=shared)
    _extraction(document, PDFExtractionJobStatus.SUCCEEDED)
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash="c" * 40,
        author_name="Synthetic",
        committer_name="Synthetic",
        authored_at=timezone.now(),
        committed_at=timezone.now(),
    )
    GitCommitFolder.objects.create(commit=commit, folder_path="docs")
    RepositorySyncJob.objects.create(repository=repository, operation="refresh", status="succeeded")
    PDFLocalPolicy.objects.create(
        repository=repository, relative_path="Deleted.pdf", state="deleted"
    )
    group = BitbucketPeopleGroup.objects.create(name="My group")
    member = BitbucketPeopleGroupMember.objects.create(group=group, person_name="Synthetic")
    for key in (
        f"bitbucket-connection:{repository.pk}",
        f"bitbucket-refresh:{repository.pk}:2026-08-30",
    ):
        Notification.objects.create(
            kind=NotificationKind.BITBUCKET_REFRESH,
            state=NotificationState.WARNING,
            event_key=key,
            title="Synthetic",
        )
    other_notification = Notification.objects.create(
        kind=NotificationKind.BITBUCKET_REFRESH,
        state=NotificationState.WARNING,
        event_key=f"bitbucket-refresh:{repository.pk}0:day",
        title="Other",
    )
    unrelated_staging = settings.BITBUCKET_TEMP_ROOT / f"repository-{repository.pk}0-{'a' * 32}"
    unrelated_staging.mkdir()
    (unrelated_staging / "keep").write_bytes(b"keep")
    unrelated_parser = settings.BITBUCKET_TEMP_ROOT / "pdf-extraction-unattributed"
    unrelated_parser.write_bytes(b"keep")

    result = remove_repository(repository.pk, confirmed=True)

    assert result.display_name == repository.display_name
    assert not any(path.exists() for path in (checkout, snapshots, staging))
    assert not BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert not RepositoryRemovalRecovery.objects.exists()
    assert not GitCommit.objects.filter(repository_id=repository.pk).exists()
    assert not GitCommitFolder.objects.filter(commit_id=commit.pk).exists()
    assert not PDFExtractionJob.objects.exists()
    assert not RepositorySyncJob.objects.exists()
    assert not PDFLocalPolicy.objects.exists()
    assert list(PDFTextRevision.objects.values_list("pk", flat=True)) == [shared.pk]
    assert list(PDFDocument.objects.values_list("pk", flat=True)) == [other_document.pk]
    assert BitbucketPeopleGroupMember.objects.filter(pk=member.pk).exists()
    assert list(Notification.objects.values_list("pk", flat=True)) == [other_notification.pk]
    assert unrelated_staging.exists() and unrelated_parser.exists()
    assert (
        settings.BITBUCKET_TEMP_ROOT / "checkout-locks" / f"repository-{repository.pk}.lock"
    ).exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT document_id FROM bitbucket_search_pdf_metadata_fts")
        assert cursor.fetchall() == [(other_document.pk,)]
        cursor.execute(
            "SELECT rowid FROM bitbucket_search_pdf_page_fts WHERE bitbucket_search_pdf_page_fts MATCH 'UniqueTokenb'"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT rowid FROM bitbucket_search_pdf_page_fts WHERE bitbucket_search_pdf_page_fts MATCH 'UniqueTokena'"
        )
        assert len(cursor.fetchall()) == 1


@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_removal_rejects_active_sync_without_file_changes(stored_repository, status):
    repository, checkout, _snapshots, _staging = stored_repository
    RepositorySyncJob.objects.create(repository=repository, operation="refresh", status=status)
    pdf_job = _extraction(_pdf(repository), PDFExtractionJobStatus.QUEUED)
    with pytest.raises(RepositoryLifecycleError, match="background refresh") as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_busy"
    assert checkout.exists() and BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert not RepositoryRemovalRecovery.objects.exists()
    pdf_job.refresh_from_db()
    assert pdf_job.status == PDFExtractionJobStatus.QUEUED


@pytest.mark.parametrize("status", [PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING])
def test_removal_cancels_active_pdf_work_before_deleting(stored_repository, status, monkeypatch):
    repository, checkout, _snapshots, _staging = stored_repository
    job = _extraction(_pdf(repository), status)
    observed = {}
    delete_database_records = repository_lifecycle._delete_database_records
    cancel_extractions = repository_lifecycle.cancel_repository_pdf_extractions

    def inspect_removal_barrier(repository_id):
        recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository_id)
        observed["intent_committed_before_cancel"] = not recovery.database_deleted
        assert pdf_indexing.claim_next_extraction_job() is None
        return cancel_extractions(repository_id)

    def inspect_cancelled_job(locked_repository):
        job.refresh_from_db()
        recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository.pk)
        observed.update(
            status=job.status,
            error_code=job.error_code,
            recovery_database_deleted=recovery.database_deleted,
            cancellation_logs=job.operation_log_entries.filter(event="indexing_cancelled").count(),
        )
        delete_database_records(locked_repository)

    monkeypatch.setattr(
        repository_lifecycle,
        "_delete_database_records",
        inspect_cancelled_job,
    )
    monkeypatch.setattr(
        repository_lifecycle,
        "cancel_repository_pdf_extractions",
        inspect_removal_barrier,
    )

    result = remove_repository(repository.pk, confirmed=True)

    assert result.repository_id == repository.pk
    assert observed == {
        "intent_committed_before_cancel": True,
        "status": PDFExtractionJobStatus.CANCELLED,
        "error_code": "indexing_cancelled_by_user",
        "recovery_database_deleted": False,
        "cancellation_logs": 1,
    }
    assert not checkout.exists()
    assert not BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert not RepositoryRemovalRecovery.objects.exists()


def test_removal_retains_intent_when_checkout_reader_outlives_bounded_wait(
    stored_repository, monkeypatch
):
    repository, checkout, _snapshots, _staging = stored_repository
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk)
    assert caught.value.code == "repository_delete_confirmation_required"
    monkeypatch.setattr(repository_lifecycle, "_REMOVAL_CHECKOUT_WAIT_SECONDS", 0)
    with (
        repository_checkout_lock(repository.pk, blocking=False, shared=True),
        pytest.raises(RepositoryLifecycleError) as caught,
    ):
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_removal_pending"
    assert "Retry removal" in caught.value.summary
    recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository.pk)
    assert not recovery.database_deleted and recovery.quarantine_manifest == []
    assert checkout.exists() and BitbucketRepository.objects.filter(pk=repository.pk).exists()

    result = remove_repository(repository.pk, confirmed=True)

    assert result.repository_id == repository.pk
    assert not checkout.exists()
    assert not RepositoryRemovalRecovery.objects.exists()


@pytest.mark.parametrize("variant", ["stored_path", "checkout", "nested", "snapshot", "root"])
def test_removal_refuses_path_tampering_and_links(stored_repository, tmp_path, settings, variant):
    repository, checkout, snapshots, _staging = stored_repository
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.pdf"
    marker.write_bytes(b"never remove")
    if variant == "stored_path":
        repository.local_path = str(outside)
        repository.save(update_fields=("local_path",))
    elif variant == "checkout":
        checkout.rename(checkout.with_name("intact-original"))
        checkout.symlink_to(outside, target_is_directory=True)
    elif variant == "nested":
        (checkout / "escape").symlink_to(outside, target_is_directory=True)
    elif variant == "snapshot":
        snapshots.rename(snapshots.with_name("intact-original"))
        snapshots.symlink_to(outside, target_is_directory=True)
    else:
        linked = tmp_path / "linked-root"
        linked.symlink_to(settings.BITBUCKET_REPOSITORIES_ROOT, target_is_directory=True)
        settings.BITBUCKET_REPOSITORIES_ROOT = linked
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "invalid_repository_path"
    assert marker.read_bytes() == b"never remove"
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()


def test_database_failure_restores_quarantined_trees(stored_repository, monkeypatch):
    repository, checkout, snapshots, staging = stored_repository
    original_delete = repository_lifecycle._delete_database_records
    revision = _revision("d")
    document = _pdf(repository, revision=revision)

    def fail(_repository):
        assert not checkout.exists() and not snapshots.exists() and not staging.exists()
        original_delete(_repository)
        raise OperationalError("synthetic failed database delete")

    monkeypatch.setattr(repository_lifecycle, "_delete_database_records", fail)
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_removal_failed"
    assert (checkout / "Test.pdf").read_bytes() == b"synthetic PDF"
    assert snapshots.exists() and staging.exists()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert PDFTextRevision.objects.filter(pk=revision.pk).exists()
    recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository.pk)
    assert not recovery.database_deleted
    assert recovery.quarantine_manifest


def test_cleanup_failure_is_tracked_and_same_removal_action_retries(stored_repository, monkeypatch):
    repository, checkout, snapshots, staging = stored_repository
    original_remove = repository_lifecycle._remove_tree

    def fail(_path):
        raise PermissionError("synthetic locked Git object")

    monkeypatch.setattr(repository_lifecycle, "_remove_tree", fail)
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_cleanup_incomplete"
    assert not BitbucketRepository.objects.filter(pk=repository.pk).exists()
    recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository.pk)
    assert recovery.database_deleted
    assert not checkout.exists() and not snapshots.exists() and not staging.exists()
    assert all(
        quarantine.exists()
        for _source, quarantine in repository_lifecycle._manifest_paths(recovery)
    )
    monkeypatch.setattr(repository_lifecycle, "_remove_tree", original_remove)
    assert remove_repository(repository.pk, confirmed=True).repository_id == repository.pk
    assert not RepositoryRemovalRecovery.objects.exists()
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_unavailable"


def test_partial_quarantine_move_failure_restores_all_originals(stored_repository, monkeypatch):
    repository, checkout, snapshots, staging = stored_repository
    replace = repository_lifecycle.os.replace
    calls = 0

    def fail_second_move(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("synthetic second directory locked")
        return replace(source, target)

    monkeypatch.setattr(repository_lifecycle.os, "replace", fail_second_move)
    with pytest.raises(RepositoryLifecycleError):
        remove_repository(repository.pk, confirmed=True)
    assert all(path.exists() for path in (checkout, snapshots, staging))
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()
    recovery = RepositoryRemovalRecovery.objects.get(repository_id=repository.pk)
    assert not recovery.database_deleted
    assert recovery.quarantine_manifest


def test_interrupted_journal_blocks_new_work_and_retry_restores_then_removes(
    stored_repository, settings
):
    repository, checkout, snapshots, staging = stored_repository
    recovery = RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk,
        display_name=repository.display_name,
        quarantine_manifest=repository_lifecycle._manifest(repository),
    )
    original, quarantine = repository_lifecycle._manifest_paths(recovery)[0]
    original.rename(quarantine)
    assert not checkout.exists()
    with pytest.raises(RepositorySyncError) as caught:
        repository_sync.queue_repository_refresh(repository.pk)
    assert caught.value.code == "repository_removal_pending"
    assert repository_sync.queue_all_repository_refreshes().eligible_total == 0
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = True
    assert repository_sync.queue_due_daily_repository_refreshes() == ()
    assert not repository.sync_jobs.exists()

    result = remove_repository(repository.pk, confirmed=True)

    assert result.repository_id == repository.pk
    assert not any(path.exists() for path in (checkout, snapshots, staging, quarantine))
    assert not RepositoryRemovalRecovery.objects.exists()


def test_pending_removal_blocks_legacy_queued_job_claim(stored_repository):
    repository, _checkout, _snapshots, _staging = stored_repository
    RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk, display_name=repository.display_name
    )
    job = RepositorySyncJob.objects.create(repository=repository, operation="refresh")
    assert repository_sync.claim_next_job() is None
    job.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.QUEUED
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_busy"


def test_partial_cleanup_retry_does_not_reuse_repository_id(stored_repository, monkeypatch):
    repository, _checkout, _snapshots, _staging = stored_repository
    remove_tree = repository_lifecycle._remove_tree
    calls = 0

    def fail_second_cleanup(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("synthetic incomplete cleanup")
        remove_tree(path)

    monkeypatch.setattr(repository_lifecycle, "_remove_tree", fail_second_cleanup)
    with pytest.raises(RepositoryLifecycleError):
        remove_repository(repository.pk, confirmed=True)
    newer = _repository("newer")
    assert newer.pk > repository.pk
    monkeypatch.setattr(repository_lifecycle, "_remove_tree", remove_tree)
    remove_repository(repository.pk, confirmed=True)
    assert BitbucketRepository.objects.filter(pk=newer.pk).exists()
    assert not RepositoryRemovalRecovery.objects.exists()


@pytest.mark.parametrize("windows", [False, True])
def test_readonly_cleanup_retries_only_windows_delete_functions(
    stored_repository, monkeypatch, windows
):
    _repository, checkout, _snapshots, _staging = stored_repository
    target = checkout / ".git" / "readonly"
    monkeypatch.setattr(repository_lifecycle, "_IS_WINDOWS", windows)

    def simulated_rmtree(_path, *, onexc):
        assert target.stat().st_mode & stat.S_IWRITE == 0
        error = PermissionError("synthetic Windows readonly bit")
        onexc(repository_lifecycle.os.unlink, str(target), error)

    monkeypatch.setattr(repository_lifecycle.shutil, "rmtree", simulated_rmtree)
    if windows:
        repository_lifecycle._remove_tree(checkout)
        assert not target.exists()
    else:
        with pytest.raises(PermissionError):
            repository_lifecycle._remove_tree(checkout)
        assert target.exists() and not target.stat().st_mode & stat.S_IWRITE


def test_quarantine_manifest_cannot_delete_an_arbitrary_directory(stored_repository, tmp_path):
    repository, _checkout, _snapshots, _staging = stored_repository
    outside = tmp_path / "outside"
    outside.mkdir()
    recovery = RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk,
        display_name=repository.display_name,
        database_deleted=True,
        quarantine_manifest=[{"kind": "checkout", "name": str(outside), "token": "a" * 32}],
    )
    with pytest.raises(RepositoryLifecycleError) as caught:
        remove_repository(repository.pk, confirmed=True)
    assert caught.value.code == "repository_cleanup_incomplete"
    assert outside.exists() and RepositoryRemovalRecovery.objects.filter(pk=recovery.pk).exists()


def test_migration_moves_only_excluded_rules_without_touching_files(stored_repository):
    repository, checkout, snapshots, _staging = stored_repository
    resumed = _repository("resuming")
    PDFLocalPolicy.objects.create(
        repository=repository, relative_path="Excluded.pdf", state="excluded"
    )
    PDFLocalPolicy.objects.create(
        repository=repository, relative_path="Deleted.pdf", state="deleted"
    )
    PDFLocalPolicy.objects.create(repository=resumed, relative_path="Resume.pdf", state="resuming")
    migration = importlib.import_module(
        "bitbucket_search.migrations.0010_repository_refresh_and_removal"
    )
    migration.move_file_exclusions_to_repositories(apps, SimpleNamespace(connection=connection))
    repository.refresh_from_db()
    resumed.refresh_from_db()
    assert repository.exclude_from_refresh and not resumed.exclude_from_refresh
    assert set(repository.pdf_local_policies.values_list("state", flat=True)) == {
        PDFLocalPolicyState.RESUMING,
        PDFLocalPolicyState.DELETED,
    }
    assert (checkout / "Test.pdf").read_bytes() == b"synthetic PDF"
    assert (snapshots / "99.pdf").read_bytes() == b"synthetic frozen PDF"
