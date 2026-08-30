from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services import git_sync, pdf_local_policy
from bitbucket_search.services.document_actions import DocumentActionError, validated_pdf_path
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import PDFSearchQuery
from bitbucket_search.services.repository_lock import repository_checkout_lock

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(shutil.which("git") is None, reason="Git is required"),
]
_PDF_BYTES = b"%PDF synthetic private local-policy content"


def _git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def registered_pdf(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = settings.MEDIA_ROOT / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = settings.MEDIA_ROOT / "bitbucket" / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="Local PDF Policy",
        canonical_remote_key="example.invalid/team/local-policy",
        remote_url="https://example.invalid/team/local-policy.git",
        sync_state=RepositorySyncState.READY,
        pdf_count=1,
        document_bytes=len(_PDF_BYTES),
    )
    checkout = managed_repository_path(repository)
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-b", "main")
    _git(checkout, "config", "user.name", "Synthetic OWL")
    _git(checkout, "config", "user.email", "owl@example.invalid")
    (checkout / "docs").mkdir()
    pdf_path = checkout / "docs" / "Frozen.pdf"
    pdf_path.write_bytes(_PDF_BYTES)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "Synthetic local document")
    _git(checkout, "sparse-checkout", "init", "--no-cone")
    repository.local_path = str(checkout)
    repository.last_synced_commit = _git(checkout, "rev-parse", "HEAD")
    repository.metadata_indexed_commit = repository.last_synced_commit
    repository.save(
        update_fields=("local_path", "last_synced_commit", "metadata_indexed_commit", "updated_at")
    )
    git_sync.apply_document_checkout_policy(repository)
    revision = PDFTextRevision.objects.create(
        content_sha256=hashlib.sha256(_PDF_BYTES).hexdigest(),
        extractor_version="synthetic-v1",
        source_byte_size=len(_PDF_BYTES),
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=11,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text="Freezetoken",
        character_count=11,
        extraction_state=PDFPageExtractionState.READY,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Frozen.pdf",
        file_size=len(_PDF_BYTES),
        git_blob_id=_git(checkout, "rev-parse", "HEAD:docs/Frozen.pdf"),
        last_seen_commit=repository.last_synced_commit,
        indexed_revision=revision,
        index_state=PDFIndexState.READY,
        open_count=7,
    )
    return document, pdf_path


def _job(document, status):
    return PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id=document.git_blob_id,
        target_source_commit=document.last_seen_commit,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version="synthetic-v1",
        status=status,
    )


def test_exclusion_freezes_searchable_openable_bytes_and_keeps_git_clean(registered_pdf):
    document, pdf_path = registered_pdf
    queued = _job(document, PDFExtractionJobStatus.QUEUED)

    frozen_document = pdf_local_policy.exclude_registered_pdf(document.pk)

    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = pdf_local_policy.policy_snapshot_path(policy, require_exists=True)
    assert policy.state == PDFLocalPolicyState.EXCLUDED
    assert snapshot.read_bytes() == _PDF_BYTES
    assert not snapshot.is_relative_to(Path(document.repository.local_path))
    assert not pdf_path.exists()
    assert validated_pdf_path(frozen_document) == snapshot
    assert _git(Path(document.repository.local_path), "status", "--porcelain") == ""
    assert search_documents(PDFSearchQuery(chips=("Freezetoken",))).total == 1
    document.refresh_from_db()
    queued.refresh_from_db()
    assert document.open_count == 7
    assert document.indexed_revision_id is not None
    assert queued.status == PDFExtractionJobStatus.CANCELLED
    again = pdf_local_policy.exclude_registered_pdf(document.pk)
    assert again.pk == document.pk
    assert PDFLocalPolicy.objects.count() == 1


def test_resume_retains_snapshot_until_successful_publication(
    registered_pdf, django_capture_on_commit_callbacks
):
    document, pdf_path = registered_pdf
    pdf_local_policy.exclude_registered_pdf(document.pk)
    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = pdf_local_policy.policy_snapshot_path(policy)

    resumed = pdf_local_policy.resume_registered_pdf(document.pk)

    policy.refresh_from_db()
    assert policy.state == PDFLocalPolicyState.RESUMING
    assert validated_pdf_path(resumed) == snapshot
    assert not pdf_path.exists()
    git_sync.apply_document_checkout_policy(document.repository)
    assert pdf_path.read_bytes() == _PDF_BYTES
    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        pdf_local_policy.complete_resumed_policies(document.repository_id, [document.relative_path])
        assert snapshot.exists()
        assert not PDFLocalPolicy.objects.exists()
    assert not snapshot.exists()
    document.refresh_from_db()
    assert validated_pdf_path(document) == pdf_path


