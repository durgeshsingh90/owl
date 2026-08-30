"""Read-only dashboard summaries from the last published Git-history snapshot."""

from __future__ import annotations

import logging
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    GitCommitFolder,
    InvalidPeopleName,
    canonical_people_name,
)
from bitbucket_search.services.logging_events import get_logger, log_event

logger = get_logger("analytics")

ACTIVITY_PERIOD_LABELS = {
    "week": "Last 7 days",
    "month": "Last month",
    "six_months": "Last 6 months",
    "year": "Last year",
}
RANKING_LIMIT = 10


@dataclass(frozen=True, slots=True)
class PersonActivity:
    name: str
    aliases: tuple[str, ...]
    commit_count: int
    repository_count: int


@dataclass(frozen=True, slots=True)
class RepositoryActivity:
    repository_id: int
    name: str
    commit_count: int


@dataclass(frozen=True, slots=True)
class FolderActivity:
    repository_id: int
    repository_name: str
    path: str
    commit_count: int


@dataclass(frozen=True, slots=True)
class ActivityCoverage:
    total_repositories: int
    indexed_repositories: int
    pending_repositories: int
    shallow_repositories: int
    stale_repositories: int
    last_indexed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BitbucketDashboard:
    period: str
    label: str
    started_at: datetime
    ended_at: datetime
    total_commits: int
    active_people: int
    active_repositories: int
    active_folders: int
    people: tuple[PersonActivity, ...]
    repositories: tuple[RepositoryActivity, ...]
    folders: tuple[FolderActivity, ...]
    coverage: ActivityCoverage

    @property
    def has_data(self) -> bool:
        return self.total_commits > 0

    @property
    def has_repositories(self) -> bool:
        return self.coverage.total_repositories > 0


def _period_start(period: str, ended_at: datetime) -> datetime:
    """Subtract calendar months in the current timezone, clamping month ends."""

    if period == "week":
        return ended_at - timedelta(days=7)
    months = {"month": 1, "six_months": 6, "year": 12}[period]
    year, zero_based_month = divmod(ended_at.year * 12 + ended_at.month - 1 - months, 12)
    month = zero_based_month + 1
    day = min(ended_at.day, monthrange(year, month)[1])
    return ended_at.replace(year=year, month=month, day=day)


def _people_activity(commits) -> tuple[PersonActivity, ...]:
    """Fold the same display-name aliases as People, without loading commits."""

    aliases: dict[str, Counter[str]] = defaultdict(Counter)
    repositories: dict[str, set[int]] = defaultdict(set)
    for row in (
        commits.values("committer_name", "repository_id")
        .annotate(commit_count=Count("id"))
        .iterator()
    ):
        try:
            name = canonical_people_name(row["committer_name"])
        except InvalidPeopleName:
            # The commit remains in repository totals, but cannot invent a person.
            continue
        identity = name.casefold()
        aliases[identity][name] += row["commit_count"]
        repositories[identity].add(row["repository_id"])

    people = []
    for identity, names in aliases.items():
        preferred = sorted(names, key=lambda name: (-names[name], name.casefold(), name))
        remaining = sorted(preferred[1:], key=lambda name: (name.casefold(), name))
        people.append(
            PersonActivity(
                name=preferred[0],
                aliases=(preferred[0], *remaining),
                commit_count=sum(names.values()),
                repository_count=len(repositories[identity]),
            )
        )
    people.sort(key=lambda person: (-person.commit_count, person.name.casefold(), person.name))
    return tuple(people)


