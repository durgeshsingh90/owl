"""Queue PDF extraction work without coupling it to a web request."""

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from bitbucket_search.models import BitbucketRepository
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import (
    launch_index_worker,
    launch_pdf_stager,
    launch_pdf_writer,
    queue_repository_pdf_extractions,
)

logger = get_logger("worker")


class Command(BaseCommand):
    help = "Queue unindexed/changed Bitbucket PDFs, optionally retrying failed revisions."

    def add_arguments(self, parser):
        parser.add_argument("--repository", type=int, action="append", dest="repository_ids")
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--no-launch", action="store_true")

    def handle(self, *args, **options):
        started = time.monotonic()
        log_event(logger, logging.INFO, "pdf_reindex_command_started")
        try:
            return self._run(*args, **options)
        except Exception as exc:
            log_event(logger, logging.ERROR, "pdf_reindex_command_failed", error=exc)
            raise
        finally:
            log_event(
                logger,
                logging.INFO,
                "pdf_reindex_command_finished",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _run(self, *args, **options):
        repositories = BitbucketRepository.objects.filter(enabled=True).order_by("id")
        if options["repository_ids"]:
            repositories = repositories.filter(pk__in=options["repository_ids"])

        queued_ids: list[int] = []
        for repository in repositories:
            result = queue_repository_pdf_extractions(
                repository,
                retry_failed=options["retry_failed"],
            )
            queued_ids.extend(result.queued_job_ids)

        if queued_ids and not options["no_launch"]:
            launch_pdf_stager()
            launch_pdf_writer()
            for _worker_number in range(settings.PDF_MAX_EXTRACTION_WORKERS):
                try:
                    launch_index_worker()
                except OSError as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_reindex_worker_spawn_failed",
                        error=exc,
                        queued_count=len(queued_ids),
                    )
                    break
        log_event(logger, logging.INFO, "pdf_reindex_jobs_queued", queued_count=len(queued_ids))
        self.stdout.write(self.style.SUCCESS(f"Queued {len(queued_ids)} PDF extraction job(s)."))
