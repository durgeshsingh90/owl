from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.conf import settings
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory
from django.urls import reverse

from bitbucket.models import HTTPSCredential, Repository, SyncJob

pytestmark = pytest.mark.django_db


def _csrf_client() -> Client:
    return Client(
        enforce_csrf_checks=True,
        HTTP_HOST="localhost",
        REMOTE_ADDR="127.0.0.1",
    )


def _csrf_token(client: Client) -> str:
    request = RequestFactory().get("/")
    token = get_token(request)
    client.cookies[settings.CSRF_COOKIE_NAME] = request.META["CSRF_COOKIE"]
    return token


def test_standalone_bitbucket_accepts_local_opaque_origin_with_valid_csrf(monkeypatch):
    launched = Mock(return_value=True)
    monkeypatch.setattr("bitbucket.views.wake_sync_worker", launched)
    client = _csrf_client()
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket:settings_save"),
        {
            "csrfmiddlewaretoken": token,
            "repository_url": "https://private.example.invalid/stash/scm/adr/architecture.git",
            "access_token": "not-a-real-token",
        },
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 202
    assert Repository.objects.filter(project="adr", slug="architecture").exists()
    assert HTTPSCredential.objects.filter(origin="https://private.example.invalid").exists()
    assert SyncJob.objects.count() == 1
    launched.assert_called_once_with()


def test_opaque_origin_does_not_bypass_csrf_token_validation(monkeypatch):
    launched = Mock(return_value=True)
    monkeypatch.setattr("bitbucket.views.wake_sync_worker", launched)
    client = _csrf_client()
    _csrf_token(client)

    response = client.post(
        reverse("bitbucket:settings_save"),
        {
            "repository_url": "https://private.example.invalid/stash/scm/adr/architecture.git",
            "access_token": "not-a-real-token",
        },
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 403
    assert Repository.objects.count() == 0
    launched.assert_not_called()
