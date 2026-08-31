from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse

from bitbucket_search.models import BitbucketRepository, GitCommit, PDFDocument
from bitbucket_search.services.people import GitPersonSummary, git_people_summaries

pytestmark = pytest.mark.django_db

BASE_DATE = datetime(2024, 1, 1, tzinfo=UTC)


def _repository(name: str, *, enabled: bool = True):
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"example.invalid/team/{name}",
        remote_url=f"https://example.invalid/team/{name}.git",
        enabled=enabled,
    )


def _commit(repository, name: str, number: int, *, day: int, in_activity_history=False):
    committed_at = BASE_DATE + timedelta(days=day)
    return GitCommit.objects.create(
        repository=repository,
        commit_hash=f"{number:040x}",
        author_name="Other author",
        committer_name=name,
        authored_at=committed_at - timedelta(days=1),
        committed_at=committed_at,
        in_activity_history=in_activity_history,
    )


def test_recency_uses_latest_commit_across_repositories_and_normalized_aliases():
    alpha = _repository("alpha")
    beta = _repository("beta")
    _commit(alpha, "Alice Smith", 1, day=1)
    _commit(alpha, "Alice Smith", 2, day=2)
    _commit(beta, "ALICE SMITH", 1, day=3)
    latest = _commit(beta, "  Ａlice   Smith  ", 2, day=4)

    (person,) = git_people_summaries()

    assert person.name == "Alice Smith"
    assert person.aliases == ("Alice Smith", "ALICE SMITH")
    assert person.commit_count == 4
    assert person.repository_names == ("alpha", "beta")
    assert person.last_committed_at == latest.committed_at


def test_recency_excludes_unreachable_indexed_history_and_disabled_repositories():
    indexed = _repository("indexed")
    legacy = _repository("legacy")
    disabled = _repository("disabled", enabled=False)
    reachable = _commit(indexed, "Alice", 1, day=1, in_activity_history=True)
    _commit(indexed, "ALICE", 2, day=100)
    _commit(indexed, "Unreachable Person", 3, day=200)
    latest_available = _commit(legacy, "Alice", 1, day=2)
    _commit(disabled, "ALICE", 1, day=300, in_activity_history=True)
    _commit(disabled, "Disabled Person", 2, day=400)
    BitbucketRepository.objects.filter(pk=indexed.pk).update(
        activity_indexed_commit=reachable.commit_hash,
        activity_indexed_at=BASE_DATE,
    )

    (person,) = git_people_summaries()

    assert person.last_committed_at == latest_available.committed_at
    assert person.commit_count == 2
    assert person.repository_names == ("indexed", "legacy")


@pytest.mark.parametrize(
    ("indexed_commit", "indexed_at"),
    [("", BASE_DATE), ("a" * 40, None)],
)
def test_recency_retains_legacy_history_until_activity_coverage_is_complete(
    indexed_commit, indexed_at
):
    repository = _repository("legacy")
    commit = _commit(repository, "Alice", 1, day=3)
    BitbucketRepository.objects.filter(pk=repository.pk).update(
        activity_indexed_commit=indexed_commit,
        activity_indexed_at=indexed_at,
    )

    (person,) = git_people_summaries()

    assert person.last_committed_at == commit.committed_at


def test_recency_is_git_committed_date_not_authored_or_owl_import_date():
    repository = _repository("architecture")
    latest = _commit(repository, "Alice", 1, day=2)
    older_commit = _commit(repository, "Alice", 2, day=1)
    GitCommit.objects.filter(pk=older_commit.pk).update(authored_at=BASE_DATE + timedelta(days=100))
    document = PDFDocument.objects.create(
        repository=repository,
        filename="notes.pdf",
        relative_path="docs/notes.pdf",
        last_commit=older_commit,
    )

    (person,) = git_people_summaries()

    assert person.last_committed_at == latest.committed_at
    assert person.last_committed_at != document.discovered_at
    assert person.pdf_count == 1


def test_recency_does_not_change_existing_default_summary_order():
    repository = _repository("architecture")
    alice = _commit(repository, "Alice", 1, day=1)
    PDFDocument.objects.create(
        repository=repository,
        filename="notes.pdf",
        relative_path="docs/notes.pdf",
        last_commit=alice,
    )
    _commit(repository, "Bob", 2, day=30)
    _commit(repository, "Charlie", 3, day=3)
    _commit(repository, "Charlie", 4, day=4)

    summaries = git_people_summaries()

    assert [person.name for person in summaries] == ["Alice", "Charlie", "Bob"]
    assert [person.last_committed_at for person in summaries] == [
        BASE_DATE + timedelta(days=day) for day in (1, 4, 30)
    ]


def test_recency_uses_two_batched_read_only_queries(django_assert_num_queries):
    for number in range(20):
        repository = _repository(f"repository-{number}")
        _commit(repository, "Common committer", 1, day=number)
        _commit(repository, f"Person {number}", 2, day=number + 1)

    with django_assert_num_queries(2) as queries:
        summaries = git_people_summaries()
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    with django_assert_num_queries(0):
        assert summaries[0].last_committed_at == BASE_DATE + timedelta(days=19)
        assert summaries[0].repository_count == 20
        assert len(summaries) == 21


def test_summary_recency_is_optional_for_existing_callers():
    summary = GitPersonSummary("Alice", ("Alice",), 1, 0)
    assert summary.last_committed_at is None


def test_people_context_exposes_global_recency_when_repository_is_filtered(client):
    first = _repository("first")
    second = _repository("second")
    _commit(first, "Alice", 1, day=1)
    latest = _commit(second, "Alice", 1, day=5)

    response = client.get(
        reverse("bitbucket_search:index"),
        {"repository": first.pk},
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    (person,) = response.context["git_people"]
    assert person["last_committed_at"] == latest.committed_at
    assert person["repository_count"] == 2
