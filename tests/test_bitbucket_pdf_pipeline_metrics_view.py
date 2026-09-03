from __future__ import annotations

import json

import pytest
from django.urls import reverse

from bitbucket_search.models import BitbucketRepository


pytestmark = pytest.mark.django_db


def test_pipeline_metrics_is_local_versioned_and_never_cached(client, settings, tmp_path):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"

    response = client.get(
        reverse("bitbucket_search:pipeline_metrics"),
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schemaVersion"] == 1
    assert payload["topBarActivityIndicator"]["state"] == "hidden"
    assert payload["topBarActivityIndicator"]["hasFreshRunningWork"] is False
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"


def test_pipeline_metrics_rejects_mutation_and_nonlocal_reads(client, settings, tmp_path):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    endpoint = reverse("bitbucket_search:pipeline_metrics")

    assert client.post(endpoint, REMOTE_ADDR="127.0.0.1").status_code == 405
    assert client.get(endpoint, REMOTE_ADDR="203.0.113.10").status_code == 403


def test_pipeline_metrics_payload_contains_no_repository_address_or_content(
    client,
    settings,
    tmp_path,
):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path / "pipeline-state"
    marker_values = (
        "secret-token-marker",
        "ssh://git@private.example.invalid/team/repository.git",
        "/private/checkout/marker",
        "private extracted PDF text marker",
    )
    BitbucketRepository.objects.create(
        display_name="secret-token-marker",
        canonical_remote_key="private.example.invalid/team/repository",
        remote_url="ssh://git@private.example.invalid/team/repository.git",
        local_path="/private/checkout/marker",
        status_message="private extracted PDF text marker",
    )

    response = client.get(
        reverse("bitbucket_search:pipeline_metrics"),
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    serialized = json.dumps(response.json(), sort_keys=True)
    for marker in marker_values:
        assert marker not in serialized
