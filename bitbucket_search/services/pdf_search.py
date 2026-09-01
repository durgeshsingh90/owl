"""SQLite FTS5-backed local PDF search and bounded result explanations."""

from __future__ import annotations

import logging
import math
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from time import perf_counter

from django.db import DatabaseError, connection, transaction
from django.db.models import Case, F, Func, IntegerField, When
from django.db.models.functions import Lower

from bitbucket_search.models import PDFDocument, PDFDocumentLifecycle
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.pdf_search_query import (
    MAX_SEARCH_PAGE_SIZE,
    PDFSearchMatchMode,
    PDFSearchQuery,
    PDFSearchScope,
    PDFSearchSort,
)
from semantic_search.models import SemanticSourceType
from semantic_search.services.search import semantic_search

METADATA_FTS_TABLE = "bitbucket_search_pdf_metadata_fts"
PAGE_FTS_TABLE = "bitbucket_search_pdf_page_fts"
SEARCH_INDEX_VERSION = 1
MAX_SNIPPET_CHARACTERS = 240
MAX_SNIPPET_SOURCE_CHARACTERS = MAX_SNIPPET_CHARACTERS * 2
# FTS5 accepts at most 64 tokens here.  Using that maximum keeps ordinary long
# exact phrases inside one marked span before the hard character window below.
FTS_SNIPPET_TOKEN_LIMIT = 64
MAX_EXPLAINED_PAGES = 8
MAX_RESULT_PAGE_SIZE = MAX_SEARCH_PAGE_SIZE
logger = get_logger("search")

_METADATA_COLUMNS = {
    PDFSearchScope.FILENAME: "filename",
    PDFSearchScope.PATH: "relative_path",
    PDFSearchScope.REPOSITORY: "repository_name",
}
_SCOPE_LABELS = {
    PDFSearchScope.CONTENT: "PDF content",
    PDFSearchScope.FILENAME: "Filename",
    PDFSearchScope.PATH: "Path",
    PDFSearchScope.REPOSITORY: "Repository",
    PDFSearchScope.NOTES: "My notes",
}
_SCOPE_BOOSTS = {
    PDFSearchScope.FILENAME: 36.0,
    PDFSearchScope.PATH: 18.0,
    PDFSearchScope.REPOSITORY: 9.0,
    PDFSearchScope.CONTENT: 5.0,
    PDFSearchScope.NOTES: 2.0,
}


@dataclass(frozen=True, slots=True)
class PDFSnippetPart:
    text: str
    highlighted: bool = False


@dataclass(frozen=True, slots=True)
class PDFChipExplanation:
    chip: str
    scopes: tuple[str, ...]
    page_numbers: tuple[int, ...]
    pages_truncated: bool = False


@dataclass(frozen=True, slots=True)
class PDFSearchHit:
    document: PDFDocument
    score: float
    explanations: tuple[PDFChipExplanation, ...]
    best_page_number: int | None
    snippet: tuple[PDFSnippetPart, ...]


@dataclass(frozen=True, slots=True)
class PDFSearchPage:
    query: PDFSearchQuery
    results: tuple[PDFSearchHit, ...]
    total: int
    page: int
    page_size: int
    page_count: int
    semantic_fallback_used: bool = False

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count

    @property
    def has_previous(self) -> bool:
        return self.page > 1 and self.page_count > 0

    @property
    def start_index(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.page_size + 1

    @property
    def end_index(self) -> int:
        return min(self.page * self.page_size, self.total)


@dataclass(frozen=True, slots=True)
class _BoundedPageText:
    text: str
    has_source_prefix: bool
    has_source_suffix: bool


def search_index_available() -> bool:
    """Return whether both derived FTS tables are present in this database."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'table' AND name IN (%s, %s)
                """,
                (METADATA_FTS_TABLE, PAGE_FTS_TABLE),
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_search_index_check_failed",
            error=error,
            stage="index_validation",
        )
        return False
    available = bool(row and row[0] == 2)
    if not available:
        log_event(
            logger,
            logging.ERROR,
            "pdf_search_index_missing",
            stage="index_validation",
            reason="missing_fts_tables",
        )
    return available


def rebuild_search_index() -> None:
    """Rebuild both derived indexes from canonical Django-owned records."""

    started = perf_counter()
    log_event(logger, logging.INFO, "pdf_search_index_rebuild_requested")
    try:
        _rebuild_search_index()
    except Exception as error:
        log_event(logger, logging.ERROR, "pdf_search_index_rebuild_failed", error=error)
        raise
    log_event(
        logger,
        logging.INFO,
        "pdf_search_index_rebuild_completed",
        elapsed_ms=round((perf_counter() - started) * 1000),
    )


