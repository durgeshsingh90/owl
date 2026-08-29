"""Queue PDF extraction work without coupling it to a web request."""

from django.conf import settings
from django.core.management.base import BaseCommand

from bitbucket_search.models import BitbucketRepository
from bitbucket_search.services.pdf_indexing import (
    launch_index_worker,
    queue_repository_pdf_extractions,
)


class Command(BaseCommand):
    help = "Queue unindexed/changed Bitbucket PDFs, optionally retrying failed revisions."

    def add_arguments(self, parser):
        parser.add_argument("--repository", type=int, action="append", dest="repository_ids")
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--no-launch", action="store_true")

    def handle(self, *args, **options):
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
            for _worker_number in range(settings.PDF_MAX_EXTRACTION_WORKERS):
                try:
                    launch_index_worker()
                except OSError:
                    break
        self.stdout.write(self.style.SUCCESS(f"Queued {len(queued_ids)} PDF extraction job(s)."))
