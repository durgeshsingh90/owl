from __future__ import annotations

import re
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketHTTPSCredential,
    BitbucketHTTPSCredentialKind,
    BitbucketRepository,
    RepositorySyncJob,
)
from bitbucket_search.services.https_credentials import (
    CLOUD_ORIGIN,
    reset_https_secret_store_cache,
)

pytestmark = pytest.mark.django_db


def _csrf_token(client: Client) -> str:
    response = client.get(reverse("bitbucket_search:index"))
    match = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        response.content.decode(),
    )
    assert response.status_code == 200
    assert match is not None
    return match.group("token")


def _csrf_client(*, host: str = "localhost", remote_address: str = "127.0.0.1") -> Client:
    return Client(
        enforce_csrf_checks=True,
        HTTP_HOST=host,
        REMOTE_ADDR=remote_address,
    )


@pytest.mark.parametrize(
    ("host", "remote_address"),
    (
        ("localhost:8000", "127.0.0.1"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("[::1]:8000", "::1"),
    ),
)
@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_loopback_repository_add_accepts_opaque_origin_with_valid_csrf_token(
    host,
    remote_address,
    monkeypatch,
):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client(host=host, remote_address=remote_address)
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 302
    assert response.url == reverse("bitbucket_search:index")
    assert BitbucketRepository.objects.filter(display_name="architecture").exists()
    assert RepositorySyncJob.objects.count() == 1
    launched.assert_called_once_with()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_opaque_origin_does_not_bypass_csrf_token_validation(monkeypatch):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client()
    _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": "git@bitbucket.org:workspace/architecture.git"},
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


def test_loopback_schedule_tick_accepts_opaque_origin_form_with_valid_csrf_token():
    client = _csrf_client()
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_schedule_tick"),
        {"csrfmiddlewaretoken": token},
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 200
    assert response.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",),
    BITBUCKET_SECRET_BACKEND="database",
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-loopback-0123456789",
)
def test_loopback_bitbucket_https_credentials_accept_opaque_origin_with_valid_csrf_token():
    reset_https_secret_store_cache()
    client = _csrf_client(host="127.0.0.1:8000")
    csrf_token = _csrf_token(client)

    saved = client.post(
        reverse("bookmark_manager:bitbucket_https_credential_save"),
        {
            "csrfmiddlewaretoken": csrf_token,
            "origin": CLOUD_ORIGIN,
            "credential_kind": BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
            "account_name": "",
            "token": "not-a-real-token",
            "return_to": "settings",
        },
        HTTP_ORIGIN="null",
    )

    assert saved.status_code == 302
    assert saved.url == reverse("bookmark_manager:settings")
    assert BitbucketHTTPSCredential.objects.filter(origin=CLOUD_ORIGIN).exists()

    removed = client.post(
        reverse("bookmark_manager:bitbucket_https_credential_remove"),
        {
            "csrfmiddlewaretoken": csrf_token,
            "origin": CLOUD_ORIGIN,
            "confirm": "remove-bitbucket-credential",
            "return_to": "settings",
        },
        HTTP_ORIGIN="null",
    )

    assert removed.status_code == 302
    assert removed.url == reverse("bookmark_manager:settings")
    assert not BitbucketHTTPSCredential.objects.exists()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_opaque_origin_rejects_a_token_from_another_csrf_cookie(monkeypatch):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client()
    _csrf_token(client)
    wrong_token = _csrf_token(_csrf_client())

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": wrong_token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


@pytest.mark.parametrize(
    ("host", "remote_address"),
    (
        ("localhost", "192.0.2.20"),
        ("owl.example.test", "127.0.0.1"),
    ),
)
@override_settings(
    ALLOWED_HOSTS=("localhost", "owl.example.test"),
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",),
    OWL_ALLOW_NON_LOOPBACK=True,
)
def test_opaque_origin_remains_rejected_outside_loopback_host_and_client(
    host,
    remote_address,
    monkeypatch,
):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client(host=host, remote_address=remote_address)
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_forwarded_loopback_address_does_not_replace_remote_address(monkeypatch):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client(remote_address="192.0.2.20")
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN="null",
        HTTP_X_FORWARDED_FOR="127.0.0.1",
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


@pytest.mark.parametrize(
    ("origin", "secure"),
    (
        ("NULL", False),
        ("file://", False),
        ("null", True),
    ),
)
@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_only_exact_null_over_loopback_http_receives_the_exception(origin, secure, monkeypatch):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client()
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN=origin,
        secure=secure,
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_non_null_foreign_origin_remains_rejected_on_loopback(monkeypatch):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    client = _csrf_client()
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "git@bitbucket.org:workspace/architecture.git",
        },
        HTTP_ORIGIN="https://attacker.example",
    )

    assert response.status_code == 403
    assert not BitbucketRepository.objects.exists()
    launched.assert_not_called()


def test_opaque_origin_remains_rejected_for_bookmark_import():
    client = _csrf_client()
    page = client.get(reverse("bookmark_manager:settings"))
    match = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        page.content.decode(),
    )
    assert page.status_code == 200
    assert match is not None

    response = client.post(
        reverse("bookmark_manager:import"),
        {"csrfmiddlewaretoken": match.group("token"), "return_to": "settings"},
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 403


def test_custom_csrf_middleware_replaces_django_default_in_the_same_slot():
    custom = "core.middleware.LoopbackOpaqueOriginCsrfMiddleware"
    default = "django.middleware.csrf.CsrfViewMiddleware"

    assert custom in settings.MIDDLEWARE
    assert default not in settings.MIDDLEWARE
    assert settings.MIDDLEWARE.index(custom) == (
        settings.MIDDLEWARE.index("django.middleware.common.CommonMiddleware") + 1
    )
    assert settings.MIDDLEWARE.index(custom) < settings.MIDDLEWARE.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
