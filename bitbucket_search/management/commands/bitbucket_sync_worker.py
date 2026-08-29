"""Run the durable Bitbucket repository synchronization queue."""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from bitbucket_search.services.pdf_indexing import (
    launch_index_worker,
    sweep_pdf_extraction_queue,
    work_one_extraction_job,
)
from bitbucket_search.services.repository_sync import work_one_job


class Command(BaseCommand):
    help = "Process queued Bitbucket repository clone and refresh jobs in the background."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check the queue once, then exit.")
        parser.add_argument(
            "--idle-timeout",
            type=int,
            default=0,
            help="Exit after this many idle seconds; zero keeps watching until stopped.",
        )
        parser.add_argument("--poll-interval", type=float, default=0.75)
        parser.add_argument(
            "--repository-only",
            action="store_true",
            help="Process repository jobs only; never claim PDF extraction jobs.",
        )
        parser.add_argument(
            "--no-spawn-index-workers",
            action="store_true",
            help="Process PDF jobs here without launching detached helper workers.",
        )
        parser.add_argument(
            "--no-startup-index-sweep",
            action="store_true",
            help="Skip PDF reconciliation because a resident supervisor already performed it.",
        )

    def handle(self, *args, **options):
        once = options["once"]
        idle_timeout = max(0, options["idle_timeout"])
        poll_interval = min(max(options["poll_interval"], 0.1), 10.0)
        repository_only = options["repository_only"]
        spawn_index_workers = not options["no_spawn_index_workers"] and not repository_only
        idle_since = time.monotonic()
        reconcile_index_queue = not options["no_startup_index_sweep"] and not repository_only
        next_index_reconciliation_at = 0.0

        while True:
            if reconcile_index_queue and time.monotonic() >= next_index_reconciliation_at:
                close_old_connections()
                try:
                    sweep_pdf_extraction_queue()
                except OperationalError:
                    pass
                finally:
                    close_old_connections()
                next_index_reconciliation_at = (
                    time.monotonic() + settings.BITBUCKET_SUPERVISOR_POLL_SECONDS
                )
            close_old_connections()
            try:
                completed_repository = work_one_job()
                completed_extraction = (
                    None
                    if completed_repository is not None or repository_only
                    else work_one_extraction_job()
                )
            except OperationalError:
                completed_repository = None
                completed_extraction = None
            finally:
                close_old_connections()
            if completed_repository is not None:
                idle_since = time.monotonic()
                self.stdout.write(
                    "Repository sync "
                    f"#{completed_repository.pk}: {completed_repository.get_status_display()}"
                )
                if once:
                    return
                # Wake a bounded set of dedicated controllers so one repository
                # containing many PDFs can use the configured parser concurrency.
                # Durable claiming still keeps the global running-job count at or
                # below the same setting, including this sync worker.
                if spawn_index_workers:
                    for _worker_number in range(settings.PDF_MAX_EXTRACTION_WORKERS):
                        try:
                            launch_index_worker()
                        except OSError:
                            break
            elif completed_extraction is not None:
                idle_since = time.monotonic()
                self.stdout.write(
                    "PDF extraction "
                    f"#{completed_extraction.pk}: {completed_extraction.get_status_display()}"
                )
                if once:
                    return
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return
            else:
                time.sleep(poll_interval)
