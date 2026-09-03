"""Run one resident worker for shared PDF/bookmark embedding jobs."""

from __future__ import annotations

import logging
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from core.process_supervision import resident_supervisor_is_alive
from semantic_search.services.jobs import sweep_semantic_index_queue, work_one_semantic_job
from semantic_search.services.logging_events import get_logger, log_event

logger = get_logger("worker")


class Command(BaseCommand):
    help = "Process queued local semantic embedding jobs in the background."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check one job, then exit.")
        parser.add_argument(
            "--idle-timeout",
            type=int,
            default=0,
            help="Exit after this many idle seconds; zero keeps watching until stopped.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=None,
            help=("Seconds between empty-queue checks; defaults to SEMANTIC_WORKER_IDLE_SECONDS."),
        )
        parser.add_argument(
            "--no-startup-sweep",
            action="store_true",
            help="Skip reconciliation because the OWL supervisor already performed it.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        log_event(logger, logging.INFO, "semantic_worker_started", worker_pid=os.getpid())
        try:
            return self._run(**options)
        except KeyboardInterrupt:
            log_event(
                logger,
                logging.INFO,
                "semantic_worker_stop_requested",
                reason="keyboard_interrupt",
            )
            raise
        except Exception as exc:
            log_event(
                logger,
                logging.CRITICAL,
                "semantic_worker_crashed",
                error=exc,
                worker_pid=os.getpid(),
            )
            raise
        finally:
            log_event(
                logger,
                logging.INFO,
                "semantic_worker_stopped",
                worker_pid=os.getpid(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _run(self, **options):
        once = bool(options["once"])
        idle_timeout = max(0, int(options["idle_timeout"]))
        configured_poll_interval = options["poll_interval"]
        if configured_poll_interval is None:
            configured_poll_interval = settings.SEMANTIC_WORKER_IDLE_SECONDS
        poll_interval = min(max(float(configured_poll_interval), 0.1), 300.0)
        idle_since = time.monotonic()
        if not options["no_startup_sweep"]:
            close_old_connections()
            try:
                # A standalone worker may be started beside an existing worker.
                # Preserve fresh leases; run_owl alone owns restart-time inherited
                # lease interruption before it launches its supervised pool.
                sweep_semantic_index_queue()
            finally:
                close_old_connections()

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
            close_old_connections()
            try:
                completed = work_one_semantic_job()
            except OperationalError as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "semantic_worker_database_failed",
                    error=exc,
                    stage="queue_execution",
                )
                completed = None
            finally:
                close_old_connections()
            if completed is not None:
                idle_since = time.monotonic()
                if once:
                    self.stdout.write(
                        f"Semantic index #{completed.pk}: {completed.get_status_display()}"
                    )
                    return
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return
            else:
                time.sleep(poll_interval)
