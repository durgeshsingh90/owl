from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bitbucket_search.services import pdf_search
from bitbucket_search.services.pdf_search import (
    MAX_SNIPPET_CHARACTERS,
    rebuild_search_index,
    search_documents,
    search_index_available,
)
from bitbucket_search.services.pdf_search_query import (
    PDFSearchFilters,
    PDFSearchMatchMode,
    PDFSearchQuery,
    PDFSearchScope,
)

pytestmark = pytest.mark.django_db


def _repository(name: str) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name.casefold()}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name.casefold()}.git",
    )


def _indexed_document(
    repository: BitbucketRepository,
    *,
    filename: str,
    path: str,
    pages: tuple[str, ...],
    digest: str,
) -> PDFDocument:
    content_digest = (digest * 64)[:64]
    git_blob_id = (digest * 40)[:40]
    revision = PDFTextRevision.objects.create(
        content_sha256=content_digest,
        extractor_version="test-extractor-v1",
        source_byte_size=sum(len(page.encode()) for page in pages),
        state=PDFTextRevisionState.READY,
        page_count=len(pages),
        extracted_character_count=sum(len(page) for page in pages),
    )
    PDFTextPage.objects.bulk_create(
        [
            PDFTextPage(
                revision=revision,
                page_number=number,
                extracted_text=text,
                character_count=len(text),
                extraction_state=(
                    PDFPageExtractionState.READY if text else PDFPageExtractionState.NO_TEXT
                ),
            )
            for number, text in enumerate(pages, start=1)
        ]
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=filename,
        relative_path=path,
        git_blob_id=git_blob_id,
        indexed_revision=revision,
        indexed_git_blob_id=git_blob_id,
        index_state=PDFIndexState.READY,
        page_count=len(pages),
        extracted_character_count=sum(len(page) for page in pages),
    )


def _set_last_committer(
    document: PDFDocument,
    *,
    committer_name: str,
    digest: str,
) -> GitCommit:
    commit = GitCommit.objects.create(
        repository=document.repository,
        commit_hash=(digest * 40)[:40],
        author_name=f"Author for {committer_name}",
        committer_name=committer_name,
        authored_at=timezone.now(),
        committed_at=timezone.now(),
    )
    document.last_commit = commit
    document.save(update_fields=("last_commit",))
    return commit


def test_fts_tables_and_triggers_search_metadata_and_page_text():
    networking = _repository("Networking")
    document = _indexed_document(
        networking,
        filename="Edge Architecture.pdf",
        path="docs/private-link/Edge Architecture.pdf",
        pages=("Network foundations", "Private Link and DDoS controls"),
        digest="a",
    )

    result = search_documents(PDFSearchQuery(chips=("Networking", "Edge", "Private Link", "DDoS")))

    assert search_index_available() is True
    assert result.total == 1
    hit = result.results[0]
    assert hit.document == document
    assert hit.best_page_number == 2
    explanations = {item.chip: item for item in hit.explanations}
    assert explanations["Networking"].scopes == ("Repository",)
    assert explanations["Edge"].scopes == ("Filename", "Path")
    assert explanations["Private Link"].page_numbers == (2,)
    assert explanations["DDoS"].page_numbers == (2,)
    assert "".join(part.text for part in hit.snippet) == "Private Link and DDoS controls"
    assert {part.text for part in hit.snippet if part.highlighted} == {
        "Private Link",
        "DDoS",
    }


def test_metadata_fts_tracks_document_and_repository_renames():
    repository = _repository("Original Workspace")
    document = _indexed_document(
        repository,
        filename="Original.pdf",
        path="docs/Original.pdf",
        pages=("content",),
        digest="5",
    )

    document.filename = "Renamed.pdf"
    document.relative_path = "moved/Renamed.pdf"
    document.save(update_fields=("filename", "relative_path"))
    repository.display_name = "Renamed Workspace"
    repository.save(update_fields=("display_name", "updated_at"))

    assert search_documents(PDFSearchQuery(chips=("Original.pdf",))).total == 0
    renamed = search_documents(PDFSearchQuery(chips=("Renamed Workspace", "Renamed.pdf")))
    assert [hit.document.pk for hit in renamed.results] == [document.pk]