@transaction.atomic
def _rebuild_search_index() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {METADATA_FTS_TABLE}")
        cursor.execute(
            f"""
            INSERT INTO {METADATA_FTS_TABLE}(
                document_id,
                filename,
                relative_path,
                repository_name
            )
            SELECT
                document.id,
                document.filename,
                document.relative_path,
                repository.display_name
            FROM bitbucket_search_pdfdocument AS document
            JOIN bitbucket_search_bitbucketrepository AS repository
              ON repository.id = document.repository_id
            """
        )
        cursor.execute(f"INSERT INTO {PAGE_FTS_TABLE}({PAGE_FTS_TABLE}) VALUES ('rebuild')")


def search_documents(query: PDFSearchQuery) -> PDFSearchPage:
    """Search only published SQLite text; never open a repository or PDF file."""

    started = perf_counter()
    log_event(
        logger,
        logging.DEBUG,
        "pdf_search_requested",
        operation="phrases" if query.has_query else "filters",
        count=len(query.chips),
        limit=query.page_size,
    )
    try:
        result = _search_documents(query)
        if result.total == 0 and query.has_query and PDFSearchScope.CONTENT in query.scopes:
            try:
                semantic_result = _semantic_fallback_page(query)
            except Exception as error:
                log_event(
                    logger,
                    logging.ERROR,
                    "pdf_semantic_fallback_failed",
                    error=error,
                    stage="semantic_fallback",
                )
            else:
                if semantic_result.total:
                    result = semantic_result
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_search_failed",
            error=error,
            elapsed_ms=round((perf_counter() - started) * 1000),
        )
        raise
    log_event(
        logger,
        logging.DEBUG,
        "pdf_search_completed",
        result_count=len(result.results),
        count=result.total,
        page_count=result.page_count,
        elapsed_ms=round((perf_counter() - started) * 1000),
    )
    return result


def _search_documents(query: PDFSearchQuery) -> PDFSearchPage:
    if not query.has_query:
        return _filter_documents_without_text_query(query)
    if not search_index_available():
        return PDFSearchPage(query, (), 0, query.page, query.page_size, 0)
    _register_sqlite_search_functions()
    selected_rows, total = _query_candidate_page(query)
    page_count = math.ceil(total / query.page_size) if total else 0
    if not selected_rows:
        return PDFSearchPage(query, (), total, query.page, query.page_size, page_count)

    selected_ids = tuple(row[0] for row in selected_rows)
    documents_by_id = {
        document.pk: document
        for document in PDFDocument.objects.filter(pk__in=selected_ids).select_related(
            "repository",
            "added_commit",
            "last_commit",
            "indexed_revision",
            "local_policy",
        )
    }
    selected = tuple(
        (documents_by_id[document_id], score, best_page_number)
        for document_id, score, best_page_number in selected_rows
        if document_id in documents_by_id
    )
    metadata_hits, document_pages, page_match_counts, best_pages = _load_selected_evidence(
        query,
        selected,
    )
    snippet_text = _load_best_page_text(
        tuple(document for document, _score, _best_page in selected),
        best_pages=best_pages,
        query=query,
    )
    results = tuple(
        _build_search_hit(
            document,
            score=score,
            query=query,
            metadata_hits=metadata_hits,
            document_pages=document_pages,
            page_match_counts=page_match_counts,
            best_page_number=best_pages.get(document.pk),
            snippet_text=snippet_text,
        )
        for document, score, _candidate_best_page in selected
    )
    return PDFSearchPage(query, results, total, query.page, query.page_size, page_count)


def _semantic_fallback_page(query: PDFSearchQuery) -> PDFSearchPage:
    """Search related local PDF content only after exact FTS returned no documents."""

    eligible = _eligible_semantic_documents(query)
    allowed_revision_ids = frozenset(
        eligible.order_by()
        .exclude(indexed_revision_id__isnull=True)
        .values_list("indexed_revision_id", flat=True)
        .distinct()
    )
    if not allowed_revision_ids:
        return PDFSearchPage(query, (), 0, query.page, query.page_size, 0)
    semantic_query = " ".join(chip.display for chip in query.chips)
    matches = semantic_search(
        SemanticSourceType.PDF_REVISION,
        semantic_query,
        allowed_source_ids=allowed_revision_ids,
    )
    if not matches:
        return PDFSearchPage(query, (), 0, query.page, query.page_size, 0)

    match_by_revision = {match.source_id: match for match in matches}
    documents = eligible.filter(indexed_revision_id__in=tuple(match_by_revision))
    if query.sort == PDFSearchSort.RELEVANCE:
        rank = Case(
            *(
                When(indexed_revision_id=match.source_id, then=position)
                for position, match in enumerate(matches)
            ),
            default=len(matches),
            output_field=IntegerField(),
        )
        documents = documents.annotate(semantic_rank=rank).order_by(
            "semantic_rank", Lower("filename"), "pk"
        )
    else:
        documents = documents.order_by(*_filtered_order_by(query.sort))

    total = documents.count()
    page_count = math.ceil(total / query.page_size) if total else 0
    if page_count and query.page > page_count:
        return PDFSearchPage(
            query,
            (),
            total,
            query.page,
            query.page_size,
            page_count,
            semantic_fallback_used=True,
        )
    start = (query.page - 1) * query.page_size
    selected = tuple(
        documents.select_related(
            "repository",
            "added_commit",
            "last_commit",
            "indexed_revision",
            "local_policy",
        )[start : start + query.page_size]
    )
    results: list[PDFSearchHit] = []
    label = semantic_query if len(semantic_query) <= 120 else f"{semantic_query[:119].rstrip()}…"
    for document in selected:
        match = match_by_revision[document.indexed_revision_id]
        page_numbers = (match.page_number,) if match.page_number is not None else ()
        results.append(
            PDFSearchHit(
                document=document,
                score=match.score,
                explanations=(
                    PDFChipExplanation(
                        chip=label,
                        scopes=("Related PDF content",),
                        page_numbers=page_numbers,
                    ),
                ),
                best_page_number=match.page_number,
                snippet=(PDFSnippetPart(match.snippet),) if match.snippet else (),
            )
        )
    return PDFSearchPage(
        query=query,
        results=tuple(results),
        total=total,
        page=query.page,
        page_size=query.page_size,
        page_count=page_count,
        semantic_fallback_used=True,
    )


