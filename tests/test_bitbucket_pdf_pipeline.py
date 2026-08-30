from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFTextPage,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services import git_sync, pdf_indexing, repository_sync
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_indexing import (
    PDFIndexingError,
    claim_next_extraction_job,
    execute_claimed_extraction_job,
    queue_repository_pdf_extractions,
)
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import PDFSearchQuery, PDFSearchScope

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


def _pipeline_git(*arguments: object, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *map(str, arguments)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result.stdout.strip()


def _commit_source(source: Path) -> None:
    _pipeline_git("add", "-A", cwd=source)
    _pipeline_git("commit", "-m", "Synthetic PDF pipeline revision", cwd=source)


@pytest.fixture
def local_git_pdf_pipeline(tmp_path, settings, monkeypatch):
    """Use only pytest's database, private media and a local file:// Git source."""

    if shutil.which("git") is None:
        pytest.skip("Git is required")
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "temporary"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    settings.PDF_EXTRACTION_TIMEOUT_SECONDS = 30
    settings.PDF_MAX_FILE_BYTES = 10 * 1024 * 1024
    settings.PDF_MAX_PAGES = 100
    settings.PDF_MAX_PAGE_TEXT_CHARS = 100_000
    settings.PDF_MAX_TOTAL_TEXT_CHARS = 1_000_000
    settings.PDF_MAX_PROCESS_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
    # Avoid inherited credentials, URL rewrites, signing and Git hooks. Both
    # fixture Git and production Git subprocesses inherit this private scope.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    forbidden_launch = Mock(side_effect=AssertionError("Detached workers are forbidden in tests"))
    monkeypatch.setattr(repository_sync, "launch_sync_worker", forbidden_launch)
    monkeypatch.setattr(pdf_indexing, "launch_index_worker", forbidden_launch)

    source = tmp_path / "source"
    _pipeline_git("init", "-b", "main", source)
    _pipeline_git("config", "user.name", "Synthetic OWL", cwd=source)
    _pipeline_git("config", "user.email", "owl@example.invalid", cwd=source)
    (source / "docs").mkdir()
    _write_text_pdf(source / "docs" / "Guide.pdf", "quartzorigin architecture", "network controls")
    _write_text_pdf(source / "docs" / "Removed.pdf", "amberremoved retirement")
    _write_text_pdf(source / "docs" / "Stable.pdf", "cobaltsame reference")
    (source / "Network.vsdx").write_bytes(b"Synthetic diagram; not parsed as a PDF")
    (source / "README.md").write_text("Unrelated source content", encoding="utf-8")
    _commit_source(source)
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic Pipeline",
        canonical_remote_key="example.invalid/team/pdf-pipeline",
        remote_url=source.as_uri(),
    )
    checkout = managed_repository_path(repository)
    assert checkout.is_relative_to(tmp_path)
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    yield repository, source, checkout
    forbidden_launch.assert_not_called()


def _sync_pipeline(repository, *, operation):
    queued = repository_sync.queue_repository_refresh(repository.pk)
    assert queued.job.operation == operation
    # No extraction may claim work while Git/catalogue synchronization is queued.
    assert claim_next_extraction_job() is None
    claimed = repository_sync.claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    completed = repository_sync.execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    return completed


def _extract_pipeline_jobs() -> tuple[int, ...]:
    completed_ids = []
    while claimed := claim_next_extraction_job():
        assert len(completed_ids) < 20, "The bounded synthetic queue must drain"
        completed = execute_claimed_extraction_job(claimed.pk)
        assert completed.status == PDFExtractionJobStatus.SUCCEEDED, completed.error_code
        completed_ids.append(completed.document_id)
    return tuple(completed_ids)


def _content_hits(*phrases: str) -> set[int]:
    results = search_documents(PDFSearchQuery(chips=phrases, scopes=(PDFSearchScope.CONTENT,)))
    return {hit.document.pk for hit in results.results}


