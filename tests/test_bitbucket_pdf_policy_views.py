from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.test import Client
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_local_policy import exclude_registered_pdf, frozen_pdf_path

pytestmark = pytest.mark.django_db


@pytest.fixture
def local_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.fixture
def policy_pdf(tmp_path: Path, settings) -> tuple[PDFDocument, Path]:
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="example.invalid/adr/architecture",
        remote_url="https://example.invalid/stash/scm/adr/architecture.git",
        sync_state=RepositorySyncState.READY,
    )
    checkout = managed_repository_path(repository)
    (checkout / "docs").mkdir(parents=True)
    subprocess.run(("git", "init", "--quiet", "-b", "main", str(checkout)), check=True)
    path = checkout / "docs" / "Architecture.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic local-policy view fixture\n%%EOF\n")
    subprocess.run(("git", "-C", str(checkout), "add", "docs/Architecture.pdf"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=OWL Synthetic Test",
            "-c",
            "user.email=owl-tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-m",
            "Synthetic PDF policy fixture",
        ),
        check=True,
    )
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=path.name,
        relative_path="docs/Architecture.pdf",
        file_size=path.stat().st_size,
    )
    return document, path


def test_old_delete_link_reports_read_only_and_never_shows_confirmation(
    local_client, policy_pdf, monkeypatch
):
    document, path = policy_pdf
    delete = Mock()
    monkeypatch.setattr("bitbucket_search.services.pdf_local_policy.delete_registered_pdf", delete)

    response = local_client.get(
        reverse("bitbucket_search:document_delete", args=(document.pk,)),
        {"return_page": "2"},
    )

    assert response.status_code == 410
    html = response.content.decode()
    assert "PDFs are read-only" in html
    assert "Individual file deletion is unavailable" in html
    assert "<form" not in html
    delete.assert_not_called()
    assert path.is_file()
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


def test_unconfirmed_delete_post_never_calls_service(local_client, policy_pdf, monkeypatch):
    document, path = policy_pdf
    delete = Mock()
    monkeypatch.setattr("bitbucket_search.services.pdf_local_policy.delete_registered_pdf", delete)

    response = local_client.post(reverse("bitbucket_search:document_delete", args=(document.pk,)))

    assert response.status_code == 410
    assert "PDFs are read-only" in response.content.decode()
    delete.assert_not_called()
    assert path.is_file()
    assert PDFDocument.objects.filter(pk=document.pk).exists()


@pytest.mark.parametrize("action", ["document_exclude", "document_resume", "document_delete"])
def test_policy_posts_require_csrf(action, policy_pdf):
    document, _path = policy_pdf
    csrf_client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    response = csrf_client.post(
        reverse(f"bitbucket_search:{action}", args=(document.pk,)), {"confirmed": "yes"}
    )
    assert response.status_code == 403
    assert not PDFLocalPolicy.objects.exists()


def test_stale_confirmed_delete_with_valid_same_origin_csrf_is_still_read_only(
    policy_pdf, monkeypatch
):
    document, _path = policy_pdf
    delete = Mock()
    monkeypatch.setattr("bitbucket_search.services.pdf_local_policy.delete_registered_pdf", delete)
    csrf_client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    url = reverse("bitbucket_search:document_delete", args=(document.pk,))
    page = csrf_client.get(reverse("bitbucket_search:index"))
    token = page.cookies["csrftoken"].value

    response = csrf_client.post(
        url,
        {
            "confirmed": "yes",
            "csrfmiddlewaretoken": token,
            "return_to": "/pdfs/?chip=networking",
        },
        HTTP_ORIGIN="http://127.0.0.1",
    )

    assert response.status_code == 410
    assert "PDFs are read-only" in response.content.decode()
    assert "Location" not in response
    delete.assert_not_called()


@pytest.mark.parametrize("action", ["document_exclude", "document_resume", "document_delete"])
def test_policy_filesystem_actions_remain_loopback_only(action, local_client, policy_pdf, settings):
    document, path = policy_pdf
    settings.OWL_ALLOW_NON_LOOPBACK = True

    response = local_client.post(
        reverse(f"bitbucket_search:{action}", args=(document.pk,)),
        {"confirmed": "yes"},
        REMOTE_ADDR="192.0.2.25",
    )

    assert response.status_code == 403
    assert path.exists()
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize("action", ["document_exclude", "document_resume"])
def test_policy_toggles_are_post_only(action, local_client, policy_pdf):
    document, _path = policy_pdf
    response = local_client.get(reverse(f"bitbucket_search:{action}", args=(document.pk,)))
    assert response.status_code == 405
    assert not PDFLocalPolicy.objects.exists()