def _eligible_semantic_documents(query: PDFSearchQuery):
    queryset = PDFDocument.objects.filter(
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        repository__enabled=True,
        indexed_revision__isnull=False,
    )
    if query.repository_ids:
        queryset = queryset.filter(repository_id__in=query.repository_ids)
    if query.index_states:
        queryset = queryset.filter(index_state__in=tuple(query.index_states))
    if query.committer_names:
        _register_sqlite_search_functions()
        queryset = queryset.annotate(
            committer_search_key=Func(
                F("last_commit__committer_name"),
                function="owl_pdf_normalize",
            )
        ).filter(
            committer_search_key__in=tuple(_normalized(name) for name in query.committer_names)
        )
    return queryset


def _filter_documents_without_text_query(query: PDFSearchQuery) -> PDFSearchPage:
    """Apply local metadata filters/sorts without pretending an FTS match exists."""

    queryset = PDFDocument.objects.filter(
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        repository__enabled=True,
    )
    if query.repository_ids:
        queryset = queryset.filter(repository_id__in=query.repository_ids)
    if query.index_states:
        queryset = queryset.filter(index_state__in=tuple(query.index_states))
    if query.committer_names:
        _register_sqlite_search_functions()
        queryset = queryset.annotate(
            committer_search_key=Func(
                F("last_commit__committer_name"),
                function="owl_pdf_normalize",
            )
        ).filter(
            committer_search_key__in=tuple(_normalized(name) for name in query.committer_names)
        )
    total = queryset.count()
    page_count = math.ceil(total / query.page_size) if total else 0
    if page_count and query.page > page_count:
        return PDFSearchPage(query, (), total, query.page, query.page_size, page_count)
    start = (query.page - 1) * query.page_size
    documents = tuple(
        queryset.select_related(
            "repository",
            "added_commit",
            "last_commit",
            "indexed_revision",
            "local_policy",
        ).order_by(*_filtered_order_by(query.sort))[start : start + query.page_size]
    )
    return PDFSearchPage(
        query=query,
        results=tuple(
            PDFSearchHit(
                document=document,
                score=0.0,
                explanations=(),
                best_page_number=None,
                snippet=(),
            )
            for document in documents
        ),
        total=total,
        page=query.page,
        page_size=query.page_size,
        page_count=page_count,
    )


def _filtered_order_by(sort: PDFSearchSort):
    identity = (Lower("filename").asc(), "pk")
    if sort == PDFSearchSort.MOST_OPENED:
        return ("-open_count", *identity)
    if sort == PDFSearchSort.LEAST_OPENED:
        return ("open_count", *identity)
    if sort == PDFSearchSort.RECENTLY_OPENED:
        return (F("last_opened_at").desc(nulls_last=True), *identity)
    if sort == PDFSearchSort.LEAST_RECENTLY_OPENED:
        return (F("last_opened_at").asc(nulls_first=True), *identity)
    if sort == PDFSearchSort.FILENAME_DESCENDING:
        return (Lower("filename").desc(), "pk")
    if sort == PDFSearchSort.GIT_UPDATED_NEWEST:
        return (F("last_commit__committed_at").desc(nulls_last=True), *identity)
    if sort == PDFSearchSort.INDEXED_NEWEST:
        return (F("last_indexed_at").desc(nulls_last=True), *identity)
    if sort == PDFSearchSort.REPOSITORY_ASCENDING:
        return (Lower("repository__display_name").asc(), *identity)
    return identity


