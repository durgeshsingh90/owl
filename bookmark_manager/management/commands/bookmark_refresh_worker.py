from django.core.management.base import BaseCommand, CommandError

from bookmark_manager.services.bookmark_refresh import (
    execute_refresh_run,
    mark_refresh_worker_failed,
)


class Command(BaseCommand):
    help = "Process one queued global Confluence bookmark refresh run."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", required=True, type=int)

    def handle(self, *args, **options):
        try:
            run = execute_refresh_run(options["run_id"])
        except Exception as exc:
            mark_refresh_worker_failed(options["run_id"])
            raise CommandError("The refresh worker stopped unexpectedly.") from exc
        if run is None:
            raise CommandError("The refresh run is missing or is no longer queued.")
        self.stdout.write(
            self.style.SUCCESS(
                f"Refresh #{run.pk}: {run.status} "
                f"({run.succeeded_bookmarks} succeeded, {run.failed_bookmarks} failed)"
            )
        )
