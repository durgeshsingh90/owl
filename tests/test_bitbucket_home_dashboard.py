"""Integration checks for independent Home dashboard activity filters."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, GitCommit, GitCommitFolder

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
LOCAL_ZONE = ZoneInfo("Europe/Dublin")
PERIOD_LABELS = (
    ("today", "Today"),
    ("week", "This week"),
    ("last_week", "Last week"),
    ("month", "This month"),
    ("six_months", "Last 6 months"),
    ("year", "This year"),
)


@pytest.fixture(autouse=True)
def fixed_dashboard_time():
    with timezone.override(LOCAL_ZONE), patch("core.views.timezone.now", return_value=NOW):
        yield


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _indexed_repository():
    now = timezone.now()
    repository = BitbucketRepository.objects.create(
        display_name="Cloud architecture",
        remote_url="https://example.invalid/eng/cloud.git",
        canonical_remote_key="example.invalid/eng/cloud",
        activity_indexed_commit="a" * 40,
        activity_indexed_at=now,
        last_synced_commit="a" * 40,
        history_is_shallow=False,
    )
    for number, days_ago in enumerate((2, 20, 100, 300)):
        commit = GitCommit.objects.create(
            repository=repository,
            commit_hash=f"{number:040x}",
            author_name="Document author",
            committer_name="Alex Engineer",
            authored_at=now - timedelta(days=days_ago),
            committed_at=now - timedelta(days=days_ago),
            in_activity_history=True,
        )
        GitCommitFolder.objects.create(commit=commit, folder_path="docs/network")
    return repository


@pytest.fixture
def calendar_history():
    repositories = tuple(
        BitbucketRepository.objects.create(
            display_name=name,
            remote_url=f"https://example.invalid/eng/{slug}.git",
            canonical_remote_key=f"example.invalid/eng/{slug}",
            activity_indexed_commit="a" * 40,
            activity_indexed_at=NOW,
            last_synced_commit="a" * 40,
            history_is_shallow=False,
        )
        for name, slug in (("Cloud architecture", "cloud"), ("Service architecture", "service"))
    )
    rows = (
        (0, "Alex Engineer", datetime(2026, 8, 26, 0, tzinfo=LOCAL_ZONE)),
        (1, "Alex Engineer", datetime(2026, 8, 26, 8, tzinfo=LOCAL_ZONE)),
        (0, "Sam Engineer", datetime(2026, 8, 26, 10, tzinfo=LOCAL_ZONE)),
        (0, "Alex Engineer", datetime(2026, 8, 24, 0, tzinfo=LOCAL_ZONE)),
        (1, "Sam Engineer", datetime(2026, 8, 25, 11, tzinfo=LOCAL_ZONE)),
        (0, "Alex Engineer", datetime(2026, 8, 17, 0, tzinfo=LOCAL_ZONE)),
        (1, "Alex Engineer", datetime(2026, 8, 23, 23, 59, tzinfo=LOCAL_ZONE)),
        (1, "Sam Engineer", datetime(2026, 8, 20, 11, tzinfo=LOCAL_ZONE)),
        (1, "Alex Engineer", datetime(2026, 8, 1, 0, tzinfo=LOCAL_ZONE)),
        (0, "Sam Engineer", datetime(2026, 1, 1, 0, tzinfo=LOCAL_ZONE)),
        (1, "Sam Engineer", datetime(2026, 4, 3, 11, tzinfo=LOCAL_ZONE)),
        (0, "Alex Engineer", datetime(2025, 12, 31, 23, 59, tzinfo=LOCAL_ZONE)),
        (1, "Sam Engineer", datetime(2026, 8, 26, 14, tzinfo=LOCAL_ZONE)),
    )
    for number, (repository_index, committer, committed_at) in enumerate(rows):
        commit = GitCommit.objects.create(
            repository=repositories[repository_index],
            commit_hash=f"{number:040x}",
            author_name="Different document author",
            committer_name=committer,
            authored_at=datetime(2024, 3, 1, tzinfo=UTC),
            committed_at=committed_at,
            in_activity_history=True,
        )
        GitCommitFolder.objects.create(commit=commit, folder_path="docs/network")
    return repositories


@pytest.mark.parametrize(
    ("period", "people_counts", "repository_counts"),
    (
        ("today", (2, 1), (2, 1)),
        ("week", (3, 2), (3, 2)),
        ("last_week", (2, 1), (1, 2)),
        ("month", (6, 3), (4, 5)),
        ("six_months", (6, 4), (4, 6)),
        ("year", (6, 5), (5, 6)),
    ),
)
def test_home_period_changes_all_git_rankings(
    loopback_client, calendar_history, period, people_counts, repository_counts
):
    with patch("subprocess.Popen") as start_process:
        response = loopback_client.get(reverse("core:dashboard"), {"git_period": period})

    assert response.status_code == 200
    start_process.assert_not_called()
    dashboard = response.context["bitbucket_dashboard"]
    assert dashboard.period == period
    assert dashboard.label == dict(PERIOD_LABELS)[period]
    assert dashboard.total_commits == sum(people_counts)
    assert [(person.name, person.commit_count) for person in dashboard.people] == [
        ("Alex Engineer", people_counts[0]),
        ("Sam Engineer", people_counts[1]),
    ]
    assert dashboard.people[0].repository_count == 2
    assert dashboard.people[1].repository_count == (1 if period in {"today", "last_week"} else 2)
    assert dashboard.active_people == 2
    assert dashboard.active_repositories == 2
    assert {row.repository_id: row.commit_count for row in dashboard.repositories} == {
        repository.pk: count
        for repository, count in zip(calendar_history, repository_counts, strict=True)
    }
    assert {row.repository_id: row.commit_count for row in dashboard.folders} == {
        repository.pk: count
        for repository, count in zip(calendar_history, repository_counts, strict=True)
    }
    html = response.content.decode()
    nav = re.search(r'<nav[^>]+aria-label="Bitbucket activity period">(.*?)</nav>', html, re.DOTALL)
    assert nav is not None
    links = re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', nav.group(1))
    assert [label for _href, _attributes, label in links] == [
        label for _period, label in PERIOD_LABELS
    ]
    for (href, attributes, _label), (link_period, _expected_label) in zip(
        links, PERIOD_LABELS, strict=True
    ):
        assert parse_qs(urlsplit(unescape(href)).query)["git_period"] == [link_period]
        assert ('aria-current="page"' in attributes) == (link_period == period)
    assert nav.group(1).count('aria-current="page"') == 1
    assert GitCommit.objects.count() == 13
    assert GitCommitFolder.objects.count() == 13


def test_home_activity_filters_preserve_other_sections_selection(loopback_client):
    response = loopback_client.get(
        reverse("core:dashboard"),
        {
            "year": "2024",
            "activity": "opened",
            "git_period": "six_months",
            "people_period": "month",
        },
    )

    assert response.status_code == 200
    filters = response.context["bitbucket_activity_filters"]
    assert len(filters) == 6
    for item in filters:
        parts = urlsplit(item["href"])
        query = parse_qs(parts.query)
        assert query["year"] == ["2024"]
        assert query["activity"] == ["opened"]
        assert query["people_period"] == ["month"]
        assert parts.fragment == "bitbucket-activity"
        assert item["selected"] == (query["git_period"] == ["six_months"])
        next_response = loopback_client.get(item["href"])
        assert next_response.context["bitbucket_dashboard"].period == query["git_period"][0]
        assert next_response.context["bookmark_people_dashboard"].period == "month"
        selected_activity = next(
            item for item in next_response.context["dashboard"].activity.filters if item.selected
        )
        selected_query = parse_qs(urlsplit(selected_activity.href).query)
        assert selected_query["activity"] == ["opened"]
        assert selected_query["year"] == ["2024"]
    bookmark_activity = response.context["dashboard"].activity
    for item in (*bookmark_activity.filters, *bookmark_activity.years):
        query = parse_qs(urlsplit(item.href).query)
        assert query["git_period"] == ["six_months"]
        assert query["people_period"] == ["month"]
    for item in response.context["bookmark_people_activity_filters"]:
        query = parse_qs(urlsplit(item["href"]).query)
        assert query["git_period"] == ["six_months"]
        assert query["activity"] == ["opened"]
        assert query["year"] == ["2024"]


def test_home_invalid_period_falls_back_and_does_not_echo_input(loopback_client, calendar_history):
    response = loopback_client.get(
        reverse("core:dashboard"),
        {"git_period": '<script>alert("invalid")</script>', "year": "invalid"},
    )

    assert response.status_code == 200
    assert response.context["bitbucket_dashboard"].period == "week"
    assert response.context["bitbucket_dashboard"].label == "This week"
    assert response.context["bitbucket_dashboard"].total_commits == 5
    assert '<script>alert("invalid")</script>' not in response.content.decode()
    for item in response.context["bitbucket_activity_filters"]:
        assert parse_qs(urlsplit(item["href"]).query)["year"] != ["invalid"]


def test_home_does_not_invent_commit_statistics_for_existing_pdf_evidence(loopback_client):
    repository = _indexed_repository()
    BitbucketRepository.objects.filter(pk=repository.pk).update(
        activity_indexed_commit="", activity_indexed_at=None
    )
    GitCommit.objects.update(in_activity_history=False)

    response = loopback_client.get(reverse("core:dashboard"))

    assert response.status_code == 200
    dashboard = response.context["bitbucket_dashboard"]
    assert dashboard.coverage.pending_repositories == 1
    assert dashboard.total_commits == 0
    assert dashboard.people == ()
    assert dashboard.repositories == ()
    assert dashboard.folders == ()


@pytest.mark.parametrize(("period", "label"), PERIOD_LABELS)
@pytest.mark.parametrize("coverage", ["none", "pending", "indexed_empty"])
def test_home_period_zero_states_keep_missing_history_distinct_from_no_commits(
    loopback_client, period, label, coverage
):
    if coverage != "none":
        BitbucketRepository.objects.create(
            display_name="Synthetic empty history",
            remote_url="https://example.invalid/eng/empty.git",
            canonical_remote_key="example.invalid/eng/empty",
            activity_indexed_at=NOW if coverage == "indexed_empty" else None,
            activity_indexed_commit="e" * 40 if coverage == "indexed_empty" else "",
            history_is_shallow=False,
        )
    response = loopback_client.get(reverse("core:dashboard"), {"git_period": period})
    assert response.status_code == 200
    dashboard = response.context["bitbucket_dashboard"]
    assert dashboard.period == period and dashboard.label == label
    assert dashboard.total_commits == dashboard.active_people == 0
    assert dashboard.people == dashboard.repositories == dashboard.folders == ()
    html = response.content.decode()
    assert {
        "none": "No repository activity yet",
        "pending": "Git activity is not indexed yet",
        "indexed_empty": "No commits in this period",
    }[coverage] in html
    if coverage != "indexed_empty":
        assert "No commits in this period" not in html