def test_resume_cleanup_is_not_applied_when_catalogue_transaction_rolls_back(
    registered_pdf, django_capture_on_commit_callbacks
):
    document, _pdf_path = registered_pdf
    pdf_local_policy.exclude_registered_pdf(document.pk)
    pdf_local_policy.resume_registered_pdf(document.pk)
    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = pdf_local_policy.policy_snapshot_path(policy)

    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        pdf_local_policy.complete_resumed_policies(document.repository_id, [document.relative_path])
        raise RuntimeError("synthetic publisher rollback")

    assert snapshot.read_bytes() == _PDF_BYTES
    assert PDFLocalPolicy.objects.get(pk=policy.pk).state == PDFLocalPolicyState.RESUMING


def test_resume_database_failure_keeps_the_exclusion_and_snapshot(registered_pdf, monkeypatch):
    document, _pdf_path = registered_pdf
    pdf_local_policy.exclude_registered_pdf(document.pk)
    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = pdf_local_policy.policy_snapshot_path(policy)
    monkeypatch.setattr(
        PDFLocalPolicy,
        "save",
        Mock(side_effect=OperationalError("synthetic private database error")),
    )

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.resume_registered_pdf(document.pk)

    assert failure.value.code == "pdf_policy_failed"
    assert "synthetic private" not in failure.value.summary
    policy.refresh_from_db()
    assert policy.state == PDFLocalPolicyState.EXCLUDED
    assert snapshot.read_bytes() == _PDF_BYTES


@pytest.mark.parametrize("excluded", [False, True])
def test_confirmed_delete_removes_file_document_jobs_text_and_fts_but_retains_tombstone(
    registered_pdf, excluded
):
    document, pdf_path = registered_pdf
    document_id = document.pk
    revision_id = document.indexed_revision_id
    finished_job = _job(document, PDFExtractionJobStatus.SUCCEEDED)
    queued_job = _job(document, PDFExtractionJobStatus.QUEUED)
    if excluded:
        pdf_local_policy.exclude_registered_pdf(document.pk)

    pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert not pdf_path.exists()
    assert not PDFDocument.objects.filter(pk=document_id).exists()
    assert not PDFExtractionJob.objects.filter(pk__in=(finished_job.pk, queued_job.pk)).exists()
    assert not PDFTextRevision.objects.filter(pk=revision_id).exists()
    assert not PDFTextPage.objects.filter(revision_id=revision_id).exists()
    assert search_documents(PDFSearchQuery(chips=("Freezetoken",))).total == 0
    assert search_documents(PDFSearchQuery(chips=("Frozen",))).total == 0
    policy = PDFLocalPolicy.objects.get()
    assert policy.document_id is None
    assert policy.relative_path == "docs/Frozen.pdf"
    assert policy.state == PDFLocalPolicyState.DELETED
    assert not pdf_local_policy.policy_snapshot_path(policy).exists()
    assert _git(Path(document.repository.local_path), "status", "--porcelain") == ""
    document.repository.refresh_from_db()
    assert document.repository.pdf_count == 0
    assert document.repository.document_bytes == 0


def test_delete_preserves_shared_text_and_unrelated_orphan_revisions(registered_pdf):
    document, _pdf_path = registered_pdf
    shared_id = document.indexed_revision_id
    sibling = PDFDocument.objects.create(
        repository=document.repository,
        filename="Other.pdf",
        relative_path="docs/Other.pdf",
        indexed_revision=document.indexed_revision,
        index_state=PDFIndexState.READY,
    )
    orphan = PDFTextRevision.objects.create(
        content_sha256="c" * 64,
        extractor_version="synthetic-v1",
        state=PDFTextRevisionState.READY,
    )

    pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    sibling.refresh_from_db()
    assert sibling.indexed_revision_id == shared_id
    assert PDFTextRevision.objects.filter(pk__in=(shared_id, orphan.pk)).count() == 2
    assert PDFTextPage.objects.filter(revision_id=shared_id).count() == 1
    assert search_documents(PDFSearchQuery(chips=("Freezetoken",))).total == 1


