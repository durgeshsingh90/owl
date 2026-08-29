from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentLifecycle,
    PDFDocumentTimelineBasis,
)
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.pdf_catalog import (
    _validated_relative_path,
    build_repository_pdf_catalog,
    publish_repository_pdf_catalog,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(shutil.which("git") is None, reason="Git is required"),
]


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/line\nbreak.pdf",
        "docs/carriage\rreturn.pdf",
        "docs/tab\tname.pdf",
        "docs/control\x1fname.pdf",
        "docs/delete\x7fname.pdf",
        "docs/next-line\u0085name.pdf",
        "docs/line-separator\u2028name.pdf",
        "docs/paragraph-separator\u2029name.pdf",
    ),
)
def test_catalog_rejects_control_characters_in_pdf_paths(relative_path):
    with pytest.raises(RepositorySyncError) as captured:
        _validated_relative_path(relative_path)

    assert captured.value.code == "invalid_pdf_path"


def _git(
    *arguments: object,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    completed = subprocess.run(
        ["git", *(str(argument) for argument in arguments)],
        cwd=cwd,
        env=process_environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(
    settings, tmp_path: Path, name: str = "documents"
) -> tuple[BitbucketRepository, Path]:
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository = BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
    )
    checkout = managed_repository_path(repository)
    checkout.parent.mkdir(parents=True)
    return repository, checkout


def _initialize_repository(checkout: Path) -> None:
    _git("init", "-b", "main", checkout)
    _git("config", "user.name", "Synthetic Committer", cwd=checkout)
    _git("config", "user.email", "committer@example.invalid", cwd=checkout)


def _commit(
    checkout: Path,
    message: str,
    *,
    observed_at: datetime | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
) -> str:
    arguments: list[object] = []
    if author_name:
        arguments.extend(("-c", f"user.name={author_name}"))
    if author_email:
        arguments.extend(("-c", f"user.email={author_email}"))
    arguments.extend(("commit", "-m", message))
    environment = None
    if observed_at is not None:
        iso_timestamp = observed_at.isoformat()
        environment = {
            "GIT_AUTHOR_DATE": iso_timestamp,
            "GIT_COMMITTER_DATE": iso_timestamp,
        }
    _git(*arguments, cwd=checkout, environment=environment)
    return _git("rev-parse", "HEAD", cwd=checkout)


def _build(repository: BitbucketRepository, commit_hash: str):
    return build_repository_pdf_catalog(
        repository,
        result_commit=commit_hash,
        progress_callback=lambda _phase, _progress, _message: None,
    )


def _publish(
    repository: BitbucketRepository,
    commit_hash: str,
    *,
    observed_at: datetime,
):
    catalog = _build(repository, commit_hash)
    publish_repository_pdf_catalog(
        repository,
        catalog,
        result_commit=commit_hash,
        observed_at=observed_at,
    )
    return catalog


def test_full_history_catalogs_root_and_nested_mixed_case_pdfs_with_mailmap_identity(
    settings,
    tmp_path,
):
    repository, checkout = _repository(settings, tmp_path)
    _initialize_repository(checkout)
    added_at = datetime(2024, 3, 4, 8, 15, tzinfo=UTC)
    (checkout / "ROOT.PDF").write_bytes(b"root pdf")
    (checkout / "docs").mkdir()
    (checkout / "docs" / "Guide.PdF").write_bytes(b"nested pdf")
    (checkout / "docs" / "Ignored.vsdx").write_bytes(b"not a pdf")
    _git("add", ".", cwd=checkout)
    addition_hash = _commit(
        checkout,
        "Add PDFs",
        observed_at=added_at,
        author_name="Legacy Author",
        author_email="legacy@example.invalid",
    )
    (checkout / ".mailmap").write_text(
        "Canonical Author <canonical@example.invalid> Legacy Author <legacy@example.invalid>\n",
        encoding="utf-8",
    )
    _git("add", ".mailmap", cwd=checkout)
    head = _commit(checkout, "Add mailmap", observed_at=added_at + timedelta(days=1))

    catalog = _build(repository, head)

    assert catalog.history_is_shallow is False
    assert [item.relative_path for item in catalog.documents] == [
        "docs/Guide.PdF",
        "ROOT.PDF",
    ]
    for document in catalog.documents:
        assert document.added_evidence == PDFDocumentAddedEvidence.CONFIRMED
        assert document.added_commit is not None
        assert document.added_commit.commit_hash == addition_hash
        assert document.added_commit.author_name == "Canonical Author"
        assert document.added_commit.authored_at == added_at
        assert document.last_commit == document.added_commit


def test_exact_rename_keeps_original_addition_lineage_and_records_rename_as_last_change(
    settings,
    tmp_path,
):
    repository, checkout = _repository(settings, tmp_path)
    _initialize_repository(checkout)
    original_added_at = datetime(2023, 6, 1, 10, 0, tzinfo=UTC)
    (checkout / "docs").mkdir()
    original = checkout / "docs" / "Original.PDF"
    original.write_bytes(b"unchanged rename content")
    _git("add", ".", cwd=checkout)
    addition_hash = _commit(checkout, "Add original", observed_at=original_added_at)
    (checkout / "archive").mkdir()
    _git("mv", original, checkout / "archive" / "Final.pdf", cwd=checkout)
    rename_hash = _commit(
        checkout,
        "Rename PDF",
        observed_at=original_added_at + timedelta(days=30),
    )

    catalog = _build(repository, rename_hash)

    assert len(catalog.documents) == 1
    document = catalog.documents[0]
    assert document.relative_path == "archive/Final.pdf"
    assert document.added_evidence == PDFDocumentAddedEvidence.CONFIRMED
    assert document.added_commit is not None
    assert document.added_commit.commit_hash == addition_hash
    assert document.added_commit.committed_at == original_added_at
    assert document.last_commit is not None
    assert document.last_commit.commit_hash == rename_hash


def test_shallow_boundary_never_claims_a_confirmed_addition(settings, tmp_path):
    source = tmp_path / "source"
    _initialize_repository(source)
    (source / "Legacy.PDF").write_bytes(b"legacy pdf")
    _git("add", ".", cwd=source)
    _commit(source, "Old PDF")
    (source / "README.md").write_text("new boundary commit", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _commit(source, "Latest non-PDF change")

    repository, checkout = _repository(settings, tmp_path, "shallow-documents")
    _git("clone", "--depth=1", "--no-local", source.as_uri(), checkout)
    head = _git("rev-parse", "HEAD", cwd=checkout)

    catalog = _build(repository, head)

    assert catalog.history_is_shallow is True
    assert len(catalog.documents) == 1
    document = catalog.documents[0]
    assert document.added_evidence == PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY
    assert document.added_commit is None


def test_publish_refresh_preserves_open_usage_and_marks_missing_pdfs_removed(
    settings,
    tmp_path,
):
    repository, checkout = _repository(settings, tmp_path)
    _initialize_repository(checkout)
    (checkout / "Keep.pdf").write_bytes(b"keep v1")
    (checkout / "Remove.pdf").write_bytes(b"remove me")
    _git("add", ".", cwd=checkout)
    first_commit = _commit(checkout, "Add current PDFs")
    first_observed_at = timezone.now() - timedelta(days=1)
    _publish(repository, first_commit, observed_at=first_observed_at)
    keep = PDFDocument.objects.get(repository=repository, relative_path="Keep.pdf")
    removed = PDFDocument.objects.get(repository=repository, relative_path="Remove.pdf")
    first_opened_at = first_observed_at + timedelta(hours=1)
    last_opened_at = first_opened_at + timedelta(hours=2)
    PDFDocument.objects.filter(pk=keep.pk).update(
        open_count=7,
        first_opened_at=first_opened_at,
        last_opened_at=last_opened_at,
    )

    (checkout / "Keep.pdf").write_bytes(b"keep v2")
    (checkout / "Remove.pdf").unlink()
    _git("add", "-A", cwd=checkout)
    refreshed_commit = _commit(checkout, "Update and remove PDFs")
    refreshed_at = timezone.now()
    _publish(repository, refreshed_commit, observed_at=refreshed_at)

    keep.refresh_from_db()
    removed.refresh_from_db()
    assert keep.open_count == 7
    assert keep.first_opened_at == first_opened_at
    assert keep.last_opened_at == last_opened_at
    assert keep.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert keep.last_seen_commit == refreshed_commit
    assert removed.lifecycle_state == PDFDocumentLifecycle.REMOVED
    assert removed.removed_at == refreshed_at


def test_second_publish_at_same_commit_preserves_confirmed_addition_evidence(
    settings,
    tmp_path,
):
    repository, checkout = _repository(settings, tmp_path)
    _initialize_repository(checkout)
    added_at = datetime(2022, 11, 5, 9, 30, tzinfo=UTC)
    (checkout / "Stable.pdf").write_bytes(b"stable")
    _git("add", ".", cwd=checkout)
    commit_hash = _commit(checkout, "Add stable PDF", observed_at=added_at)
    observed_at = timezone.now()
    _publish(repository, commit_hash, observed_at=observed_at)
    document = PDFDocument.objects.get(repository=repository)
    original_added_commit_id = document.added_commit_id
    original_timeline_at = document.timeline_at
    repository.metadata_indexed_commit = commit_hash
    repository.save(update_fields=("metadata_indexed_commit", "updated_at"))

    reused_catalog = _publish(
        repository,
        commit_hash,
        observed_at=observed_at + timedelta(minutes=5),
    )

    document.refresh_from_db()
    assert reused_catalog.documents[0].added_evidence == PDFDocumentAddedEvidence.NOT_FOUND
    assert document.added_evidence == PDFDocumentAddedEvidence.CONFIRMED
    assert document.added_commit_id == original_added_commit_id
    assert document.timeline_basis == PDFDocumentTimelineBasis.GIT_ADDED
    assert document.timeline_at == original_timeline_at == added_at
