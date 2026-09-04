"""Database-backed search for the standalone Bitbucket PDF catalogue."""

from __future__ import annotations

import re

from django.db import DatabaseError, connection
from django.db.models import Q, QuerySet

from bitbucket.models import Document

_SEARCH_TERM = re.compile(r"\w+", re.UNICODE)


def _terms(query: str) -> tuple[str, ...]:
    return tuple(_SEARCH_TERM.findall(query)[:20])


def _fallback_search(documents: QuerySet[Document], terms: tuple[str, ...]) -> QuerySet[Document]:
    for term in terms:
        documents = documents.filter(
            Q(filename__icontains=term)
            | Q(relative_path__icontains=term)
            | Q(repository__project__icontains=term)
            | Q(repository__slug__icontains=term)
            | Q(added_by__icontains=term)
            | Q(latest_commit_author__icontains=term)
            | Q(latest_commit_message__icontains=term)
            | Q(extracted_text__icontains=term)
        )
    return documents


def search_documents(documents: QuerySet[Document], query: str) -> QuerySet[Document]:
    """Search persisted metadata/text through SQLite FTS5, with a portable fallback."""

    terms = _terms(query)
    if not terms:
        return documents
    if connection.vendor != "sqlite":
        return _fallback_search(documents, terms)

    fts_query = " AND ".join(f'"{term}"*' for term in terms)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rowid FROM bitbucket_document_fts WHERE bitbucket_document_fts MATCH %s",
                (fts_query,),
            )
            document_ids = [row[0] for row in cursor.fetchall()]
    except DatabaseError:
        return _fallback_search(documents, terms)
    return documents.filter(pk__in=document_ids)