def test_delete_requires_explicit_boolean_confirmation_before_any_mutation(registered_pdf):
    document, pdf_path = registered_pdf
    for confirmation in (False, None, "true", 1):
        with pytest.raises(DocumentActionError) as failure:
            pdf_local_policy.delete_registered_pdf(document.pk, confirmed=confirmation)
        assert failure.value.code == "pdf_delete_confirmation_required"
    assert pdf_path.read_bytes() == _PDF_BYTES
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize("operation", ["exclude", "delete"])
@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_active_repository_job_prevents_document_mutations(registered_pdf, operation, status):
    document, pdf_path = registered_pdf
    RepositorySyncJob.objects.create(repository=document.repository, status=status)

    with pytest.raises(DocumentActionError) as failure:
        if operation == "exclude":
            pdf_local_policy.exclude_registered_pdf(document.pk)
        else:
            pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert failure.value.code == "repository_busy"
    assert pdf_path.read_bytes() == _PDF_BYTES
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize(
    "state",
    [
        RepositorySyncState.QUEUED,
        RepositorySyncState.CLONING,
        RepositorySyncState.FETCHING,
        RepositorySyncState.UPDATING,
    ],
)
def test_active_repository_state_prevents_document_mutations(registered_pdf, state):
    document, pdf_path = registered_pdf
    document.repository.sync_state = state
    document.repository.save(update_fields=("sync_state", "updated_at"))

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.exclude_registered_pdf(document.pk)

    assert failure.value.code == "repository_busy"
    assert pdf_path.read_bytes() == _PDF_BYTES


def test_running_extraction_and_checkout_lock_prevent_document_mutation(registered_pdf):
    document, pdf_path = registered_pdf
    job = _job(document, PDFExtractionJobStatus.RUNNING)
    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)
    assert failure.value.code == "pdf_extraction_busy"
    job.status = PDFExtractionJobStatus.SUCCEEDED
    job.completed_at = timezone.now()
    job.save(update_fields=("status", "completed_at"))
    with (
        repository_checkout_lock(document.repository_id, blocking=False),
        pytest.raises(DocumentActionError) as busy,
    ):
        pdf_local_policy.exclude_registered_pdf(document.pk)
    assert busy.value.code == "repository_busy"
    assert pdf_path.read_bytes() == _PDF_BYTES


def test_dirty_checkout_is_preserved_and_queued_job_cancellation_rolls_back(registered_pdf):
    document, pdf_path = registered_pdf
    job = _job(document, PDFExtractionJobStatus.QUEUED)
    pdf_path.write_bytes(b"preserve synthetic local edits")

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert failure.value.code == "dirty_working_tree"
    assert pdf_path.read_bytes() == b"preserve synthetic local edits"
    job.refresh_from_db()
    assert job.status == PDFExtractionJobStatus.QUEUED
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize("state", [RepositorySyncState.FAILED, RepositorySyncState.INTERRUPTED])
def test_new_exclusion_waits_for_published_checkout_but_deletion_remains_available(
    registered_pdf, state
):
    document, pdf_path = registered_pdf
    unpublished_bytes = b"X" * len(_PDF_BYTES)
    pdf_path.write_bytes(unpublished_bytes)
    checkout = Path(document.repository.local_path)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "Synthetic pull before catalogue failure")
    document.repository.sync_state = state
    document.repository.save(update_fields=("sync_state", "updated_at"))

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.exclude_registered_pdf(document.pk)

    assert failure.value.code == "repository_not_ready"
    assert not PDFLocalPolicy.objects.exists()
    assert pdf_path.read_bytes() == unpublished_bytes
    pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)
    assert not pdf_path.exists()
    assert not PDFDocument.objects.filter(pk=document.pk).exists()


