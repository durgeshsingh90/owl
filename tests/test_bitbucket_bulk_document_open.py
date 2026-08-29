from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.test import Client
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import BitbucketRepository, PDFDocument
from bitbucket_search.services.document_actions import (
    BulkDocumentOpenFailure,
    BulkDocumentOpenResult,
    BulkDocumentUsageFailure,
)

pytestmark = pytest.mark.django_db


def _documents() -> tuple[PDFDocument, PDFDocument]:
    repository = BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="bitbucket.org/team/architecture",
        remote_url="ssh://git@bitbucket.org/team/architecture.git",
    )
    first = PDFDocument.objects.create(
        repository=repository,
        filename="First.pdf",
        relative_path="docs/First.pdf",
        open_count=4,
    )
    second = PDFDocument.objects.create(
        repository=repository,
        filename="Second.pdf",
        relative_path="docs/Second.pdf",
    )
    return first, second


def test_bulk_open_is_post_only_csrf_protected_and_strictly_loopback(client):
    url = reverse("bitbucket_search:documents_open_all")

    assert client.get(url, REMOTE_ADDR="127.0.0.1").status_code == 405
    assert (
        Client(enforce_csrf_checks=True)
        .post(url, {"document_id": "1"}, REMOTE_ADDR="127.0.0.1")
        .status_code
        == 403
    )
    assert client.post(url, {"document_id": "1"}, REMOTE_ADDR="198.51.100.12").status_code == 403


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    (
        ({}, "invalid_document_selection"),
        ({"document_id": "not-an-id"}, "invalid_document_selection"),
        ({"document_id": "0"}, "invalid_document_selection"),
        ({"document_id": [str(number) for number in range(1, 52)]}, "too_many_documents"),
    ),
)
def test_bulk_open_rejects_invalid_or_oversized_result_page_selections(
    client,
    monkeypatch,
    payload,
    expected_code,
):
    service = Mock()
    monkeypatch.setattr(views, "open_registered_pdfs", service)

    response = client.post(
        reverse("bitbucket_search:documents_open_all"),
        payload,
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json()["code"] == expected_code
    service.assert_not_called()


def test_bulk_open_requires_server_enforced_confirmation_above_threshold(
    client,
    monkeypatch,
    settings,
):
    settings.OPEN_ALL_CONFIRM_THRESHOLD = 10
    service = Mock(
        return_value=BulkDocumentOpenResult(
            requested_count=11,
            opened_documents=(),
            failures=(
                BulkDocumentOpenFailure(
                    document_id=1,
                    code="native_action_failed",
                    summary="Could not open this PDF.",
                ),
            ),
        )
    )
    monkeypatch.setattr(views, "open_registered_pdfs", service)
    url = reverse("bitbucket_search:documents_open_all")
    document_ids = [str(number) for number in range(1, 12)]

    unconfirmed = client.post(
        url,
        {"document_id": document_ids, "confirmed": "0"},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["code"] == "confirmation_required"
    service.assert_not_called()

    confirmed = client.post(
        url,
        {"document_id": document_ids, "confirmed": "1"},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert confirmed.status_code == 503
    service.assert_called_once_with(tuple(range(1, 12)))


def test_bulk_open_deduplicates_ids_and_reports_partial_async_result(client, monkeypatch):
    first, second = _documents()
    result = BulkDocumentOpenResult(
        requested_count=2,
        opened_documents=(first,),
        failures=(
            BulkDocumentOpenFailure(
                document_id=second.pk,
                code="native_action_failed",
                summary="OWL could not ask your operating system to open this PDF.",
            ),
        ),
    )
    service = Mock(return_value=result)
    monkeypatch.setattr(views, "open_registered_pdfs", service)

    response = client.post(
        reverse("bitbucket_search:documents_open_all"),
        {
            "document_id": [str(first.pk), str(first.pk), str(second.pk)],
            "return_to": "/pdfs/?chip=architecture&page=2",
        },
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    assert response.json() == {
        "state": "partially_opened",
        "requestedCount": 2,
        "openedCount": 1,
        "failedCount": 1,
        "usageFailureCount": 0,
        "documents": [
            {
                "documentId": first.pk,
                "openCount": 4,
                "lastOpenedAt": None,
            }
        ],
        "failures": [
            {
                "documentId": second.pk,
                "code": "native_action_failed",
                "detail": "OWL could not ask your operating system to open this PDF.",
            }
        ],
        "usageFailures": [],
    }
    service.assert_called_once_with((first.pk, second.pk))


def test_bulk_open_redirect_preserves_only_safe_search_return_and_messages_partial_result(
    client,
    monkeypatch,
):
    first, second = _documents()
    result = BulkDocumentOpenResult(
        requested_count=2,
        opened_documents=(first,),
        failures=(
            BulkDocumentOpenFailure(
                document_id=second.pk,
                code="native_action_failed",
                summary="Could not open this PDF.",
            ),
        ),
    )
    monkeypatch.setattr(views, "open_registered_pdfs", Mock(return_value=result))

    response = client.post(
        reverse("bitbucket_search:documents_open_all"),
        {
            "document_id": [str(first.pk), str(second.pk)],
            "return_to": "/pdfs/?chip=private+link&page=2#untrusted-fragment",
        },
        REMOTE_ADDR="127.0.0.1",
        follow=True,
    )

    assert response.redirect_chain == [("/pdfs/?chip=private+link&page=2", 302)]
    assert "Opened 1 of 2 PDFs. 1 could not be opened." in response.content.decode()

    unsafe = client.post(
        reverse("bitbucket_search:documents_open_all"),
        {
            "document_id": str(first.pk),
            "return_to": "https://example.invalid/pdfs/?chip=secret",
        },
        REMOTE_ADDR="127.0.0.1",
    )
    assert unsafe.status_code == 302
    assert unsafe.url == reverse("bitbucket_search:index")


def test_bulk_open_all_failures_return_safe_structured_failure(client, monkeypatch):
    first, _second = _documents()
    result = BulkDocumentOpenResult(
        requested_count=1,
        opened_documents=(),
        failures=(
            BulkDocumentOpenFailure(
                document_id=first.pk,
                code="native_action_failed",
                summary="OWL could not ask your operating system to open this PDF.",
            ),
        ),
    )
    monkeypatch.setattr(views, "open_registered_pdfs", Mock(return_value=result))

    response = client.post(
        reverse("bitbucket_search:documents_open_all"),
        {"document_id": str(first.pk)},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 503
    assert response.json()["state"] == "failed"
    assert response.json()["openedCount"] == 0
    assert response.json()["failedCount"] == 1


def test_bulk_open_reports_usage_failure_separately_from_successful_native_open(
    client,
    monkeypatch,
):
    first, _second = _documents()
    result = BulkDocumentOpenResult(
        requested_count=1,
        opened_documents=(first,),
        failures=(),
        usage_failures=(
            BulkDocumentUsageFailure(
                document_id=first.pk,
                code="document_unavailable",
                summary="This PDF opened, but its usage could not be recorded.",
            ),
        ),
    )
    monkeypatch.setattr(views, "open_registered_pdfs", Mock(return_value=result))

    response = client.post(
        reverse("bitbucket_search:documents_open_all"),
        {"document_id": str(first.pk)},
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["state"] == "opened_with_warnings"
    assert payload["openedCount"] == 1
    assert payload["failedCount"] == 0
    assert payload["usageFailureCount"] == 1
    assert payload["usageFailures"][0]["documentId"] == first.pk
