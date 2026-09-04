import logging
import os
import time

from django.core.management.base import BaseCommand, CommandError

from bookmark_manager.services.bookmark_refresh import (
    execute_refresh_run,
    mark_refresh_worker_failed,
)
from bookmark_manager.services.logging_events import get_logger, log_event, logging_context

logger = get_logger("refresh_worker")


class Command(BaseCommand):
    help = "Process one queued global Confluence bookmark refresh run."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", required=True, type=int)

    def handle(self, *args, **options):
        started_at = time.monotonic()
        with logging_context(run_id=options["run_id"], worker_pid=os.getpid()):
            log_event(logger, logging.INFO, "refresh_worker_started")
            failure_reported = False
            try:
                try:
                    run = execute_refresh_run(options["run_id"])
                except Exception as exc:
                    # Record the original failure before attempting any DB recovery.
                    failure_reported = True
                    log_event(
                        logger,
                        logging.CRITICAL,
                        "refresh_worker_crashed",
                        error=exc,
                        stage="execute",
                    )
                    try:
                        mark_refresh_worker_failed(options["run_id"])
                    except Exception as recovery_error:
                        log_event(
                            logger,
                            logging.ERROR,
                            "refresh_worker_recovery_failed",
                            error=recovery_error,
                            stage="failure_persistence",
                        )
                        raise
                    raise CommandError("The refresh worker stopped unexpectedly.") from exc
                if run is None:
                    failure_reported = True
                    log_event(
                        logger,
                        logging.WARNING,
                        "refresh_worker_run_unavailable",
                        reason="missing_or_not_queued",
                    )
                    raise CommandError("The refresh run is missing or is no longer queued.")
                log_event(
                    logger,
                    logging.INFO,
                    "refresh_worker_completed",
                    status=run.status,
                    succeeded_count=run.succeeded_bookmarks,
                    failed_count=run.failed_bookmarks,
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Refresh #{run.pk}: {run.status} "
                        f"({run.succeeded_bookmarks} succeeded, {run.failed_bookmarks} failed)"
                    )
                )
            except BaseException as exc:
                if not failure_reported:
                    log_event(
                        logger,
                        logging.CRITICAL,
                        "refresh_worker_terminated",
                        error=exc,
                    )
                raise
            finally:
                log_event(
                    logger,
                    logging.INFO,
                    "refresh_worker_stopped",
                    elapsed_ms=(time.monotonic() - started_at) * 1000,
                )
