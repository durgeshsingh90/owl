from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views as bitbucket_views
from bitbucket_search.models import (
    BitbucketRepository,
    BitbucketStarredPerson,
    GitCommit,
)
from bitbucket_search.services import people as people_service
from bitbucket_search.services.people import (
    StarredPersonConflictError,
    StarredPersonValidationError,
    set_starred_person,
    starred_people_identities,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _repository(name: str = "architecture", *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.org/workspace/{name}",
        remote_url=f"ssh://git@bitbucket.org/workspace/{name}.git",
        enabled=enabled,
    )


def _commit(
    repository: BitbucketRepository,
    marker: str,
    person_name: str,
) -> GitCommit:
    committed_at = timezone.now() - timedelta(hours=1)
    return GitCommit.objects.create(
        repository=repository,
        commit_hash=marker * 40,
        author_name=f"Author {marker}",
        committer_name=person_name,
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )


def test_starred_person_model_normalizes_names_and_enforces_one_identity():
    starred = BitbucketStarredPerson.objects.create(person_name="  Alice   Smith ")

    assert starred.person_name == "Alice Smith"
    assert starred.normalized_person_name == "alice smith"
    assert str(starred) == "Alice Smith"

    with pytest.raises(ValidationError):
        BitbucketStarredPerson.objects.create(person_name="ALICE SMITH")
    with pytest.raises(ValidationError, match="cannot contain control characters"):
        BitbucketStarredPerson.objects.create(person_name="Bad\nName")


def test_set_starred_person_is_idempotent_and_orphan_star_can_be_removed():
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    _commit(repository, "b", "Alice Smith")
    _commit(repository, "c", "ALICE SMITH")

    starred = set_starred_person("  alice smith ", starred=True)

    assert starred.person_name == "Alice Smith"
    assert starred.identity_key == "alice smith"
    assert starred.starred is True
    assert starred_people_identities() == frozenset({"alice smith"})
    stored = BitbucketStarredPerson.objects.get()
    assert stored.person_name == "Alice Smith"

    repeated_star = set_starred_person("ALICE SMITH", starred=True)
    assert repeated_star.starred is True
    assert BitbucketStarredPerson.objects.count() == 1

    GitCommit.objects.filter(repository=repository).delete()
    assert not GitCommit.objects.filter(repository=repository).exists()
    assert starred_people_identities() == frozenset({"alice smith"})

    unstarred = set_starred_person("ALICE SMITH", starred=False)

    assert unstarred.person_name == "Alice Smith"
    assert unstarred.identity_key == "alice smith"
    assert unstarred.starred is False
    assert starred_people_identities() == frozenset()

    repeated_unstar = set_starred_person("ALICE SMITH", starred=False)
    assert repeated_unstar.starred is False
    assert not BitbucketStarredPerson.objects.exists()


def test_unstar_uses_current_canonical_display_after_alias_frequency_changes():
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    _commit(repository, "b", "Alice Smith")
    _commit(repository, "c", "ALICE SMITH")
    starred = set_starred_person("alice smith", starred=True)
    assert starred.person_name == "Alice Smith"

    _commit(repository, "d", "ALICE SMITH")
    _commit(repository, "e", "ALICE SMITH")
    _commit(repository, "f", "ALICE SMITH")

    unstarred = set_starred_person("alice smith", starred=False)

    assert unstarred.person_name == "ALICE SMITH"
    assert unstarred.identity_key == "alice smith"
    assert unstarred.starred is False


def test_set_starred_person_only_requires_known_committer_when_adding():
    enabled = _repository("enabled")
    disabled = _repository("disabled", enabled=False)
    _commit(enabled, "a", "Known Committer")
    _commit(disabled, "b", "Disabled Committer")
    set_starred_person("Known Committer", starred=True)

    with pytest.raises(StarredPersonValidationError, match="Unknown Git committer"):
        set_starred_person("Unknown Committer", starred=True)
    with pytest.raises(StarredPersonValidationError, match="Unknown Git committer"):
        set_starred_person("Disabled Committer", starred=True)

    unknown_unstar = set_starred_person("Unknown Committer", starred=False)
    disabled_unstar = set_starred_person("Disabled Committer", starred=False)

    assert unknown_unstar.starred is False
    assert disabled_unstar.starred is False
    assert starred_people_identities() == frozenset({"known committer"})


def test_set_starred_person_requires_a_boolean_desired_state():
    with pytest.raises(StarredPersonValidationError, match="must be true or false"):
        set_starred_person("Alice Smith", starred="true")


@pytest.mark.parametrize("error_type", (IntegrityError, OperationalError))
def test_set_starred_person_translates_database_races_to_safe_conflict(monkeypatch, error_type):
    def raise_conflict(*_args, **_kwargs):
        raise error_type("synthetic database conflict")

    monkeypatch.setattr(people_service, "_set_starred_person", raise_conflict)

    with pytest.raises(StarredPersonConflictError, match="person star is busy"):
        set_starred_person("Alice Smith", starred=True)


def test_people_context_exposes_stable_identity_keys_and_star_state(loopback_client):
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    _commit(repository, "b", "Bob Jones")
    BitbucketStarredPerson.objects.create(person_name="Alice Smith")

    response = loopback_client.get(reverse("bitbucket_search:index"))

    assert response.status_code == 200
    people = {person["name"]: person for person in response.context["git_people"]}
    assert people["Alice Smith"]["identity_key"] == "alice smith"
    assert people["Alice Smith"]["starred"] is True
    assert people["Bob Jones"]["identity_key"] == "bob jones"
    assert people["Bob Jones"]["starred"] is False


def test_people_star_endpoint_returns_json_then_supports_safe_redirect(loopback_client):
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    path = reverse("bitbucket_search:people_star_toggle")

    starred = loopback_client.post(
        path,
        {"person": "alice smith", "starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert starred.status_code == 200
    assert starred.json() == {
        "state": "success",
        "label": "Person updated",
        "detail": "Starred Alice Smith",
        "person": "Alice Smith",
        "identity_key": "alice smith",
        "starred": True,
    }
    repeated_star = loopback_client.post(
        path,
        {"person": "ALICE SMITH", "starred": "TRUE"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert repeated_star.status_code == 200
    assert repeated_star.json()["starred"] is True
    assert BitbucketStarredPerson.objects.count() == 1

    return_to = f"{reverse('bitbucket_search:index')}?committer=Alice+Smith"
    unstarred = loopback_client.post(
        path,
        {"person": "Alice Smith", "starred": "false", "return_to": return_to},
    )

    assert unstarred.status_code == 302
    assert unstarred.url == return_to
    assert not BitbucketStarredPerson.objects.exists()


def test_people_star_endpoint_accepts_query_state_for_native_form_fallback(loopback_client):
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    path = f"{reverse('bitbucket_search:people_star_toggle')}?starred=true"

    post_state_wins = loopback_client.post(
        path,
        {"person": "alice smith", "starred": "false"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert post_state_wins.status_code == 200
    assert post_state_wins.json()["starred"] is False
    assert not BitbucketStarredPerson.objects.exists()

    response = loopback_client.post(
        path,
        {
            "person": "alice smith",
            "return_to": reverse("bitbucket_search:index"),
        },
    )

    assert response.status_code == 302
    assert response.url == reverse("bitbucket_search:index")
    assert starred_people_identities() == frozenset({"alice smith"})


def test_people_star_endpoint_rejects_unknown_people_without_changing_state(loopback_client):
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    set_starred_person("Alice Smith", starred=True)

    response = loopback_client.post(
        reverse("bitbucket_search:people_star_toggle"),
        {"person": "Unknown Person", "starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json() == {
        "state": "invalid",
        "label": "Star not changed",
        "detail": "Unknown Git committer selection. Refresh and try again.",
    }
    assert starred_people_identities() == frozenset({"alice smith"})

    no_op_unstar = loopback_client.post(
        reverse("bitbucket_search:people_star_toggle"),
        {"person": "Unknown Person", "starred": "false"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert no_op_unstar.status_code == 200
    assert no_op_unstar.json()["starred"] is False
    assert starred_people_identities() == frozenset({"alice smith"})


@pytest.mark.parametrize("starred", (None, "", "toggle", "yes", "1"))
def test_people_star_endpoint_rejects_missing_or_malformed_desired_state(
    loopback_client,
    starred,
):
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    payload = {"person": "Alice Smith"}
    if starred is not None:
        payload["starred"] = starred

    response = loopback_client.post(
        reverse("bitbucket_search:people_star_toggle"),
        payload,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json() == {
        "state": "invalid",
        "label": "Star not changed",
        "detail": "Starred state must be true or false.",
    }
    assert not BitbucketStarredPerson.objects.exists()


def test_people_star_endpoint_returns_conflict_for_database_race(loopback_client, monkeypatch):
    def raise_conflict(*_args, **_kwargs):
        raise StarredPersonConflictError("The person star is busy. Refresh and try again.")

    monkeypatch.setattr(bitbucket_views, "set_starred_person", raise_conflict)

    response = loopback_client.post(
        reverse("bitbucket_search:people_star_toggle"),
        {"person": "Alice Smith", "starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "conflict",
        "label": "Star not changed",
        "detail": "The person star is busy. Refresh and try again.",
    }


def test_people_star_endpoint_is_post_only_csrf_protected_and_local_only(loopback_client):
    path = reverse("bitbucket_search:people_star_toggle")

    assert loopback_client.get(path).status_code == 405

    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert csrf_client.post(path, {"person": "Alice Smith"}).status_code == 403

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.40")
    assert remote_client.post(path, {"person": "Alice Smith"}).status_code == 403


def test_people_star_endpoint_accepts_loopback_opaque_origin_with_valid_csrf():
    repository = _repository()
    _commit(repository, "a", "Alice Smith")
    client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="localhost",
        REMOTE_ADDR="127.0.0.1",
    )
    page = client.get(reverse("bitbucket_search:index"))

    response = client.post(
        reverse("bitbucket_search:people_star_toggle"),
        {
            "csrfmiddlewaretoken": page.cookies["csrftoken"].value,
            "person": "Alice Smith",
            "starred": "true",
        },
        HTTP_ORIGIN="null",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    assert response.json()["starred"] is True
    assert starred_people_identities() == frozenset({"alice smith"})
