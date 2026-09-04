"""Crawl Bitbucket PDFs and persist their searchable database catalogue."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from bitbucket.models import (
    Contributor,
    Document,
    DocumentIndexState,
    DocumentKind,
    Repository,
)
from bitbucket.services.api import (
    AddedMetadata,
    BitbucketAPIClient,
    BitbucketAPIError,
    CommitMetadata,
)
from bitbucket.services.pdf_extraction import ExtractedPDF, PDFExtractionError, extract_pdf


@dataclass(frozen=True, slots=True)
class CatalogStats:
    found_pdf_count: int
    indexed_pdf_count: int
    failed_pdf_count: int
    vsdx_count: int
    downloaded_pdf_count: int
    unchanged_pdf_count: int


@dataclass(frozen=True, slots=True)
class _CrawlResult:
    path: str
    latest: CommitMetadata | None = None
    added: AddedMetadata | None = None
    extracted: ExtractedPDF | None = None
    unchanged: bool = False
    error: str = ""


def _safe_failure(path: str, error: Exception) -> _CrawlResult:
    if isinstance(error, (BitbucketAPIError, PDFExtractionError)):
        message = str(error)
    else:
        message = "The PDF could not be indexed."
    return _CrawlResult(path=path, error=message[:1000])


def _crawl_pdf(
    path: str,
    existing: Document | None,
    client: BitbucketAPIClient,
) -> _CrawlResult:
    try:
        latest = client.latest_metadata(path)
        unchanged = bool(
            existing is not None
            and existing.index_state == DocumentIndexState.INDEXED
            and latest is not None
            and latest.commit_id
            and existing.latest_commit_id == latest.commit_id
        )
        added = (
            client.added_metadata(path)
            if existing is None or not existing.commit_id or existing.added_at is None
            else None
        )
        if unchanged:
            return _CrawlResult(path=path, latest=latest, added=added, unchanged=True)
        content = client.download_pdf(path)
        return _CrawlResult(
            path=path,
            latest=latest,
            added=added,
            extracted=extract_pdf(content),
        )
    except BitbucketAPIError as exc:
        if exc.code == "authentication_required":
            raise
        return _safe_failure(path, exc)
    except Exception as exc:
        return _safe_failure(path, exc)


def refresh_catalog(
    repository: Repository,
    client: BitbucketAPIClient,
    *,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> CatalogStats:
    """Refresh PDF metadata/text while preserving open counts and unchanged content."""

    pdf_paths, vsdx_count = client.document_paths()
    existing = {
        document.relative_path: document
        for document in Document.objects.filter(repository=repository, kind=DocumentKind.PDF)
    }
    worker_count = int(getattr(settings, "BITBUCKET_APP_MAX_WORKERS", 1))
    results: list[_CrawlResult] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(_crawl_pdf, path, existing.get(path), client) for path in pdf_paths
        ]
        for completed, future in enumerate(futures, start=1):
            result = future.result()
            results.append(result)
            failed += bool(result.error)
            if on_progress is not None:
                on_progress(completed, len(futures), failed)

    now = timezone.now()
    creates: list[Document] = []
    updates: list[Document] = []
    for result in results:
        document = existing.pop(result.path, None)
        if document is None:
            document = Document(
                repository=repository,
                kind=DocumentKind.PDF,
                relative_path=result.path,
            )
            creates.append(document)
        else:
            updates.append(document)

        document.filename = PurePosixPath(result.path).name[:500]
        document.last_scanned_at = now
        if result.added is not None:
            document.added_at = result.added.authored_at
            document.added_by = result.added.author
            document.added_by_email = result.added.email
            document.commit_id = result.added.commit_id
        if result.latest is not None:
            document.latest_commit_id = result.latest.commit_id
            document.latest_commit_message = result.latest.message
            document.latest_commit_author = result.latest.author
            document.latest_commit_at = result.latest.authored_at
        if result.extracted is not None:
            document.file_size = result.extracted.file_size
            document.page_count = result.extracted.page_count
            document.content_sha256 = result.extracted.content_sha256
            document.extracted_text = result.extracted.text
            document.text_truncated = result.extracted.text_truncated
            document.index_state = DocumentIndexState.INDEXED
            document.index_error = ""
        elif result.unchanged:
            document.index_state = DocumentIndexState.INDEXED
            document.index_error = ""
        else:
            document.index_state = DocumentIndexState.FAILED
            document.index_error = result.error
        document.updated_at = now

    indexed_count = sum(not result.error for result in results)
    stale_ids = [document.pk for document in existing.values()]
    with transaction.atomic():
        if stale_ids:
            Document.objects.filter(pk__in=stale_ids).delete()
        Document.objects.filter(repository=repository, kind=DocumentKind.VSDX).delete()
        Document.objects.bulk_create(creates, batch_size=500)
        if updates:
            Document.objects.bulk_update(
                updates,
                (
                    "filename",
                    "added_at",
                    "added_by",
                    "added_by_email",
                    "commit_id",
                    "latest_commit_id",
                    "latest_commit_message",
                    "latest_commit_author",
                    "latest_commit_at",
                    "file_size",
                    "page_count",
                    "content_sha256",
                    "extracted_text",
                    "text_truncated",
                    "index_state",
                    "index_error",
                    "last_scanned_at",
                    "updated_at",
                ),
                batch_size=500,
            )
        Contributor.objects.filter(repository=repository).delete()
        Repository.objects.filter(pk=repository.pk).update(
            pdf_count=len(pdf_paths),
            indexed_pdf_count=indexed_count,
            failed_pdf_count=failed,
            vsdx_count=vsdx_count,
        )

    return CatalogStats(
        found_pdf_count=len(pdf_paths),
        indexed_pdf_count=indexed_count,
        failed_pdf_count=failed,
        vsdx_count=vsdx_count,
        downloaded_pdf_count=sum(result.extracted is not None for result in results),
        unchanged_pdf_count=sum(result.unchanged for result in results),
    )