@transaction.atomic(savepoint=False)
def _build_dashboard(*, period: str, now: datetime | None) -> BitbucketDashboard:
    # OWL's SQLite connection keeps these six reads on one snapshot, even when a
    # repository worker publishes a replacement between dashboard queries.
    ended_at = now if now is not None else timezone.now()
    if timezone.is_naive(ended_at):
        ended_at = timezone.make_aware(ended_at)
    ended_at = timezone.localtime(ended_at)
    started_at = _period_start(period, ended_at)

    indexed = Q(activity_indexed_at__isnull=False) & ~Q(activity_indexed_commit="")
    coverage_values = BitbucketRepository.objects.filter(enabled=True).aggregate(
        total=Count("id"),
        indexed=Count("id", filter=indexed),
        shallow=Count("id", filter=indexed & Q(history_is_shallow=True)),
        stale=Count(
            "id",
            filter=indexed
            & ~Q(last_synced_commit="")
            & ~Q(activity_indexed_commit=F("last_synced_commit")),
        ),
        last_indexed_at=Max("activity_indexed_at", filter=indexed),
    )
    coverage = ActivityCoverage(
        total_repositories=coverage_values["total"],
        indexed_repositories=coverage_values["indexed"],
        pending_repositories=coverage_values["total"] - coverage_values["indexed"],
        shallow_repositories=coverage_values["shallow"],
        stale_repositories=coverage_values["stale"],
        last_indexed_at=coverage_values["last_indexed_at"],
    )
    # Publication replaces this explicit history membership atomically. PDF
    # provenance can retain older, unreachable commits and must not inflate it.
    commits = (
        GitCommit.objects.filter(
            repository__enabled=True,
            repository__activity_indexed_at__isnull=False,
            in_activity_history=True,
            committed_at__gte=started_at,
            committed_at__lte=ended_at,
        )
        .exclude(repository__activity_indexed_commit="")
        .order_by()
    )
    totals = commits.aggregate(
        total_commits=Count("id"),
        active_repositories=Count("repository_id", distinct=True),
    )
    all_people = _people_activity(commits)
    repositories = tuple(
        RepositoryActivity(
            repository_id=row["repository_id"],
            name=row["repository__display_name"],
            commit_count=row["commit_count"],
        )
        for row in (
            commits.values("repository_id", "repository__display_name")
            .annotate(commit_count=Count("id"))
            .order_by("-commit_count", "repository__display_name", "repository_id")[:RANKING_LIMIT]
        )
    )
    # A folder is an identity within its repository, not a global pathname. A
    # single commit may touch several folders, so folder totals are not additive.
    changed_folders = GitCommitFolder.objects.filter(commit__in=commits).order_by()
    active_folders = (
        changed_folders.values("commit__repository_id", "folder_path").distinct().count()
    )
    folders = tuple(
        FolderActivity(
            repository_id=row["commit__repository_id"],
            repository_name=row["commit__repository__display_name"],
            path=row["folder_path"],
            commit_count=row["commit_count"],
        )
        for row in (
            changed_folders.values(
                "commit__repository_id", "commit__repository__display_name", "folder_path"
            )
            .annotate(commit_count=Count("commit_id", distinct=True))
            .order_by(
                "-commit_count",
                "commit__repository__display_name",
                "folder_path",
                "commit__repository_id",
            )[:RANKING_LIMIT]
        )
    )
    return BitbucketDashboard(
        period=period,
        label=ACTIVITY_PERIOD_LABELS[period],
        started_at=started_at,
        ended_at=ended_at,
        total_commits=totals["total_commits"],
        active_people=len(all_people),
        active_repositories=totals["active_repositories"],
        active_folders=active_folders,
        people=all_people[:RANKING_LIMIT],
        repositories=repositories,
        folders=folders,
        coverage=coverage,
    )


def get_bitbucket_dashboard(
    period: str = "week", *, now: datetime | None = None
) -> BitbucketDashboard:
    """Summarize available Git history without Git access or database mutations.

    Windows include their starting instant and end now; commits dated in the
    future are excluded. Month, six-month and year windows are rolling calendar
    intervals in the application's current timezone, not fixed day counts.
    """

    selected = period if isinstance(period, str) and period in ACTIVITY_PERIOD_LABELS else "week"
    log_event(logger, logging.DEBUG, "activity_dashboard_requested", stage=selected)
    try:
        dashboard = _build_dashboard(period=selected, now=now)
    except Exception as error:
        log_event(logger, logging.ERROR, "activity_dashboard_failed", error=error)
        raise
    log_event(
        logger,
        logging.DEBUG,
        "activity_dashboard_completed",
        count=dashboard.total_commits,
        indexed_count=dashboard.coverage.indexed_repositories,
    )
    return dashboard
