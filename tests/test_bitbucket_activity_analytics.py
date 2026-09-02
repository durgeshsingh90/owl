from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, GitCommit, GitCommitFolder
from bitbucket_search.services.activity_analytics import (
    ACTIVITY_PERIOD_LABELS,
    get_bitbucket_dashboard,
)

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 30, 11, 30, tzinfo=UTC)


def _repository(name="Repository", *, indexed=True, enabled=True, **values):
    key = f"example.invalid/team/{BitbucketRepository.objects.count() + 1}"
    defaults = {
        "display_name": name,
        "canonical_remote_key": key,
        "remote_url": f"https://{key}.git",
        "enabled": enabled,
        "history_is_shallow": False,
        "last_synced_commit": "a" * 40,
        "activity_indexed_commit": "a" * 40 if indexed else "",
        "activity_indexed_at": NOW - timedelta(minutes=1) if indexed else None,
    }
    defaults.update(values)
    return BitbucketRepository.objects.create(**defaults)


def _commit(repository, person="Alice", *, number=None, at=NOW, folders=(), **values):
    number = number if number is not None else repository.git_commits.count() + 1
    defaults = {
        "repository": repository,
        "commit_hash": f"{number:040x}",
        "author_name": "Different Git Author",
        "committer_name": person,
        "authored_at": NOW - timedelta(days=100),
        "committed_at": at,
        "in_activity_history": True,
    }
    defaults.update(values)
    commit = GitCommit.objects.create(**defaults)
    GitCommitFolder.objects.bulk_create(
        [GitCommitFolder(commit=commit, folder_path=path) for path in folders]
    )
    return commit


def test_counts_commits_from_all_files_using_committer_not_author():
    alpha = _repository("Alpha")
    beta = _repository("Beta")
    _commit(alpha, "Alice", folders=("src", "docs"))
    _commit(alpha, "Bob", folders=("src", ""))
    # The same commit hash in another repository counts for that repository too.
    _commit(beta, "Alice", folders=("src",))

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 3
    assert dashboard.active_people == 2
    assert dashboard.active_repositories == 2
    assert dashboard.active_folders == 4
    assert [(person.name, person.commit_count) for person in dashboard.people] == [
        ("Alice", 2),
        ("Bob", 1),
    ]
    assert dashboard.people[0].repository_count == 2
    assert [(repo.name, repo.commit_count) for repo in dashboard.repositories] == [
        ("Alpha", 2),
        ("Beta", 1),
    ]
    assert dashboard.folders[0].repository_id == alpha.pk
    assert dashboard.folders[0].path == "src"
    assert dashboard.folders[0].commit_count == 2
    assert sum(folder.commit_count for folder in dashboard.folders) == 5
    assert dashboard.has_data and dashboard.has_repositories


def test_people_aliases_fold_unicode_whitespace_case_and_repository_identity():
    first = _repository("Shared repository name")
    second = _repository("Shared repository name")
    _commit(first, "Alice Smith")
    _commit(first, "Alice Smith")
    _commit(second, "Ａlice   Smith")
    _commit(second, "ALICE SMITH")
    _commit(second, "  alice smith  ")

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.active_people == 1
    (person,) = dashboard.people
    assert person.name == "Alice Smith"
    assert person.aliases == ("Alice Smith", "ALICE SMITH", "alice smith")
    assert person.commit_count == 5
    assert person.repository_count == 2
    assert dashboard.active_repositories == 2


def test_invalid_people_names_do_not_invent_people_but_commits_still_count():
    repository = _repository()
    for name in ("", "\n", "Bad\tName"):
        _commit(repository, name)

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 3
    assert dashboard.active_people == 0
    assert dashboard.people == ()


def test_date_window_includes_both_bounds_and_excludes_old_future_and_provenance_only():
    repository = _repository()
    monday = datetime(2026, 8, 24, tzinfo=UTC)
    for at in (NOW, monday):
        _commit(repository, at=at, folders=("included",))
    _commit(repository, at=monday - timedelta(microseconds=1), folders=("old",))
    _commit(repository, at=NOW + timedelta(microseconds=1), folders=("future",))
    _commit(repository, in_activity_history=False, folders=("stale-provenance",))

    with timezone.override("UTC"):
        dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 2
    assert dashboard.started_at == monday
    assert dashboard.ended_at == NOW
    assert dashboard.active_folders == 1
    assert dashboard.folders[0].path == "included"
    assert dashboard.folders[0].commit_count == 2


