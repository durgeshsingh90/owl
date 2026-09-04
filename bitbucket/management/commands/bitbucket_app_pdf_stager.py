"""Serialize durable per-PDF extractor results into size-bounded JSONL chunks."""

from __future__ import annotations

import logging
import os
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from bitbucket.services.logging_events import get_logger, log_event
from bitbucket.services.pdf_indexing import work_one_staging_job
from bitbucket.services.pdf_jsonl_staging import (
    JSONLStager,
    cleanup_expired_imported_chunks,
    jsonl_stager_lock,
)
from bitbucket.services.repository_lock import RepositoryCheckoutBusy
from core.process_supervision import resident_supervisor_is_alive

logger = get_logger("stager")


class Command(BaseCommand):
    help = "Stage extracted Bitbucket PDF text into durable JSONL chunks."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--idle-timeout", type=int, default=0)
        parser.add_argument("--poll-interval", type=float, default=0.1)
        parser.add_argument("--max-poll-interval", type=float, default=1.0)

    def handle(self, *args, **options):
        started = time.monotonic()
        try:
            with jsonl_stager_lock(blocking=False):
                removed = cleanup_expired_imported_chunks()
                stager = JSONLStager()
                log_event(
                    logger,
                    logging.INFO,
                    "pdf_jsonl_stager_started",
                    worker_pid=os.getpid(),
                    count=len(removed),
                )
                return self._run(stager=stager, **options)
        except RepositoryCheckoutBusy:
            log_event(
                logger,
                logging.INFO,
                "pdf_jsonl_stager_already_running",
                worker_pid=os.getpid(),
            )
            return None
        finally:
            log_event(
                logger,
                logging.INFO,
                "pdf_jsonl_stager_stopped",
                worker_pid=os.getpid(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _run(self, *, stager: JSONLStager, **options):
        once = options["once"]
        idle_timeout = max(0, options["idle_timeout"])
        poll_interval = min(max(options["poll_interval"], 0.05), 10.0)
        max_poll_interval = min(
            max(options.get("max_poll_interval", 1.0), poll_interval),
            10.0,
        )
        idle_delay = poll_interval
        idle_since = time.monotonic()
        while True:
            if not resident_supervisor_is_alive():
                return None
            close_old_connections()
            try:
                completed = work_one_staging_job(stager)
            finally:
                close_old_connections()
            if completed is not None:
                idle_since = time.monotonic()
                idle_delay = poll_interval
                job_id = getattr(completed, "job_id", None)
                chunk = getattr(completed, "sealed_chunk", None)
                if chunk is None and hasattr(completed, "path"):
                    chunk = completed
                if job_id is not None:
                    self.stdout.write(f"PDF staging #{job_id}")
                elif chunk is not None:
                    self.stdout.write(f"JSONL chunk sealed: {chunk.path.name}")
                if once:
                    return None
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return None
            else:
                time.sleep(idle_delay)
                idle_delay = min(max_poll_interval, idle_delay * 2)
