from __future__ import annotations

import sqlite3

import pytest
from django.http import QueryDict

from bitbucket_search.services.pdf_search_query import (
    DEFAULT_PDF_SEARCH_SCOPES,
    MAX_COMMITTER_FILTERS,
    MAX_COMMITTER_NAME_CHARACTERS,
    MAX_SEARCH_CHIPS,
    MAX_SEARCH_INPUT_CHARACTERS,
    MAX_SEARCH_PAGE_SIZE,
    InvalidPDFSearchQuery,
    PDFIndexState,
    PDFSearchChip,
    PDFSearchFilters,
    PDFSearchMatchMode,
    PDFSearchQuery,
    PDFSearchScope,
    PDFSearchSort,
    parse_pdf_search_query,
)


def test_chips_preserve_display_case_after_nfkc_and_whitespace_normalization():
    query = PDFSearchQuery(
        chips=("  ＰＲＩＶＡＴＥ \n Link  ", "private link", "DDoS"),
    )

    assert [(chip.display, chip.key) for chip in query.chips] == [
        ("PRIVATE Link", "private link"),
        ("DDoS", "ddos"),
    ]


def test_blank_non_text_and_control_character_chips_fail_closed():
    with pytest.raises(InvalidPDFSearchQuery, match="cannot be blank"):
        PDFSearchQuery(chips=(" \t\n ",))
    with pytest.raises(InvalidPDFSearchQuery, match="must be text"):
        PDFSearchQuery(chips=(123,))  # type: ignore[arg-type]
    with pytest.raises(InvalidPDFSearchQuery, match="control characters"):
        PDFSearchQuery(chips=("private\x00link",))


def test_search_input_and_chip_count_are_bounded_before_deduplication():
    accepted = PDFSearchQuery(chips=("x" * MAX_SEARCH_INPUT_CHARACTERS,))
    assert len(accepted.chips[0].display) == MAX_SEARCH_INPUT_CHARACTERS

    with pytest.raises(InvalidPDFSearchQuery, match="4096 characters"):
        PDFSearchQuery(chips=("x" * (MAX_SEARCH_INPUT_CHARACTERS + 1),))
    with pytest.raises(InvalidPDFSearchQuery, match="at most 32 chips"):
        PDFSearchQuery(chips=tuple(f"chip-{index}" for index in range(MAX_SEARCH_CHIPS + 1)))
    with pytest.raises(InvalidPDFSearchQuery, match="4096 characters"):
        PDFSearchQuery(chips=("x" * 2100, "X" * 2100))


def test_default_match_mode_and_scopes_exclude_notes_but_notes_can_be_selected():
    default_query = PDFSearchQuery(chips=("Network",))
    notes_query = PDFSearchQuery(
        chips=("Network",),
        match_mode="any",
        scopes=("notes",),
    )

    assert default_query.match_mode is PDFSearchMatchMode.ALL
    assert default_query.scopes == DEFAULT_PDF_SEARCH_SCOPES
    assert PDFSearchScope.NOTES not in default_query.scopes
    assert notes_query.match_mode is PDFSearchMatchMode.ANY
    assert notes_query.scopes == (PDFSearchScope.NOTES,)


def test_scope_values_are_fixed_deduplicated_and_at_least_one_is_required():
    query = PDFSearchQuery(scopes=("filename", "content", "filename"))
    assert query.scopes == (PDFSearchScope.FILENAME, PDFSearchScope.CONTENT)

    with pytest.raises(InvalidPDFSearchQuery, match="at least one"):
        PDFSearchQuery(scopes=())
    with pytest.raises(InvalidPDFSearchQuery, match="Unknown PDF search scope"):
        PDFSearchQuery(scopes=("absolute_path",))  # type: ignore[arg-type]