@pytest.mark.parametrize(
    ("period", "at", "expected_start", "expected_end"),
    [
        ("today", (2026, 8, 30), (2026, 8, 30, 0, 0), (2026, 8, 30, 11, 30)),
        ("week", (2026, 8, 30), (2026, 8, 24, 0, 0), (2026, 8, 30, 11, 30)),
        ("week", (2026, 8, 31), (2026, 8, 31, 0, 0), (2026, 8, 31, 11, 30)),
        ("last_week", (2026, 8, 30), (2026, 8, 17, 0, 0), (2026, 8, 24, 0, 0)),
        ("last_week", (2026, 8, 31), (2026, 8, 24, 0, 0), (2026, 8, 31, 0, 0)),
        ("last_week", (2026, 1, 1), (2025, 12, 22, 0, 0), (2025, 12, 29, 0, 0)),
        ("month", (2026, 3, 31), (2026, 3, 1, 0, 0), (2026, 3, 31, 11, 30)),
        ("month", (2024, 3, 31), (2024, 3, 1, 0, 0), (2024, 3, 31, 11, 30)),
        ("month", (2024, 2, 29), (2024, 2, 1, 0, 0), (2024, 2, 29, 11, 30)),
        ("month", (2026, 1, 1), (2026, 1, 1, 0, 0), (2026, 1, 1, 11, 30)),
        ("six_months", (2026, 8, 31), (2026, 2, 28, 11, 30), (2026, 8, 31, 11, 30)),
        ("six_months", (2024, 8, 31), (2024, 2, 29, 11, 30), (2024, 8, 31, 11, 30)),
        ("six_months", (2026, 1, 31), (2025, 7, 31, 11, 30), (2026, 1, 31, 11, 30)),
        ("year", (2024, 2, 29), (2024, 1, 1, 0, 0), (2024, 2, 29, 11, 30)),
        ("year", (2026, 1, 1), (2026, 1, 1, 0, 0), (2026, 1, 1, 11, 30)),
    ],
)
def test_periods_use_calendar_boundaries_and_keep_six_months_rolling(
    period, at, expected_start, expected_end
):
    with timezone.override("UTC"):
        dashboard = get_bitbucket_dashboard(period, now=datetime(*at, 11, 30, tzinfo=UTC))

    assert dashboard.period == period
    assert dashboard.label == ACTIVITY_PERIOD_LABELS[period]
    assert dashboard.started_at == datetime(*expected_start, tzinfo=UTC)
    assert dashboard.ended_at == datetime(*expected_end, tzinfo=UTC)


def test_period_labels_and_order_distinguish_current_from_previous_week():
    assert tuple(ACTIVITY_PERIOD_LABELS.items()) == (
        ("today", "Today"),
        ("week", "This week"),
        ("last_week", "Last week"),
        ("month", "This month"),
        ("six_months", "Last 6 months"),
        ("year", "This year"),
    )


def test_calendar_windows_use_application_timezone_across_daylight_saving():
    repository = _repository()
    at = datetime(2026, 3, 31, 10, 30, tzinfo=UTC)  # 11:30 local after DST.
    included = datetime(2026, 3, 1, tzinfo=UTC)  # Local month began before DST.
    _commit(repository, at=included)
    _commit(repository, at=included - timedelta(microseconds=1))

    with timezone.override("Europe/Dublin"):
        dashboard = get_bitbucket_dashboard("month", now=at)

    assert dashboard.started_at == included
    assert dashboard.ended_at == at
    assert dashboard.total_commits == 1


