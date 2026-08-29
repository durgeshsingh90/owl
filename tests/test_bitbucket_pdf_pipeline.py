from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFTextPage,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_indexing import (
    claim_next_extraction_job,
    execute_claimed_extraction_job,
    queue_repository_pdf_extractions,
)
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import PDFSearchQuery

pytestmark = pytest.mark.django_db


def _write_text_pdf(path: Path, *page_texts: str) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for page_text in page_texts:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 150 Td ({page_text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as target:
        writer.write(target)


def test_background_pipeline_extracts_publishes_and_searches_without_pdf_rescan(
    tmp_path,
    settings,
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    settings.PDF_EXTRACTION_TIMEOUT_SECONDS = 30
    settings.PDF_MAX_FILE_BYTES = 10 * 1024 * 1024
    settings.PDF_MAX_PAGES = 100
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 1_000_000
    settings.PDF_MAX_PROCESS_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
    repository = BitbucketRepository.objects.create(
        display_name="Network Architecture",
        canonical_remote_key="example.invalid/team/network-architecture",
        remote_url="ssh://git@example.invalid/team/network-architecture.git",
        sync_state=RepositorySyncState.READY,
        last_synced_commit="b" * 40,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs").mkdir()
    pdf_path = checkout / "docs" / "Edge Design.pdf"
    _write_text_pdf(pdf_path, "Private Link overview", "DDoS network controls")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Edge Design.pdf",
        file_size=pdf_path.stat().st_size,
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
    )

    queued = queue_repository_pdf_extractions(repository)
    claimed = claim_next_extraction_job()
    completed = execute_claimed_extraction_job(claimed.pk)

    document.refresh_from_db()
    assert queued.queued_job_ids == (completed.pk,)
    assert completed.status == PDFExtractionJobStatus.SUCCEEDED
    assert document.index_state == PDFIndexState.READY
    assert document.page_count == 2
    assert tuple(
        PDFTextPage.objects.filter(revision=document.indexed_revision).values_list(
            "page_number", "extracted_text"
        )
    ) == (
        (1, "Private Link overview"),
        (2, "DDoS network controls"),
    )

    result = search_documents(PDFSearchQuery(chips=("Private Link", "DDoS")))

    assert [hit.document for hit in result.results] == [document]
    assert result.results[0].best_page_number == 1
    assert [item.page_numbers for item in result.results[0].explanations] == [(1,), (2,)]