def test_default_search_unions_extracted_text_filename_and_repo_path_without_reading_files(
    monkeypatch,
):
    repository = _repository("Search sources")
    content_match = _indexed_document(
        repository,
        filename="Overview.pdf",
        path="docs/Overview.pdf",
        pages=("The aurora deployment uses redundant gateways.",),
        digest="a",
    )
    filename_match = _indexed_document(
        repository,
        filename="Aurora Guide.pdf",
        path="docs/Aurora Guide.pdf",
        pages=("Unrelated body",),
        digest="b",
    )
    path_match = _indexed_document(
        repository,
        filename="Design.pdf",
        path="services/aurora/Design.pdf",
        pages=("Unrelated description",),
        digest="c",
    )
    _indexed_document(
        repository,
        filename="Other.pdf",
        path="docs/Other.pdf",
        pages=("No matching term",),
        digest="d",
    )

    def no_pdf_read(*args, **kwargs):
        pytest.fail("Search must use published database text, not read local PDF files")

    monkeypatch.setattr(Path, "open", no_pdf_read)
    result = search_documents(PDFSearchQuery(chips=("AURORA",)))
    hits = {hit.document.pk: hit for hit in result.results}

    assert result.total == 3
    assert set(hits) == {content_match.pk, filename_match.pk, path_match.pk}
    assert hits[content_match.pk].explanations[0].scopes == ("PDF content",)
    assert hits[filename_match.pk].explanations[0].scopes == ("Filename", "Path")
    assert hits[path_match.pk].explanations[0].scopes == ("Path",)


def test_default_all_chips_can_match_different_sources_on_the_same_pdf():
    repository = _repository("Cross-field search")
    expected = _indexed_document(
        repository,
        filename="Gateway Guide.pdf",
        path="platform/payments/Gateway Guide.pdf",
        pages=("Fencing prevents split brain.", "Quorum controls failover."),
        digest="a",
    )
    _indexed_document(
        repository,
        filename="Other.pdf",
        path="platform/payments/Other.pdf",
        pages=("Fencing prevents split brain.",),
        digest="b",
    )
    result = search_documents(
        PDFSearchQuery(chips=("gateway", "platform/payments", "split brain", "quorum"))
    )
    assert [hit.document.pk for hit in result.results] == [expected.pk]


@pytest.mark.parametrize("state", [PDFIndexState.PENDING, PDFIndexState.FAILED])
def test_filename_and_repo_path_remain_searchable_before_text_is_available(state):
    repository = _repository("Unindexed inventory")
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Gateway Guide.pdf",
        relative_path="platform/payments/Gateway Guide.pdf",
        index_state=state,
    )
    result = search_documents(PDFSearchQuery(chips=("gateway", "platform/payments")))
    assert [hit.document.pk for hit in result.results] == [document.pk]
    assert result.results[0].best_page_number is None


def test_exact_phrase_all_any_and_scopes_are_document_level():
    repository = _repository("Cloud Platform")
    first = _indexed_document(
        repository,
        filename="Private Link Guide.pdf",
        path="security/Private Link Guide.pdf",
        pages=("Network appears here", "Edge appears on another page"),
        digest="b",
    )
    _indexed_document(
        repository,
        filename="Private Network Notes.pdf",
        path="other/Private Network Notes.pdf",
        pages=("Link is separated from private",),
        digest="c",
    )

    phrase = search_documents(
        PDFSearchQuery(
            chips=("Private Link",),
            scopes=(PDFSearchScope.FILENAME,),
        )
    )
    across_pages = search_documents(
        PDFSearchQuery(
            chips=("Network", "Edge"),
            scopes=(PDFSearchScope.CONTENT,),
        )
    )
    any_result = search_documents(
        PDFSearchQuery(
            chips=("does not exist", "Edge"),
            match_mode=PDFSearchMatchMode.ANY,
            scopes=(PDFSearchScope.CONTENT,),
        )
    )

    assert [hit.document for hit in phrase.results] == [first]
    assert [hit.document for hit in across_pages.results] == [first]
    assert [hit.document for hit in any_result.results] == [first]


@pytest.mark.parametrize(
    ("chip", "expected"),
    (("NEAR(alpha beta)", 0), ("alpha OR beta", 0), ('" alpha *', 1)),
)
def test_fts_operators_and_quotes_remain_literal_data(chip, expected):
    repository = _repository(f"Safe-{abs(hash(chip))}")
    _indexed_document(
        repository,
        filename="Operators.pdf",
        path="docs/Operators.pdf",
        pages=("alpha beta",),
        digest=hex(abs(hash(chip)) % 16)[2:],
    )

    result = search_documents(PDFSearchQuery(chips=(chip,)))

    # Punctuation is tokenized as data: the final value may match the literal
    # ``alpha`` token, but neither ``*`` nor the quote becomes FTS syntax.
    assert result.total == expected


