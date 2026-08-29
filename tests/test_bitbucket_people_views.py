from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

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
    assert "Pushed by" not in html


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
                filename=f"Guide {number:02d}.pdf",
                relative_path=f"docs/Guide {number:02d}.pdf",
                last_commit=commit,
            )
            for number in range(51)
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

    assert response.context["search_page"].total == 51
    assert f"people_group={group.pk}" in response.context["next_search_page_url"]
    assert "committer=" not in response.context["next_search_page_url"]
