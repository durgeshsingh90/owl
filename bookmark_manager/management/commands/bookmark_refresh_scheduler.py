"""Run the durable weekly Confluence refresh scheduler."""

from __future__ import annotations

import logging
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections

from bookmark_manager.services.bookmark_refresh import (
    queue_due_scheduled_refresh,
    refresh_schedule_snapshot,
)
from bookmark_manager.services.logging_events import get_logger, log_event, logging_context

logger = get_logger("refresh_scheduler")


class Command(BaseCommand):
    help = "Queue due weekly Confluence refreshes and two-hour recovery retries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Check the schedule once, then exit.",
        )
        parser.add_argument(
            "--poll-seconds",
            type=int,
            default=settings.CONFLUENCE_REFRESH_SCHEDULER_POLL_SECONDS,
            help="Seconds between schedule checks while running continuously.",
        )

    def handle(self, *args, **options):
        if not 5 <= options["poll_seconds"] <= 3600:
            log_event(
                logger,
                logging.WARNING,
                "refresh_scheduler_options_rejected",
                reason="invalid_poll_interval",
            )
            raise CommandError("--poll-seconds must be between 5 and 3600.")
        started_at = time.monotonic()
        with logging_context(worker_pid=os.getpid()):
            log_event(logger, logging.INFO, "refresh_scheduler_started")
            try:
                return self._run_scheduler(*args, **options)
            except KeyboardInterrupt:
                log_event(logger, logging.INFO, "refresh_scheduler_stop_requested")
                raise
            except BaseException as exc:
                log_event(
                    logger,
                    logging.CRITICAL,
                    "refresh_scheduler_terminated",
                    error=exc,
                )
                raise
            finally:
                log_event(
                    logger,
                    logging.INFO,
                    "refresh_scheduler_stopped",
                    elapsed_ms=(time.monotonic() - started_at) * 1000,
                )

    def _run_scheduler(self, *args, **options):
        poll_seconds = options["poll_seconds"]

        while True:
            try:
                close_old_connections()
                run, queued = queue_due_scheduled_refresh()
                schedule = refresh_schedule_snapshot()
            except DatabaseError as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "refresh_scheduler_check_failed",
                    error=exc,
                    stage="schedule_check",
                )
                self.stderr.write(
                    self.style.WARNING(
                        "The refresh schedule could not be checked; OWL will try again."
                    )
                )
                if options["once"]:
                    raise CommandError("The refresh schedule could not be checked.") from exc
                log_event(
                    logger,
                    logging.WARNING,
                    "refresh_scheduler_retry_wait",
                    delay_seconds=poll_seconds,
                )
            else:
                if queued and run is not None:
                    log_event(
                        logger,
                        logging.INFO,
                        "refresh_scheduler_worker_launched",
                        run_id=run.pk,
                        trigger=run.trigger,
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Queued {run.get_trigger_display().lower()} refresh #{run.pk}."
                        )
                    )
                elif options["once"]:
                    next_run = schedule.get("next_run_at") or "not scheduled"
                    self.stdout.write(f"No refresh is due. Next check target: {next_run}.")
            finally:
                close_old_connections()

            if options["once"]:
                return
            time.sleep(poll_seconds)