def test_failed_refresh_does_not_prevent_existing_frozen_pdf_from_resuming(registered_pdf):
    document, _pdf_path = registered_pdf
    pdf_local_policy.exclude_registered_pdf(document.pk)
    document.repository.sync_state = RepositorySyncState.FAILED
    document.repository.save(update_fields=("sync_state", "updated_at"))

    repeated = pdf_local_policy.exclude_registered_pdf(document.pk)
    resumed = pdf_local_policy.resume_registered_pdf(document.pk)

    assert repeated.pk == resumed.pk == document.pk
    assert PDFLocalPolicy.objects.get(document=document).state == PDFLocalPolicyState.RESUMING


@pytest.mark.parametrize(
    "path", ["../outside.pdf", "/tmp/outside.pdf", "docs/../Frozen.pdf", "docs\\Frozen.pdf"]
)
def test_unsafe_registered_paths_are_rejected_before_sparse_changes(
    registered_pdf, path, monkeypatch
):
    document, pdf_path = registered_pdf
    PDFDocument.objects.filter(pk=document.pk).update(relative_path=path)
    apply = Mock(side_effect=AssertionError("unsafe path must not change Git"))
    monkeypatch.setattr(git_sync, "apply_document_checkout_policy", apply)

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert failure.value.code == "invalid_document_path"
    apply.assert_not_called()
    assert pdf_path.read_bytes() == _PDF_BYTES


@pytest.mark.parametrize("symlink_part", ["file", "git", "checkout"])
def test_symlinked_working_paths_are_rejected_without_touching_targets(
    registered_pdf, tmp_path, symlink_part
):
    document, pdf_path = registered_pdf
    checkout = Path(document.repository.local_path)
    if symlink_part == "file":
        target = tmp_path / "outside.pdf"
        pdf_path.replace(target)
        pdf_path.symlink_to(target)
    elif symlink_part == "git":
        target = tmp_path / "outside-git"
        (checkout / ".git").replace(target)
        (checkout / ".git").symlink_to(target, target_is_directory=True)
    else:
        target = tmp_path / "outside-checkout"
        checkout.replace(target)
        checkout.symlink_to(target, target_is_directory=True)

    with pytest.raises(DocumentActionError):
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert target.exists()
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


def test_snapshot_symlink_is_not_opened_or_deleted(registered_pdf, tmp_path):
    document, _pdf_path = registered_pdf
    pdf_local_policy.exclude_registered_pdf(document.pk)
    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = pdf_local_policy.policy_snapshot_path(policy)
    outside = tmp_path / "outside.pdf"
    snapshot.replace(outside)
    snapshot.symlink_to(outside)

    for action in (
        lambda: validated_pdf_path(PDFDocument.objects.get(pk=document.pk)),
        lambda: pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True),
    ):
        with pytest.raises(DocumentActionError):
            action()
    assert outside.read_bytes() == _PDF_BYTES
    assert PDFDocument.objects.filter(pk=document.pk).exists()


def test_partial_backup_failure_never_overwrites_original(registered_pdf, monkeypatch):
    document, pdf_path = registered_pdf

    def broken_copy(_source, destination):
        destination.write_bytes(b"partial")
        raise OSError("synthetic-private-failure")

    monkeypatch.setattr(pdf_local_policy, "_copy_regular_file", broken_copy)
    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert failure.value.code == "pdf_policy_failed"
    assert "synthetic-private" not in failure.value.summary
    assert pdf_path.read_bytes() == _PDF_BYTES
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


def test_sparse_checkout_failure_restores_original_and_database(registered_pdf, monkeypatch):
    document, pdf_path = registered_pdf
    real_apply = git_sync.apply_document_checkout_policy
    calls = 0

    def fail_after_first_apply(*args, **kwargs):
        nonlocal calls
        calls += 1
        real_apply(*args, **kwargs)
        if calls == 1:
            raise OSError("synthetic sparse failure")

    monkeypatch.setattr(git_sync, "apply_document_checkout_policy", fail_after_first_apply)
    with pytest.raises(DocumentActionError):
        pdf_local_policy.exclude_registered_pdf(document.pk)

    assert pdf_path.read_bytes() == _PDF_BYTES
    assert _git(Path(document.repository.local_path), "status", "--porcelain") == ""
    assert not PDFLocalPolicy.objects.exists()


