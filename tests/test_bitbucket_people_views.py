from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketRepository,
    GitCommit,
    PDFDocument,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _repository() -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
    )


def _committed_pdf(
    repository: BitbucketRepository,
    *,
    person: str,
    filename: str,
    marker: str,
) -> PDFDocument:
    committed_at = timezone.now() - timedelta(hours=1)
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash=marker * 40,
        author_name=f"Author of {filename}",
        committer_name=person,
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=filename,
        relative_path=f"docs/{filename}",
        last_commit=commit,
    )


def test_empty_people_panels_keep_a_visible_disabled_search_bar(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "data-people-search-toggle" not in html
    for panel_id in ("bb-people-desktop", "bb-people-mobile"):
        assert f'id="{panel_id}-search-panel" data-people-search-panel' in html
        assert f'for="{panel_id}-search"' in html
        search = re.search(rf'<input\s+id="{panel_id}-search"(?P<attributes>[^>]*)>', html)
        assert search is not None
        assert "disabled" in search.group("attributes")
        assert f'aria-describedby="{panel_id}-search-status"' in search.group("attributes")


def test_people_context_and_multi_committer_filter_use_or_semantics(loopback_client):
    repository = _repository()
    alice = _committed_pdf(
        repository,
        person="Alice Smith",
        filename="Alice.pdf",
        marker="a",
    )
    bob = _committed_pdf(
        repository,
        person="Bob Jones",
        filename="Bob.pdf",
        marker="b",
    )
    carol = _committed_pdf(
        repository,
        person="Carol Reviewer",
        filename="Carol.pdf",
        marker="c",
    )

    response = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"committer": ["alice smith", "BOB JONES"]},
    )

    assert response.status_code == 200
    assert response.context["git_people_total"] == 3
    assert response.context["selected_people_count"] == 2
    assert response.context["search_active"] is True
    assert response.context["search_page"].total == 2
    result_ids = {hit.document.pk for hit in response.context["search_page"].results}
    assert result_ids == {alice.pk, bob.pk}
    assert carol.pk not in result_ids
    selected = {person["name"] for person in response.context["git_people"] if person["selected"]}
    assert selected == {"Alice Smith", "Bob Jones"}
    html = response.content.decode()
    assert "Latest Git commit by" in html
    assert "Committers from available history" in html
    assert "Apply people" not in html
    assert "Choose any combination" not in html
    assert "No saved groups" not in html
    assert "Create one from committers found in available history" not in html
    assert "data-committer-select" in html
    assert len(re.findall(r"\bdata-people-filter-search(?=[\s>])", html)) == 2
    assert "data-people-search-toggle" not in html
    assert 'id="bb-people-desktop-search-panel" data-people-search-panel' in html
    assert 'id="bb-people-mobile-search-panel" data-people-search-panel' in html
    assert "Clear selection" not in html
    assert ">Clear</a>" in html
    assert "Create group" in html
    assert "Pushed by" not in html


def test_people_repository_badges_stay_global_when_pdf_results_are_filtered(loopback_client):
    architecture = _repository()
    payments = BitbucketRepository.objects.create(
        display_name="payments-platform",
        canonical_remote_key="bitbucket.org/workspace/payments-platform",
        remote_url="https://bitbucket.org/workspace/payments-platform.git",
    )
    first = _committed_pdf(architecture, person="Alice Smith", filename="A.pdf", marker="a")
    _committed_pdf(payments, person="Alice Smith", filename="B.pdf", marker="b")
    _committed_pdf(architecture, person="Bob Jones", filename="C.pdf", marker="c")

    response = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"repository": architecture.pk, "committer": "Alice Smith"},
    )

    assert response.status_code == 200
    people = {person["name"]: person for person in response.context["git_people"]}
    assert people["Alice Smith"]["repository_count"] == 2
    assert people["Alice Smith"]["repository_names"] == ("architecture", "payments-platform")
    assert people["Alice Smith"]["pdf_count"] == 2
    assert people["Alice Smith"]["commit_count"] == 2
    assert people["Bob Jones"]["repository_count"] == 1
    assert [hit.document.pk for hit in response.context["search_page"].results] == [first.pk]
    html = response.content.decode()
    # The shared partial supplies the same count and hover names on both layouts.
    assert html.count('aria-label="2 repositories: architecture, payments-platform"') == 2
    assert html.count('title="Repositories: architecture, payments-platform">2 repos</b>') == 2
    assert html.count('title="Repositories: architecture">1 repo</b>') == 2
    assert "2 PDFs · 2 commits" in html