@pytest.mark.parametrize("action", ["document_exclude", "document_resume", "document_delete"])
def test_policy_routes_reject_missing_documents(action, local_client):
    response = local_client.post(
        reverse(f"bitbucket_search:{action}", args=(987654321,)),
        {"confirmed": "yes"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 404


def test_legacy_saved_copy_stays_accessible_without_file_refresh_controls(local_client, policy_pdf):
    document, original_path = policy_pdf
    original_bytes = original_path.read_bytes()
    exclude_registered_pdf(document.pk)
    document.refresh_from_db()
    assert document.local_policy.state == PDFLocalPolicyState.EXCLUDED
    saved_path = frozen_pdf_path(document)
    assert saved_path is not None
    assert saved_path.read_bytes() == original_bytes
    assert not original_path.exists()
    assert views._display_full_path(document) == str(saved_path)

    page = local_client.get(reverse("bitbucket_search:index"))
    html = page.content.decode()
    assert '<span class="bb-pdf-policy-badge">Excluded</span>' not in html
    assert 'data-tooltip="Include in refresh"' not in html
    assert 'data-tooltip="Exclude from refresh"' not in html
    assert 'data-tooltip="Delete PDF"' not in html
    assert f'data-pdf-local-path="{saved_path}"' in html


@pytest.mark.parametrize("action", ["document_exclude", "document_resume"])
def test_old_refresh_controls_return_gone_without_mutating_saved_copy(
    action, local_client, policy_pdf, monkeypatch
):
    document, _path = policy_pdf
    exclude_registered_pdf(document.pk)
    wake = Mock(return_value=(1, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", wake)

    response = local_client.post(
        reverse(f"bitbucket_search:{action}", args=(document.pk,)),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 410
    assert response.json()["code"] == "repository_refresh_controls"
    assert "whole repository" in response.json()["detail"]
    document.refresh_from_db()
    assert document.local_policy.state == PDFLocalPolicyState.EXCLUDED
    wake.assert_not_called()
    html = local_client.get(reverse("bitbucket_search:index")).content.decode()
    assert 'data-tooltip="Awaiting refresh"' not in html
    assert f"/documents/{document.pk}/include/" not in html


def test_stale_delete_keeps_legacy_saved_copy_and_database_document(local_client, policy_pdf):
    document, original_path = policy_pdf
    exclude_registered_pdf(document.pk)
    document.refresh_from_db()
    saved_path = frozen_pdf_path(document)
    assert saved_path is not None
    saved_bytes = saved_path.read_bytes()
    document_id = document.pk

    response = local_client.post(
        reverse("bitbucket_search:document_delete", args=(document_id,)),
        {"confirmed": "yes"},
    )

    assert response.status_code == 410
    assert PDFDocument.objects.filter(pk=document_id).exists()
    assert not original_path.exists()
    assert saved_path.read_bytes() == saved_bytes
    policy = PDFLocalPolicy.objects.get(repository=document.repository)
    assert policy.state == PDFLocalPolicyState.EXCLUDED
    assert policy.document_id == document_id
    assert (managed_repository_path(document.repository) / ".git").is_dir()


def test_retired_file_exclusion_does_not_redirect_off_site(local_client, policy_pdf):
    document, _path = policy_pdf
    response = local_client.post(
        reverse("bitbucket_search:document_exclude", args=(document.pk,)),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 410
    assert "whole repository" in response.json()["detail"]

    retired_delete = local_client.get(
        reverse("bitbucket_search:document_delete", args=(document.pk,)),
        {"return_to": "https://untrusted.example.invalid/"},
    )
    assert retired_delete.status_code == 410
    assert "Location" not in retired_delete
    assert "https://untrusted.example.invalid/" not in retired_delete.content.decode()


def test_timeline_eager_loads_local_policy_without_per_pdf_queries(
    policy_pdf, django_assert_num_queries
):
    document, _path = policy_pdf
    PDFDocument.objects.create(
        repository=document.repository,
        filename="Second.pdf",
        relative_path="docs/Second.pdf",
    )
    with django_assert_num_queries(2):
        _page, groups = views._pdf_timeline_page(1)
    assert sum(len(group.rows) for group in groups) == 2


def test_loaded_timeline_rows_expose_only_read_only_file_controls(local_client, policy_pdf):
    document, _path = policy_pdf
    response = local_client.get(
        reverse("bitbucket_search:document_page"),
        {"page": "1"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    html = response.json()["html"]
    assert f'method="post" action="/pdfs/documents/{document.pk}/exclude/"' not in html
    assert f'method="get" action="/pdfs/documents/{document.pk}/delete/"' not in html
    assert html.count('name="csrfmiddlewaretoken"') == 3
    assert html.count('name="return_page" value="1"') == 2
