"""Truthful Git-committer summaries and persisted People groups."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Max, Q

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketPeopleGroupMember,
    BitbucketStarredPerson,
    GitCommit,
    InvalidPeopleName,
    PDFDocument,
    PDFDocumentLifecycle,
    canonical_people_name,
    normalize_people_name,
)
from bitbucket_search.services.logging_events import get_logger, log_event

logger = get_logger("actions")


class PeopleGroupValidationError(ValueError):
    """Raised when a requested People group is invalid or no longer resolvable."""


class StarredPersonValidationError(ValueError):
    """Raised when a requested Git committer star cannot be changed safely."""


class StarredPersonConflictError(RuntimeError):
    """Raised when concurrent database work prevents a safe star update."""


@dataclass(frozen=True, slots=True)
class GitPersonSummary:
    """Available-history statistics for one case-insensitive Git committer."""

    name: str
    aliases: tuple[str, ...]
    commit_count: int
    pdf_count: int
    repository_names: tuple[str, ...] = ()
    last_committed_at: datetime | None = None

    @property
    def repository_count(self) -> int:
        """Count repository identities, even when display names are shared."""

        return len(self.repository_names)


@dataclass(frozen=True, slots=True)
class StarredPersonSetResult:
    """Canonical state returned after setting one Git committer star."""

    person_name: str
    identity_key: str
    starred: bool


def git_people_summaries() -> tuple[GitPersonSummary, ...]:
    """Summarize Git committers from enabled repositories' available history.

    ``GitCommit`` already guarantees one row per repository and commit hash, so
    counting rows here counts each available commit once. PDF counts describe
    active PDFs whose latest available Git commit has this committer identity.
    Repository involvement comes from that history, independently of whether
    any current PDF is attributed to the person or matches the current search.
    Recency uses the latest Git committer timestamp in the same available history,
    not the authored timestamp or when OWL discovered a PDF.
    """

    alias_counts: dict[str, Counter[str]] = defaultdict(Counter)
    repositories_by_identity: dict[str, dict[int, str]] = defaultdict(dict)
    latest_commit_by_identity: dict[str, datetime] = {}
    for row in (
        GitCommit.objects.filter(repository__enabled=True)
        .filter(
            Q(in_activity_history=True)
            | Q(repository__activity_indexed_commit="")
            | Q(repository__activity_indexed_at__isnull=True)
        )
        .order_by()
        .values("committer_name", "repository_id", "repository__display_name")
        .annotate(row_count=Count("id"), last_committed_at=Max("committed_at"))
        .iterator()
    ):
        try:
            display_name = canonical_people_name(row["committer_name"])
        except InvalidPeopleName:
            continue
        identity = display_name.casefold()
        alias_counts[identity][display_name] += row["row_count"]
        repositories_by_identity[identity][row["repository_id"]] = row["repository__display_name"]
        committed_at = row["last_committed_at"]
        if committed_at is not None and (
            identity not in latest_commit_by_identity
            or committed_at > latest_commit_by_identity[identity]
        ):
            latest_commit_by_identity[identity] = committed_at

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
                repository_names=tuple(
                    repository_name
                    for _repository_id, repository_name in sorted(
                        repositories_by_identity[identity].items(),
                        key=lambda repository: (
                            repository[1].casefold(),
                            repository[1],
                            repository[0],
                        ),
                    )
                ),
                last_committed_at=latest_commit_by_identity.get(identity),
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


def starred_people_identities() -> frozenset[str]:
    """Return the durable normalized identities currently starred in OWL."""

    return frozenset(
        BitbucketStarredPerson.objects.order_by().values_list(
            "normalized_person_name",
            flat=True,
        )
    )


def set_starred_person(person_name: object, *, starred: bool) -> StarredPersonSetResult:
    """Atomically set one Git committer's OWL-owned star to the desired state."""

    log_event(logger, logging.INFO, "people_star_set_requested", starred=starred)
    try:
        result = _set_starred_person(person_name, starred=starred)
    except StarredPersonValidationError as error:
        log_event(
            logger,
            logging.WARNING,
            "people_star_set_rejected",
            error=error,
            reason="invalid_person",
        )
        raise
    except (IntegrityError, OperationalError) as error:
        log_event(
            logger,
            logging.WARNING,
            "people_star_set_conflicted",
            error=error,
            reason="database_conflict",
        )
        raise StarredPersonConflictError(
            "The person star is busy. Refresh and try again."
        ) from error
    except Exception as error:
        log_event(logger, logging.ERROR, "people_star_set_failed", error=error)
        raise
    log_event(
        logger,
        logging.INFO,
        "people_star_set_completed",
        starred=result.starred,
    )
    return result


@transaction.atomic
def _set_starred_person(person_name: object, *, starred: bool) -> StarredPersonSetResult:
    if not isinstance(starred, bool):
        raise StarredPersonValidationError("Starred state must be true or false.")
    try:
        requested_name = canonical_people_name(person_name)
    except InvalidPeopleName as error:
        raise StarredPersonValidationError(str(error)) from error
    requested_identity = requested_name.casefold()

    known_people = {
        normalize_people_name(alias): person
        for person in git_people_summaries()
        for alias in person.aliases
    }
    person = known_people.get(requested_identity)

    starred_person = (
        BitbucketStarredPerson.objects.select_for_update()
        .filter(normalized_person_name=requested_identity)
        .first()
    )
    if person is not None:
        display_name = person.name
    elif starred_person is not None:
        display_name = starred_person.person_name
    else:
        display_name = requested_name

    if not starred:
        if starred_person is not None:
            starred_person.delete()
        return StarredPersonSetResult(
            person_name=display_name,
            identity_key=requested_identity,
            starred=False,
        )

    if starred_person is not None:
        return StarredPersonSetResult(
            person_name=display_name,
            identity_key=requested_identity,
            starred=True,
        )

    if person is None:
        raise StarredPersonValidationError(
            "Unknown Git committer selection. Refresh and try again."
        )

    identity_key = normalize_people_name(person.name)
    try:
        BitbucketStarredPerson.objects.create(person_name=person.name)
    except ValidationError as error:
        raise StarredPersonValidationError(
            "The person star could not be changed. Refresh and try again."
        ) from error

    return StarredPersonSetResult(
        person_name=person.name,
        identity_key=identity_key,
        starred=True,
    )


def create_people_group(
    name: object,
    member_names: Iterable[object],
) -> BitbucketPeopleGroup:
    """Create a group containing canonical names for currently known committers."""

    log_event(logger, logging.INFO, "people_group_create_requested")
    try:
        group, member_count = _create_people_group(name, member_names)
    except PeopleGroupValidationError as error:
        log_event(
            logger,
            logging.WARNING,
            "people_group_create_rejected",
            error=error,
            reason="invalid_group",
        )
        raise
    except Exception as error:
        log_event(logger, logging.ERROR, "people_group_create_failed", error=error)
        raise
    log_event(logger, logging.INFO, "people_group_create_completed", count=member_count)
    return group


@transaction.atomic
def _create_people_group(name: object, member_names: Iterable[object]):
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
    return group, len(canonical_members)