def _register_sqlite_search_functions() -> None:
    connection.ensure_connection()
    raw_connection = connection.connection
    if raw_connection is None:
        raise DatabaseError("The PDF search database connection is unavailable.")
    if getattr(connection, "_owl_pdf_search_functions_connection", None) is raw_connection:
        return
    raw_connection.create_function("owl_pdf_normalize", 1, _normalized, deterministic=True)
    raw_connection.create_function(
        "owl_pdf_filename_stem",
        1,
        lambda value: _normalized(str(value).removesuffix(".pdf")),
        deterministic=True,
    )
    raw_connection.create_function(
        "owl_pdf_rank_component",
        1,
        _rank_component,
        deterministic=True,
    )
    raw_connection.create_function(
        "owl_pdf_open_bonus",
        1,
        lambda value: min(math.log1p(max(int(value or 0), 0)) * 0.08, 1.0),
        deterministic=True,
    )
    connection._owl_pdf_search_functions_connection = raw_connection


def _eligible_documents_cte(query: PDFSearchQuery) -> tuple[str, list[object]]:
    conditions = ["document.lifecycle_state = %s", "repository.enabled = 1"]
    parameters: list[object] = [PDFDocumentLifecycle.ACTIVE]
    if query.repository_ids:
        placeholders = ", ".join("%s" for _value in query.repository_ids)
        conditions.append(f"document.repository_id IN ({placeholders})")
        parameters.extend(query.repository_ids)
    if query.index_states:
        placeholders = ", ".join("%s" for _value in query.index_states)
        conditions.append(f"document.index_state IN ({placeholders})")
        parameters.extend(state.value for state in query.index_states)
    if query.committer_names:
        placeholders = ", ".join("%s" for _value in query.committer_names)
        conditions.append(f"owl_pdf_normalize(last_commit.committer_name) IN ({placeholders})")
        parameters.extend(_normalized(name) for name in query.committer_names)
    return (
        f"""
        eligible_documents AS MATERIALIZED (
            SELECT document.id AS document_id,
                   document.filename,
                   document.open_count,
                   document.last_opened_at,
                   document.last_indexed_at,
                   document.indexed_revision_id,
                   repository.display_name AS repository_name,
                   last_commit.committed_at AS git_updated_at
            FROM bitbucket_search_pdfdocument AS document
            JOIN bitbucket_search_bitbucketrepository AS repository
              ON repository.id = document.repository_id
            LEFT JOIN bitbucket_search_gitcommit AS last_commit
              ON last_commit.id = document.last_commit_id
            WHERE {" AND ".join(conditions)}
        )
        """,
        parameters,
    )


def _metadata_hit_sql(query: PDFSearchQuery, *, eligible_name: str) -> tuple[str, list[object]]:
    branches: list[str] = []
    parameters: list[object] = []
    for chip_index, chip in enumerate(query.chips):
        for scope in query.scopes:
            column = _METADATA_COLUMNS.get(scope)
            if column is None:
                continue
            branches.append(
                f"""
                SELECT {chip_index} AS chip_index,
                       CAST({METADATA_FTS_TABLE}.document_id AS INTEGER) AS document_id,
                       '{scope.value}' AS scope,
                       bm25({METADATA_FTS_TABLE}, 0.0, 1.0, 1.0, 1.0) AS rank
                FROM {METADATA_FTS_TABLE}
                JOIN {eligible_name} AS eligible
                  ON eligible.document_id = CAST({METADATA_FTS_TABLE}.document_id AS INTEGER)
                WHERE {METADATA_FTS_TABLE} MATCH %s
                """
            )
            parameters.append(f"{column} : {chip.fts5_phrase}")
    if not branches:
        return (
            "SELECT 0 AS chip_index, 0 AS document_id, '' AS scope, 0.0 AS rank WHERE 0",
            [],
        )
    return " UNION ALL ".join(branches), parameters


def _content_hit_sql(query: PDFSearchQuery, *, eligible_name: str) -> tuple[str, list[object]]:
    if PDFSearchScope.CONTENT not in query.scopes:
        return (
            "SELECT 0 AS chip_index, 0 AS document_id, 0 AS page_number WHERE 0",
            [],
        )
    branches: list[str] = []
    parameters: list[object] = []
    for chip_index, chip in enumerate(query.chips):
        branches.append(
            f"""
            SELECT {chip_index} AS chip_index,
                   eligible.document_id,
                   page.page_number
            FROM {PAGE_FTS_TABLE}
            JOIN bitbucket_search_pdftextpage AS page
              ON page.id = {PAGE_FTS_TABLE}.rowid
            JOIN {eligible_name} AS eligible
              ON eligible.indexed_revision_id = page.revision_id
            WHERE {PAGE_FTS_TABLE} MATCH %s
            """
        )
        parameters.append(chip.fts5_phrase)
    return " UNION ALL ".join(branches), parameters


