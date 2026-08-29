"""Truthful Git-committer summaries and persisted People groups."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketPeopleGroupMember,
    GitCommit,
    InvalidPeopleName,
    PDFDocument,
    PDFDocumentLifecycle,
    canonical_people_name,
    normalize_people_name,
)


class PeopleGroupValidationError(ValueError):
    """Raised when a requested People group is invalid or no longer resolvable."""


@dataclass(frozen=True, slots=True)
class GitPersonSummary:
    """Available-history statistics for one case-insensitive Git committer."""

    name: str
    aliases: tuple[str, ...]
    commit_count: int
    pdf_count: int


def git_people_summaries() -> tuple[GitPersonSummary, ...]:
    """Summarize Git committers from enabled repositories' available history.

    ``GitCommit`` already guarantees one row per repository and commit hash, so
    counting rows here counts each available commit once. PDF counts describe
    active PDFs whose latest available Git commit has this committer identity.
    """

    alias_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in (
        GitCommit.objects.filter(repository__enabled=True)
        .order_by()
        .values("committer_name")
        .annotate(row_count=Count("id"))
        .iterator()
    ):
        try:
            display_name = canonical_people_name(row["committer_name"])
        except InvalidPeopleName:
            continue
        alias_counts[display_name.casefold()][display_name] += row["row_count"]

    pdf_counts: Counter[str] = Counter()
    for row in (
        PDFDocument.objects.filter(
            repository__enabled=True,
            lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            last_commit__repository__enabled=True,
        )
        .order_by()
        .values("last_commit__committer_name")
        .annotate(row_count=Count("id"))
        .iterator()
    ):
        try:
            identity = normalize_people_name(row["last_commit__committer_name"])
        except InvalidPeopleName:
            continue
        if identity in alias_counts:
            pdf_counts[identity] += row["row_count"]

    summaries: list[GitPersonSummary] = []
    for identity, counts_by_alias in alias_counts.items():
        aliases_by_preference = sorted(
            counts_by_alias,
            key=lambda alias: (-counts_by_alias[alias], alias.casefold(), alias),
        )
        canonical_name = aliases_by_preference[0]
        remaining_aliases = sorted(
            aliases_by_preference[1:],
            key=lambda alias: (alias.casefold(), alias),
        )
        summaries.append(
            GitPersonSummary(
                name=canonical_name,
                aliases=(canonical_name, *remaining_aliases),
                commit_count=sum(counts_by_alias.values()),
                pdf_count=pdf_counts[identity],
            )
        )

    summaries.sort(
        key=lambda person: (
            -person.pdf_count,
            -person.commit_count,
            person.name.casefold(),
            person.name,
        )
    )
    return tuple(summaries)


@transaction.atomic
def create_people_group(
    name: object,
    member_names: Iterable[object],
) -> BitbucketPeopleGroup:
    """Create a group containing canonical names for currently known committers."""

    try:
        group_name = canonical_people_name(name)
    except InvalidPeopleName as error:
        raise PeopleGroupValidationError(str(error)) from error

    known_people = {normalize_people_name(person.name): person for person in git_people_summaries()}
    if isinstance(member_names, (str, bytes)):
        requested_members = (member_names,)
    else:
        try:
            requested_members = tuple(member_names)
        except TypeError as error:
            raise PeopleGroupValidationError(
                "Select at least one known Git committer for this People group."
            ) from error

    canonical_members: dict[str, str] = {}
    unknown_members: list[str] = []
    for raw_name in requested_members:
        try:
            display_name = canonical_people_name(raw_name)
            identity = display_name.casefold()
        except InvalidPeopleName as error:
            raise PeopleGroupValidationError(str(error)) from error
        person = known_people.get(identity)
        if person is None:
            unknown_members.append(display_name)
            continue
        canonical_members[identity] = person.name

    if unknown_members:
        unknown_display = ", ".join(sorted(unknown_members, key=str.casefold))
        raise PeopleGroupValidationError(
            f"Unknown Git committer selection: {unknown_display}. Refresh and try again."
        )
    if not canonical_members:
        raise PeopleGroupValidationError(
            "Select at least one known Git committer for this People group."
        )

    if BitbucketPeopleGroup.objects.filter(normalized_name=group_name.casefold()).exists():
        raise PeopleGroupValidationError("A People group with this name already exists.")

    try:
        group = BitbucketPeopleGroup.objects.create(name=group_name)
        for identity, person_name in sorted(canonical_members.items()):
            BitbucketPeopleGroupMember.objects.create(
                group=group,
                person_name=person_name,
                normalized_person_name=identity,
            )
    except ValidationError as error:
        message = error.messages[0] if error.messages else "The People group is invalid."
        raise PeopleGroupValidationError(message) from error
    except IntegrityError as error:
        raise PeopleGroupValidationError(
            "A People group with this name or member already exists."
        ) from error
    return group
