"""Run the durable PDF extraction/index queue."""

from __future__ import annotations

import logging
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections

from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import (
    sweep_pdf_extraction_queue,
    work_one_extraction_job,
)
from bitbucket_search.services.pdf_pipeline_controller import worker_slot_admitted
from core.process_supervision import resident_supervisor_is_alive

logger = get_logger("worker")


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
            "--slot-number",
            type=int,
            default=1,
            help="One-based resident slot used by the job-boundary admission controller.",
        )
        parser.add_argument(
            "--no-startup-sweep",
            action="store_true",
            help="Skip reconciliation because a resident supervisor already performed it.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        log_event(logger, logging.INFO, "pdf_index_worker_started", worker_pid=os.getpid())
        try:
            return self._run(*args, **options)
        except KeyboardInterrupt:
            log_event(
                logger, logging.INFO, "pdf_index_worker_stop_requested", reason="keyboard_interrupt"
            )
            raise
        except Exception as exc:
            log_event(
                logger,
                logging.CRITICAL,
                "pdf_index_worker_crashed",
                error=exc,
                worker_pid=os.getpid(),
            )
            raise
        finally:
            log_event(
                logger,
                logging.INFO,
                "pdf_index_worker_stopped",
                worker_pid=os.getpid(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _run(self, *args, **options):
        once = options["once"]
        idle_timeout = max(0, options["idle_timeout"])
        poll_interval = min(max(options["poll_interval"], 0.1), 10.0)
        slot_number = options["slot_number"]
        if slot_number < 1:
            raise CommandError("--slot-number must be at least 1.")
        idle_since = time.monotonic()
        reconcile_queue = not options["no_startup_sweep"]
        next_reconciliation_at = 0.0
        consecutive_database_errors = 0

        while True:
            if not resident_supervisor_is_alive():
                log_event(
                    logger,
                    logging.WARNING,
                    "resident_worker_parent_stopped",
                    worker_pid=os.getpid(),
                    parent_pid=os.getppid(),
                )
                return
            if reconcile_queue and time.monotonic() >= next_reconciliation_at:
                close_old_connections()
                try:
                    sweep_pdf_extraction_queue()
                except OperationalError as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_worker_queue_recovery_failed",
                        error=exc,
                        stage="startup_or_periodic_sweep",
                    )
                finally:
                    close_old_connections()
                next_reconciliation_at = (
                    time.monotonic() + settings.BITBUCKET_SUPERVISOR_POLL_SECONDS
                )
            close_old_connections()
            try:
                completed = work_one_extraction_job() if worker_slot_admitted(slot_number) else None
            except OperationalError as exc:
                consecutive_database_errors += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "pdf_worker_database_failed",
                    error=exc,
                    stage="queue_execution",
                )
                if (
                    consecutive_database_errors
                    >= settings.PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD
                ):
                    raise
                completed = None
            else:
                consecutive_database_errors = 0
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