@pytest.mark.parametrize(
    ("period", "now", "start"),
    [
        ("today", datetime(2026, 8, 30, 22, 30, tzinfo=UTC), datetime(2026, 8, 29, 23, tzinfo=UTC)),
        ("today", datetime(2026, 8, 30, 23, 30, tzinfo=UTC), datetime(2026, 8, 30, 23, tzinfo=UTC)),
        ("today", datetime(2026, 3, 29, 1, 30, tzinfo=UTC), datetime(2026, 3, 29, tzinfo=UTC)),
        (
            "today",
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
            datetime(2026, 10, 24, 23, tzinfo=UTC),
        ),
        ("week", datetime(2026, 8, 30, 22, 30, tzinfo=UTC), datetime(2026, 8, 23, 23, tzinfo=UTC)),
        ("week", datetime(2026, 8, 30, 23, 30, tzinfo=UTC), datetime(2026, 8, 30, 23, tzinfo=UTC)),
        ("month", datetime(2026, 8, 31, 23, 30, tzinfo=UTC), datetime(2026, 8, 31, 23, tzinfo=UTC)),
        ("year", datetime(2026, 8, 30, 11, 30, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_current_calendar_periods_include_local_start_and_now_not_previous_or_future(
    period, now, start
):
    repository = _repository()
    _commit(repository, "Before", at=start - timedelta(microseconds=1))
    _commit(repository, "At start", at=start)
    _commit(repository, "At now", at=now)
    _commit(repository, "Future", at=now + timedelta(microseconds=1))

    with timezone.override("Europe/Dublin"):
        dashboard = get_bitbucket_dashboard(period, now=now)

    assert dashboard.started_at == start
    assert dashboard.ended_at.astimezone(UTC) == now
    assert dashboard.started_at.tzinfo == ZoneInfo("Europe/Dublin")
    assert {person.name for person in dashboard.people} == {"At start", "At now"}
    assert dashboard.total_commits == 2


@pytest.mark.parametrize("period", ["today", "week", "month", "year"])
def test_exact_midnight_calendar_boundary_includes_only_commit_at_new_period_start(period):
    # 1 January 2024 is a Monday: all four calendar periods turn over together.
    now = datetime(2024, 1, 1, tzinfo=UTC)
    repository = _repository()
    _commit(repository, "Prior period", at=now - timedelta(microseconds=1))
    _commit(repository, "New period", at=now)
    _commit(repository, "Future", at=now + timedelta(microseconds=1))

    with timezone.override("UTC"):
        dashboard = get_bitbucket_dashboard(period, now=now)

    assert dashboard.started_at == dashboard.ended_at == now
    assert dashboard.total_commits == 1
    assert dashboard.people[0].name == "New period"


@pytest.mark.parametrize(
    ("at", "start", "end", "hours"),
    [
        ((2026, 3, 30, 12), (2026, 3, 23, 0), (2026, 3, 29, 23), 167),
        ((2026, 10, 26, 12), (2026, 10, 18, 23), (2026, 10, 26, 0), 169),
        ((2026, 8, 30, 22), (2026, 8, 16, 23), (2026, 8, 23, 23), 168),
        ((2026, 8, 30, 23), (2026, 8, 23, 23), (2026, 8, 30, 23), 168),
    ],
)
def test_previous_week_is_half_open_monday_to_monday_across_dst(at, start, end, hours):
    now = datetime(*at, tzinfo=UTC)
    start = datetime(*start, tzinfo=UTC)
    end = datetime(*end, tzinfo=UTC)
    repository = _repository()
    _commit(repository, "Before", at=start - timedelta(microseconds=1))
    _commit(repository, "Monday start", at=start)
    _commit(repository, "Sunday finish", at=end - timedelta(microseconds=1))
    _commit(repository, "Next Monday", at=end)
    _commit(repository, "Future", at=now + timedelta(microseconds=1))

    with timezone.override("Europe/Dublin"):
        dashboard = get_bitbucket_dashboard("last_week", now=now)

    assert dashboard.started_at == start
    assert dashboard.ended_at == end
    assert (dashboard.ended_at.astimezone(UTC) - dashboard.started_at.astimezone(UTC)) == timedelta(
        hours=hours
    )
    assert {person.name for person in dashboard.people} == {"Monday start", "Sunday finish"}


def test_rolling_six_month_window_keeps_local_wall_time_and_inclusive_lower_bound():
    repository = _repository()
    now = datetime(2026, 8, 31, 10, 30, tzinfo=UTC)
    start = datetime(2026, 2, 28, 11, 30, tzinfo=UTC)
    _commit(repository, at=start)
    _commit(repository, at=start - timedelta(microseconds=1))

    with timezone.override("Europe/Dublin"):
        dashboard = get_bitbucket_dashboard("six_months", now=now)

    assert dashboard.started_at == start
    assert dashboard.ended_at == now
    assert dashboard.total_commits == 1


@pytest.mark.parametrize("period", ["today", "month", "year"])
def test_local_new_year_can_begin_before_the_utc_date_changes(period):
    repository = _repository()
    start = datetime(2025, 12, 31, 18, 30, tzinfo=UTC)
    now = datetime(2025, 12, 31, 19, tzinfo=UTC)
    _commit(repository, "Prior local year", at=start - timedelta(microseconds=1))
    _commit(repository, "New local year", at=start)

    with timezone.override("Asia/Kolkata"):
        dashboard = get_bitbucket_dashboard(period, now=now)

    assert dashboard.started_at == start
    assert dashboard.started_at.year == 2026
    assert dashboard.ended_at == now
    assert dashboard.total_commits == 1
    assert dashboard.people[0].name == "New local year"


@pytest.mark.parametrize("period", [None, "", "unknown", [], 99])
def test_invalid_period_falls_back_to_week(period):
    with timezone.override("UTC"):
        dashboard = get_bitbucket_dashboard(period, now=NOW)
    assert dashboard.period == "week"
    assert dashboard.label == "This week"
    assert dashboard.started_at == datetime(2026, 8, 24, tzinfo=UTC)


def test_naive_clock_is_interpreted_in_application_timezone():
    with timezone.override("Europe/Dublin"):
        dashboard = get_bitbucket_dashboard(now=datetime(2026, 8, 30, 12, 30))
    assert dashboard.ended_at == NOW


def test_default_clock_uses_current_time(monkeypatch):
    monkeypatch.setattr("bitbucket_search.services.activity_analytics.timezone.now", lambda: NOW)
    assert get_bitbucket_dashboard().ended_at == NOW


def test_disabled_and_unpublished_repositories_do_not_contribute_activity_or_coverage():
    enabled = _repository("Enabled")
    disabled = _repository("Disabled", enabled=False, history_is_shallow=True)
    pending = _repository("Pending", indexed=False)
    incomplete_timestamp = _repository("No timestamp", activity_indexed_at=None)
    incomplete_commit = _repository("No revision", activity_indexed_commit="")
    for repository in (enabled, disabled, pending, incomplete_timestamp, incomplete_commit):
        _commit(repository, repository.display_name, folders=("src",))

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 1
    assert dashboard.active_people == 1
    assert dashboard.active_repositories == 1
    assert dashboard.active_folders == 1
    assert dashboard.coverage.total_repositories == 4
    assert dashboard.coverage.indexed_repositories == 1
    assert dashboard.coverage.pending_repositories == 3
    assert dashboard.coverage.shallow_repositories == 0


def test_last_successful_snapshot_remains_visible_when_refresh_head_has_advanced():
    repository = _repository("Refreshing", last_synced_commit="b" * 40, history_is_shallow=True)
    _commit(repository, folders=("src",))

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 1
    assert dashboard.coverage.indexed_repositories == 1
    assert dashboard.coverage.stale_repositories == 1
    assert dashboard.coverage.shallow_repositories == 1
    assert dashboard.coverage.pending_repositories == 0
    assert dashboard.coverage.last_indexed_at == NOW - timedelta(minutes=1)


@pytest.mark.parametrize("period", tuple(ACTIVITY_PERIOD_LABELS))
def test_all_periods_keep_excluded_repository_history_and_truthful_limited_coverage(period):
    now = datetime(2026, 8, 31, 11, 30, tzinfo=UTC)
    commit_at = NOW if period == "last_week" else now
    excluded = _repository(
        "Refresh excluded",
        exclude_from_refresh=True,
        history_is_shallow=True,
        last_synced_commit="b" * 40,
    )
    pending = _repository("No indexed history", indexed=False)
    disabled = _repository("Disabled", enabled=False)
    _commit(excluded, "Included committer", at=commit_at, folders=("docs",))
    _commit(excluded, "Provenance only", at=commit_at, in_activity_history=False)
    _commit(pending, "Unpublished", at=commit_at)
    _commit(disabled, "Disabled", at=commit_at)

    with timezone.override("UTC"):
        dashboard = get_bitbucket_dashboard(period, now=now)

    assert dashboard.total_commits == 1
    assert dashboard.people[0].name == "Included committer"
    assert dashboard.people[0].repository_count == 1
    assert dashboard.coverage.total_repositories == 2
    assert dashboard.coverage.indexed_repositories == 1
    assert dashboard.coverage.pending_repositories == 1
    assert dashboard.coverage.shallow_repositories == 1
    assert dashboard.coverage.stale_repositories == 1


def test_zero_states_distinguish_no_repositories_pending_and_no_recent_activity():
    empty = get_bitbucket_dashboard(now=NOW)
    assert not empty.has_repositories and not empty.has_data
    assert empty.coverage.total_repositories == 0
    assert empty.coverage.last_indexed_at is None
    assert empty.people == empty.repositories == empty.folders == ()

    repository = _repository(indexed=False)
    pending = get_bitbucket_dashboard(now=NOW)
    assert pending.has_repositories and not pending.has_data
    assert pending.coverage.pending_repositories == 1

    repository.activity_indexed_commit = "a" * 40
    repository.activity_indexed_at = NOW
    repository.save(update_fields=["activity_indexed_commit", "activity_indexed_at"])
    _commit(repository, at=NOW - timedelta(days=30))
    quiet = get_bitbucket_dashboard(now=NOW)
    assert quiet.has_repositories and not quiet.has_data
    assert quiet.coverage.indexed_repositories == 1
    assert quiet.coverage.pending_repositories == 0


def test_folder_identity_is_repository_and_direct_parent_including_root():
    first = _repository("Shared name")
    second = _repository("Shared name")
    _commit(first, folders=("", "docs/deep"))
    _commit(second, folders=("", "docs/deep"))

    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.active_folders == 4
    assert {(row.repository_id, row.path) for row in dashboard.folders} == {
        (first.pk, ""),
        (second.pk, ""),
        (first.pk, "docs/deep"),
        (second.pk, "docs/deep"),
    }
    assert all(row.commit_count == 1 for row in dashboard.folders)
    assert not any(row.path == "docs" for row in dashboard.folders)


def test_rankings_are_top_ten_but_totals_cover_every_person_repository_and_folder():
    for index in range(15):
        repository = _repository(f"Repository {index:02}")
        _commit(repository, f"Person {index:02}", folders=(f"src/{index:02}",))
    dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 15
    assert dashboard.active_people == 15
    assert dashboard.active_repositories == 15
    assert dashboard.active_folders == 15
    assert len(dashboard.people) == len(dashboard.repositories) == len(dashboard.folders) == 10
    assert [person.name for person in dashboard.people] == [
        f"Person {index:02}" for index in range(10)
    ]
    assert [repository.name for repository in dashboard.repositories] == [
        f"Repository {index:02}" for index in range(10)
    ]


@pytest.mark.parametrize("period", tuple(ACTIVITY_PERIOD_LABELS))
def test_dashboard_uses_eight_read_only_queries_independent_of_repository_count(
    django_assert_num_queries, period, monkeypatch
):
    now = datetime(2026, 8, 31, 11, 30, tzinfo=UTC)
    commit_at = NOW if period == "last_week" else now
    for index in range(30):
        repository = _repository(f"Repository {index:02}")
        for person in ("Shared person", f"Person {index:02}"):
            _commit(repository, person, at=commit_at, folders=("src", "docs"))

    def no_external_work(*args, **kwargs):
        pytest.fail("Dashboard statistics must not run Git or access the network")

    monkeypatch.setattr("subprocess.Popen", no_external_work)
    monkeypatch.setattr("socket.create_connection", no_external_work)
    with (
        timezone.override("UTC"),
        CaptureQueriesContext(connection) as queries,
        django_assert_num_queries(8),
    ):
        dashboard = get_bitbucket_dashboard(period, now=now)
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    with django_assert_num_queries(0):
        assert dashboard.total_commits == 60
        assert dashboard.people[0].repository_count == 30
        assert dashboard.people[0].commit_count == 30
        assert dashboard.active_people == 31
        assert dashboard.active_folders == 60


def test_results_are_immutable_eager_values():
    repository = _repository()
    _commit(repository, folders=("src",))
    dashboard = get_bitbucket_dashboard(now=NOW)
    with pytest.raises(FrozenInstanceError):
        dashboard.total_commits = 100
    with pytest.raises(FrozenInstanceError):
        dashboard.people[0].commit_count = 100


@pytest.mark.django_db(transaction=True)
def test_reads_share_one_atomic_snapshot_without_writing_data():
    repository = _repository()
    _commit(repository, folders=("src",))
    assert not connection.in_atomic_block
    snapshots = []

    def record_snapshot(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith("SELECT"):
            snapshots.append(connection.in_atomic_block)
        return execute(sql, params, many, context)

    with (
        connection.execute_wrapper(record_snapshot),
        CaptureQueriesContext(connection) as queries,
    ):
        dashboard = get_bitbucket_dashboard(now=NOW)

    assert dashboard.total_commits == 1
    assert snapshots == [True] * 8
    assert not connection.in_atomic_block
    assert all(
        query["sql"].lstrip().upper().startswith(("SELECT", "BEGIN", "COMMIT")) for query in queries
    )


def test_analytics_errors_are_logged_without_raw_exception_content(monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError("private repository name and credential")

    monkeypatch.setattr("bitbucket_search.services.activity_analytics._build_dashboard", fail)
    with (
        caplog.at_level("ERROR", logger="owl.bitbucket.analytics"),
        pytest.raises(RuntimeError),
    ):
        get_bitbucket_dashboard(now=NOW)
    assert "event=activity_dashboard_failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "credential" not in caplog.text