def test_failed_git_compensation_preserves_original_and_reports_repair_needed(
    registered_pdf, monkeypatch
):
    document, pdf_path = registered_pdf
    real_apply = git_sync.apply_document_checkout_policy
    calls = 0

    def fail_both_attempts(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_apply(*args, **kwargs)
        raise OSError("synthetic private compensation error")

    monkeypatch.setattr(git_sync, "apply_document_checkout_policy", fail_both_attempts)
    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.exclude_registered_pdf(document.pk)

    assert failure.value.code == "pdf_policy_rollback_failed"
    assert "synthetic private" not in failure.value.summary
    assert pdf_path.read_bytes() == _PDF_BYTES
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize("excluded", [False, True])
def test_database_failure_after_file_removal_restores_document_files_and_index(
    registered_pdf, monkeypatch, excluded
):
    document, pdf_path = registered_pdf
    if excluded:
        pdf_local_policy.exclude_registered_pdf(document.pk)
    original_delete = PDFDocument.delete

    def fail_after_delete(target, *args, **kwargs):
        original_delete(target, *args, **kwargs)
        raise OperationalError("synthetic database failure")

    monkeypatch.setattr(PDFDocument, "delete", fail_after_delete)
    with pytest.raises(DocumentActionError):
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    restored = PDFDocument.objects.get(pk=document.pk)
    assert validated_pdf_path(restored).read_bytes() == _PDF_BYTES
    assert search_documents(PDFSearchQuery(chips=("Freezetoken",))).total == 1
    assert _git(Path(document.repository.local_path), "status", "--porcelain") == ""
    assert pdf_path.exists() is (not excluded)
    assert PDFLocalPolicy.objects.filter(state=PDFLocalPolicyState.DELETED).count() == 0


@pytest.mark.parametrize("mode", [0o444, 0o755], ids=["read-only", "executable"])
def test_database_failure_restores_original_file_permissions_and_clean_git_state(
    registered_pdf, monkeypatch, mode
):
    document, pdf_path = registered_pdf
    checkout = Path(document.repository.local_path)
    pdf_path.chmod(mode)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "--allow-empty", "-m", "Synthetic PDF file permissions")
    original_delete = PDFDocument.delete

    def fail_after_delete(target, *args, **kwargs):
        original_delete(target, *args, **kwargs)
        backups = tuple(pdf_local_policy._private_root().glob(".pdf-action-*/*.pdf"))
        assert backups
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)
        raise OperationalError("synthetic post-removal failure")

    monkeypatch.setattr(PDFDocument, "delete", fail_after_delete)
    with pytest.raises(DocumentActionError):
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert pdf_path.read_bytes() == _PDF_BYTES
    assert stat.S_IMODE(pdf_path.stat().st_mode) == mode
    assert _git(checkout, "status", "--porcelain") == ""


@pytest.mark.django_db(transaction=True)
def test_database_commit_failure_restores_file_and_tombstone_state(registered_pdf, monkeypatch):
    document, pdf_path = registered_pdf
    original_commit = connection.commit
    attempts = 0

    def fail_first_commit():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("synthetic commit failure")
        return original_commit()

    monkeypatch.setattr(connection, "commit", fail_first_commit)
    with pytest.raises(DocumentActionError):
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()
    assert pdf_path.read_bytes() == _PDF_BYTES
    assert _git(Path(document.repository.local_path), "status", "--porcelain") == ""


def test_recovery_copy_cleanup_failure_does_not_report_full_deletion_success(
    registered_pdf, monkeypatch
):
    document, pdf_path = registered_pdf
    document_id = document.pk
    monkeypatch.setattr(
        pdf_local_policy._FileChanges,
        "discard",
        Mock(side_effect=OSError("synthetic cleanup failure")),
    )

    with pytest.raises(DocumentActionError) as failure:
        pdf_local_policy.delete_registered_pdf(document.pk, confirmed=True)

    assert failure.value.code == "pdf_policy_cleanup_failed"
    assert "not fully complete" in failure.value.summary
    assert not PDFDocument.objects.filter(pk=document_id).exists()
    assert not pdf_path.exists()
    assert PDFLocalPolicy.objects.get().state == PDFLocalPolicyState.DELETED
    recovery_copies = tuple(pdf_local_policy._private_root().glob(".pdf-action-*/*.pdf"))
    assert len(recovery_copies) == 1
    assert recovery_copies[0].read_bytes() == _PDF_BYTES
