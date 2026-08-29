from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketPeopleGroupMember,
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentLifecycle,
)
from bitbucket_search.services.people import (
    PeopleGroupValidationError,
    create_people_group,
    git_people_summaries,
)

pytestmark = pytest.mark.django_db


def _repository(name: str, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.example.invalid/team/{name}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/{name}.git",
        enabled=enabled,
    )


def _commit(
    repository: BitbucketRepository,
    commit_hash: str,
    committer_name: str,
) -> GitCommit:
    committed_at = timezone.now() - timedelta(days=1)
    return GitCommit.objects.create(
        repository=repository,
        commit_hash=commit_hash,
        author_name="Git Author",
        committer_name=committer_name,
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )


def _document(
    repository: BitbucketRepository,
    relative_path: str,
    last_commit: GitCommit,
    *,
    lifecycle_state: str = PDFDocumentLifecycle.ACTIVE,
) -> PDFDocument:
    return PDFDocument.objects.create(
        repository=repository,
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        last_commit=last_commit,
        lifecycle_state=lifecycle_state,
    )


def test_people_group_models_normalize_names_and_keep_update_fields_consistent():
    group = BitbucketPeopleGroup.objects.create(name="  Ｐlatform\u3000  Team  ")
    member = BitbucketPeopleGroupMember.objects.create(
        group=group,
        person_name="  Alice   Smith  ",
    )

    assert group.name == "Platform Team"
    assert group.normalized_name == "platform team"
    assert member.person_name == "Alice Smith"
    assert member.normalized_person_name == "alice smith"

    group.name = "Cloud Reviewers"
    group.save(update_fields={"name"})
    member.person_name = "Bob Reviewer"
    member.save(update_fields={"person_name"})
    group.refresh_from_db()
    member.refresh_from_db()

    assert group.normalized_name == "cloud reviewers"
    assert member.normalized_person_name == "bob reviewer"


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (BitbucketPeopleGroup, {"name": ""}),
        (BitbucketPeopleGroup, {"name": "Team\nName"}),
        (BitbucketPeopleGroup, {"name": "x" * 256}),
    ],
)
def test_people_group_rejects_blank_control_and_oversized_names(model, kwargs):
    with pytest.raises(ValidationError):
        model.objects.create(**kwargs)


def test_people_group_member_rejects_invalid_and_duplicate_normalized_names():
    group = BitbucketPeopleGroup.objects.create(name="Platform Team")
    BitbucketPeopleGroupMember.objects.create(group=group, person_name="Alice")

    with pytest.raises(ValidationError):
        BitbucketPeopleGroupMember.objects.create(group=group, person_name="ALICE")
    with pytest.raises(ValidationError, match="control characters"):
        BitbucketPeopleGroupMember.objects.create(group=group, person_name="Bad\tName")


def test_people_group_name_is_case_insensitively_unique():
    BitbucketPeopleGroup.objects.create(name="Architecture Reviewers")

    with pytest.raises(ValidationError):
        BitbucketPeopleGroup.objects.create(name="  ARCHITECTURE reviewers ")


def test_git_people_summaries_collapse_aliases_and_count_committed_pdfs():
    enabled = _repository("enabled")
    alice_first = _commit(enabled, "a" * 40, "Alice Smith")
    alice_second = _commit(enabled, "b" * 40, "  Alice   Smith ")
    _commit(enabled, "c" * 40, "ALICE SMITH")
    bob = _commit(enabled, "d" * 40, "Bob Jones")

    _document(enabled, "docs/alice-one.pdf", alice_first)
    _document(enabled, "docs/alice-two.pdf", alice_second)
    _document(
        enabled,
        "docs/alice-removed.pdf",
        alice_first,
        lifecycle_state=PDFDocumentLifecycle.REMOVED,
    )
    _document(enabled, "docs/bob.pdf", bob)

    summaries = git_people_summaries()

    assert [person.name for person in summaries] == ["Alice Smith", "Bob Jones"]
    assert summaries[0].aliases == ("Alice Smith", "ALICE SMITH")
    assert summaries[0].commit_count == 3
    assert summaries[0].pdf_count == 2
    assert summaries[1].commit_count == 1
    assert summaries[1].pdf_count == 1


def test_git_people_summaries_exclude_disabled_repositories_and_inactive_pdfs():
    enabled = _repository("enabled")
    disabled = _repository("disabled", enabled=False)
    known_commit = _commit(enabled, "a" * 40, "Known Committer")
    disabled_commit = _commit(disabled, "b" * 40, "Disabled Committer")
    _document(enabled, "docs/known.pdf", known_commit)
    _document(disabled, "docs/disabled.pdf", disabled_commit)

    summaries = git_people_summaries()

    assert [(person.name, person.commit_count, person.pdf_count) for person in summaries] == [
        ("Known Committer", 1, 1)
    ]


def test_create_people_group_stores_canonical_known_members_and_deduplicates_aliases():
    repository = _repository("documents")
    _commit(repository, "a" * 40, "Alice Smith")
    _commit(repository, "b" * 40, "Alice Smith")
    _commit(repository, "c" * 40, "ALICE SMITH")
    _commit(repository, "d" * 40, "Bob Jones")

    group = create_people_group(
        "  Architecture   Reviewers ",
        ["alice smith", "ALICE SMITH", "Bob Jones"],
    )

    assert group.name == "Architecture Reviewers"
    assert list(group.members.values_list("person_name", flat=True)) == [
        "Alice Smith",
        "Bob Jones",
    ]


def test_create_people_group_validates_all_members_atomically():
    repository = _repository("documents")
    _commit(repository, "a" * 40, "Alice Smith")

    with pytest.raises(PeopleGroupValidationError, match="Unknown Git committer"):
        create_people_group("Reviewers", ["Alice Smith", "Unknown Person"])

    assert not BitbucketPeopleGroup.objects.exists()

    with pytest.raises(PeopleGroupValidationError, match="Select at least one"):
        create_people_group("Reviewers", [])
    assert not BitbucketPeopleGroup.objects.exists()


def test_create_people_group_rejects_duplicate_names_and_disabled_committers():
    enabled = _repository("enabled")
    disabled = _repository("disabled", enabled=False)
    _commit(enabled, "a" * 40, "Known Committer")
    _commit(disabled, "b" * 40, "Disabled Committer")
    create_people_group("Reviewers", ["Known Committer"])

    with pytest.raises(PeopleGroupValidationError, match="already exists"):
        create_people_group("REVIEWERS", ["Known Committer"])
    with pytest.raises(PeopleGroupValidationError, match="Unknown Git committer"):
        create_people_group("Disabled Team", ["Disabled Committer"])


def test_people_group_membership_persists_when_available_git_history_changes():
    repository = _repository("documents")
    commit = _commit(repository, "a" * 40, "Alice Smith")
    group = create_people_group("Reviewers", ["Alice Smith"])

    commit.delete()

    assert group.members.get().person_name == "Alice Smith"
    assert git_people_summaries() == ()
