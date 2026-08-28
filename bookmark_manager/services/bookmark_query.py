"""Local bookmark discovery without contacting Confluence or rewriting the tree.

The service is the shared boundary for the Bookmark Manager tree and flat results
views.  It deliberately returns immutable collections of model instances and node
identifiers: filtering and sorting never update ``ConfluencePageNode.parent``.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Self

from django.db.models import F, Q, QuerySet
from django.db.models.functions import Lower
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkRecency,
    ConfluencePageNode,
    SavedBookmarkView,
    Tag,
)
from bookmark_manager.services.confluence_validation import extract_page_id_from_url
from bookmark_manager.services.web_bookmarks import WebBookmarkError, canonicalize_web_url


class InvalidBookmarkQuery(ValueError):
    """Raised when a request or saved view contains an invalid query value."""


class BookmarkSort(StrEnum):
    ADDED_NEWEST = "added_newest"
    ADDED_OLDEST = "added_oldest"
    UPDATED_NEWEST = "updated_newest"
    UPDATED_OLDEST = "updated_oldest"
    CREATED_NEWEST = "created_newest"
    CREATED_OLDEST = "created_oldest"
    TITLE_ASCENDING = "title_ascending"
    TITLE_DESCENDING = "title_descending"
    AUTHOR_ASCENDING = "author_ascending"
    FAVORITES_FIRST = "favorites_first"
    PINNED_FIRST = "pinned_first"
    MOST_OPENED = "most_opened"
    LEAST_OPENED = "least_opened"
    RECENTLY_OPENED = "recently_opened"
    LEAST_RECENTLY_OPENED = "least_recently_opened"
    RECENTLY_REFRESHED = "recently_refreshed"


SORT_LABELS: Mapping[BookmarkSort, str] = {
    BookmarkSort.ADDED_NEWEST: "Added newest",
    BookmarkSort.ADDED_OLDEST: "Added oldest",
    BookmarkSort.UPDATED_NEWEST: "Updated newest",
    BookmarkSort.UPDATED_OLDEST: "Updated oldest",
    BookmarkSort.CREATED_NEWEST: "Created newest",
    BookmarkSort.CREATED_OLDEST: "Created oldest",
    BookmarkSort.TITLE_ASCENDING: "Title A–Z",
    BookmarkSort.TITLE_DESCENDING: "Title Z–A",
    BookmarkSort.AUTHOR_ASCENDING: "Author A–Z",
    BookmarkSort.FAVORITES_FIRST: "Favorites first",
    BookmarkSort.PINNED_FIRST: "Pinned first",
    BookmarkSort.MOST_OPENED: "Most opened",
    BookmarkSort.LEAST_OPENED: "Least opened",
    BookmarkSort.RECENTLY_OPENED: "Recently opened",
    BookmarkSort.LEAST_RECENTLY_OPENED: "Least recently opened",
    BookmarkSort.RECENTLY_REFRESHED: "Recently refreshed",
}


class BookmarkDateField(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    ADDED = "added"
    REFRESHED = "refreshed"
    VIEWED = "viewed"


DATE_FIELD_LABELS: Mapping[BookmarkDateField, str] = {
    BookmarkDateField.CREATED: "Created",
    BookmarkDateField.UPDATED: "Updated",
    BookmarkDateField.ADDED: "Added",
    BookmarkDateField.REFRESHED: "Refreshed",
    BookmarkDateField.VIEWED: "Viewed",
}


class DatePreset(StrEnum):
    ANY_TIME = "any_time"
    TODAY = "today"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_3_MONTHS = "last_3_months"
    LAST_6_MONTHS = "last_6_months"
    THIS_YEAR = "this_year"
    LAST_YEAR = "last_year"
    OLDER = "older"
    CUSTOM_RANGE = "custom_range"


DATE_PRESET_LABELS: Mapping[DatePreset, str] = {
    DatePreset.ANY_TIME: "Any Time",
    DatePreset.TODAY: "Today",
    DatePreset.LAST_7_DAYS: "Last 7 Days",
    DatePreset.LAST_30_DAYS: "Last 30 Days",
    DatePreset.LAST_3_MONTHS: "Last 3 Months",
    DatePreset.LAST_6_MONTHS: "Last 6 Months",
    DatePreset.THIS_YEAR: "This Year",
    DatePreset.LAST_YEAR: "Last Year",
    DatePreset.OLDER: "Older",
    DatePreset.CUSTOM_RANGE: "Custom Range",
}


_DATE_MODEL_FIELDS: Mapping[BookmarkDateField, str] = {
    BookmarkDateField.CREATED: "created_at",
    BookmarkDateField.UPDATED: "updated_at",
    BookmarkDateField.ADDED: "saved_at",
    BookmarkDateField.REFRESHED: "last_refreshed_at",
    BookmarkDateField.VIEWED: "last_viewed_at",
}


@dataclass(frozen=True, slots=True)
class BookmarkDateFilter:
    field: BookmarkDateField
    preset: DatePreset
    start: date | datetime | None = None
    end: date | datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", _enum_value(BookmarkDateField, self.field, "date field"))
        object.__setattr__(self, "preset", _enum_value(DatePreset, self.preset, "date preset"))
        if self.preset == DatePreset.CUSTOM_RANGE:
            if self.start is None and self.end is None:
                raise InvalidBookmarkQuery("A custom date range needs a start or end date.")
        elif self.start is not None or self.end is not None:
            raise InvalidBookmarkQuery("Start and end dates are only valid for a custom range.")

        for label, value in (("start", self.start), ("end", self.end)):
            if isinstance(value, datetime) and timezone.is_naive(value):
                raise InvalidBookmarkQuery(f"The custom {label} timestamp must include a timezone.")
            if value is not None and not isinstance(value, date):
                raise InvalidBookmarkQuery(f"The custom {label} value must be a date.")


@dataclass(frozen=True, slots=True)
class BookmarkQuery:
    """Validated local search/filter/sort state, suitable for a saved view."""

    search: str = ""
    favorite: bool | None = None
    pinned: bool | None = None
    tags: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    spaces: tuple[str, ...] = ()
    category_ids: tuple[int, ...] = ()
    availability: tuple[str, ...] = ()
    recency: tuple[BookmarkRecency, ...] = ()
    changed_since_viewed: bool | None = None
    dates: tuple[BookmarkDateFilter, ...] = ()
    open_count_min: int | None = None
    open_count_max: int | None = None
    recently_changed_days: int | None = None
    broken: bool | None = None
    sort: BookmarkSort = BookmarkSort.ADDED_NEWEST

    def __post_init__(self) -> None:
        search = str(self.search).strip()
        if len(search) > 4096:
            raise InvalidBookmarkQuery("Bookmark search is limited to 4096 characters.")
        object.__setattr__(self, "search", search)
        object.__setattr__(self, "favorite", _optional_boolean(self.favorite, "favorite"))
        object.__setattr__(self, "pinned", _optional_boolean(self.pinned, "pinned"))
        object.__setattr__(
            self,
            "changed_since_viewed",
            _optional_boolean(self.changed_since_viewed, "changed since viewed"),
        )
        object.__setattr__(self, "broken", _optional_boolean(self.broken, "broken"))
        object.__setattr__(
            self,
            "tags",
            _clean_text_values(self.tags, "tags", normalizer=_canonical_tag_display),
        )
        object.__setattr__(self, "people", _clean_text_values(self.people, "people"))
        object.__setattr__(self, "spaces", _clean_text_values(self.spaces, "spaces"))
        if isinstance(self.category_ids, (str, bytes)) or not isinstance(
            self.category_ids, Iterable
        ):
            raise InvalidBookmarkQuery("Categories must be a list of identifiers.")
        category_ids = tuple(dict.fromkeys(self.category_ids))
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in category_ids
        ):
            raise InvalidBookmarkQuery("Every category identifier must be a positive number.")
        object.__setattr__(self, "category_ids", category_ids)
        object.__setattr__(
            self,
            "availability",
            _choice_values(BookmarkAvailability, self.availability, "availability"),
        )
        object.__setattr__(
            self,
            "recency",
            _choice_values(BookmarkRecency, self.recency, "recency"),
        )
        if isinstance(self.dates, (str, bytes)) or not isinstance(self.dates, Iterable):
            raise InvalidBookmarkQuery("Dates must be a list of date filters.")
        date_filters: list[BookmarkDateFilter] = []
        for value in self.dates:
            if isinstance(value, BookmarkDateFilter):
                date_filter = value
            elif isinstance(value, Mapping):
                try:
                    date_filter = BookmarkDateFilter(**value)
                except TypeError as exc:
                    raise InvalidBookmarkQuery("A date filter has an invalid shape.") from exc
            else:
                raise InvalidBookmarkQuery("Every date filter must be an object.")
            if date_filter.preset != DatePreset.ANY_TIME:
                date_filters.append(date_filter)
        object.__setattr__(self, "dates", tuple(date_filters))
        object.__setattr__(self, "sort", _enum_value(BookmarkSort, self.sort, "sort"))

        for name in ("open_count_min", "open_count_max"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise InvalidBookmarkQuery(
                    f"{name.replace('_', ' ').title()} must be zero or greater."
                )
        if (
            self.open_count_min is not None
            and self.open_count_max is not None
            and self.open_count_min > self.open_count_max
        ):
            raise InvalidBookmarkQuery("Minimum open count cannot exceed maximum open count.")
        if self.recently_changed_days not in (None, 7, 30):
            raise InvalidBookmarkQuery("Recently changed supports 7 or 30 days.")

    def to_filter_dict(self) -> dict[str, Any]:
        """Return the JSON-safe filter payload stored by ``SavedBookmarkView``."""

        return {
            "favorite": self.favorite,
            "pinned": self.pinned,
            "tags": list(self.tags),
            "people": list(self.people),
            "spaces": list(self.spaces),
            "category_ids": list(self.category_ids),
            "availability": list(self.availability),
            "recency": [value.value for value in self.recency],
            "changed_since_viewed": self.changed_since_viewed,
            "dates": [
                {
                    "field": value.field.value,
                    "preset": value.preset.value,
                    "start": _date_to_json(value.start),
                    "end": _date_to_json(value.end),
                }
                for value in self.dates
            ],
            "open_count_min": self.open_count_min,
            "open_count_max": self.open_count_max,
            "recently_changed_days": self.recently_changed_days,
            "broken": self.broken,
        }

    @classmethod
    def from_filter_dict(
        cls,
        filters: Mapping[str, Any],
        *,
        search: str = "",
        sort: BookmarkSort | str = BookmarkSort.ADDED_NEWEST,
    ) -> Self:
        """Restore validated query state from a saved-view filter object."""

        if not isinstance(filters, Mapping):
            raise InvalidBookmarkQuery("Saved bookmark filters must be an object.")
        known = {
            "favorite",
            "pinned",
            "tags",
            "people",
            "spaces",
            "category_ids",
            "availability",
            "recency",
            "changed_since_viewed",
            "dates",
            "open_count_min",
            "open_count_max",
            "recently_changed_days",
            "broken",
        }
        unknown = sorted(set(filters) - known)
        if unknown:
            raise InvalidBookmarkQuery(f"Unknown saved bookmark filter: {unknown[0]}.")

        payload = dict(filters)
        raw_dates = _saved_sequence(payload.pop("dates", ()), "dates")
        payload["dates"] = tuple(
            BookmarkDateFilter(
                field=_saved_mapping(item, "date filter").get("field", ""),
                preset=_saved_mapping(item, "date filter").get("preset", ""),
                start=_date_from_json(_saved_mapping(item, "date filter").get("start")),
                end=_date_from_json(_saved_mapping(item, "date filter").get("end")),
            )
            for item in raw_dates
        )
        for name in ("tags", "people", "spaces", "category_ids", "availability", "recency"):
            if name in payload:
                payload[name] = tuple(_saved_sequence(payload[name], name))
        return cls(search=search, sort=sort, **payload)

    @classmethod
    def from_saved_view(cls, view: SavedBookmarkView) -> Self:
        return cls.from_filter_dict(view.filters, search=view.search_text, sort=view.sort)


@dataclass(frozen=True, slots=True)
class ActiveFilter:
    key: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class BookmarkQueryCounts:
    all_bookmarks: int
    matching: int
    favorites: int
    pinned: int
    changed_since_viewed: int
    broken: int
    never_viewed: int
    by_availability: Mapping[str, int] = field(default_factory=dict)
    by_recency: Mapping[str, int] = field(default_factory=dict)
    by_space: Mapping[str, int] = field(default_factory=dict)
    by_tag: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BookmarkQueryResult:
    bookmarks: tuple[Bookmark, ...]
    matched_node_ids: frozenset[int]
    visible_node_ids: frozenset[int]
    active_filters: tuple[ActiveFilter, ...]
    counts: BookmarkQueryCounts
    sort: BookmarkSort
    flat_mode: bool

    @property
    def active_filter_count(self) -> int:
        return len(self.active_filters)

    @property
    def matching_count(self) -> int:
        return len(self.bookmarks)


def query_bookmarks(
    query: BookmarkQuery | None = None,
    *,
    at: datetime | None = None,
) -> BookmarkQueryResult:
    """Search, filter, and sort bookmarks entirely from the local database."""

    query = query or BookmarkQuery()
    if not isinstance(query, BookmarkQuery):
        raise InvalidBookmarkQuery("query must be a BookmarkQuery.")
    observation_time = at or timezone.now()
    if not isinstance(observation_time, datetime) or timezone.is_naive(observation_time):
        raise InvalidBookmarkQuery("The query timestamp must include a timezone.")

    queryset = Bookmark.objects.select_related("tree_node").prefetch_related("tags")
    queryset = _apply_database_filters(queryset, query, at=observation_time)
    if query.recency:
        requested = set(query.recency)
        recency_queryset = (
            queryset.select_related(None)
            .prefetch_related(None)
            .only("id", "saved_at", "updated_at", "availability_status")
        )
        matching_ids = [
            bookmark.pk
            for bookmark in recency_queryset.iterator(chunk_size=1000)
            if bookmark.recency_at(at=observation_time) in requested
        ]
        queryset = queryset.filter(pk__in=matching_ids)
    if query.search:
        queryset = _apply_local_search(queryset, query.search)

    queryset = _apply_sort(queryset, query.sort)
    bookmarks = tuple(queryset)
    matched_node_ids = frozenset(bookmark.tree_node_id for bookmark in bookmarks)
    visible_node_ids = visible_node_ids_with_ancestors(matched_node_ids)
    return BookmarkQueryResult(
        bookmarks=bookmarks,
        matched_node_ids=matched_node_ids,
        visible_node_ids=visible_node_ids,
        active_filters=active_filter_descriptors(query),
        counts=_query_counts(bookmarks, at=observation_time),
        sort=query.sort,
        flat_mode=sort_requires_flat_mode(query.sort),
    )


def visible_node_ids_with_ancestors(node_ids: Iterable[int]) -> frozenset[int]:
    """Return existing node IDs plus their ancestors without changing hierarchy."""

    requested = {
        value
        for value in node_ids
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    if not requested:
        return frozenset()
    parents = dict(ConfluencePageNode.objects.values_list("id", "parent_id"))
    visible: set[int] = set()
    for requested_id in requested:
        current_id: int | None = requested_id
        branch_seen: set[int] = set()
        while current_id is not None and current_id in parents and current_id not in branch_seen:
            branch_seen.add(current_id)
            visible.add(current_id)
            current_id = parents[current_id]
    return frozenset(visible)


def sort_requires_flat_mode(sort: BookmarkSort | str) -> bool:
    """Only default Added-newest browsing is allowed to retain tree ordering."""

    choice = _enum_value(BookmarkSort, sort, "sort")
    return choice != BookmarkSort.ADDED_NEWEST


def active_filter_descriptors(query: BookmarkQuery) -> tuple[ActiveFilter, ...]:
    """Return stable, user-readable chips for every active search/filter value."""

    descriptors: list[ActiveFilter] = []
    if query.search:
        descriptors.append(ActiveFilter("search", "Search", query.search))
    _append_boolean_filter(descriptors, "favorite", "Favorite", query.favorite)
    _append_boolean_filter(descriptors, "pinned", "Pinned", query.pinned)
    for value in query.tags:
        descriptors.append(ActiveFilter("tag", "Tag", value))
    for value in query.people:
        descriptors.append(ActiveFilter("person", "Person", value))
    for value in query.spaces:
        descriptors.append(ActiveFilter("space", "Space", value))
    category_names = dict(
        BookmarkCategory.objects.filter(pk__in=query.category_ids).values_list("pk", "name")
    )
    for value in query.category_ids:
        descriptors.append(
            ActiveFilter("category", "Category", category_names.get(value, str(value)))
        )
    for value in query.availability:
        descriptors.append(
            ActiveFilter("availability", "Availability", BookmarkAvailability(value).label)
        )
    for value in query.recency:
        descriptors.append(ActiveFilter("recency", "Status", value.label))
    _append_boolean_filter(
        descriptors,
        "changed_since_viewed",
        "Changed since viewed",
        query.changed_since_viewed,
    )
    for value in query.dates:
        descriptors.append(
            ActiveFilter(
                f"date:{value.field.value}",
                DATE_FIELD_LABELS[value.field],
                _date_filter_description(value),
            )
        )
    if query.open_count_min is not None or query.open_count_max is not None:
        descriptors.append(
            ActiveFilter(
                "open_count",
                "Open count",
                _number_range_description(query.open_count_min, query.open_count_max),
            )
        )
    if query.recently_changed_days is not None:
        descriptors.append(
            ActiveFilter(
                "recently_changed",
                "Recently changed",
                f"Last {query.recently_changed_days} days",
            )
        )
    _append_boolean_filter(descriptors, "broken", "Broken/inaccessible", query.broken)
    return tuple(descriptors)


def _apply_database_filters(
    queryset: QuerySet[Bookmark],
    query: BookmarkQuery,
    *,
    at: datetime,
) -> QuerySet[Bookmark]:
    if query.favorite is not None:
        queryset = queryset.filter(favorite=query.favorite)
    if query.pinned is not None:
        queryset = queryset.filter(pinned=query.pinned)
    for normalized_tag in query.tags:
        # A separate join per call implements the documented ALL-tags rule.
        queryset = queryset.filter(tags__normalized_name=Tag.normalize_name(normalized_tag))
    if query.people:
        people_filter = Q()
        for person in query.people:
            people_filter |= (
                Q(author_id__iexact=person)
                | Q(author_name__iexact=person)
                | Q(created_by_id__iexact=person)
                | Q(created_by_name__iexact=person)
                | Q(modified_by_id__iexact=person)
                | Q(modified_by_name__iexact=person)
            )
        queryset = queryset.filter(people_filter)
    if query.spaces:
        space_filter = Q()
        for space in query.spaces:
            space_filter |= Q(space_key__iexact=space) | Q(space_name__iexact=space)
        queryset = queryset.filter(space_filter)
    if query.category_ids:
        queryset = queryset.filter(category_id__in=query.category_ids)
    if query.availability:
        queryset = queryset.filter(availability_status__in=query.availability)
    if query.changed_since_viewed is not None:
        changed = Q(last_viewed_version__isnull=False) & ~Q(last_viewed_version=F("version"))
        queryset = queryset.filter(changed if query.changed_since_viewed else ~changed)
    for date_filter in query.dates:
        queryset = _apply_date_filter(queryset, date_filter, at=at)
    if query.open_count_min is not None:
        queryset = queryset.filter(open_count__gte=query.open_count_min)
    if query.open_count_max is not None:
        queryset = queryset.filter(open_count__lte=query.open_count_max)
    if query.recently_changed_days is not None:
        queryset = queryset.filter(updated_at__gte=at - timedelta(days=query.recently_changed_days))
    if query.broken is not None:
        if query.broken:
            queryset = queryset.exclude(availability_status=BookmarkAvailability.ACTIVE)
        else:
            queryset = queryset.filter(availability_status=BookmarkAvailability.ACTIVE)
    return queryset.distinct()


def _apply_local_search(queryset: QuerySet[Bookmark], raw_search: str) -> QuerySet[Bookmark]:
    url_identity = _url_identity_filter(raw_search)
    if url_identity is not None:
        return queryset.filter(url_identity).distinct()

    for search_term in _independent_search_terms(raw_search):
        direct = _direct_search_filter(search_term)
        direct_ids = queryset.filter(direct).values_list("pk", flat=True)
        path_node_ids = _node_ids_with_matching_breadcrumb(search_term)
        queryset = queryset.filter(
            Q(pk__in=direct_ids) | Q(tree_node_id__in=path_node_ids)
        ).distinct()
    return queryset


def matching_bookmarks_for_url(value: str) -> tuple[Bookmark, ...]:
    """Find every saved bookmark with the pasted URL or its embedded Page ID."""

    identity = _url_identity_filter(value)
    if identity is None:
        return ()
    return tuple(
        Bookmark.objects.select_related("tree_node").filter(identity).distinct().order_by("pk")
    )


def _url_identity_filter(value: str) -> Q | None:
    """Build a network-free identity lookup for one complete HTTP(S) URL."""

    candidate = str(value or "").strip()
    try:
        canonical_url, _hostname = canonicalize_web_url(candidate)
    except WebBookmarkError:
        return None

    identity = Q(canonical_url=canonical_url) | Q(url__iexact=candidate)
    page_id = extract_page_id_from_url(candidate)
    if page_id is not None:
        identity |= Q(page_id=page_id)
    return identity


def _direct_search_filter(search_term: str) -> Q:
    direct = (
        Q(title__icontains=search_term)
        | Q(page_id__icontains=search_term)
        | Q(url__icontains=search_term)
        | Q(space_name__icontains=search_term)
        | Q(space_key__icontains=search_term)
        | Q(author_id__icontains=search_term)
        | Q(author_name__icontains=search_term)
        | Q(created_by_id__icontains=search_term)
        | Q(created_by_name__icontains=search_term)
        | Q(modified_by_id__icontains=search_term)
        | Q(modified_by_name__icontains=search_term)
        | Q(tags__name__icontains=search_term)
        | Q(notes__icontains=search_term)
        | Q(page_text__icontains=search_term)
        | Q(category__name__icontains=search_term)
        | Q(category__domain__icontains=search_term)
    )
    if search_term.isdecimal():
        direct |= Q(pk=int(search_term))
    elif search_term.startswith("#") and search_term[1:].isdecimal():
        direct |= Q(pk=int(search_term[1:]))
    return direct


def _independent_search_terms(raw_search: str) -> tuple[str, ...]:
    """Return unique whitespace-separated terms in the order the user entered them."""

    return tuple(dict.fromkeys(_search_key(raw_search).split()))


def _node_ids_with_matching_breadcrumb(search: str) -> frozenset[int]:
    needle = _search_key(search)
    rows = tuple(ConfluencePageNode.objects.values_list("id", "parent_id", "title"))
    parent_by_id = {node_id: parent_id for node_id, parent_id, _title in rows}
    title_by_id = {node_id: title for node_id, _parent_id, title in rows}
    breadcrumb_by_id: dict[int, tuple[str, ...]] = {}

    for node_id in parent_by_id:
        branch: list[int] = []
        seen: set[int] = set()
        cursor: int | None = node_id
        while cursor is not None and cursor not in breadcrumb_by_id and cursor not in seen:
            seen.add(cursor)
            branch.append(cursor)
            cursor = parent_by_id.get(cursor)
        prefix = breadcrumb_by_id.get(cursor, ())
        for branch_id in reversed(branch):
            prefix = (*prefix, title_by_id[branch_id])
            breadcrumb_by_id[branch_id] = prefix

    matches = set()
    for node_id, titles in breadcrumb_by_id.items():
        slash_path = " / ".join(titles)
        chevron_path = " > ".join(titles)
        if needle in _search_key(f"{slash_path} {chevron_path}"):
            matches.add(node_id)
    return frozenset(matches)


def _apply_sort(queryset: QuerySet[Bookmark], sort: BookmarkSort) -> QuerySet[Bookmark]:
    ordering: Mapping[BookmarkSort, Sequence[Any]] = {
        BookmarkSort.ADDED_NEWEST: (F("saved_at").desc(), F("id").desc()),
        BookmarkSort.ADDED_OLDEST: (F("saved_at").asc(), F("id").asc()),
        BookmarkSort.UPDATED_NEWEST: (F("updated_at").desc(nulls_last=True), F("id").asc()),
        BookmarkSort.UPDATED_OLDEST: (F("updated_at").asc(nulls_last=True), F("id").asc()),
        BookmarkSort.CREATED_NEWEST: (F("created_at").desc(nulls_last=True), F("id").asc()),
        BookmarkSort.CREATED_OLDEST: (F("created_at").asc(nulls_last=True), F("id").asc()),
        BookmarkSort.TITLE_ASCENDING: (Lower("title").asc(), F("id").asc()),
        BookmarkSort.TITLE_DESCENDING: (Lower("title").desc(), F("id").asc()),
        BookmarkSort.AUTHOR_ASCENDING: (Lower("author_name").asc(), F("id").asc()),
        BookmarkSort.FAVORITES_FIRST: (
            F("favorite").desc(),
            F("saved_at").desc(),
            F("id").desc(),
        ),
        BookmarkSort.PINNED_FIRST: (
            F("pinned").desc(),
            F("saved_at").desc(),
            F("id").desc(),
        ),
        BookmarkSort.MOST_OPENED: (F("open_count").desc(), F("id").asc()),
        BookmarkSort.LEAST_OPENED: (F("open_count").asc(), F("id").asc()),
        BookmarkSort.RECENTLY_OPENED: (
            F("last_viewed_at").desc(nulls_last=True),
            F("id").asc(),
        ),
        BookmarkSort.LEAST_RECENTLY_OPENED: (
            F("last_viewed_at").asc(nulls_first=True),
            F("id").asc(),
        ),
        BookmarkSort.RECENTLY_REFRESHED: (
            F("last_refreshed_at").desc(nulls_last=True),
            F("id").asc(),
        ),
    }
    return queryset.order_by(*ordering[sort])


def _apply_date_filter(
    queryset: QuerySet[Bookmark],
    date_filter: BookmarkDateFilter,
    *,
    at: datetime,
) -> QuerySet[Bookmark]:
    if date_filter.preset == DatePreset.ANY_TIME:
        return queryset
    lower, upper, upper_inclusive = _resolve_date_range(date_filter, at=at)
    field_name = _DATE_MODEL_FIELDS[date_filter.field]
    if lower is not None:
        queryset = queryset.filter(**{f"{field_name}__gte": lower})
    if upper is not None:
        lookup = "lte" if upper_inclusive else "lt"
        queryset = queryset.filter(**{f"{field_name}__{lookup}": upper})
    return queryset


def _resolve_date_range(
    date_filter: BookmarkDateFilter,
    *,
    at: datetime,
) -> tuple[datetime | None, datetime | None, bool]:
    preset = date_filter.preset
    local_at = timezone.localtime(at)
    zone = timezone.get_current_timezone()
    today = local_at.date()
    today_start = datetime.combine(today, time.min, tzinfo=zone)
    if preset == DatePreset.TODAY:
        return today_start, today_start + timedelta(days=1), False
    if preset == DatePreset.LAST_7_DAYS:
        return at - timedelta(days=7), at, True
    if preset == DatePreset.LAST_30_DAYS:
        return at - timedelta(days=30), at, True
    if preset == DatePreset.LAST_3_MONTHS:
        return _subtract_calendar_months(local_at, 3), at, True
    if preset == DatePreset.LAST_6_MONTHS:
        return _subtract_calendar_months(local_at, 6), at, True
    this_year = datetime.combine(date(today.year, 1, 1), time.min, tzinfo=zone)
    if preset == DatePreset.THIS_YEAR:
        return this_year, at, True
    last_year = datetime.combine(date(today.year - 1, 1, 1), time.min, tzinfo=zone)
    if preset == DatePreset.LAST_YEAR:
        return last_year, this_year, False
    if preset == DatePreset.OLDER:
        return None, last_year, False
    if preset == DatePreset.CUSTOM_RANGE:
        lower = _custom_date_boundary(date_filter.start, is_end=False, zone=zone)
        upper = _custom_date_boundary(date_filter.end, is_end=True, zone=zone)
        if lower is not None and upper is not None and lower > upper:
            raise InvalidBookmarkQuery("Custom date range start cannot follow its end.")
        return lower, upper, isinstance(date_filter.end, datetime)
    raise InvalidBookmarkQuery(f"Unsupported date preset: {preset}.")


def _query_counts(bookmarks: tuple[Bookmark, ...], *, at: datetime) -> BookmarkQueryCounts:
    availability = Counter(bookmark.availability_status for bookmark in bookmarks)
    recency = Counter(bookmark.recency_at(at=at).value for bookmark in bookmarks)
    spaces = Counter((bookmark.space_name or bookmark.space_key) for bookmark in bookmarks)
    tags: Counter[str] = Counter()
    for bookmark in bookmarks:
        tags.update(tag.name for tag in bookmark.tags.all())
    return BookmarkQueryCounts(
        all_bookmarks=Bookmark.objects.count(),
        matching=len(bookmarks),
        favorites=sum(bookmark.favorite for bookmark in bookmarks),
        pinned=sum(bookmark.pinned for bookmark in bookmarks),
        changed_since_viewed=sum(bookmark.changed_since_viewed for bookmark in bookmarks),
        broken=sum(
            bookmark.availability_status != BookmarkAvailability.ACTIVE for bookmark in bookmarks
        ),
        never_viewed=sum(bookmark.last_viewed_at is None for bookmark in bookmarks),
        by_availability=dict(sorted(availability.items())),
        by_recency=dict(sorted(recency.items())),
        by_space=dict(sorted((key, value) for key, value in spaces.items() if key)),
        by_tag=dict(sorted(tags.items())),
    )


def _clean_text_values(
    values: Iterable[str],
    label: str,
    *,
    normalizer=None,
) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise InvalidBookmarkQuery(f"{label.title()} must be a list of text values.")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        if not isinstance(raw_value, str):
            raise InvalidBookmarkQuery(f"Every {label} value must be text.")
        value = normalizer(raw_value) if normalizer else raw_value.strip()
        if not value:
            raise InvalidBookmarkQuery(f"{label.title()} cannot contain a blank value.")
        identity = _search_key(value)
        if identity not in seen:
            seen.add(identity)
            cleaned.append(value)
    return tuple(cleaned)


def _choice_values(choice_type, values: Iterable[Any], label: str) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise InvalidBookmarkQuery(f"{label.title()} must be a list of values.")
    cleaned = _clean_text_values(
        (value.value if hasattr(value, "value") else value for value in values), label
    )
    return tuple(_enum_value(choice_type, value, label) for value in cleaned)


def _enum_value(enum_type, value, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBookmarkQuery(f"Unknown {label}: {value!s}.") from exc


def _optional_boolean(value: bool | None, label: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise InvalidBookmarkQuery(f"{label.title()} must be true, false, or unset.")
    return value


def _saved_sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidBookmarkQuery(f"Saved {label} must be a list.")
    return value


def _saved_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidBookmarkQuery(f"Saved {label} must be an object.")
    return value


def _date_to_json(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date_from_json(value: Any) -> date | datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidBookmarkQuery("Saved custom dates must be ISO-8601 text.")
    try:
        if "T" in value or " " in value:
            return datetime.fromisoformat(value)
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidBookmarkQuery("Saved custom dates must use ISO-8601 format.") from exc


def _custom_date_boundary(
    value: date | datetime | None,
    *,
    is_end: bool,
    zone,
) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    boundary = datetime.combine(value, time.min, tzinfo=zone)
    return boundary + timedelta(days=1) if is_end else boundary


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    target_month = value.month - months
    target_year = value.year
    while target_month <= 0:
        target_month += 12
        target_year -= 1
    next_month = date(target_year + (target_month == 12), (target_month % 12) + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return value.replace(year=target_year, month=target_month, day=min(value.day, last_day))


def _search_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _canonical_tag_display(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _append_boolean_filter(
    descriptors: list[ActiveFilter], key: str, label: str, value: bool | None
) -> None:
    if value is not None:
        descriptors.append(ActiveFilter(key, label, "Yes" if value else "No"))


def _number_range_description(minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f"{minimum}–{maximum}"
    if minimum is not None:
        return f"At least {minimum}"
    return f"At most {maximum}"


def _date_filter_description(value: BookmarkDateFilter) -> str:
    if value.preset != DatePreset.CUSTOM_RANGE:
        return DATE_PRESET_LABELS[value.preset]
    if value.start is not None and value.end is not None:
        return f"{value.start.isoformat()} – {value.end.isoformat()}"
    if value.start is not None:
        return f"From {value.start.isoformat()}"
    return f"Through {value.end.isoformat()}"