def _content_document_hit_sql(
    query: PDFSearchQuery,
    *,
    eligible_name: str,
) -> tuple[str, list[object]]:
    """Aggregate broad page matches to one bounded row per chip/document in SQL."""

    if PDFSearchScope.CONTENT not in query.scopes:
        return (
            "SELECT 0 AS chip_index, 0 AS document_id, 0.0 AS score WHERE 0",
            [],
        )
    branches: list[str] = []
    parameters: list[object] = []
    for chip_index, chip in enumerate(query.chips):
        branches.append(
            f"""
            SELECT {chip_index} AS chip_index,
                   eligible.document_id,
                   {_SCOPE_BOOSTS[PDFSearchScope.CONTENT]} AS score
            FROM {PAGE_FTS_TABLE}
            JOIN bitbucket_search_pdftextpage AS page
              ON page.id = {PAGE_FTS_TABLE}.rowid
            JOIN {eligible_name} AS eligible
              ON eligible.indexed_revision_id = page.revision_id
            WHERE {PAGE_FTS_TABLE} MATCH %s
            GROUP BY eligible.document_id
            """
        )
        parameters.append(chip.fts5_phrase)
    return " UNION ALL ".join(branches), parameters


def _candidate_ctes(query: PDFSearchQuery) -> tuple[str, list[object]]:
    eligible_sql, eligible_parameters = _eligible_documents_cte(query)
    metadata_sql, metadata_parameters = _metadata_hit_sql(
        query,
        eligible_name="eligible_documents",
    )
    content_sql, content_parameters = _content_document_hit_sql(
        query,
        eligible_name="eligible_documents",
    )
    required_chip_count = len(query.chips) if query.match_mode == PDFSearchMatchMode.ALL else 1
    exact_filename_score = "0.0"
    filename_parameters: list[object] = []
    if len(query.chips) == 1:
        exact_filename_score = (
            "CASE WHEN filename_normalized = %s OR filename_stem_normalized = %s "
            "THEN 120.0 ELSE 0.0 END"
        )
        filename_parameters.extend((query.chips[0].key, query.chips[0].key))
    contains_all = " AND ".join("INSTR(filename_normalized, %s) > 0" for _chip in query.chips)
    contains_all_score = f"CASE WHEN {contains_all} THEN 55.0 ELSE 0.0 END"
    filename_parameters.extend(chip.key for chip in query.chips)
    ctes = f"""
        WITH
        {eligible_sql},
        metadata_hits AS MATERIALIZED (
            {metadata_sql}
        ),
        content_document_hits AS MATERIALIZED (
            {content_sql}
        ),
        chip_documents AS MATERIALIZED (
            SELECT chip_index, document_id FROM metadata_hits
            UNION
            SELECT chip_index, document_id FROM content_document_hits
        ),
        candidate_ids AS MATERIALIZED (
            SELECT document_id
            FROM chip_documents
            GROUP BY document_id
            HAVING COUNT(*) >= {required_chip_count}
        ),
        metadata_scores AS (
            SELECT document_id,
                   SUM(
                       CASE scope
                           WHEN 'filename' THEN {_SCOPE_BOOSTS[PDFSearchScope.FILENAME]}
                           WHEN 'path' THEN {_SCOPE_BOOSTS[PDFSearchScope.PATH]}
                           WHEN 'repository' THEN {_SCOPE_BOOSTS[PDFSearchScope.REPOSITORY]}
                           ELSE 0.0
                       END + owl_pdf_rank_component(rank)
                   ) AS score
            FROM metadata_hits
            GROUP BY document_id
        ),
        content_scores AS (
            SELECT document_id, SUM(score) AS score
            FROM content_document_hits
            GROUP BY document_id
        ),
        candidate_details AS (
            SELECT eligible.*,
                   owl_pdf_normalize(eligible.filename) AS filename_normalized,
                   owl_pdf_filename_stem(eligible.filename) AS filename_stem_normalized,
                   owl_pdf_normalize(eligible.repository_name) AS repository_normalized,
                   COALESCE(metadata_scores.score, 0.0)
                       + COALESCE(content_scores.score, 0.0) AS base_score,
                   NULL AS best_page_number
            FROM candidate_ids
            JOIN eligible_documents AS eligible USING (document_id)
            LEFT JOIN metadata_scores USING (document_id)
            LEFT JOIN content_scores USING (document_id)
        ),
        candidate_scores AS (
            SELECT candidate_details.*,
                   base_score
                       + {exact_filename_score}
                       + {contains_all_score}
                       + owl_pdf_open_bonus(open_count) AS score
            FROM candidate_details
        )
    """
    parameters = [
        *eligible_parameters,
        *metadata_parameters,
        *content_parameters,
        *filename_parameters,
    ]
    return ctes, parameters


