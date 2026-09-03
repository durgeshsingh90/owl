"""Publish durably staged PDF extraction results through one SQLite writer."""

from __future__ import annotations

import logging
import os
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_indexing import work_one_publication_job
from core.process_supervision import resident_supervisor_is_alive

logger = get_logger("writer")


class Command(BaseCommand):
    help = "Publish staged Bitbucket PDF extraction results in the background."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--idle-timeout", type=int, default=0)
        parser.add_argument("--poll-interval", type=float, default=0.25)

    def handle(self, *args, **options):
        started = time.monotonic()
        log_event(logger, logging.INFO, "pdf_writer_started", worker_pid=os.getpid())
        try:
            return self._run(**options)
        finally:
            log_event(
                logger,
                logging.INFO,
                "pdf_writer_stopped",
                worker_pid=os.getpid(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _run(self, **options):
        once = options["once"]
        idle_timeout = max(0, options["idle_timeout"])
        poll_interval = min(max(options["poll_interval"], 0.1), 10.0)
        idle_since = time.monotonic()
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
                completed = work_one_publication_job()
            except DatabaseError as exc:
                log_event(logger, logging.ERROR, "pdf_writer_database_failed", error=exc)
                completed = None
            finally:
                close_old_connections()
            if completed is not None:
                idle_since = time.monotonic()
                self.stdout.write(
                    f"PDF publication #{completed.pk}: {completed.get_status_display()}"
                )
                if once:
                    return
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return
            else:
                time.sleep(poll_interval)
