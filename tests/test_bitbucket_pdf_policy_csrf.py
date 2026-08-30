from unittest.mock import Mock

import pytest
from django.test import Client
from django.urls import reverse

from bitbucket_search.models import BitbucketRepository, PDFDocument
from bitbucket_search.services.document_actions import DocumentActionError

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("route", "service"),
    [
        ("document_exclude", "exclude_registered_pdf"),
        ("document_resume", "resume_registered_pdf"),
        ("document_delete", "delete_registered_pdf"),
    ],
)
@pytest.mark.parametrize(
    ("origin", "with_token", "remote_addr", "allowed"),
    [
        ("null", True, "127.0.0.1", True),
        ("null", False, "127.0.0.1", False),
        ("https://foreign.example.invalid", True, "127.0.0.1", False),
        ("null", True, "192.0.2.25", False),
    ],
)
def test_local_pdf_controls_preserve_csrf_for_opaque_browser_origins(
    monkeypatch, route, service, origin, with_token, remote_addr, allowed
):
    repository = BitbucketRepository.objects.create(
        display_name="Sample",
        canonical_remote_key="example.invalid/team/sample",
        remote_url="https://example.invalid/team/sample.git",
    )
    document = PDFDocument.objects.create(
        repository=repository, filename="Sample.pdf", relative_path="Sample.pdf"
    )
    action = Mock(side_effect=DocumentActionError("repository_busy", "Synthetic busy repository"))
    monkeypatch.setattr(f"bitbucket_search.views.{service}", action)
    client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=remote_addr)
    page = client.get(reverse("bitbucket_search:index"))
    data = {"confirmed": "yes"}
    if with_token:
        data["csrfmiddlewaretoken"] = page.cookies["csrftoken"].value

    response = client.post(
        reverse(f"bitbucket_search:{route}", args=(document.pk,)),
        data,
        HTTP_ORIGIN=origin,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    if allowed:
        assert response.status_code != 403
        action.assert_called_once()
    else:
        assert response.status_code == 403
        action.assert_not_called()
