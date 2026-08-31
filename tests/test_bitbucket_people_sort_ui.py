from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketRepository,
    GitCommit,
    PDFDocument,
)


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_attribute(self, attribute):
        return [attrs for _tag, attrs in self.tags if attribute in attrs]


def test_people_sort_behavior_in_isolated_dom():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated People sorting tests.")
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "--test", str(project_root / "tests/js/bitbucket_people_sort.test.js")],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _repository():
    return BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="https://bitbucket.org/workspace/architecture.git",
    )


def _pdf(repository, name, marker, committed_at):
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash=marker * 40,
        author_name="Author",
        committer_name=name,
        authored_at=committed_at,
        committed_at=committed_at,
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=f"{marker}.pdf",
        relative_path=f"docs/{marker}.pdf",
        last_commit=commit,
    )


@pytest.mark.django_db
def test_empty_people_panels_have_disabled_sort_and_load_deferred_script(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    assert response.status_code == 200
    html = response.content.decode()
    parsed = Tags(html)
    controls = parsed.with_attribute("data-people-sort")
    assert len(controls) == 2
    for control in controls:
        assert "disabled" in control
        assert control["aria-label"]
        assert "name" not in control  # Sorting is not a PDF query/filter submission.
        assert any(
            tag == "label" and attrs.get("for") == control["id"] for tag, attrs in parsed.tags
        )
    for select in re.findall(r"<select\b[^>]*data-people-sort[^>]*>.*?</select>", html, re.S):
        assert set(re.findall(r'<option\b[^>]*value="([^"]+)"', select)) == {
            "most_pdfs",
            "recent",
            "most_commits",
            "name",
        }
    scripts = [
        attrs
        for tag, attrs in parsed.tags
        if tag == "script" and "bitbucket_search/people_sort.js" in attrs.get("src", "")
    ]
    assert len(scripts) == 1
    assert "defer" in scripts[0]


@pytest.mark.django_db
def test_people_sort_rows_include_escaped_names_counts_and_real_commit_timestamps(loopback_client):
    repository = _repository()
    person = 'Alice "<img src=x>" & Bob'
    committed_at = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)
    _pdf(repository, person, "a", committed_at)

    response = loopback_client.get(reverse("bitbucket_search:index"))
    assert response.status_code == 200
    html = response.content.decode()
    parsed = Tags(html)
    assert all("disabled" not in control for control in parsed.with_attribute("data-people-sort"))
    rows = parsed.with_attribute("data-people-name")
    assert len(rows) == 2
    for row in rows:
        assert row["data-people-entry-kind"] == "committer"
        assert row["data-people-name"] == person
        assert row["data-people-last-commit"] == str(int(committed_at.timestamp()))
        assert row["data-people-commit-count"] == "1"
        assert row["data-people-pdf-count"] == "1"
    assert f'data-people-name="{escape(person)}"' in html
    assert "<img src=x>" not in html


def test_people_sort_does_not_invent_a_timestamp_for_unknown_history():
    html = render_to_string(
        "bitbucket_search/_people_panel.html",
        {
            "people_panel_id": "unknown-date",
            "git_people_total": 1,
            "git_people": [{"name": "Alice", "commit_count": 1, "pdf_count": 0}],
            "selected_people_count": 0,
            "selected_group_count": 0,
            "csrf_token": "not-a-real-token",
        },
    )
    [row] = Tags(html).with_attribute("data-people-name")
    assert row["data-people-last-commit"] == ""


@pytest.mark.django_db
def test_people_sort_leaves_selected_people_groups_and_repository_filter_intact(loopback_client):
    repository = _repository()
    committed_at = datetime(2026, 8, 1, 17, 30, tzinfo=UTC)
    alice = _pdf(repository, "Alice", "a", committed_at)
    bob = _pdf(repository, "Bob", "b", committed_at)
    _pdf(repository, "Carol", "c", committed_at)
    group = BitbucketPeopleGroup.objects.create(name="Architecture Reviewers")
    group.members.create(person_name="Bob")

    response = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"committer": "Alice", "people_group": group.pk, "repository": repository.pk},
    )
    assert response.status_code == 200
    assert {hit.document.pk for hit in response.context["search_page"].results} == {
        alice.pk,
        bob.pk,
    }
    assert response.context["selected_people_count"] == 1
    assert response.context["selected_group_count"] == 1
    assert ("repository", str(repository.pk)) in response.context["people_filter_hidden_fields"]
    parsed = Tags(response.content.decode())
    assert len(parsed.with_attribute("data-people-sort")) == 2
    checked_people = [
        row["value"] for row in parsed.with_attribute("data-committer-select") if "checked" in row
    ]
    assert checked_people == ["Alice", "Alice"]
    checked_groups = [
        row["value"]
        for row in parsed.with_attribute("data-people-group-select")
        if "checked" in row
    ]
    assert checked_groups == [str(group.pk), str(group.pk)]