def _search_order_sql(sort: PDFSearchSort) -> str:
    identity = "filename_normalized ASC, document_id ASC"
    if sort == PDFSearchSort.MOST_OPENED:
        return f"open_count DESC, score DESC, {identity}"
    if sort == PDFSearchSort.LEAST_OPENED:
        return f"open_count ASC, score DESC, {identity}"
    if sort == PDFSearchSort.RECENTLY_OPENED:
        return f"last_opened_at IS NULL ASC, last_opened_at DESC, score DESC, {identity}"
    if sort == PDFSearchSort.LEAST_RECENTLY_OPENED:
        return f"last_opened_at IS NOT NULL ASC, last_opened_at ASC, score DESC, {identity}"
    if sort == PDFSearchSort.FILENAME_ASCENDING:
        return f"{identity}, score DESC"
    if sort == PDFSearchSort.FILENAME_DESCENDING:
        return "filename_normalized DESC, document_id ASC"
    if sort == PDFSearchSort.GIT_UPDATED_NEWEST:
        return f"git_updated_at IS NULL ASC, git_updated_at DESC, score DESC, {identity}"
    if sort == PDFSearchSort.INDEXED_NEWEST:
        return f"last_indexed_at IS NULL ASC, last_indexed_at DESC, score DESC, {identity}"
    if sort == PDFSearchSort.REPOSITORY_ASCENDING:
        return f"repository_normalized ASC, score DESC, {identity}"
    return f"score DESC, {identity}"


def _query_candidate_page(
    query: PDFSearchQuery,
) -> tuple[tuple[tuple[int, float, int | None], ...], int]:
    ctes, parameters = _candidate_ctes(query)
    offset = (query.page - 1) * query.page_size
    sql = f"""
        {ctes}
        SELECT document_id,
               score,
               best_page_number,
               COUNT(*) OVER () AS total_count
        FROM candidate_scores
        ORDER BY {_search_order_sql(query.sort)}
        LIMIT %s OFFSET %s
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, (*parameters, query.page_size, offset))
            rows = tuple(cursor.fetchmany(min(query.page_size, MAX_RESULT_PAGE_SIZE) + 1))
    except DatabaseError as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_search_sql_failed",
            error=error,
            stage="candidate_page",
            limit=query.page_size,
        )
        # Punctuation-only literal phrases safely produce no matches rather than
        # exposing FTS syntax errors to the caller.
        return (), 0
    if rows:
        total = int(rows[0][3])
        return (
            tuple(
                (
                    int(document_id),
                    float(score),
                    int(best_page_number) if best_page_number is not None else None,
                )
                for document_id, score, best_page_number, _total in rows[: query.page_size]
            ),
            total,
        )
    if query.page == 1:
        return (), 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"{ctes} SELECT COUNT(*) FROM candidate_scores", parameters)
            row = cursor.fetchone()
    except DatabaseError as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_search_sql_failed",
            error=error,
            stage="candidate_count",
        )
        return (), 0
    return (), int(row[0]) if row else 0


def _selected_documents_cte(selected) -> tuple[str, list[object]]:
    values = ", ".join("(%s, %s)" for _item in selected)
    parameters: list[object] = []
    for document, _score, _best_page in selected:
        parameters.extend((document.pk, document.indexed_revision_id))
    return (
        f"selected_documents(document_id, indexed_revision_id) AS (VALUES {values})",
        parameters,
    )


def _load_selected_evidence(query: PDFSearchQuery, selected):
    metadata_hits: dict[str, dict[int, set[PDFSearchScope]]] = defaultdict(lambda: defaultdict(set))
    document_pages: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    page_match_counts: dict[tuple[str, int], int] = {}
    best_pages: dict[int, int] = {}
    if not selected:
        return metadata_hits, document_pages, page_match_counts, best_pages

    selected_cte, selected_parameters = _selected_documents_cte(selected)
    metadata_sql, metadata_parameters = _metadata_hit_sql(
        query,
        eligible_name="selected_documents",
    )
    metadata_scope_count = sum(scope in _METADATA_COLUMNS for scope in query.scopes)
    metadata_limit = len(selected) * len(query.chips) * metadata_scope_count
    if metadata_limit:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH {selected_cte}, selected_metadata_hits AS ({metadata_sql})
                SELECT DISTINCT chip_index, document_id, scope
                FROM selected_metadata_hits
                ORDER BY chip_index, document_id, scope
                """,
                (*selected_parameters, *metadata_parameters),
            )
            rows = cursor.fetchmany(metadata_limit + 1)
        if len(rows) > metadata_limit:
            raise DatabaseError("PDF metadata evidence exceeded its selected-result bound.")
        for chip_index, document_id, scope in rows:
            metadata_hits[query.chips[int(chip_index)].key][int(document_id)].add(
                PDFSearchScope(scope)
            )

    if PDFSearchScope.CONTENT in query.scopes:
        content_sql, content_parameters = _content_hit_sql(
            query,
            eligible_name="selected_documents",
        )
        content_limit = len(selected) * len(query.chips) * MAX_EXPLAINED_PAGES
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH {selected_cte},
                selected_content_hits AS ({content_sql}),
                ranked_content_hits AS (
                    SELECT chip_index,
                           document_id,
                           page_number,
                           COUNT(*) OVER (
                               PARTITION BY chip_index, document_id
                           ) AS matched_page_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY chip_index, document_id
                               ORDER BY page_number ASC
                           ) AS evidence_rank
                    FROM selected_content_hits
                )
                SELECT chip_index, document_id, page_number, matched_page_count
                FROM ranked_content_hits
                WHERE evidence_rank <= {MAX_EXPLAINED_PAGES}
                ORDER BY chip_index, document_id, page_number
                """,
                (*selected_parameters, *content_parameters),
            )
            rows = cursor.fetchmany(content_limit + 1)
        if len(rows) > content_limit:
            raise DatabaseError("PDF page evidence exceeded its selected-result bound.")
        for chip_index, document_id, page_number, matched_page_count in rows:
            chip_key = query.chips[int(chip_index)].key
            document_id = int(document_id)
            document_pages[chip_key][document_id].add(int(page_number))
            page_match_counts[(chip_key, document_id)] = int(matched_page_count)

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH {selected_cte},
                selected_content_hits AS ({content_sql}),
                page_chip_counts AS (
                    SELECT document_id,
                           page_number,
                           COUNT(DISTINCT chip_index) AS chip_count
                    FROM selected_content_hits
                    GROUP BY document_id, page_number
                ),
                ranked_best_pages AS (
                    SELECT document_id,
                           page_number,
                           ROW_NUMBER() OVER (
                               PARTITION BY document_id
                               ORDER BY chip_count DESC, page_number ASC
                           ) AS page_rank
                    FROM page_chip_counts
                )
                SELECT document_id, page_number
                FROM ranked_best_pages
                WHERE page_rank = 1
                ORDER BY document_id
                """,
                (*selected_parameters, *content_parameters),
            )
            rows = cursor.fetchmany(len(selected) + 1)
        if len(rows) > len(selected):
            raise DatabaseError("PDF best-page evidence exceeded its selected-result bound.")
        best_pages = {int(document_id): int(page_number) for document_id, page_number in rows}

    return metadata_hits, document_pages, page_match_counts, best_pages