def test_removed_and_disabled_documents_are_excluded_and_filters_apply():
    first_repository = _repository("First")
    second_repository = _repository("Second")
    active = _indexed_document(
        first_repository,
        filename="Active.pdf",
        path="docs/Active.pdf",
        pages=("shared needle",),
        digest="d",
    )
    removed = _indexed_document(
        second_repository,
        filename="Removed.pdf",
        path="docs/Removed.pdf",
        pages=("shared needle",),
        digest="e",
    )
    PDFDocument.objects.filter(pk=removed.pk).update(lifecycle_state=PDFDocumentLifecycle.REMOVED)

    result = search_documents(
        PDFSearchQuery(
            chips=("shared",),
            filters=PDFSearchFilters(
                repository_ids=(first_repository.pk,),
                index_states=(PDFIndexState.READY,),
            ),
        )
    )

    assert [hit.document for hit in result.results] == [active]


def test_repository_and_index_filters_work_without_a_text_chip():
    first_repository = _repository("Filter First")
    second_repository = _repository("Filter Second")
    selected = _indexed_document(
        first_repository,
        filename="Selected.pdf",
        path="docs/Selected.pdf",
        pages=("content",),
        digest="3",
    )
    _indexed_document(
        second_repository,
        filename="Other.pdf",
        path="docs/Other.pdf",
        pages=("content",),
        digest="4",
    )

    result = search_documents(
        PDFSearchQuery(
            filters=PDFSearchFilters(
                repository_ids=(first_repository.pk,),
                index_states=(PDFIndexState.READY,),
            )
        )
    )

    assert [hit.document for hit in result.results] == [selected]


def test_committer_filter_without_text_uses_case_insensitive_or_and_excludes_unknown():
    repository = _repository("Committer Browse")
    alice_document = _indexed_document(
        repository,
        filename="Alice.pdf",
        path="docs/Alice.pdf",
        pages=("content",),
        digest="a",
    )
    bob_document = _indexed_document(
        repository,
        filename="Bob.pdf",
        path="docs/Bob.pdf",
        pages=("content",),
        digest="b",
    )
    no_commit_document = _indexed_document(
        repository,
        filename="Unknown.pdf",
        path="docs/Unknown.pdf",
        pages=("content",),
        digest="c",
    )
    _set_last_committer(alice_document, committer_name="Alice Smith", digest="1")
    _set_last_committer(bob_document, committer_name="Bob Jones", digest="2")

    result = search_documents(
        PDFSearchQuery(
            filters=PDFSearchFilters(
                committer_names=("ＡＬＩＣＥ smith", "BOB JONES"),
            )
        )
    )

    assert {hit.document for hit in result.results} == {alice_document, bob_document}
    assert no_commit_document not in {hit.document for hit in result.results}


def test_committer_filter_is_applied_before_fts_text_matching():
    repository = _repository("Committer Search")
    alice_document = _indexed_document(
        repository,
        filename="Alice Search.pdf",
        path="docs/Alice Search.pdf",
        pages=("shared searchable phrase",),
        digest="d",
    )
    bob_document = _indexed_document(
        repository,
        filename="Bob Search.pdf",
        path="docs/Bob Search.pdf",
        pages=("shared searchable phrase",),
        digest="e",
    )
    no_commit_document = _indexed_document(
        repository,
        filename="Unknown Search.pdf",
        path="docs/Unknown Search.pdf",
        pages=("shared searchable phrase",),
        digest="f",
    )
    _set_last_committer(alice_document, committer_name="Alice Smith", digest="3")
    _set_last_committer(bob_document, committer_name="Bob Jones", digest="4")

    with CaptureQueriesContext(connection) as captured:
        result = search_documents(
            PDFSearchQuery(
                chips=("shared searchable",),
                filters=PDFSearchFilters(committer_names=("bob jones",)),
            )
        )

    assert [hit.document for hit in result.results] == [bob_document]
    assert alice_document not in {hit.document for hit in result.results}
    assert no_commit_document not in {hit.document for hit in result.results}
    candidate_sql = next(
        query["sql"]
        for query in captured.captured_queries
        if "eligible_documents AS MATERIALIZED" in query["sql"]
        and "COUNT(*) OVER () AS total_count" in query["sql"]
    )
    assert "owl_pdf_normalize(last_commit.committer_name) IN (" in candidate_sql