def test_real_git_clone_refresh_and_isolated_pdf_extraction_update_search_incrementally(
    local_git_pdf_pipeline, monkeypatch
):
    repository, source, checkout = local_git_pdf_pipeline
    clone = Mock(wraps=git_sync._clone)
    monkeypatch.setattr(git_sync, "_clone", clone)

    initial = _sync_pipeline(repository, operation=RepositorySyncOperation.CLONE)

    assert initial.status == RepositorySyncJobStatus.SUCCEEDED, initial.error_code
    assert repository.pdf_count == 3 and repository.vsdx_count == 1
    assert PDFTextPage.objects.count() == 0
    assert PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.QUEUED).count() == 3
    assert not (checkout / "README.md").exists()
    documents = {document.filename: document for document in repository.pdf_documents.all()}
    guide = documents["Guide.pdf"]
    stable = documents["Stable.pdf"]
    removed = documents["Removed.pdf"]
    assert set(_extract_pipeline_jobs()) == {document.pk for document in documents.values()}
    assert _content_hits("quartzorigin", "network controls") == {guide.pk}
    assert _content_hits("amberremoved") == {removed.pk}
    assert _content_hits("cobaltsame") == {stable.pk}
    initial_revision_ids = dict(repository.pdf_documents.values_list("pk", "indexed_revision_id"))
    marker = checkout / ".git" / "owl-pipeline-checkout-marker"
    marker.write_text("Keep the existing checkout", encoding="utf-8")
    original_job_count = PDFExtractionJob.objects.count()
    original_page_count = PDFTextPage.objects.count()

    unchanged = _sync_pipeline(repository, operation=RepositorySyncOperation.REFRESH)

    assert unchanged.status == RepositorySyncJobStatus.SUCCEEDED, unchanged.error_code
    assert _extract_pipeline_jobs() == ()
    assert PDFExtractionJob.objects.count() == original_job_count
    assert PDFTextPage.objects.count() == original_page_count
    assert dict(repository.pdf_documents.values_list("pk", "indexed_revision_id")) == (
        initial_revision_ids
    )

    _write_text_pdf(source / "docs" / "Guide.pdf", "quartzupdated architecture", "network controls")
    _write_text_pdf(source / "docs" / "New.pdf", "cedaradded diagram notes")
    (source / "docs" / "Removed.pdf").unlink()
    _commit_source(source)
    changed = _sync_pipeline(repository, operation=RepositorySyncOperation.REFRESH)

    assert changed.status == RepositorySyncJobStatus.SUCCEEDED, changed.error_code
    assert repository.local_path == str(checkout)
    assert repository.last_synced_commit == _pipeline_git("rev-parse", "HEAD", cwd=source)
    assert repository.pdf_count == 3
    assert marker.read_text(encoding="utf-8") == "Keep the existing checkout"
    assert clone.call_count == 1
    assert not (checkout / "docs" / "Removed.pdf").exists()
    assert (checkout / "docs" / "New.pdf").is_file()
    added = repository.pdf_documents.get(filename="New.pdf")
    removed.refresh_from_db()
    assert removed.lifecycle_state == PDFDocumentLifecycle.REMOVED
    assert _content_hits("amberremoved") == set()
    assert set(
        PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.QUEUED).values_list(
            "document_id", flat=True
        )
    ) == {guide.pk, added.pk}
    assert set(_extract_pipeline_jobs()) == {guide.pk, added.pk}
    guide.refresh_from_db()
    stable.refresh_from_db()
    assert guide.indexed_revision_id != initial_revision_ids[guide.pk]
    assert stable.indexed_revision_id == initial_revision_ids[stable.pk]
    assert stable.extraction_jobs.count() == 1
    assert _content_hits("quartzorigin") == set()
    assert _content_hits("quartzupdated", "network controls") == {guide.pk}
    assert _content_hits("cedaradded") == {added.pk}
    assert _content_hits("cobaltsame") == {stable.pk}


@pytest.mark.parametrize("failure_stage", ("inventory", "catalogue"))
def test_failed_sync_stage_cannot_publish_or_queue_new_pdfs(
    local_git_pdf_pipeline, monkeypatch, failure_stage
):
    repository, source, checkout = local_git_pdf_pipeline
    assert (
        _sync_pipeline(repository, operation=RepositorySyncOperation.CLONE).status
        == RepositorySyncJobStatus.SUCCEEDED
    )
    _extract_pipeline_jobs()
    catalogue_before = set(repository.pdf_documents.values_list("pk", "relative_path"))
    jobs_before = PDFExtractionJob.objects.count()
    successful_commit = repository.last_synced_commit
    _write_text_pdf(source / "docs" / "Unpublished.pdf", "neverpublished pending material")
    _commit_source(source)
    failure = git_sync.RepositorySyncError("synthetic_inventory_failure", "Synthetic stage failure")
    if failure_stage == "inventory":
        monkeypatch.setattr(git_sync, "discover_documents", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(
            repository_sync, "build_repository_pdf_catalog", Mock(side_effect=failure)
        )

    completed = _sync_pipeline(repository, operation=RepositorySyncOperation.REFRESH)

    assert completed.status == RepositorySyncJobStatus.FAILED
    assert completed.error_code == "synthetic_inventory_failure"
    # The actual Git pull completed, but the following failed stage must not
    # publish these new bytes or queue their extraction against an old catalogue.
    assert (checkout / "docs" / "Unpublished.pdf").is_file()
    assert repository.sync_state == RepositorySyncState.FAILED
    assert repository.last_synced_commit == successful_commit
    assert set(repository.pdf_documents.values_list("pk", "relative_path")) == catalogue_before
    assert PDFExtractionJob.objects.count() == jobs_before
    assert claim_next_extraction_job() is None
    assert _content_hits("neverpublished") == set()
    assert _content_hits("quartzorigin")


def test_manual_refresh_retries_failed_unchanged_pdf_then_real_extractor_recovers(
    local_git_pdf_pipeline,
):
    repository, _source, _checkout = local_git_pdf_pipeline
    assert (
        _sync_pipeline(repository, operation=RepositorySyncOperation.CLONE).status
        == RepositorySyncJobStatus.SUCCEEDED
    )
    claimed = claim_next_extraction_job()
    assert claimed is not None
    failed = execute_claimed_extraction_job(
        claimed.pk,
        extraction_runner=Mock(
            side_effect=PDFIndexingError(
                "extractor_unavailable", "Synthetic parser startup failure"
            )
        ),
    )
    assert failed.status == PDFExtractionJobStatus.FAILED
    failed_document = PDFDocument.objects.get(pk=failed.document_id)
    original_blob = failed_document.git_blob_id
    _extract_pipeline_jobs()
    jobs_before = PDFExtractionJob.objects.count()

    refreshed = _sync_pipeline(repository, operation=RepositorySyncOperation.REFRESH)

    assert refreshed.status == RepositorySyncJobStatus.SUCCEEDED, refreshed.error_code
    failed_document.refresh_from_db()
    assert failed_document.git_blob_id == original_blob
    assert failed_document.index_state == PDFIndexState.PENDING
    assert PDFExtractionJob.objects.count() == jobs_before + 1
    assert _extract_pipeline_jobs() == (failed_document.pk,)
    failed_document.refresh_from_db()
    assert failed_document.index_state == PDFIndexState.READY
    assert failed_document.extraction_error_code == ""
    assert failed_document.extraction_jobs.count() == 2
    assert _content_hits("quartzorigin")
    assert _content_hits("amberremoved")
    assert _content_hits("cobaltsame")
