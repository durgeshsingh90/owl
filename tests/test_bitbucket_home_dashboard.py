"""Integration checks for independent Home dashboard activity filters."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, GitCommit, GitCommitFolder

pytestmark = pytest.mark.django_db


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


@pytest.mark.parametrize(
    ("period", "expected_commits"),
    (("week", 1), ("month", 2), ("six_months", 3), ("year", 4)),
)
def test_home_period_changes_all_git_rankings(loopback_client, period, expected_commits):
    _indexed_repository()

    with patch("subprocess.Popen") as start_process:
        response = loopback_client.get(reverse("core:dashboard"), {"git_period": period})

    assert response.status_code == 200
    start_process.assert_not_called()
    dashboard = response.context["bitbucket_dashboard"]
    assert dashboard.period == period
    assert dashboard.total_commits == expected_commits
    assert dashboard.people[0].name == "Alex Engineer"
    assert dashboard.people[0].commit_count == expected_commits
    assert dashboard.repositories[0].commit_count == expected_commits
    assert dashboard.folders[0].commit_count == expected_commits
    assert GitCommit.objects.count() == 4
    assert GitCommitFolder.objects.count() == 4


def test_home_activity_filters_preserve_other_sections_selection(loopback_client):
    response = loopback_client.get(
        reverse("core:dashboard"),
        {"year": "2024", "activity": "opened", "git_period": "six_months"},
    )

    assert response.status_code == 200
    filters = response.context["bitbucket_activity_filters"]
    assert len(filters) == 4
    for item in filters:
        parts = urlsplit(item["href"])
        query = parse_qs(parts.query)
        assert query["year"] == ["2024"]
        assert query["activity"] == ["opened"]
        assert parts.fragment == "bitbucket-activity"
        assert item["selected"] == (query["git_period"] == ["six_months"])
    bookmark_activity = response.context["dashboard"].activity
    for item in (*bookmark_activity.filters, *bookmark_activity.years):
        assert parse_qs(urlsplit(item.href).query)["git_period"] == ["six_months"]


def test_home_invalid_period_falls_back_and_does_not_echo_input(loopback_client):
    response = loopback_client.get(
        reverse("core:dashboard"),
        {"git_period": '<script>alert("invalid")</script>', "year": "invalid"},
    )

    assert response.status_code == 200
    assert response.context["bitbucket_dashboard"].period == "week"
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
