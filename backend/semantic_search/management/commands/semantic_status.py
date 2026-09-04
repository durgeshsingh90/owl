"""Print compact local semantic queue and corpus counts."""

from django.core.management.base import BaseCommand

from semantic_search.services.jobs import semantic_queue_snapshot


class Command(BaseCommand):
    help = "Show local PDF/bookmark semantic indexing progress."

    def handle(self, *args, **options):
        status = semantic_queue_snapshot()
        self.stdout.write(
            "Semantic jobs: "
            f"{status.running} running, {status.queued} queued, "
            f"{status.failed} failed, {status.succeeded} completed"
        )
        self.stdout.write(
            "Semantic corpus: "
            f"{status.indexed_bookmarks} bookmarks, "
            f"{status.embedded_chunks} chunks"
        )
