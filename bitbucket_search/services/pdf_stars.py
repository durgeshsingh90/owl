"""Persistent OWL-owned stars for registered repository PDFs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, OperationalError, transaction

from bitbucket_search.models import PDFDocument
from bitbucket_search.services.logging_events import get_logger, log_event

logger = get_logger("actions")


class PDFStarValidationError(ValueError):
    """Raised when a requested PDF star state is not valid."""


class PDFStarNotFoundError(LookupError):
    """Raised when the requested registered PDF no longer exists."""


class PDFStarConflictError(RuntimeError):
    """Raised when a concurrent database write prevents a safe update."""


@dataclass(frozen=True, slots=True)
class PDFStarSetResult:
    """Stable response data for one idempotent star-state request."""

    document_id: int
    filename: str
    starred: bool


def set_document_star(document_id: object, *, starred: bool) -> PDFStarSetResult:
    """Set one registered PDF's local star to the requested state."""

    logged_document_id = (
        document_id
        if isinstance(document_id, int) and not isinstance(document_id, bool) and document_id > 0
        else None
    )
    logged_starred = starred if isinstance(starred, bool) else None
    log_event(
        logger,
        logging.INFO,
        "pdf_star_set_requested",
        document_id=logged_document_id,
        starred=logged_starred,
    )
    try:
        result = _set_document_star(document_id, starred=starred)
    except (PDFStarValidationError, PDFStarNotFoundError) as error:
        log_event(
            logger,
            logging.WARNING,
            "pdf_star_set_rejected",
            error=error,
            document_id=logged_document_id,
        )
        raise
    except (IntegrityError, OperationalError) as error:
        log_event(
            logger,
            logging.WARNING,
            "pdf_star_set_conflicted",
            error=error,
            document_id=logged_document_id,
        )
        raise PDFStarConflictError("The PDF star is busy. Refresh and try again.") from error
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_star_set_failed",
            error=error,
            document_id=logged_document_id,
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "pdf_star_set_completed",
        document_id=result.document_id,
        starred=result.starred,
    )
    return result


@transaction.atomic
def _set_document_star(document_id: object, *, starred: bool) -> PDFStarSetResult:
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id <= 0:
        raise PDFStarNotFoundError("This PDF is not registered in OWL.")
    if not isinstance(starred, bool):
        raise PDFStarValidationError("Starred state must be true or false.")

    try:
        document = PDFDocument.objects.select_for_update().get(pk=document_id)
    except PDFDocument.DoesNotExist as error:
        raise PDFStarNotFoundError("This PDF is not registered in OWL.") from error

    if document.starred != starred:
        document.starred = starred
        document.save(update_fields=("starred",))

    return PDFStarSetResult(
        document_id=document.pk,
        filename=document.filename,
        starred=document.starred,
    )
