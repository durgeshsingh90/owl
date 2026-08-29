"""Run the durable PDF extraction/index queue."""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from bitbucket_search.services.pdf_indexing import (
    sweep_pdf_extraction_queue,
    work_one_extraction_job,
)


class Command(BaseCommand):
    help = "Process queued Bitbucket PDF extraction jobs in the background."

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
            "--no-startup-sweep",
            action="store_true",
            help="Skip reconciliation because a resident supervisor already performed it.",
        )

    def handle(self, *args, **options):
        once = options["once"]
        idle_timeout = max(0, options["idle_timeout"])
        poll_interval = min(max(options["poll_interval"], 0.1), 10.0)
        idle_since = time.monotonic()
        reconcile_queue = not options["no_startup_sweep"]
        next_reconciliation_at = 0.0

        while True:
            if reconcile_queue and time.monotonic() >= next_reconciliation_at:
                close_old_connections()
                try:
                    sweep_pdf_extraction_queue()
                except OperationalError:
                    pass
                finally:
                    close_old_connections()
                next_reconciliation_at = (
                    time.monotonic() + settings.BITBUCKET_SUPERVISOR_POLL_SECONDS
                )
            close_old_connections()
            try:
                completed = work_one_extraction_job()
            except OperationalError:
                completed = None
            finally:
                close_old_connections()
            if completed is not None:
                idle_since = time.monotonic()
                self.stdout.write(
                    f"PDF extraction #{completed.pk}: {completed.get_status_display()}"
                )
                if once:
                    return
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return
            else:
                time.sleep(poll_interval)