def test_people_repository_hover_names_are_escaped(loopback_client):
    repository = _repository()
    repository.display_name = 'Ops "<img src=x onerror=alert(1)>" & Platform'
    repository.save(update_fields=("display_name",))
    _committed_pdf(repository, person="Alice Smith", filename="A.pdf", marker="a")

    response = loopback_client.get(reverse("bitbucket_search:index"))

    assert response.status_code == 200
    html = response.content.decode()
    badges = re.findall(r'<b class="bb-people-repository-count"[^>]*>1 repo</b>', html)
    assert len(badges) == 2
    for badge in badges:
        assert f'title="Repositories: {escape(repository.display_name)}"' in badge
        assert 'tabindex="0" role="note"' in badge
        assert "<img src=x onerror=alert(1)>" not in badge


def test_people_group_can_be_created_then_filters_to_any_member(loopback_client):
    repository = _repository()
    alice = _committed_pdf(
        repository,
        person="Alice Smith",
        filename="Alice.pdf",
        marker="a",
    )
    bob = _committed_pdf(
        repository,
        person="Bob Jones",
        filename="Bob.pdf",
        marker="b",
    )
    carol = _committed_pdf(
        repository,
        person="Carol Reviewer",
        filename="Carol.pdf",
        marker="c",
    )

    created = loopback_client.post(
        reverse("bitbucket_search:people_group_create"),
        {
            "name": "Architecture Reviewers",
            "member": ["Alice Smith", "Bob Jones"],
            "return_to": reverse("bitbucket_search:index"),
        },
    )

    assert created.status_code == 302
    assert created.url == reverse("bitbucket_search:index")
    group = BitbucketPeopleGroup.objects.get()
    assert list(group.members.values_list("person_name", flat=True)) == [
        "Alice Smith",
        "Bob Jones",
    ]

    response = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"people_group": group.pk},
    )

    assert response.status_code == 200
    assert response.context["selected_group_count"] == 1
    assert response.context["selected_people_count"] == 0
    assert response.context["search_page"].total == 2
    result_ids = {hit.document.pk for hit in response.context["search_page"].results}
    assert result_ids == {alice.pk, bob.pk}
    assert carol.pk not in result_ids
    group_summary = response.context["people_groups"][0]
    assert group_summary.selected is True
    assert group_summary.member_count == 2
    assert group_summary.pdf_count == 2
    html = response.content.decode()
    assert 'name="people_group"' in html
    assert "data-people-group-select checked" in html
    assert "Architecture Reviewers" in html

    combined = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"people_group": group.pk, "committer": "Carol Reviewer"},
    )
    assert combined.context["search_page"].total == 3
    assert {hit.document.pk for hit in combined.context["search_page"].results} == {
        alice.pk,
        bob.pk,
        carol.pk,
    }


def test_people_group_creation_is_loopback_only_and_rerenders_validation(loopback_client):
    repository = _repository()
    _committed_pdf(
        repository,
        person="Alice Smith",
        filename="Alice.pdf",
        marker="a",
    )
    path = reverse("bitbucket_search:people_group_create")

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.40")
    assert remote_client.post(path, {"name": "Remote", "member": "Alice Smith"}).status_code == 403

    invalid = loopback_client.post(
        path,
        {
            "name": "Architecture Reviewers",
            "member": "Unknown Person",
            "return_to": reverse("bitbucket_search:index"),
        },
    )

    assert invalid.status_code == 400
    assert "Unknown Git committer selection" in invalid.content.decode()
    assert invalid.context["open_people_group_form"] is True
    assert invalid.context["people_group_form_name"] == "Architecture Reviewers"
    assert not BitbucketPeopleGroup.objects.exists()


def test_group_filter_pagination_keeps_group_identity_without_expanding_url(loopback_client):
    repository = _repository()
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash="a" * 40,
        author_name="Alice Author",
        committer_name="Alice Smith",
        authored_at=timezone.now() - timedelta(hours=2),
        committed_at=timezone.now() - timedelta(hours=1),
    )
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Guide {number:03d}.pdf",
                relative_path=f"docs/Guide {number:03d}.pdf",
                last_commit=commit,
            )
            for number in range(201)
        ]
    )
    created = loopback_client.post(
        reverse("bitbucket_search:people_group_create"),
        {
            "name": "Alice documents",
            "member": "Alice Smith",
            "return_to": reverse("bitbucket_search:index"),
        },
    )
    assert created.status_code == 302
    group = BitbucketPeopleGroup.objects.get()

    response = loopback_client.get(
        reverse("bitbucket_search:index"),
        {"people_group": group.pk},
    )

    assert response.context["search_page"].total == 201
    assert f"people_group={group.pk}" in response.context["next_search_page_url"]
    assert "committer=" not in response.context["next_search_page_url"]
