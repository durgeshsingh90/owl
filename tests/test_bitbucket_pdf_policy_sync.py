from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFIndexState,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import (
    RepositorySyncError,
    apply_document_checkout_policy,
    managed_repository_path,
    require_clean_document_checkout,
)
from bitbucket_search.services.pdf_catalog import (
    build_repository_pdf_catalog,
    publish_repository_pdf_catalog,
)
from bitbucket_search.services.pdf_local_policy import (
    delete_registered_pdf,
    exclude_registered_pdf,
    frozen_pdf_path,
    policy_snapshot_path,
    resume_registered_pdf,
)
from bitbucket_search.services.repository_sync import (
    claim_next_job,
    execute_claimed_job,
    queue_repository_refresh,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(shutil.which("git") is None, reason="Git is required"),
]


def _git(*arguments: object, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *(str(argument) for argument in arguments)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _commit_and_push(source: Path) -> None:
    _git("add", "-A", cwd=source)
    _git("commit", "-m", "Synthetic document update", cwd=source)
    _git("push", "origin", "main", cwd=source)


def _sync(repository: BitbucketRepository):
    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    result = execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    return result


def _repository(tmp_path: Path, settings, *, files=None, clone=True):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "staging"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git("init", "--bare", remote)
    _git("init", "-b", "main", source)
    _git("config", "user.name", "Synthetic OWL", cwd=source)
    _git("config", "user.email", "owl@example.invalid", cwd=source)
    for relative, content in (
        files
        or {
            "docs/Frozen.pdf": b"original frozen PDF",
            "docs/Keep.pdf": b"keep PDF",
            "diagrams/Keep.vsdx": b"keep diagram",
            "README.md": b"unrelated source file",
        }
    ).items():
        candidate = source / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(content)
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Synthetic documents", cwd=source)
    _git("remote", "add", "origin", remote.as_uri(), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    repository = BitbucketRepository.objects.create(
        display_name="policy-documents",
        canonical_remote_key="synthetic.invalid/team/policy-documents",
        remote_url=remote.as_uri(),
    )
    repository.local_path = str(managed_repository_path(repository))
    repository.save(update_fields={"local_path"})
    if clone:
        assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED
    return repository, source, Path(repository.local_path)


def _exclude(repository, document):
    require_clean_document_checkout(repository)
    policy = PDFLocalPolicy.objects.create(
        repository=repository,
        document=document,
        relative_path=document.relative_path,
        state=PDFLocalPolicyState.EXCLUDED,
    )
    apply_document_checkout_policy(repository)
    return policy


def test_excluded_pdf_keeps_frozen_metadata_and_text_while_other_pdfs_refresh(tmp_path, settings):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    revision = PDFTextRevision.objects.create(
        content_sha256="a" * 64,
        extractor_version="synthetic-v1",
        state=PDFTextRevisionState.READY,
        page_count=1,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text="frozen searchable text",
        extraction_state=PDFPageExtractionState.READY,
    )
    document.indexed_revision = revision
    document.indexed_git_blob_id = document.git_blob_id
    document.index_state = PDFIndexState.READY
    document.open_count = 9
    document.save()
    frozen_state = (
        document.git_blob_id,
        document.file_size,
        document.last_seen_commit,
        document.indexed_revision_id,
    )
    extraction_job_count = document.extraction_jobs.count()
    _exclude(repository, document)
    assert not (checkout / document.relative_path).exists()
    assert _git("status", "--porcelain=v1", cwd=checkout) == ""
    (source / "docs/Frozen.pdf").write_bytes(b"changed remote content must not replace frozen text")
    (source / "docs/New.pdf").write_bytes(b"new PDF")
    _commit_and_push(source)

    completed = _sync(repository)

    document.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert not (checkout / document.relative_path).exists()
    assert document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert (
        document.git_blob_id,
        document.file_size,
        document.last_seen_commit,
        document.indexed_revision_id,
    ) == frozen_state
    assert document.indexed_revision.pages.get().extracted_text == "frozen searchable text"
    assert document.open_count == 9
    assert document.extraction_jobs.count() == extraction_job_count
    assert repository.pdf_count == 3
    assert repository.document_bytes == (
        frozen_state[1] + len(b"keep PDF") + len(b"new PDF") + len(b"keep diagram")
    )
    assert (checkout / "docs/New.pdf").read_bytes() == b"new PDF"
    assert _git("status", "--porcelain=v1", cwd=checkout) == ""


def test_deleted_pdf_never_returns_to_checkout_or_database_on_refresh(tmp_path, settings):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    PDFLocalPolicy.objects.create(
        repository=repository,
        relative_path=document.relative_path,
        state=PDFLocalPolicyState.DELETED,
    )
    document.delete()
    apply_document_checkout_policy(repository)
    (source / "docs/Frozen.pdf").write_bytes(b"remote PDF still exists and has changed")
    (source / "docs/New.pdf").write_bytes(b"new PDF")
    _commit_and_push(source)

    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED
    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED

    assert not (checkout / "docs/Frozen.pdf").exists()
    assert not repository.pdf_documents.filter(relative_path="docs/Frozen.pdf").exists()
    assert repository.pdf_count == 2
    assert (checkout / "docs/Keep.pdf").read_bytes() == b"keep PDF"
    assert _git("status", "--porcelain=v1", cwd=checkout) == ""


def test_deleting_an_excluded_pdf_removes_snapshot_and_database_but_keeps_git_clean(
    tmp_path, settings
):
    repository, _source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    document_id = document.pk
    exclude_registered_pdf(document_id)
    policy = PDFLocalPolicy.objects.get(document_id=document_id)
    snapshot = policy_snapshot_path(policy, require_exists=True)
    assert snapshot.read_bytes() == b"original frozen PDF"
    assert not (checkout / document.relative_path).exists()

    delete_registered_pdf(document_id, confirmed=True)

    policy.refresh_from_db()
    assert policy.state == PDFLocalPolicyState.DELETED
    assert policy.document_id is None
    assert not snapshot.exists()
    assert not (checkout / document.relative_path).exists()
    assert not PDFDocument.objects.filter(pk=document_id).exists()
    assert (checkout / "docs/Keep.pdf").read_bytes() == b"keep PDF"
    assert _git("status", "--porcelain=v1", cwd=checkout) == ""
    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED
    assert not repository.pdf_documents.filter(relative_path=document.relative_path).exists()
    assert not (checkout / document.relative_path).exists()


def test_failed_resume_catalogue_preserves_frozen_snapshot_and_published_document(
    tmp_path, settings, monkeypatch, django_capture_on_commit_callbacks
):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    original_blob = document.git_blob_id
    exclude_registered_pdf(document.pk)
    policy = PDFLocalPolicy.objects.get(document=document)
    snapshot = policy_snapshot_path(policy, require_exists=True)
    (source / document.relative_path).write_bytes(b"new remote PDF for resume")
    _commit_and_push(source)
    resume_registered_pdf(document.pk)

    def fail_catalogue(*args, **kwargs):
        raise RepositorySyncError("pdf_catalog_failed", "Synthetic catalogue failure.")

    with monkeypatch.context() as patch:
        patch.setattr(
            "bitbucket_search.services.repository_sync.build_repository_pdf_catalog", fail_catalogue
        )
        completed = _sync(repository)

    document.refresh_from_db()
    policy.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.FAILED
    assert policy.state == PDFLocalPolicyState.RESUMING
    assert document.git_blob_id == original_blob
    assert document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert snapshot.read_bytes() == b"original frozen PDF"
    assert frozen_pdf_path(document) == snapshot
    assert (checkout / document.relative_path).read_bytes() == b"new remote PDF for resume"

    with django_capture_on_commit_callbacks(execute=True):
        recovered = _sync(repository)

    document.refresh_from_db()
    assert recovered.status == RepositorySyncJobStatus.SUCCEEDED
    assert document.git_blob_id != original_blob
    assert not PDFLocalPolicy.objects.filter(pk=policy.pk).exists()
    assert not snapshot.exists()


@pytest.mark.parametrize("remote_removed", (False, True))
def test_resuming_pdf_rejoins_latest_catalogue_or_is_removed_if_gone_remotely(
    tmp_path, settings, remote_removed
):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    old_blob = document.git_blob_id
    policy = _exclude(repository, document)
    if remote_removed:
        (source / document.relative_path).unlink()
    else:
        (source / document.relative_path).write_bytes(b"resumed latest PDF")
    _commit_and_push(source)
    policy.state = PDFLocalPolicyState.RESUMING
    policy.save(update_fields={"state"})

    completed = _sync(repository)

    document.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert not PDFLocalPolicy.objects.filter(pk=policy.pk).exists()
    if remote_removed:
        assert document.lifecycle_state == PDFDocumentLifecycle.REMOVED
        assert document.removed_at is not None
        assert repository.pdf_count == 1
    else:
        assert document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
        assert document.git_blob_id != old_blob
        assert (checkout / document.relative_path).read_bytes() == b"resumed latest PDF"
        assert repository.pdf_count == 2


def test_clone_honors_preexisting_deleted_tombstones(tmp_path, settings):
    repository, _source, checkout = _repository(tmp_path, settings, clone=False)
    PDFLocalPolicy.objects.create(
        repository=repository,
        relative_path="docs/Frozen.pdf",
        state=PDFLocalPolicyState.DELETED,
    )

    completed = _sync(repository)

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert not (checkout / "docs/Frozen.pdf").exists()
    assert list(repository.pdf_documents.values_list("relative_path", flat=True)) == [
        "docs/Keep.pdf"
    ]


def test_resuming_at_already_synced_commit_refreshes_git_attribution(tmp_path, settings):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    initial_last_commit_id = document.last_commit_id
    policy = _exclude(repository, document)
    (source / document.relative_path).write_bytes(b"changed while frozen")
    _commit_and_push(source)
    updated_commit = _git("rev-parse", "HEAD", cwd=source)
    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.metadata_indexed_commit == updated_commit
    document.refresh_from_db()
    assert document.last_commit_id == initial_last_commit_id
    policy.state = PDFLocalPolicyState.RESUMING
    policy.save(update_fields={"state"})

    completed = _sync(repository)

    document.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert (checkout / document.relative_path).read_bytes() == b"changed while frozen"
    assert document.last_commit.commit_hash == updated_commit
    assert not PDFLocalPolicy.objects.filter(pk=policy.pk).exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create wildcard filenames")
def test_sparse_exclusion_escapes_wildcards_and_keeps_neighboring_files(tmp_path, settings):
    excluded = "docs/Review [1]*?#!.pdf"
    neighbor = "docs/Review 1anythingX#!.pdf"
    repository, _source, checkout = _repository(
        tmp_path,
        settings,
        files={excluded: b"excluded PDF", neighbor: b"neighbor PDF"},
    )
    document = repository.pdf_documents.get(relative_path=excluded)
    _exclude(repository, document)

    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED

    assert not (checkout / excluded).exists()
    assert (checkout / neighbor).read_bytes() == b"neighbor PDF"
    assert repository.pdf_count == 2


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create wildcard filenames")
def test_deleting_literal_wildcard_filename_does_not_suppress_neighbor(tmp_path, settings):
    deleted = "docs/Review [1]*?#!.pdf"
    neighbor = "docs/Review 1anythingX#!.pdf"
    repository, _source, checkout = _repository(
        tmp_path,
        settings,
        files={deleted: b"deleted PDF", neighbor: b"neighbor PDF"},
    )
    document = repository.pdf_documents.get(relative_path=deleted)

    delete_registered_pdf(document.pk, confirmed=True)

    assert _sync(repository).status == RepositorySyncJobStatus.SUCCEEDED
    assert not (checkout / deleted).exists()
    assert (checkout / neighbor).read_bytes() == b"neighbor PDF"
    assert repository.pdf_documents.get().relative_path == neighbor
    assert repository.pdf_count == 1


def test_deleted_pdf_path_replaced_by_directory_does_not_hide_unrelated_children(
    tmp_path, settings
):
    repository, source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    PDFLocalPolicy.objects.create(
        repository=repository,
        relative_path=document.relative_path,
        state=PDFLocalPolicyState.DELETED,
    )
    document.delete()
    apply_document_checkout_policy(repository)
    replacement_directory = source / "docs/Frozen.pdf"
    replacement_directory.unlink()
    replacement_directory.mkdir()
    (replacement_directory / "Nested.pdf").write_bytes(b"unrelated nested PDF")
    (replacement_directory / "Unrelated.txt").write_bytes(b"keep unrelated nested file")
    _commit_and_push(source)

    completed = _sync(repository)

    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert (checkout / "docs/Frozen.pdf/Nested.pdf").read_bytes() == b"unrelated nested PDF"
    assert (
        checkout / "docs/Frozen.pdf/Unrelated.txt"
    ).read_bytes() == b"keep unrelated nested file"
    assert repository.pdf_documents.filter(relative_path="docs/Frozen.pdf/Nested.pdf").exists()
    assert not repository.pdf_documents.filter(relative_path="docs/Frozen.pdf").exists()


def test_policy_refresh_still_preserves_dirty_tracked_pdfs(tmp_path, settings):
    repository, _source, checkout = _repository(tmp_path, settings)
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    _exclude(repository, document)
    (checkout / "docs/Keep.pdf").write_bytes(b"preserve local PDF edits")

    completed = _sync(repository)

    assert completed.status == RepositorySyncJobStatus.FAILED
    assert repository.sync_state == RepositorySyncState.BLOCKED_DIRTY
    assert completed.error_code == "dirty_working_tree"
    assert (checkout / "docs/Keep.pdf").read_bytes() == b"preserve local PDF edits"
    assert not (checkout / "docs/Frozen.pdf").exists()


@pytest.mark.parametrize(
    "unsafe_path", ("../outside.pdf", "/outside.pdf", "C:bad.pdf", "docs/a\nb.pdf", "a\\b.pdf")
)
def test_invalid_policy_path_cannot_change_sparse_checkout(tmp_path, settings, unsafe_path):
    repository, _source, checkout = _repository(tmp_path, settings)
    original_patterns = _git("sparse-checkout", "list", cwd=checkout)
    PDFLocalPolicy.objects.create(
        repository=repository,
        relative_path=unsafe_path,
        state=PDFLocalPolicyState.DELETED,
    )

    with pytest.raises(RepositorySyncError, match="unsafe path"):
        apply_document_checkout_policy(repository)

    assert _git("sparse-checkout", "list", cwd=checkout) == original_patterns
    assert (checkout / "docs/Frozen.pdf").read_bytes() == b"original frozen PDF"


def test_policy_change_after_catalogue_build_cannot_remove_existing_inventory(tmp_path, settings):
    repository, _source, _checkout = _repository(tmp_path, settings)
    catalog = build_repository_pdf_catalog(
        repository,
        result_commit=repository.last_synced_commit,
        progress_callback=lambda *_args: None,
    )
    document = repository.pdf_documents.get(relative_path="docs/Frozen.pdf")
    PDFLocalPolicy.objects.create(
        repository=repository,
        document=document,
        relative_path=document.relative_path,
        state=PDFLocalPolicyState.EXCLUDED,
    )

    with pytest.raises(RepositorySyncError, match="policy changed"):
        publish_repository_pdf_catalog(
            repository,
            catalog,
            result_commit=repository.last_synced_commit,
            observed_at=repository.last_sync_successful_at,
        )

    assert (
        PDFDocument.objects.filter(
            repository=repository, lifecycle_state=PDFDocumentLifecycle.ACTIVE
        ).count()
        == 2
    )
