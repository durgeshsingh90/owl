from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentLifecycle,
    PDFDocumentTimelineBasis,
    PDFIndexState,
)

pytestmark = pytest.mark.django_db


def _repository(name: str = "documents") -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
    )


def _commit(repository: BitbucketRepository, commit_hash: str = "a" * 40) -> GitCommit:
    committed_at = timezone.now() - timedelta(days=2)
    return GitCommit.objects.create(
        repository=repository,
        commit_hash=commit_hash,
        author_name="Synthetic Author",
        committer_name="Synthetic Committer",
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )


def test_repository_history_defaults_are_conservative():
    repository = _repository()

    assert repository.history_is_shallow is True
    assert repository.metadata_indexed_commit == ""


def test_git_commit_identity_is_unique_within_each_repository():
    repository = _repository()
    commit = _commit(repository)

    assert str(commit) == f"documents — {'a' * 12}"
    with pytest.raises(IntegrityError), transaction.atomic():
        _commit(repository)

    another_repository = _repository("other-documents")
    assert _commit(another_repository).commit_hash == commit.commit_hash


def test_pdf_document_defaults_to_truthful_owl_discovery_metadata():
    repository = _repository()
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Architecture.pdf",
        relative_path="docs/Architecture.pdf",
        file_size=4096,
        last_seen_commit="b" * 40,
    )

    assert document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert document.added_evidence == PDFDocumentAddedEvidence.NOT_FOUND
    assert document.timeline_basis == PDFDocumentTimelineBasis.OWL_DISCOVERED
    assert document.open_count == 0
    assert document.index_state == PDFIndexState.PENDING
    assert document.indexed_revision is None
    assert document.page_count == 0
    assert document.extracted_character_count == 0
    assert document.first_opened_at is None
    assert document.last_opened_at is None
    assert document.discovered_at is not None
    assert document.last_seen_at is not None
    assert document.timeline_at is not None
    assert repository.pdf_documents.get() == document


def test_pdf_document_path_is_unique_per_repository_and_commit_links_are_nullable():
    repository = _repository()
    commit = _commit(repository)
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Guide.pdf",
        relative_path="docs/Guide.pdf",
        added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
        added_commit=commit,
        last_commit=commit,
        timeline_basis=PDFDocumentTimelineBasis.GIT_ADDED,
        timeline_at=commit.committed_at,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        PDFDocument.objects.create(
            repository=repository,
            filename="Guide.pdf",
            relative_path="docs/Guide.pdf",
        )

    commit.delete()
    document.refresh_from_db()
    assert document.added_commit is None
    assert document.last_commit is None


def test_pdf_open_timestamps_must_remain_chronological():
    repository = _repository()
    observed_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        PDFDocument.objects.create(
            repository=repository,
            filename="Out-of-order.pdf",
            relative_path="docs/Out-of-order.pdf",
            first_opened_at=observed_at,
            last_opened_at=observed_at - timedelta(seconds=1),
        )


def test_pdf_timeline_indexes_are_declared_for_global_and_repository_queries():
    index_names = {index.name for index in PDFDocument._meta.indexes}

    assert index_names == {
        "bb_pdf_active_timeline_idx",
        "bb_pdf_index_state_idx",
        "bb_pdf_repo_timeline_idx",
    }
