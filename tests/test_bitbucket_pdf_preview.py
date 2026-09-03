from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from bitbucket_search.models import BitbucketRepository, PDFDocument, RepositorySyncState
from bitbucket_search.services.git_sync import managed_repository_path

pytestmark = pytest.mark.django_db


def _preview_document(tmp_path: Path, settings) -> tuple[PDFDocument, Path]:
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="bitbucket.org/team/architecture",
        remote_url="ssh://git@bitbucket.org/team/architecture.git",
        sync_state=RepositorySyncState.READY,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    pdf_path = checkout / "Architecture Guide.pdf"
    pdf_bytes = b"%PDF-1.4\nsynthetic inline preview\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path=pdf_path.name,
        file_size=len(pdf_bytes),
    )
    return document, pdf_path


def _csrf_token(client: Client) -> str:
    page = client.get(reverse("bitbucket_search:index"))
    match = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        page.content.decode(),
    )
    assert page.status_code == 200
    assert match is not None
    return match.group("token")


def test_preview_streams_registered_pdf_inline_and_counts_open(tmp_path, settings):
    document, pdf_path = _preview_document(tmp_path, settings)
    client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1:8000",
        REMOTE_ADDR="127.0.0.1",
    )
    token = _csrf_token(client)

    response = client.post(
        reverse("bitbucket_search:document_preview", args=(document.pk,)),
        {"csrfmiddlewaretoken": token},
        HTTP_ORIGIN="null",
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'inline; filename="Architecture Guide.pdf"'
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"
    assert b"".join(response.streaming_content) == pdf_path.read_bytes()
    document.refresh_from_db()
    assert document.open_count == 1


def test_preview_requires_post_csrf_and_strict_loopback(tmp_path, settings):
    document, _pdf_path = _preview_document(tmp_path, settings)
    route = reverse("bitbucket_search:document_preview", args=(document.pk,))

    assert Client().get(route, REMOTE_ADDR="127.0.0.1").status_code == 405

    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="localhost",
        REMOTE_ADDR="127.0.0.1",
    )
    _csrf_token(csrf_client)
    assert csrf_client.post(route, HTTP_ORIGIN="null").status_code == 403

    with override_settings(OWL_ALLOW_NON_LOOPBACK=True):
        assert Client().post(route, REMOTE_ADDR="192.0.2.25").status_code == 403