def test_fts5_phrases_quote_user_syntax_and_only_owl_selects_boolean_operators():
    all_query = PDFSearchQuery(
        chips=('Private "Link" OR NEAR(secret*) ^ path:', "DDoS"),
    )
    any_query = PDFSearchQuery(
        chips=('Private "Link" OR NEAR(secret*) ^ path:', "DDoS"),
        match_mode=PDFSearchMatchMode.ANY,
    )

    assert all_query.chips[0].fts5_phrase == ('"private ""link"" or near(secret*) ^ path:"')
    assert all_query.fts5_chip_phrases == (
        '"private ""link"" or near(secret*) ^ path:"',
        '"ddos"',
    )
    assert all_query.fts5_match_expression == (
        '"private ""link"" or near(secret*) ^ path:" AND "ddos"'
    )
    assert any_query.fts5_match_expression.endswith(' OR "ddos"')

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE documents USING fts5(body)")
        connection.executemany(
            "INSERT INTO documents(body) VALUES (?)",
            [
                ("Private Link OR NEAR secret path",),
                ("secret",),
                ("DDoS",),
            ],
        )
        all_rows = connection.execute(
            "SELECT rowid FROM documents WHERE documents MATCH ?",
            (all_query.fts5_match_expression,),
        ).fetchall()
        any_rows = connection.execute(
            "SELECT rowid FROM documents WHERE documents MATCH ? ORDER BY rowid",
            (any_query.fts5_match_expression,),
        ).fetchall()
    finally:
        connection.close()

    assert all_rows == []
    assert any_rows == [(1,), (3,)]


def test_empty_query_has_no_fts_expression():
    query = PDFSearchQuery()

    assert query.has_query is False
    assert query.fts5_match_expression == ""


def test_repository_and_index_filters_are_typed_ordered_and_deduplicated():
    filters = PDFSearchFilters(
        repository_ids=(7, 3, 7),
        index_states=("ready", "stale_error", "ready"),
    )
    query = PDFSearchQuery(filters=filters)

    assert query.repository_ids == (7, 3)
    assert query.index_states == (PDFIndexState.READY, PDFIndexState.STALE_ERROR)

    with pytest.raises(InvalidPDFSearchQuery, match="positive number"):
        PDFSearchFilters(repository_ids=(0,))
    with pytest.raises(InvalidPDFSearchQuery, match="Unknown PDF search index state"):
        PDFSearchFilters(index_states=("invented",))  # type: ignore[arg-type]


def test_committer_filters_are_nfkc_normalized_and_case_insensitively_deduplicated():
    filters = PDFSearchFilters(
        committer_names=("  Ａlice \n Smith  ", "alice smith", "BOB"),
    )
    query = PDFSearchQuery(filters=filters)

    assert filters.committer_names == ("Alice Smith", "BOB")
    assert query.committer_names == ("Alice Smith", "BOB")


def test_committer_filters_reject_invalid_values_and_enforce_bounds():
    with pytest.raises(InvalidPDFSearchQuery, match="cannot be blank"):
        PDFSearchFilters(committer_names=(" \t\n ",))
    with pytest.raises(InvalidPDFSearchQuery, match="must be text"):
        PDFSearchFilters(committer_names=(123,))  # type: ignore[arg-type]
    with pytest.raises(InvalidPDFSearchQuery, match="control characters"):
        PDFSearchFilters(committer_names=("Alice\x00Smith",))
    with pytest.raises(InvalidPDFSearchQuery, match="cannot exceed 255 characters"):
        PDFSearchFilters(committer_names=("x" * (MAX_COMMITTER_NAME_CHARACTERS + 1),))
    with pytest.raises(InvalidPDFSearchQuery, match="at most 100 committer filters"):
        PDFSearchFilters(
            committer_names=tuple("Alice" for _index in range(MAX_COMMITTER_FILTERS + 1))
        )
    with pytest.raises(InvalidPDFSearchQuery, match="must be a list"):
        PDFSearchFilters(committer_names="Alice")  # type: ignore[arg-type]


@pytest.mark.parametrize("sort", tuple(PDFSearchSort))
def test_every_declared_sort_is_accepted(sort):
    assert PDFSearchQuery(sort=sort).sort is sort


def test_page_and_page_size_are_positive_and_page_size_is_capped_at_two_hundred():
    query = PDFSearchQuery(page=9, page_size=MAX_SEARCH_PAGE_SIZE)
    assert (query.page, query.page_size) == (9, 200)

    for values in ({"page": 0}, {"page_size": 0}, {"page_size": 201}):
        with pytest.raises(InvalidPDFSearchQuery):
            PDFSearchQuery(**values)
    with pytest.raises(InvalidPDFSearchQuery, match="Unknown PDF search sort"):
        PDFSearchQuery(sort="score_desc")  # type: ignore[arg-type]


