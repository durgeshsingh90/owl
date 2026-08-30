"""Dated writer and latest-editor summaries for saved Confluence pages.

The local bookmark catalogue stores one source snapshot, not a Confluence edit
ledger. These rankings count pages with dated attribution, never edit events or
the time at which OWL downloaded a page.
"""

from __future__ import annotations

import unicodedata
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from bookmark_manager.models import Bookmark, BookmarkSource
from bookmark_manager.services.logging_events import logged_operation

BOOKMARK_PEOPLE_PERIOD_LABELS = {
    "today": "Today",
    "week": "Last 7 days",
    "month": "Last month",
    "year": "Last year",
}
RANKING_LIMIT = 10


@dataclass(frozen=True, slots=True)
class BookmarkPersonActivity:
    name: str
    aliases: tuple[str, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class BookmarkPeopleCoverage:
    total_pages: int
    written_metadata_pages: int
    updated_metadata_pages: int
    missing_written_metadata_pages: int
    missing_updated_metadata_pages: int


@dataclass(frozen=True, slots=True)
class BookmarkPeopleDashboard:
    period: str
    label: str
    started_at: datetime
    ended_at: datetime
    written_pages: int
    updated_pages: int
    active_people: int
    writers: tuple[BookmarkPersonActivity, ...]
    updaters: tuple[BookmarkPersonActivity, ...]
    coverage: BookmarkPeopleCoverage

    @property
    def has_data(self) -> bool:
        return bool(self.written_pages or self.updated_pages)

    @property
    def has_confluence_pages(self) -> bool:
        return self.coverage.total_pages > 0


def _canonical_name(value: object) -> str:
    """Fold ordinary formatting without inventing labels for missing people."""

    if not isinstance(value, str):
        return ""
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    if not name or len(name) > 500:
        return ""
    if any(unicodedata.category(character).startswith("C") for character in name):
        return ""
    return name


def _valid_source_date(value: object, *, ended_at: datetime) -> bool:
    return isinstance(value, datetime) and timezone.is_aware(value) and value <= ended_at


def _period_start(period: str, ended_at: datetime) -> datetime:
    if period == "today":
        return ended_at.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    if period == "week":
        return ended_at - timedelta(days=7)
    months = 1 if period == "month" else 12
    year, zero_based_month = divmod(ended_at.year * 12 + ended_at.month - 1 - months, 12)
    month = zero_based_month + 1
    return ended_at.replace(
        year=year,
        month=month,
        day=min(ended_at.day, monthrange(year, month)[1]),
    )


def _rank_people(
    page_counts: Counter[str], aliases: dict[str, Counter[str]]
) -> tuple[BookmarkPersonActivity, ...]:
    people = []
    for identity, count in page_counts.items():
        preferred = sorted(
            aliases[identity],
            key=lambda name: (-aliases[identity][name], name.casefold(), name),
        )
        remaining = sorted(preferred[1:], key=lambda name: (name.casefold(), name))
        people.append(
            BookmarkPersonActivity(
                name=preferred[0],
                aliases=(preferred[0], *remaining),
                page_count=count,
            )
        )
    people.sort(key=lambda person: (-person.page_count, person.name.casefold(), person.name))
    return tuple(people[:RANKING_LIMIT])


@logged_operation("get_bookmark_people_dashboard", quiet=True)
def get_bookmark_people_dashboard(
    period: str = "week", *, now: datetime | None = None
) -> BookmarkPeopleDashboard:
    """Read one consistent snapshot of source names and dates, without writes.

    All saved Confluence pages are included, matching the People sidebar even
    when their most recent refresh failed or the source page is unavailable.
    Ancestor-only tree nodes and ordinary web bookmarks are not saved Confluence
    pages. Attribution uses display names because stable source person IDs are
    not consistently retained by the current source adapter.
    """

    selected = (
        period if isinstance(period, str) and period in BOOKMARK_PEOPLE_PERIOD_LABELS else "week"
    )
    ended_at = now if now is not None else timezone.now()
    if timezone.is_naive(ended_at):
        ended_at = timezone.make_aware(ended_at)
    ended_at = timezone.localtime(ended_at)
    started_at = _period_start(selected, ended_at)

    writer_counts: Counter[str] = Counter()
    updater_counts: Counter[str] = Counter()
    aliases: dict[str, Counter[str]] = defaultdict(Counter)
    total_pages = written_metadata_pages = updated_metadata_pages = 0
    missing_updated_metadata_pages = written_pages = updated_pages = 0

    # A single streaming SELECT is a single database snapshot. No related objects
    # or page contents are loaded, and each saved page is encountered once.
    rows = (
        Bookmark.objects.filter(source_type=BookmarkSource.CONFLUENCE)
        .order_by()
        .values(
            "author_name",
            "created_by_name",
            "modified_by_name",
            "created_at",
            "updated_at",
            "version",
        )
        .iterator()
    )
    for row in rows:
        total_pages += 1
        writers_by_identity: dict[str, set[str]] = defaultdict(set)
        for raw_name in (row["author_name"], row["created_by_name"]):
            if name := _canonical_name(raw_name):
                writers_by_identity[name.casefold()].add(name)
        updater_name = _canonical_name(row["modified_by_name"])
        created_at = row["created_at"]
        updated_at = row["updated_at"]
        valid_creation = _valid_source_date(created_at, ended_at=ended_at)
        valid_update = _valid_source_date(updated_at, ended_at=ended_at)
        # Legacy imports can retain version=1 despite a later modification date.
        # Either a later version or a strictly later source timestamp is evidence
        # of an update. A creation-only version 1 is never credited as an edit.
        has_update = row["version"] > 1 or (
            valid_creation and valid_update and updated_at > created_at
        )
        consistent_update = valid_update and (
            created_at is None or (valid_creation and updated_at >= created_at)
        )
        written_metadata = bool(valid_creation and writers_by_identity)
        updated_metadata = bool(has_update and consistent_update and updater_name)
        written_metadata_pages += written_metadata
        updated_metadata_pages += updated_metadata
        missing_updated_metadata_pages += bool(has_update and not updated_metadata)

        # De-duplicate names within this page before counting either role. The
        # same alias on both roles also gets only one display-preference vote.
        page_aliases: dict[str, set[str]] = defaultdict(set)
        if written_metadata and started_at <= created_at:
            written_pages += 1
            for identity, names in writers_by_identity.items():
                writer_counts[identity] += 1
                page_aliases[identity].update(names)
        if updated_metadata and started_at <= updated_at:
            updated_pages += 1
            identity = updater_name.casefold()
            updater_counts[identity] += 1
            page_aliases[identity].add(updater_name)
        for identity, names in page_aliases.items():
            aliases[identity].update(names)

    return BookmarkPeopleDashboard(
        period=selected,
        label=BOOKMARK_PEOPLE_PERIOD_LABELS[selected],
        started_at=started_at,
        ended_at=ended_at,
        written_pages=written_pages,
        updated_pages=updated_pages,
        active_people=len(writer_counts.keys() | updater_counts.keys()),
        writers=_rank_people(writer_counts, aliases),
        updaters=_rank_people(updater_counts, aliases),
        coverage=BookmarkPeopleCoverage(
            total_pages=total_pages,
            written_metadata_pages=written_metadata_pages,
            updated_metadata_pages=updated_metadata_pages,
            missing_written_metadata_pages=total_pages - written_metadata_pages,
            missing_updated_metadata_pages=missing_updated_metadata_pages,
        ),
    )