def test_exact_filename_outranks_content_and_snippet_is_bounded_and_plain_text():
    repository = _repository("Ranking")
    exact = _indexed_document(
        repository,
        filename="Edge.pdf",
        path="docs/Edge.pdf",
        pages=("ordinary content",),
        digest="f",
    )
    body = _indexed_document(
        repository,
        filename="Other.pdf",
        path="docs/Other.pdf",
        pages=("x" * 300 + " <script>Edge</script> " + "y" * 300,),
        digest="1",
    )

    result = search_documents(PDFSearchQuery(chips=("Edge",)))

    assert [hit.document for hit in result.results] == [exact, body]
    snippet = result.results[1].snippet
    assert sum(len(part.text) for part in snippet) <= MAX_SNIPPET_CHARACTERS + 2
    assert "<script>" in "".join(part.text for part in snippet)
    assert all(not hasattr(part, "__html__") for part in snippet)


def test_search_never_opens_a_pdf_and_rebuild_restores_derived_rows(monkeypatch):
    repository = _repository("Local Only")
    document = _indexed_document(
        repository,
        filename="Local.pdf",
        path="docs/Local.pdf",
        pages=("local searchable text",),
        digest="2",
    )
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("opened a file"))

    rebuild_search_index()
    result = search_documents(PDFSearchQuery(chips=("searchable",)))

    assert [hit.document for hit in result.results] == [document]


def test_common_term_search_never_uses_unbounded_cursor_fetchall(monkeypatch):
    repository = _repository("Broad Terms")
    for number in range(65):
        _indexed_document(
            repository,
            filename=f"Broad-{number:03d}.pdf",
            path=f"docs/Broad-{number:03d}.pdf",
            pages=tuple(f"common term page {page}" for page in range(1, 13)),
            digest=f"{number + 100:064x}",
        )

    real_cursor = pdf_search.connection.cursor
    fetchmany_sizes: list[int | None] = []

    class BoundedCursor(AbstractContextManager):
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def __iter__(self):
            return iter(self.cursor)

        def __enter__(self):
            self.cursor.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.cursor.__exit__(exc_type, exc_value, traceback)

        def fetchall(self):
            pytest.fail("PDF search must not fetch an unbounded result set")

        def fetchmany(self, size=None):
            fetchmany_sizes.append(size)
            return self.cursor.fetchmany(size)

    monkeypatch.setattr(
        pdf_search.connection,
        "cursor",
        lambda *args, **kwargs: BoundedCursor(real_cursor(*args, **kwargs)),
    )

    first_query = PDFSearchQuery(chips=("common term",), page=1, page_size=50)
    candidate_ctes, _parameters = pdf_search._candidate_ctes(first_query)
    assert "content_document_hits AS MATERIALIZED" in candidate_ctes
    assert "GROUP BY eligible.document_id" in candidate_ctes
    assert "content_page_hits AS MATERIALIZED" not in candidate_ctes

    first = search_documents(first_query)
    second = search_documents(PDFSearchQuery(chips=("common term",), page=2, page_size=50))
    out_of_range = search_documents(PDFSearchQuery(chips=("common term",), page=3, page_size=50))

    assert first.total == second.total == out_of_range.total == 65
    assert len(first.results) == 50
    assert len(second.results) == 15
    assert out_of_range.results == ()
    assert out_of_range.page_count == 2
    assert [hit.document.filename for hit in first.results[:2]] == [
        "Broad-000.pdf",
        "Broad-001.pdf",
    ]
    assert second.results[0].document.filename == "Broad-050.pdf"
    assert fetchmany_sizes
    assert all(size is not None for size in fetchmany_sizes)


def test_snippet_loader_queries_only_exact_revision_page_targets():
    repository = _repository("Exact Snippet")
    document = _indexed_document(
        repository,
        filename="Many Pages.pdf",
        path="docs/Many Pages.pdf",
        pages=tuple(f"page {number}" for number in range(1, 31)),
        digest="6",
    )

    with CaptureQueriesContext(connection) as captured:
        text = pdf_search._load_best_page_text(
            (document,),
            best_pages={document.pk: 23},
            query=PDFSearchQuery(chips=("page 23",)),
        )

    assert text[document.pk].text == "\x01page 23\x02"
    assert text[document.pk].has_source_prefix is False
    assert text[document.pk].has_source_suffix is False
    assert len(captured) == 1
    sql = captured.captured_queries[0]["sql"]
    assert "target_pages(revision_id, page_number)" in sql
    assert "raw_snippets AS NOT MATERIALIZED" in sql
    assert "LIMIT -1 OFFSET 0" in sql
    assert "snippet(" in sql
    assert "SUBSTR(marked_text, marked_start, 480)" in sql
    assert "CROSS JOIN" not in sql
    assert "page.extracted_text" not in sql


