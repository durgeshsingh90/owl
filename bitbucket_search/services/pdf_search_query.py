"""Validated, database-independent query state for local PDF search.

This module owns the public syntax accepted from the Bitbucket Search UI.  It
does not query Django models or construct SQL: callers receive immutable,
canonical state plus safely quoted FTS5 phrases for a dedicated search service.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, Self

MAX_SEARCH_INPUT_CHARACTERS = 4096
MAX_SEARCH_CHIPS = 32
MAX_SEARCH_PAGE_SIZE = 50
DEFAULT_SEARCH_PAGE_SIZE = 50
MAX_COMMITTER_FILTERS = 100
MAX_COMMITTER_NAME_CHARACTERS = 255


class InvalidPDFSearchQuery(ValueError):
    """Raised when PDF search state cannot be used safely or deterministically."""


class PDFSearchMatchMode(StrEnum):
    ALL = "all"
    ANY = "any"


class PDFSearchScope(StrEnum):
    CONTENT = "content"
    FILENAME = "filename"
    PATH = "path"
    REPOSITORY = "repository"
    NOTES = "notes"


DEFAULT_PDF_SEARCH_SCOPES = (
    PDFSearchScope.CONTENT,
    PDFSearchScope.FILENAME,
    PDFSearchScope.PATH,
    PDFSearchScope.REPOSITORY,
)


class PDFIndexState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    NO_TEXT = "no_text"
    PARTIAL = "partial"
    FAILED = "failed"
    STALE_ERROR = "stale_error"


class PDFSearchSort(StrEnum):
    RELEVANCE = "relevance"
    STARRED_FIRST = "starred_first"
    MOST_OPENED = "most_opened"
    LEAST_OPENED = "least_opened"
    RECENTLY_OPENED = "recently_opened"
    LEAST_RECENTLY_OPENED = "least_recently_opened"
    FILENAME_ASCENDING = "filename_ascending"
    FILENAME_DESCENDING = "filename_descending"
    GIT_UPDATED_NEWEST = "git_updated_newest"
    INDEXED_NEWEST = "indexed_newest"
    REPOSITORY_ASCENDING = "repository_ascending"


@dataclass(frozen=True, slots=True)
class PDFSearchChip:
    """One display-preserving, case-insensitive exact-phrase concept."""

    display: str
    key: str

    @classmethod
    def from_raw(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise InvalidPDFSearchQuery("Every PDF search chip must be text.")
        display = " ".join(unicodedata.normalize("NFKC", value).split())
        if not display:
            raise InvalidPDFSearchQuery("PDF search chips cannot be blank.")
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in display):
            raise InvalidPDFSearchQuery("PDF search chips cannot contain control characters.")
        return cls(display=display, key=display.casefold())

    @property
    def fts5_phrase(self) -> str:
        """Return a literal FTS5 phrase, never an operator-bearing expression."""

        return f'"{self.key.replace(chr(34), chr(34) * 2)}"'


@dataclass(frozen=True, slots=True)
class PDFSearchFilters:
    """Canonical filters that are safe to pass to the PDF search backend."""

    repository_ids: tuple[int, ...] = ()
    index_states: tuple[PDFIndexState, ...] = ()
    committer_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repository_ids",
            _positive_identifiers(self.repository_ids, "repository"),
        )
        object.__setattr__(
            self,
            "index_states",
            _enum_sequence(PDFIndexState, self.index_states, "index state"),
        )
        object.__setattr__(
            self,
            "committer_names",
            _canonical_committer_names(self.committer_names),
        )


@dataclass(frozen=True, slots=True)
class PDFSearchQuery:
    """Complete validated state consumed by the eventual FTS-backed service."""

    chips: tuple[PDFSearchChip | str, ...] = ()
    match_mode: PDFSearchMatchMode = PDFSearchMatchMode.ALL
    scopes: tuple[PDFSearchScope, ...] = DEFAULT_PDF_SEARCH_SCOPES
    filters: PDFSearchFilters = field(default_factory=PDFSearchFilters)
    sort: PDFSearchSort = PDFSearchSort.RELEVANCE
    page: int = 1
    page_size: int = DEFAULT_SEARCH_PAGE_SIZE

    def __post_init__(self) -> None:
        if isinstance(self.chips, (str, bytes)) or not isinstance(self.chips, Sequence):
            raise InvalidPDFSearchQuery("PDF search chips must be a list of text values.")
        if len(self.chips) > MAX_SEARCH_CHIPS:
            raise InvalidPDFSearchQuery(f"PDF search supports at most {MAX_SEARCH_CHIPS} chips.")

        raw_character_count = 0
        canonical: list[PDFSearchChip] = []
        seen: set[str] = set()
        for value in self.chips:
            if isinstance(value, PDFSearchChip):
                chip = PDFSearchChip.from_raw(value.display)
                raw_character_count += len(value.display)
            else:
                if not isinstance(value, str):
                    raise InvalidPDFSearchQuery("Every PDF search chip must be text.")
                raw_character_count += len(value)
                chip = PDFSearchChip.from_raw(value)
            if chip.key in seen:
                continue
            seen.add(chip.key)
            canonical.append(chip)

        canonical_character_count = sum(len(chip.display) for chip in canonical)
        if max(raw_character_count, canonical_character_count) > MAX_SEARCH_INPUT_CHARACTERS:
            raise InvalidPDFSearchQuery(
                f"PDF search input is limited to {MAX_SEARCH_INPUT_CHARACTERS} characters."
            )

        object.__setattr__(self, "chips", tuple(canonical))
        object.__setattr__(
            self,
            "match_mode",
            _enum_value(PDFSearchMatchMode, self.match_mode, "match mode"),
        )
        scopes = _enum_sequence(PDFSearchScope, self.scopes, "scope")
        if not scopes:
            raise InvalidPDFSearchQuery("Select at least one PDF search scope.")
        object.__setattr__(self, "scopes", scopes)
        if not isinstance(self.filters, PDFSearchFilters):
            raise InvalidPDFSearchQuery("PDF search filters must be PDFSearchFilters.")
        object.__setattr__(self, "sort", _enum_value(PDFSearchSort, self.sort, "sort"))
        object.__setattr__(self, "page", _bounded_integer(self.page, "page", minimum=1))
        object.__setattr__(
            self,
            "page_size",
            _bounded_integer(
                self.page_size,
                "page size",
                minimum=1,
                maximum=MAX_SEARCH_PAGE_SIZE,
            ),
        )

    @property
    def has_query(self) -> bool:
        return bool(self.chips)

    @property
    def repository_ids(self) -> tuple[int, ...]:
        return self.filters.repository_ids

    @property
    def index_states(self) -> tuple[PDFIndexState, ...]:
        return self.filters.index_states

    @property
    def committer_names(self) -> tuple[str, ...]:
        return self.filters.committer_names

    @property
    def fts5_chip_phrases(self) -> tuple[str, ...]:
        """Return one independently executable literal phrase per chip."""

        return tuple(chip.fts5_phrase for chip in self.chips)

    @property
    def fts5_match_expression(self) -> str:
        """Combine phrases for an FTS representation with one row per document.

        Page text uses one FTS row per PDF page, so document-wide ALL/ANY must
        intersect/union the independently executed ``fts5_chip_phrases`` by
        document ID instead of applying this combined expression to one page.
        """

        separator = " AND " if self.match_mode is PDFSearchMatchMode.ALL else " OR "
        return separator.join(self.fts5_chip_phrases)

    @classmethod
    def from_querydict(cls, values: QueryValues) -> Self:
        return parse_pdf_search_query(values)


class QueryValues(Protocol):
    """The small QueryDict surface used by ``parse_pdf_search_query``."""

    def __contains__(self, key: object) -> bool: ...

    def get(self, key: str, default: Any = None) -> Any: ...

    def getlist(self, key: str) -> list[Any]: ...


def parse_pdf_search_query(values: QueryValues | Mapping[str, Any]) -> PDFSearchQuery:
    """Parse canonical GET keys from a Django ``QueryDict`` or simple mapping.

    Repeated ``chip`` values are the canonical chip representation. ``q`` is a
    no-JavaScript/single-input fallback and is used only when ``chip`` is absent.
    Other accepted keys are ``match_mode``, repeated ``scope``, the
    ``scope_present`` form sentinel, repeated ``repository``, repeated
    ``index_state``, repeated ``committer``, ``sort``, ``page``, and
    ``page_size``. A bare request defaults every implemented scope; a submitted
    form whose sentinel is present but whose scope list is empty is rejected.
    """

    if not isinstance(values, Mapping):
        raise InvalidPDFSearchQuery("PDF search request data must be a query mapping.")

    if "chip" in values:
        raw_chips = tuple(_getlist(values, "chip"))
    else:
        fallback = _single_value(values, "q", "")
        raw_chips = (fallback,) if str(fallback).strip() else ()

    if "scope" in values:
        scopes: tuple[Any, ...] = tuple(value for value in _getlist(values, "scope") if value)
    elif "scope_present" in values:
        _single_value(values, "scope_present", "")
        scopes = ()
    else:
        scopes = DEFAULT_PDF_SEARCH_SCOPES

    repositories = tuple(
        _query_positive_integer(value, "repository")
        for value in _getlist(values, "repository")
        if value != ""
    )
    index_states = tuple(value for value in _getlist(values, "index_state") if value)
    committer_names = tuple(_getlist(values, "committer"))

    return PDFSearchQuery(
        chips=raw_chips,
        match_mode=_single_value(values, "match_mode", PDFSearchMatchMode.ALL),
        scopes=scopes,
        filters=PDFSearchFilters(
            repository_ids=repositories,
            index_states=index_states,
            committer_names=committer_names,
        ),
        sort=_single_value(values, "sort", PDFSearchSort.RELEVANCE),
        page=_query_positive_integer(_single_value(values, "page", "1"), "page"),
        page_size=_query_positive_integer(
            _single_value(values, "page_size", str(DEFAULT_SEARCH_PAGE_SIZE)),
            "page size",
        ),
    )


def _getlist(values: Mapping[str, Any], key: str) -> list[Any]:
    getlist = getattr(values, "getlist", None)
    if callable(getlist):
        return list(getlist(key))
    value = values.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _single_value(values: Mapping[str, Any], key: str, default: Any) -> Any:
    entries = _getlist(values, key)
    if not entries:
        return default
    if len(entries) != 1:
        raise InvalidPDFSearchQuery(f"PDF search {key.replace('_', ' ')} must occur once.")
    return entries[0]


def _positive_identifiers(values: Sequence[int], label: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidPDFSearchQuery(f"PDF search {label} filters must be a list.")
    canonical: list[int] = []
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InvalidPDFSearchQuery(
                f"Every PDF search {label} identifier must be a positive number."
            )
        if value not in seen:
            seen.add(value)
            canonical.append(value)
    return tuple(canonical)


def _canonical_committer_names(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidPDFSearchQuery("PDF search committer filters must be a list.")
    if len(values) > MAX_COMMITTER_FILTERS:
        raise InvalidPDFSearchQuery(
            f"PDF search supports at most {MAX_COMMITTER_FILTERS} committer filters."
        )

    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise InvalidPDFSearchQuery("Every PDF search committer must be text.")
        display = " ".join(unicodedata.normalize("NFKC", value).split())
        if not display:
            raise InvalidPDFSearchQuery("PDF search committers cannot be blank.")
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in display):
            raise InvalidPDFSearchQuery("PDF search committers cannot contain control characters.")
        if len(display) > MAX_COMMITTER_NAME_CHARACTERS:
            raise InvalidPDFSearchQuery(
                "PDF search committer names cannot exceed "
                f"{MAX_COMMITTER_NAME_CHARACTERS} characters."
            )
        key = display.casefold()
        if key in seen:
            continue
        seen.add(key)
        canonical.append(display)
    return tuple(canonical)


def _query_positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise InvalidPDFSearchQuery(f"PDF search {label} must be a positive number.")
    parsed = int(value)
    if parsed < 1:
        raise InvalidPDFSearchQuery(f"PDF search {label} must be a positive number.")
    return parsed


def _enum_sequence(enum_type, values: Sequence[Any], label: str) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidPDFSearchQuery(f"PDF search {label}s must be a list.")
    canonical: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        parsed = _enum_value(enum_type, value, label)
        if parsed not in seen:
            seen.add(parsed)
            canonical.append(parsed)
    return tuple(canonical)


def _enum_value(enum_type, value: Any, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPDFSearchQuery(f"Unknown PDF search {label}: {value!s}.") from exc


def _bounded_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidPDFSearchQuery(f"PDF search {label} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise InvalidPDFSearchQuery(f"PDF search {label} cannot exceed {maximum}.")
    return value
