"""Rebuild the derived PDF FTS5 tables from canonical database rows."""

from django.core.management.base import BaseCommand, CommandError

from bitbucket_search.services.pdf_search import rebuild_search_index, search_index_available


class Command(BaseCommand):
    help = "Rebuild the derived Bitbucket PDF search index from stored metadata and page text."

    def handle(self, *args, **options):
        if not search_index_available():
            raise CommandError("The PDF FTS5 tables are unavailable. Run migrations first.")
        rebuild_search_index()
        self.stdout.write(self.style.SUCCESS("The local PDF search index was rebuilt."))