def test_huge_page_snippet_fetch_and_python_processing_remain_bounded(monkeypatch):
    repository = _repository("Huge Snippet")
    page_text = "x" * 550_000 + " Needle Phrase " + "y" * 550_000
    document = _indexed_document(
        repository,
        filename="Huge.pdf",
        path="docs/Huge.pdf",
        pages=(page_text,),
        digest="e",
    )
    observed_source_lengths: list[int] = []
    original_snippet_parts = pdf_search._snippet_parts

    def guarded_snippet_parts(text, **kwargs):
        observed_source_lengths.append(len(text))
        assert len(text) <= pdf_search.MAX_SNIPPET_SOURCE_CHARACTERS
        return original_snippet_parts(text, **kwargs)

    monkeypatch.setattr(pdf_search, "_snippet_parts", guarded_snippet_parts)

    result = search_documents(
        PDFSearchQuery(chips=("Needle Phrase",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert [hit.document for hit in result.results] == [document]
    assert observed_source_lengths == [pdf_search.MAX_SNIPPET_SOURCE_CHARACTERS]
    snippet = result.results[0].snippet
    assert {part.text for part in snippet if part.highlighted} == {"Needle Phrase"}
    assert sum(len(part.text) for part in snippet) <= MAX_SNIPPET_CHARACTERS + 2
    assert snippet[0].text == "…"
    assert snippet[-1].text == "…"


def test_fts_snippet_preserves_unicode_case_and_diacritic_highlighting():
    repository = _repository("Unicode Snippet")
    document = _indexed_document(
        repository,
        filename="Unicode.pdf",
        path="docs/Unicode.pdf",
        pages=("Intro RÉSUMÉ CAFÉ naïve outro",),
        digest="f",
    )

    result = search_documents(
        PDFSearchQuery(chips=("resume cafe",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert [hit.document for hit in result.results] == [document]
    assert "".join(part.text for part in result.results[0].snippet) == (
        "Intro RÉSUMÉ CAFÉ naïve outro"
    )
    assert [part.text for part in result.results[0].snippet if part.highlighted] == ["RÉSUMÉ CAFÉ"]


def test_fts_snippet_keeps_markers_for_an_eleven_token_exact_phrase():
    repository = _repository("Long Phrase Snippet")
    phrase = " ".join(f"token{number}" for number in range(1, 12))
    document = _indexed_document(
        repository,
        filename="Long Phrase.pdf",
        path="docs/Long Phrase.pdf",
        pages=(f"before {phrase} after",),
        digest="d",
    )

    result = search_documents(PDFSearchQuery(chips=(phrase,), scopes=(PDFSearchScope.CONTENT,)))

    assert [hit.document for hit in result.results] == [document]
    assert [part.text for part in result.results[0].snippet if part.highlighted] == [phrase]


def test_many_chip_huge_page_snippet_uses_one_rowid_constrained_fts_lookup():
    repository = _repository("Many Chip Snippet")
    chips = tuple(f"needle{number:02d}" for number in range(32))
    page_text = "x" * 550_000 + " " + " ".join(chips) + " " + "y" * 550_000
    document = _indexed_document(
        repository,
        filename="Many Chips.pdf",
        path="docs/Many Chips.pdf",
        pages=(page_text,),
        digest="0",
    )
    captured: list[tuple[str, tuple[object, ...]]] = []

    def capture_snippet_sql(execute, sql, params, many, context):
        if "raw_snippets AS NOT MATERIALIZED" in sql:
            captured.append((sql, tuple(params)))
        return execute(sql, params, many, context)

    query = PDFSearchQuery(chips=chips, scopes=(PDFSearchScope.CONTENT,))
    with connection.execute_wrapper(capture_snippet_sql):
        snippets = pdf_search._load_best_page_text(
            (document,),
            best_pages={document.pk: 1},
            query=query,
        )

    assert len(snippets[document.pk].text) <= pdf_search.MAX_SNIPPET_SOURCE_CHARACTERS
    assert len(captured) == 1
    sql, parameters = captured[0]
    assert sql.count(f"{pdf_search.PAGE_FTS_TABLE} MATCH") == 1
    assert sql.count("snippet(") == 1
    assert len(parameters) == 7
    assert parameters[2] == " OR ".join(query.fts5_chip_phrases)
    assert "target_pages(revision_id, page_number)" in sql
    assert f"{pdf_search.PAGE_FTS_TABLE}.rowid = page.id" in sql
    assert "CROSS JOIN" not in sql
    assert "search_chips" not in sql
    assert "LOWER(" not in sql
    assert "INSTR(page.extracted_text" not in sql

    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
        plan = "\n".join(str(row[3]) for row in cursor.fetchall())
        cursor.execute(f"EXPLAIN {sql}", parameters)
        bytecode = cursor.fetchall()
    assert "CO-ROUTINE raw_snippets" in plan
    assert "MATERIALIZE raw_snippets" not in plan
    assert "USE TEMP B-TREE" not in plan
    assert "VIRTUAL TABLE INDEX" in plan
    assert "INDEX 0:=M1" in plan
    snippet_opcodes = [
        row
        for row in bytecode
        if row[1] == "Function" and str(row[5]).casefold().startswith("snippet(")
    ]
    assert len(snippet_opcodes) == 1


def test_page_explanations_are_capped_without_losing_match_count_or_best_page():
    repository = _repository("Page Bounds")
    document = _indexed_document(
        repository,
        filename="Repeated.pdf",
        path="docs/Repeated.pdf",
        pages=tuple(f"repeated needle on page {number}" for number in range(1, 31)),
        digest="7",
    )

    result = search_documents(
        PDFSearchQuery(chips=("repeated needle",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert [hit.document for hit in result.results] == [document]
    hit = result.results[0]
    assert hit.best_page_number == 1
    assert hit.explanations[0].page_numbers == tuple(range(1, 9))
    assert hit.explanations[0].pages_truncated is True
    assert "page 1" in "".join(part.text for part in hit.snippet)


def test_shared_revision_snippet_and_content_match_are_returned_for_every_document():
    repository = _repository("Shared Revision")
    first = _indexed_document(
        repository,
        filename="First.pdf",
        path="docs/First.pdf",
        pages=("shared revision phrase",),
        digest="8",
    )
    second = PDFDocument.objects.create(
        repository=repository,
        filename="Second.pdf",
        relative_path="docs/Second.pdf",
        indexed_revision=first.indexed_revision,
        indexed_git_blob_id=first.indexed_git_blob_id,
        index_state=PDFIndexState.READY,
        page_count=1,
        extracted_character_count=first.extracted_character_count,
    )

    result = search_documents(
        PDFSearchQuery(chips=("shared revision",), scopes=(PDFSearchScope.CONTENT,))
    )

    assert {hit.document for hit in result.results} == {first, second}
    assert all(hit.best_page_number == 1 for hit in result.results)
    assert all(
        "".join(part.text for part in hit.snippet) == "shared revision phrase"
        for hit in result.results
    )


def test_candidate_sql_applies_repository_and_index_filters_before_fts_matching():
    selected_repository = _repository("Early Selected")
    other_repository = _repository("Early Other")
    selected = _indexed_document(
        selected_repository,
        filename="Selected.pdf",
        path="docs/Selected.pdf",
        pages=("early filter phrase",),
        digest="9",
    )
    _indexed_document(
        other_repository,
        filename="Other.pdf",
        path="docs/Other.pdf",
        pages=("early filter phrase",),
        digest="a",
    )

    with CaptureQueriesContext(connection) as captured:
        result = search_documents(
            PDFSearchQuery(
                chips=("early filter",),
                filters=PDFSearchFilters(
                    repository_ids=(selected_repository.pk,),
                    index_states=(PDFIndexState.READY,),
                ),
            )
        )

    assert [hit.document for hit in result.results] == [selected]
    candidate_sql = next(
        query["sql"]
        for query in captured.captured_queries
        if "eligible_documents AS MATERIALIZED" in query["sql"]
        and "COUNT(*) OVER () AS total_count" in query["sql"]
    )
    assert "document.repository_id IN (" in candidate_sql
    assert "document.index_state IN (" in candidate_sql