def _normalized(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _rank_component(rank: float) -> float:
    return min(max(-rank, 0.0) * 1_000_000.0, 8.0)


def _load_best_page_text(
    documents,
    *,
    best_pages: dict[int, int],
    query: PDFSearchQuery,
):
    targets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for document in documents:
        page_number = best_pages.get(document.pk)
        if page_number is None or document.indexed_revision_id is None:
            continue
        targets[(document.indexed_revision_id, page_number)].append(document.pk)
    if not targets:
        return {}

    target_values = ", ".join("(%s, %s)" for _target in targets)
    target_parameters: list[object] = []
    for revision_id, page_number in targets:
        target_parameters.extend((revision_id, page_number))
    # A best page can contain only a subset of an ALL query whose other chips
    # matched metadata or different pages.  The snippet therefore asks FTS5 to
    # mark any chip on this exact page, while candidate semantics remain owned
    # by the document-level search query above.  Unlike the former chip cross
    # join, this executes one indexed MATCH/snippet operation per target page.
    snippet_expression = " OR ".join(query.fts5_chip_phrases)
    context_before = MAX_SNIPPET_CHARACTERS // 2
    sql = f"""
        WITH
        target_pages(revision_id, page_number) AS (VALUES {target_values}),
        raw_snippets AS NOT MATERIALIZED (
            SELECT page.revision_id,
                   page.page_number,
                   snippet(
                       {PAGE_FTS_TABLE},
                       0,
                       CHAR(1),
                       CHAR(2),
                       CHAR(3),
                       {FTS_SNIPPET_TOKEN_LIMIT}
                   ) AS marked_text
            FROM target_pages
            JOIN bitbucket_search_pdftextpage AS page
              ON page.revision_id = target_pages.revision_id
             AND page.page_number = target_pages.page_number
            JOIN {PAGE_FTS_TABLE}
              ON {PAGE_FTS_TABLE}.rowid = page.id
            WHERE {PAGE_FTS_TABLE} MATCH %s
            LIMIT -1 OFFSET 0
        ),
        located_snippets AS (
            SELECT revision_id,
                   page_number,
                   marked_text,
                   INSTR(marked_text, CHAR(1)) AS highlight_position
            FROM raw_snippets
        ),
        bounded_snippets AS (
            SELECT revision_id,
                   page_number,
                   marked_text,
                   CASE
                       WHEN highlight_position > %s
                       THEN highlight_position - %s
                       ELSE 1
                   END AS marked_start
            FROM located_snippets
        )
        SELECT revision_id,
               page_number,
               SUBSTR(marked_text, marked_start, %s) AS bounded_text,
               marked_start > 1 OR SUBSTR(marked_text, 1, 1) = CHAR(3)
                   AS has_source_prefix,
               marked_start - 1 + %s < LENGTH(marked_text)
                   OR SUBSTR(marked_text, -1, 1) = CHAR(3)
                   AS has_source_suffix
        FROM bounded_snippets
    """
    parameters = (
        *target_parameters,
        snippet_expression,
        context_before,
        context_before,
        MAX_SNIPPET_SOURCE_CHARACTERS,
        MAX_SNIPPET_SOURCE_CHARACTERS,
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, parameters)
        rows = cursor.fetchmany(len(targets) + 1)
    if len(rows) > len(targets):
        raise DatabaseError("PDF snippet windows exceeded their selected-page bound.")

    result: dict[int, _BoundedPageText] = {}
    for revision_id, page_number, text, has_source_prefix, has_source_suffix in rows:
        bounded = _BoundedPageText(
            text=str(text or ""),
            has_source_prefix=bool(has_source_prefix),
            has_source_suffix=bool(has_source_suffix),
        )
        for document_id in targets[(revision_id, page_number)]:
            result[document_id] = bounded
    return result


def _build_search_hit(
    document: PDFDocument,
    *,
    score: float,
    query: PDFSearchQuery,
    metadata_hits,
    document_pages,
    page_match_counts,
    best_page_number: int | None,
    snippet_text,
) -> PDFSearchHit:
    explanations: list[PDFChipExplanation] = []
    for chip in query.chips:
        scopes = set(metadata_hits[chip.key].get(document.pk, ()))
        pages = tuple(sorted(document_pages[chip.key].get(document.pk, ())))
        if pages:
            scopes.add(PDFSearchScope.CONTENT)
        matched_page_count = page_match_counts.get((chip.key, document.pk), len(pages))
        explanations.append(
            PDFChipExplanation(
                chip=chip.display,
                scopes=tuple(_SCOPE_LABELS[item] for item in query.scopes if item in scopes),
                page_numbers=pages,
                pages_truncated=matched_page_count > len(pages),
            )
        )
    source = snippet_text.get(document.pk)
    snippet = (
        _snippet_parts(
            source.text,
            query=query,
            source_prefix=source.has_source_prefix,
            source_suffix=source.has_source_suffix,
        )
        if source is not None
        else ()
    )
    return PDFSearchHit(document, score, tuple(explanations), best_page_number, snippet)


def _snippet_parts(
    text: str,
    *,
    query: PDFSearchQuery,
    source_prefix: bool = False,
    source_suffix: bool = False,
) -> tuple[PDFSnippetPart, ...]:
    if not text:
        return ()
    # Extracted PDF text strips Cc controls before indexing, so FTS5's CHAR(1),
    # CHAR(2), and CHAR(3) sentinels cannot collide with source content.  Parsing
    # FTS5's own markers preserves Unicode/diacritic matches (for example,
    # ``cafe`` highlighting ``CAFÉ``) without a second Python approximation.
    del query
    source_prefix = source_prefix or text.startswith("\x03")
    source_suffix = source_suffix or text.endswith("\x03")
    parsed: list[PDFSnippetPart] = []
    buffer: list[str] = []
    highlighted = False

    def flush() -> None:
        if not buffer:
            return
        value = "".join(buffer)
        buffer.clear()
        if parsed and parsed[-1].highlighted == highlighted:
            previous = parsed[-1]
            parsed[-1] = PDFSnippetPart(previous.text + value, highlighted)
        else:
            parsed.append(PDFSnippetPart(value, highlighted))

    for character in text:
        if character == "\x01":
            flush()
            highlighted = True
        elif character == "\x02":
            flush()
            highlighted = False
        elif character == "\x03":
            flush()
        else:
            buffer.append(character)
    flush()
    if not parsed:
        return ()

    visible_length = sum(len(part.text) for part in parsed)
    center = 0
    cursor = 0
    for part in parsed:
        if part.highlighted:
            center = cursor
            break
        cursor += len(part.text)
    half = MAX_SNIPPET_CHARACTERS // 2
    start = max(center - half, 0)
    end = min(start + MAX_SNIPPET_CHARACTERS, visible_length)
    start = max(end - MAX_SNIPPET_CHARACTERS, 0)
    prefix = "…" if start or source_prefix else ""
    suffix = "…" if end < visible_length or source_suffix else ""

    parts: list[PDFSnippetPart] = []
    if prefix:
        parts.append(PDFSnippetPart(prefix))
    cursor = 0
    for part in parsed:
        part_end = cursor + len(part.text)
        overlap_start = max(start - cursor, 0)
        overlap_end = min(end - cursor, len(part.text))
        if overlap_start < overlap_end:
            value = part.text[overlap_start:overlap_end]
            parts.append(PDFSnippetPart(value, part.highlighted))
        cursor = part_end
        if cursor >= end:
            break
    if suffix:
        parts.append(PDFSnippetPart(suffix))
    return tuple(parts)