def test_querydict_parser_reads_repeated_canonical_state():
    values = QueryDict(mutable=True)
    values.setlist("chip", [" Private   Link ", "DDoS", "private link"])
    values["q"] = "unfinished input is not a chip"
    values["match_mode"] = "any"
    values.setlist("scope", ["filename", "content", "filename"])
    values.setlist("repository", ["12", "3", "12"])
    values.setlist("index_state", ["ready", "no_text", "ready"])
    values.setlist("committer", [" Alice   Smith ", "BOB", "alice smith"])
    values["sort"] = "filename_ascending"
    values["page"] = "4"
    values["page_size"] = "25"

    query = PDFSearchQuery.from_querydict(values)

    assert [chip.display for chip in query.chips] == ["Private Link", "DDoS"]
    assert query.match_mode is PDFSearchMatchMode.ANY
    assert query.scopes == (PDFSearchScope.FILENAME, PDFSearchScope.CONTENT)
    assert query.repository_ids == (12, 3)
    assert query.index_states == (PDFIndexState.READY, PDFIndexState.NO_TEXT)
    assert query.committer_names == ("Alice Smith", "BOB")
    assert query.sort is PDFSearchSort.FILENAME_ASCENDING
    assert (query.page, query.page_size) == (4, 25)


def test_querydict_parser_uses_q_only_as_the_no_chip_fallback():
    query = parse_pdf_search_query(QueryDict("q=Private+Link"))

    assert [chip.display for chip in query.chips] == ["Private Link"]
    assert query.scopes == DEFAULT_PDF_SEARCH_SCOPES
    assert query.page == 1
    assert query.page_size == 200


def test_querydict_parser_rejects_explicit_empty_scopes_and_malformed_numbers():
    with pytest.raises(InvalidPDFSearchQuery, match="at least one"):
        parse_pdf_search_query(QueryDict("scope="))
    with pytest.raises(InvalidPDFSearchQuery, match="at least one"):
        parse_pdf_search_query(QueryDict("scope_present=1"))
    with pytest.raises(InvalidPDFSearchQuery, match="repository must be a positive number"):
        parse_pdf_search_query(QueryDict("repository=-1"))
    with pytest.raises(InvalidPDFSearchQuery, match="page must be a positive number"):
        parse_pdf_search_query(QueryDict("page=1.5"))
    with pytest.raises(InvalidPDFSearchQuery, match="cannot exceed 200"):
        parse_pdf_search_query(QueryDict("page_size=201"))


def test_bare_get_defaults_scopes_but_submitted_scope_sentinel_preserves_checked_values():
    bare_query = parse_pdf_search_query(QueryDict())
    submitted_query = parse_pdf_search_query(QueryDict("scope_present=1&scope=filename&scope=path"))

    assert bare_query.scopes == DEFAULT_PDF_SEARCH_SCOPES
    assert submitted_query.scopes == (PDFSearchScope.FILENAME, PDFSearchScope.PATH)


def test_querydict_parser_rejects_repeated_scalar_parameters():
    with pytest.raises(InvalidPDFSearchQuery, match="match mode must occur once"):
        parse_pdf_search_query(QueryDict("match_mode=all&match_mode=any"))


def test_simple_mapping_parser_supports_list_values_like_querydict():
    query = parse_pdf_search_query(
        {
            "chip": ["Alpha", "Omega"],
            "scope": ["path", "repository"],
            "repository": ["8", "5"],
        }
    )

    assert [chip.key for chip in query.chips] == ["alpha", "omega"]
    assert query.scopes == (PDFSearchScope.PATH, PDFSearchScope.REPOSITORY)
    assert query.repository_ids == (8, 5)


def test_public_chip_constructor_recanonicalizes_untrusted_manual_instances():
    untrusted = PDFSearchChip(display="  Private   Link  ", key="attacker supplied")
    query = PDFSearchQuery(chips=(untrusted,))

    assert query.chips == (PDFSearchChip(display="Private Link", key="private link"),)
