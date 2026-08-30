from __future__ import annotations

import hashlib
import logging
from unittest.mock import Mock

import pytest
from django.db import connection, transaction

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositorySyncState,
)
from bitbucket_search.services import pdf_indexing
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_lock import pdf_extraction_claim_lock

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("parser_succeeds", [True, False])
def test_other_repository_cache_removal_falls_back_to_parsing(
    tmp_path, settings, monkeypatch, caplog, parser_succeeds
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    source_bytes = b"%PDF synthetic shared architecture revision"
    content_hash = hashlib.sha256(source_bytes).hexdigest()
    text = "Recreated architecture content"
    old_revision = PDFTextRevision.objects.create(
        content_sha256=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
        source_byte_size=len(source_bytes),
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=len(text),
    )
    PDFTextPage.objects.create(
        revision=old_revision,
        page_number=1,
        extracted_text=text,
        character_count=len(text),
        extraction_state=PDFPageExtractionState.READY,
    )
    repositories = []
    for name in ("Removed", "Surviving"):
        repository = BitbucketRepository.objects.create(
            display_name=name,
            canonical_remote_key=f"example.invalid/{name.casefold()}",
            remote_url=f"https://example.invalid/{name.casefold()}.git",
            sync_state=RepositorySyncState.READY,
            last_synced_commit="b" * 40,
        )
        checkout = managed_repository_path(repository)
        (checkout / ".git").mkdir(parents=True)
        pdf_path = checkout / "Architecture.pdf"
        pdf_path.write_bytes(source_bytes)
        repository.local_path = str(checkout)
        repository.save(update_fields=("local_path", "updated_at"))
        document = PDFDocument.objects.create(
            repository=repository,
            filename=pdf_path.name,
            relative_path=pdf_path.name,
            file_size=len(source_bytes),
            git_blob_id="a" * 40,
            last_seen_commit="b" * 40,
            indexed_revision=old_revision if name == "Removed" else None,
        )
        repositories.append((repository, document, pdf_path))
    removed_repository, _removed_document, _removed_path = repositories[0]
    surviving_repository, document, pdf_path = repositories[1]
    pdf_indexing.queue_repository_pdf_extractions(surviving_repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    assert claimed is not None

    hash_pdf = pdf_indexing._hash_pdf
    hash_calls = 0
    old_revision_id = old_revision.pk

    def prune_after_cache_lookup(path, heartbeat):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 2:
            # Reproduce removal after the cache lookup but before publication;
            # deletion uses the same serialization gate as revision attachment.
            with pdf_extraction_claim_lock(), transaction.atomic():
                removed_repository.delete()
                assert not old_revision.documents.exists()
                old_revision.delete()
        return hash_pdf(path, heartbeat)

    monkeypatch.setattr(pdf_indexing, "_hash_pdf", prune_after_cache_lookup)
    stage = pdf_indexing.StagedPDFExtraction(
        state=PDFTextRevisionState.READY if parser_succeeds else "corrupt",
        pages=(
            pdf_indexing.StagedPDFPage(
                page_number=1,
                text=text,
                character_count=len(text),
                state=PDFPageExtractionState.READY,
            ),
        )
        if parser_succeeds
        else (),
        page_count=1 if parser_succeeds else 0,
        extracted_character_count=len(text) if parser_succeeds else 0,
        source_size_bytes=len(source_bytes),
        content_sha256_before=content_hash,
        content_sha256_after=content_hash,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )
    parser = Mock(return_value=stage)
    with caplog.at_level(logging.DEBUG, logger="owl.bitbucket.indexing"):
        result = pdf_indexing.execute_claimed_extraction_job(claimed.pk, extraction_runner=parser)

    document.refresh_from_db()
    assert hash_calls == 2
    parser.assert_called_once()
    assert parser.call_args.args[0] == pdf_path
    assert "pdf_text_revision_cache_miss" in caplog.text
    assert "pdf_extraction_unexpected_error" not in caplog.text
    assert not PDFTextRevision.objects.filter(pk=old_revision_id).exists()
    assert BitbucketRepository.objects.filter(pk=surviving_repository.pk).exists()
    if parser_succeeds:
        assert result.status == PDFExtractionJobStatus.SUCCEEDED
        assert result.error_code == ""
        assert document.index_state == PDFIndexState.READY
        assert document.indexed_revision_id != old_revision_id
        assert document.indexed_revision.pages.get().extracted_text == text
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rowid FROM bitbucket_search_pdf_page_fts "
                "WHERE bitbucket_search_pdf_page_fts MATCH %s",
                ['"Recreated architecture"'],
            )
            assert cursor.fetchall() == [(document.indexed_revision.pages.get().pk,)]
    else:
        assert result.status == PDFExtractionJobStatus.FAILED
        assert result.error_code == "corrupt_pdf"
        assert document.index_state == PDFIndexState.FAILED
        assert document.indexed_revision_id is None
