from __future__ import annotations

import pytest
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentLifecycle,
)
from bitbucket_search.services.people import GitPersonSummary, git_people_summaries

pytestmark = pytest.mark.django_db


def _repository(name: str, *, key: str | None = None, enabled: bool = True):
    remote_key = f"example.invalid/team/{key or name}"
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=remote_key,
        remote_url=f"https://{remote_key}.git",
        enabled=enabled,
    )


def _commit(repository, committer_name: str, number: int = 1):
    now = timezone.now()
    return GitCommit.objects.create(
        repository=repository,
        commit_hash=f"{number:040x}",
        author_name="Different Git Author",
        committer_name=committer_name,
        authored_at=now,
        committed_at=now,
    )


def _document(repository, commit, *, removed=False):
    return PDFDocument.objects.create(
        repository=repository,
        filename="notes.pdf",
        relative_path="docs/notes.pdf",
        last_commit=commit,
        lifecycle_state=PDFDocumentLifecycle.REMOVED if removed else PDFDocumentLifecycle.ACTIVE,
    )


def test_repositories_are_distinct_across_repeated_commits_and_canonical_aliases():
    zulu = _repository("Zulu")
    alpha = _repository("alpha")
    for number in range(1, 4):
        _commit(zulu, "Alice Smith", number)
    _commit(alpha, "ALICE SMITH", 1)
    _commit(alpha, "  Ａlice   Smith  ", 2)

    (person,) = git_people_summaries()

    assert person.name == "Alice Smith"
    assert person.aliases == ("Alice Smith", "ALICE SMITH")
    assert person.commit_count == 5
    assert person.pdf_count == 0
    assert person.repository_count == 2
    assert person.repository_names == ("alpha", "Zulu")


def test_two_repository_ids_with_same_display_name_count_separately():
    first = _repository("Shared name", key="first")
    second = _repository("Shared name", key="second")
    _commit(first, "Alice")
    _commit(second, "Alice")

    (person,) = git_people_summaries()

    assert person.commit_count == 2
    assert person.repository_count == 2
    assert person.repository_names == ("Shared name", "Shared name")


def test_disabled_repositories_do_not_contribute_names_counts_or_alias_preference():
    enabled = _repository("Enabled")
    disabled = _repository("Disabled", enabled=False)
    _commit(enabled, "Alice")
    for number in range(1, 5):
        _commit(disabled, "ALICE", number)
    _commit(disabled, "Disabled Person", 5)

    (person,) = git_people_summaries()

    assert person.name == "Alice"
    assert person.aliases == ("Alice",)
    assert person.commit_count == 1
    assert person.repository_count == 1
    assert person.repository_names == ("Enabled",)


def test_repository_involvement_uses_history_even_without_any_active_pdf():
    removed = _repository("Removed PDF")
    history_only = _repository("History only")
    commit = _commit(removed, "Alice")
    _document(removed, commit, removed=True)
    _commit(history_only, "Alice")

    (person,) = git_people_summaries()

    assert person.pdf_count == 0
    assert person.commit_count == 2
    assert person.repository_count == 2
    assert person.repository_names == ("History only", "Removed PDF")


def test_repository_involvement_does_not_follow_only_latest_pdf_committer():
    repository = _repository("Shared history")
    _commit(repository, "Alice", 1)
    latest = _commit(repository, "Bob", 2)
    _document(repository, latest)

    summaries = {person.name: person for person in git_people_summaries()}

    assert summaries["Alice"].pdf_count == 0
    assert summaries["Bob"].pdf_count == 1
    assert (
        summaries["Alice"].repository_names
        == summaries["Bob"].repository_names
        == ("Shared history",)
    )


def test_people_keep_existing_pdf_commit_name_order_independent_of_repository_count():
    primary = _repository("zebra")
    alice = _commit(primary, "Alice", 1)
    _document(primary, alice)
    for number in range(2, 6):
        _commit(primary, "Charlie", number)
    for name in ("beta", "Alpha", "alpha"):
        _commit(_repository(name), "Bob")

    summaries = git_people_summaries()

    assert [person.name for person in summaries] == ["Alice", "Charlie", "Bob"]
    assert [person.repository_count for person in summaries] == [1, 1, 3]
    assert summaries[-1].repository_names == ("Alpha", "alpha", "beta")


def test_repository_count_remains_two_queries_for_many_people_and_repositories(
    django_assert_num_queries,
):
    for index in range(60):
        repository = _repository(f"Repository {index:02}")
        _commit(repository, f"Person {index % 12:02}")
        _commit(repository, "Common committer", 2)

    with django_assert_num_queries(2):
        summaries = git_people_summaries()
    with django_assert_num_queries(0):
        assert summaries[0].name == "Common committer"
        assert summaries[0].repository_count == 60
        assert summaries[0].repository_names == tuple(
            f"Repository {index:02}" for index in range(60)
        )
        assert all(person.repository_count == 5 for person in summaries[1:])


def test_invalid_committer_names_cannot_create_repository_involvement():
    repository = _repository("Documents")
    for number, name in enumerate(("", "\n", "Bad\tName"), start=1):
        _commit(repository, name, number)
    assert git_people_summaries() == ()


def test_existing_summary_construction_defaults_to_empty_repository_tuple():
    person = GitPersonSummary("Alice", ("Alice",), 1, 0)
    assert person.repository_names == ()
    assert person.repository_count == 0


def test_people_use_reachable_history_after_activity_indexing_but_keep_legacy_evidence():
    indexed = _repository("Indexed history")
    legacy = _repository("Pending history")
    reachable = _commit(indexed, "Alice", 1)
    _commit(indexed, "Alice", 2)
    _commit(indexed, "No longer reachable", 3)
    _commit(legacy, "Alice", 1)
    GitCommit.objects.filter(pk=reachable.pk).update(in_activity_history=True)
    BitbucketRepository.objects.filter(pk=indexed.pk).update(
        activity_indexed_commit=reachable.commit_hash,
        activity_indexed_at=timezone.now(),
    )

    (person,) = git_people_summaries()

    assert person.name == "Alice"
    assert person.commit_count == 2
    assert person.repository_count == 2
    assert person.repository_names == ("Indexed history", "Pending history")
