from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentTimelineBasis,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import DocumentActionError

pytestmark = pytest.mark.django_db


def _repository(name: str = "networking") -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.org/cloud-team/{name}",
        remote_url=f"ssh://git@bitbucket.org/cloud-team/{name}.git",
        sync_state=RepositorySyncState.READY,
    )


@pytest.mark.parametrize(
    ("value", "expected_key", "expected_label"),
    (
        (date(2026, 8, 29), "today", "Today"),
        (date(2026, 8, 28), "yesterday", "Yesterday"),
        (date(2026, 8, 24), "week", "This Week"),
        (date(2026, 8, 1), "month", "This Month"),
        (date(2026, 5, 29), "three-months", "Last 3 Months"),
        (date(2026, 2, 28), "six-months", "Last 6 Months"),
        (date(2026, 1, 1), "year", "This Year"),
        (date(2025, 12, 31), "last-year", "Last Year"),
        (date(2024, 12, 31), "year-2024", "2024"),
    ),
)
def test_timeline_buckets_are_exclusive_and_newest_first(value, expected_key, expected_label):
    key, label, _detail = views._timeline_bucket(value, today=date(2026, 8, 29))

    assert key == expected_key
    assert label == expected_label


def test_timeline_row_labels_git_author_without_claiming_push_or_project_evidence(
    tmp_path,
    settings,
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed-repositories"
    repository = _repository()
    repository.history_is_shallow = False
    repository.save(update_fields=("history_is_shallow", "updated_at"))
    committed_at = datetime(2026, 8, 20, 9, 15, tzinfo=UTC)
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash="a" * 40,
        author_name="A. Architect",
        committer_name="C. Committer",
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Architecture.pdf",
        relative_path="docs/Architecture.pdf",
        added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
        added_commit=commit,
        timeline_at=committed_at,
        timeline_basis=PDFDocumentTimelineBasis.GIT_ADDED,
    )

    row = views._timeline_row(document)

    assert row.added_by_label == "A. Architect · Git author"
    assert "Git commit date" in row.added_date_detail
    assert row.project_label == ""
    assert row.history_label == "Full reachable history"
    assert "Pushed by" not in repr(row)
    assert row.full_path.endswith(f"/{repository.pk}-networking/docs/Architecture.pdf")
    assert row.display_path == "networking/docs/Architecture.pdf"
    assert row.path_copy_available is True


def test_unknown_addition_uses_owl_discovery_and_available_history_labels(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed-repositories"
    repository = _repository()
    discovered_at = timezone.now() - timedelta(days=7)
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Legacy.pdf",
        relative_path="legacy/Legacy.pdf",
        discovered_at=discovered_at,
        timeline_at=discovered_at,
        added_evidence=PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY,
        timeline_basis=PDFDocumentTimelineBasis.OWL_DISCOVERED,
    )

    row = views._timeline_row(document)

    assert row.added_by_label == "Unavailable in available Git history"
    assert "OWL discovery date" in row.added_date_detail
    assert row.history_label == "Available history · Git-added date unavailable"


@override_settings(BITBUCKET_PDF_PAGE_SIZE=10)
def test_document_page_returns_json_for_progressive_loading_and_html_without_javascript(client):
    repository = _repository()
    observed_at = timezone.now()
    for index in range(11):
        PDFDocument.objects.create(
            repository=repository,
            filename=f"Document-{index:02d}.pdf",
            relative_path=f"docs/Document-{index:02d}.pdf",
            timeline_at=observed_at - timedelta(minutes=index),
        )

    url = f"{reverse('bitbucket_search:document_page')}?page=2"
    response = client.get(
        url,
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 2
    assert payload["nextPageUrl"] == ""
    assert payload["html"].count("data-pdf-row") == 1
    assert "Document-10.pdf" in payload["html"]
    assert 'name="return_page" value="2"' in payload["html"]

    fallback = client.get(url, REMOTE_ADDR="127.0.0.1")
    assert fallback.status_code == 200
    assert "Document-10.pdf" in fallback.content.decode()


@override_settings(OWL_ALLOW_NON_LOOPBACK=True)
def test_native_document_endpoints_remain_strictly_loopback(client):
    open_response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": 1}),
        REMOTE_ADDR="198.51.100.12",
    )
    reveal_response = client.post(
        reverse("bitbucket_search:document_reveal", kwargs={"document_id": 1}),
        REMOTE_ADDR="198.51.100.12",
    )

    assert open_response.status_code == 403
    assert reveal_response.status_code == 403


def test_native_document_endpoints_return_safe_async_results(client, monkeypatch):
    repository = _repository()
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Guide.pdf",
        relative_path="docs/Guide.pdf",
        open_count=3,
        last_opened_at=timezone.now(),
    )
    opened = Mock(return_value=document)
    revealed = Mock(return_value=document)
    monkeypatch.setattr(views, "open_registered_pdf", opened)
    monkeypatch.setattr(views, "reveal_registered_pdf", revealed)

    common = {
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
    }
    open_response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": document.pk}),
        **common,
    )
    reveal_response = client.post(
        reverse("bitbucket_search:document_reveal", kwargs={"document_id": document.pk}),
        **common,
    )

    assert open_response.status_code == 200
    assert open_response.json()["openCount"] == 3
    assert reveal_response.status_code == 200
    assert reveal_response.json()["openCount"] == 3
    opened.assert_called_once_with(document.pk)
    revealed.assert_called_once_with(document.pk)

    fallback = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": document.pk}),
        {"return_page": "2"},
        REMOTE_ADDR="127.0.0.1",
    )
    assert fallback.status_code == 302
    assert fallback.url == (
        f"{reverse('bitbucket_search:document_page')}?page=2#pdf-document-{document.pk}"
    )


def test_document_action_failure_never_echoes_a_local_path(client, monkeypatch):
    monkeypatch.setattr(
        views,
        "open_registered_pdf",
        Mock(
            side_effect=DocumentActionError(
                "document_unavailable",
                "This PDF is missing from the managed repository.",
            )
        ),
    )

    response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": 404}),
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "failed",
        "code": "document_unavailable",
        "detail": "This PDF is missing from the managed repository.",
    }


def test_document_routes_never_accept_a_browser_supplied_filesystem_path(client):
    response = client.post(
        "/pdfs/documents/%2Ftmp%2Fsecret.pdf/open/",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 404
