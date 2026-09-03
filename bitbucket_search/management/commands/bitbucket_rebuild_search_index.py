"""Rebuild the derived PDF FTS5 tables from canonical database rows."""

import logging
import time

from django.core.management.base import BaseCommand, CommandError

from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_search import (
    ensure_search_index_available,
    rebuild_search_index,
)

logger = get_logger("worker")


class Command(BaseCommand):
    help = "Rebuild the derived Bitbucket PDF search index from stored metadata and page text."

    def handle(self, *args, **options):
        started = time.monotonic()
        log_event(logger, logging.INFO, "pdf_search_rebuild_started")
        try:
            if not ensure_search_index_available():
                raise CommandError(
                    "The PDF FTS5 index could not be created from the canonical PDF records."
                )
            rebuild_search_index()
        except Exception as exc:
            log_event(logger, logging.ERROR, "pdf_search_rebuild_failed", error=exc)
            raise
        log_event(
            logger,
            logging.INFO,
            "pdf_search_rebuild_completed",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        self.stdout.write(self.style.SUCCESS("The local PDF search index was rebuilt."))
