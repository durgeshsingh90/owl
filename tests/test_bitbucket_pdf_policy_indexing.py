from unittest.mock import Mock

import pytest

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncState,
)
from bitbucket_search.services import pdf_indexing

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_roots(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"


def target(name="Architecture"):
    repository = BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{name}",
        remote_url=f"https://example.invalid/team/{name}.git",
        sync_state=RepositorySyncState.READY,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        relative_path=f"docs/{name}.pdf",
        filename=f"{name}.pdf",
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )
    return repository, document


def policy_for(document, state):
    return PDFLocalPolicy.objects.create(
        document=document,
        repository=document.repository,
        relative_path=document.relative_path,
        state=state,
    )


@pytest.mark.parametrize("state", PDFLocalPolicyState.values)
def test_policy_pdf_is_skipped_by_refresh_retry_and_startup_sweep(state):
    repository, document = target()
    policy_for(document, state)

    assert pdf_indexing.queue_repository_pdf_extractions(repository).queued_job_ids == ()
    assert (
        pdf_indexing.queue_repository_pdf_extractions(repository, retry_failed=True).queued_job_ids
        == ()
    )
    assert pdf_indexing.sweep_pdf_extraction_queue().queued_job_ids == ()
    assert pdf_indexing.claim_next_extraction_job() is None
    assert not PDFExtractionJob.objects.exists()
    assert pdf_indexing.extraction_status_snapshot().pending_documents == 0


@pytest.mark.parametrize("state", PDFLocalPolicyState.values)
def test_preexisting_queue_cannot_claim_policy_pdf_but_other_pdfs_continue(state):
    repository, document = target()
    queued = pdf_indexing.queue_repository_pdf_extractions(repository)
    policy_for(document, state)
    other_repository, other_document = target("Other")
    pdf_indexing.queue_repository_pdf_extractions(other_repository)

    claimed = pdf_indexing.claim_next_extraction_job()

    assert claimed.document_id == other_document.pk
    result = pdf_indexing.queue_repository_pdf_extractions(repository)
    assert result.cancelled_job_ids == queued.queued_job_ids
    assert PDFExtractionJob.objects.get(pk=queued.queued_job_ids[0]).status == "cancelled"


@pytest.mark.parametrize("state", PDFLocalPolicyState.values)
def test_claim_overtaken_by_policy_cancels_without_touching_saved_document(state):
    repository, document = target()
    pdf_indexing.queue_repository_pdf_extractions(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    policy_for(document, state)
    PDFDocument.objects.filter(pk=document.pk).update(index_state=PDFIndexState.READY)
    parser = Mock(side_effect=AssertionError("Excluded PDFs must not reach the parser"))

    completed = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=parser)

    assert completed.status == PDFExtractionJobStatus.CANCELLED
    assert completed.error_code == "pdf_refresh_excluded"
    parser.assert_not_called()
    document.refresh_from_db()
    assert document.index_state == PDFIndexState.READY
    assert document.extraction_error_code == ""


def test_final_publish_rechecks_policy():
    repository, document = target()
    pdf_indexing.queue_repository_pdf_extractions(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    policy_for(document, PDFLocalPolicyState.EXCLUDED)

    with pytest.raises(pdf_indexing.PDFExtractionExcluded):
        pdf_indexing._attach_revision(claimed.pk, content_sha256="c" * 64, staged=None)

    document.refresh_from_db()
    assert document.indexed_revision_id is None
