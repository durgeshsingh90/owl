"""Bounded in-memory PDF metadata and text extraction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.conf import settings


class PDFExtractionError(RuntimeError):
    """A safe extraction error suitable for job and UI status."""


@dataclass(frozen=True, slots=True)
class ExtractedPDF:
    file_size: int
    page_count: int
    content_sha256: str
    text: str
    text_truncated: bool


def extract_pdf(content: bytes) -> ExtractedPDF:
    """Extract page count and searchable text without retaining the PDF file."""

    max_bytes = int(getattr(settings, "BITBUCKET_APP_PDF_MAX_BYTES", 104_857_600))
    max_pages = int(getattr(settings, "BITBUCKET_APP_PDF_MAX_PAGES", 25_000))
    max_characters = int(getattr(settings, "BITBUCKET_APP_PDF_MAX_TEXT_CHARACTERS", 50_000_000))
    if len(content) > max_bytes:
        raise PDFExtractionError("The PDF exceeds the configured extraction size limit.")

    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as document:
            page_count = len(document)
            if page_count > max_pages:
                raise PDFExtractionError("The PDF exceeds the configured page limit.")
            parts: list[str] = []
            used_characters = 0
            text_truncated = False
            for page in document:
                page_text = page.get_text().replace("\x00", "")
                remaining = max_characters - used_characters
                if len(page_text) > remaining:
                    if remaining > 0:
                        parts.append(page_text[:remaining])
                    text_truncated = True
                    break
                parts.append(page_text)
                used_characters += len(page_text)
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError("PDF text extraction failed.") from exc

    return ExtractedPDF(
        file_size=len(content),
        page_count=page_count,
        content_sha256=hashlib.sha256(content).hexdigest(),
        text="".join(parts),
        text_truncated=text_truncated,
    )
