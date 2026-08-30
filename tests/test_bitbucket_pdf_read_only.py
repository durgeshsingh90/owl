from __future__ import annotations

import hashlib
from unittest.mock import Mock

import pytest
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFLocalPolicy,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import PDFSearchQuery, PDFSearchScope

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("job_status", [None, "queued", "running"])
@pytest.mark.parametrize(
    ("method", "confirmed", "asynchronous"),
    [
        ("get", False, False),
        ("get", False, True),
        ("post", False, False),
        ("post", True, False),
        ("post", True, True),
    ],
)
def test_retired_pdf_delete_cannot_change_bytes_index_or_background_jobs(
    client, settings, tmp_path, monkeypatch, method, confirmed, asynchronous, job_status
):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = settings.MEDIA_ROOT / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = settings.MEDIA_ROOT / "bitbucket" / "tmp"
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    repository = BitbucketRepository.objects.create(
        display_name="Read-only repository",
        canonical_remote_key="example.invalid/adr/read-only",
        remote_url="https://example.invalid/adr/read-only.git",
        sync_state=RepositorySyncState.READY,
        pdf_count=1,
    )
    checkout = managed_repository_path(repository)
    (checkout / "docs").mkdir(parents=True)
    pdf_path = checkout / "docs" / "Architecture.pdf"
    original_bytes = b"%PDF-1.4\nSynthetic read-only fixture.\n%%EOF\n"
    pdf_path.write_bytes(original_bytes)
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path",))
    content = "Readkeeptext remains searchable."
    revision = PDFTextRevision.objects.create(
        content_sha256=hashlib.sha256(original_bytes).hexdigest(),
        extractor_version="synthetic-v1",
        source_byte_size=len(original_bytes),
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=len(content),
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text=content,
        character_count=len(content),
        extraction_state=PDFPageExtractionState.READY,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Architecture.pdf",
        file_size=len(original_bytes),
        git_blob_id="a" * 40,
        last_seen_commit="b" * 40,
        indexed_revision=revision,
        indexed_git_blob_id="a" * 40,
        indexed_source_commit="b" * 40,
        index_state=PDFIndexState.READY,
        open_count=5,
        page_count=1,
        extracted_character_count=len(content),
    )
    sync_job = RepositorySyncJob.objects.create(
        repository=repository, status=RepositorySyncJobStatus.SUCCEEDED
    )
    if job_status:
        PDFExtractionJob.objects.create(
            document=document,
            repository_sync_job=sync_job,
            target_git_blob_id="c" * 40,
            target_source_commit="d" * 40,
            target_relative_path=document.relative_path,
            target_file_size=len(original_bytes),
            target_extractor_version="synthetic-v1",
            status=PDFExtractionJobStatus(job_status),
        )
    models = (
        BitbucketRepository,
        PDFDocument,
        PDFTextRevision,
        PDFTextPage,
        PDFExtractionJob,
        RepositorySyncJob,
        PDFLocalPolicy,
    )
    before = {model: tuple(model.objects.order_by("pk").values()) for model in models}
    before_stat = pdf_path.stat()
    search = PDFSearchQuery(chips=("Readkeeptext",), scopes=(PDFSearchScope.CONTENT,))
    assert search_documents(search).total == 1
    delete = Mock(side_effect=AssertionError("Read-only PDF endpoint called deletion"))
    monkeypatch.setattr("bitbucket_search.services.pdf_local_policy.delete_registered_pdf", delete)

    response = getattr(client, method)(
        reverse("bitbucket_search:document_delete", args=(document.pk,)),
        {"confirmed": "yes" if confirmed else "", "return_to": "https://foreign.example.invalid/"},
        **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if asynchronous else {}),
    )

    assert response.status_code == 410
    assert "Location" not in response
    if asynchronous:
        assert response.json() == {
            "state": "gone",
            "code": "pdf_read_only",
            "detail": "PDFs are read-only. Individual file deletion is unavailable.",
        }
    else:
        assert (
            response.content.decode()
            == "PDFs are read-only. Individual file deletion is unavailable."
        )
    delete.assert_not_called()
    assert pdf_path.read_bytes() == original_bytes
    after_stat = pdf_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    assert {model: tuple(model.objects.order_by("pk").values()) for model in models} == before
    assert search_documents(search).total == 1
