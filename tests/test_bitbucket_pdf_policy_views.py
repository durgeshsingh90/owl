from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncJob,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import DocumentActionError
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


def test_delete_confirmation_get_is_read_only_and_explains_scope(
    local_client, policy_pdf, monkeypatch
):
    document, path = policy_pdf
    delete = Mock()
    monkeypatch.setattr(views, "delete_registered_pdf", delete)

    response = local_client.get(
        reverse("bitbucket_search:document_delete", args=(document.pk,)),
        {"return_page": "2"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Delete this local PDF?" in html
    assert "Remove its database record and any indexed text not shared with another PDF" in html
    assert 'class="app-main"' in html
    assert 'class="app-layout"' not in html
    assert "minimal exclusion rule" in html
    assert "remote repository and Git history are not changed" in html
    assert 'name="csrfmiddlewaretoken"' in html
    assert 'name="confirmed" value="yes"' in html
    assert f"/pdfs/documents/page/?page=2#pdf-document-{document.pk}" in html
    delete.assert_not_called()
    assert path.is_file()
    assert PDFDocument.objects.filter(pk=document.pk).exists()
    assert not PDFLocalPolicy.objects.exists()


def test_unconfirmed_delete_post_never_calls_service(local_client, policy_pdf, monkeypatch):
    document, path = policy_pdf
    delete = Mock()
    monkeypatch.setattr(views, "delete_registered_pdf", delete)

    response = local_client.post(reverse("bitbucket_search:document_delete", args=(document.pk,)))

    assert response.status_code == 400
    assert "Confirm below" in response.content.decode()
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


def test_confirmed_delete_accepts_same_origin_csrf_and_preserves_search_return(
    policy_pdf, monkeypatch
):
    document, _path = policy_pdf
    delete = Mock()
    monkeypatch.setattr(views, "delete_registered_pdf", delete)
    csrf_client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    url = reverse("bitbucket_search:document_delete", args=(document.pk,))
    confirmation = csrf_client.get(url)
    token = confirmation.cookies["csrftoken"].value

    response = csrf_client.post(
        url,
        {
            "confirmed": "yes",
            "csrfmiddlewaretoken": token,
            "return_to": "/pdfs/?chip=networking",
        },
        HTTP_ORIGIN="http://127.0.0.1",
    )

    assert response.status_code == 302
    assert response.url == "/pdfs/?chip=networking"
    delete.assert_called_once_with(document.pk, confirmed=True)


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


def test_exclude_retains_a_saved_copy_and_renders_include_action(local_client, policy_pdf):
    document, original_path = policy_pdf
    original_bytes = original_path.read_bytes()
    response = local_client.post(
        reverse("bitbucket_search:document_exclude", args=(document.pk,)),
        {"return_to": "/pdfs/?chip=architecture"},
    )

    assert response.status_code == 302
    assert response.url == f"/pdfs/?chip=architecture#pdf-document-{document.pk}"
    document.refresh_from_db()
    assert document.local_policy.state == PDFLocalPolicyState.EXCLUDED
    saved_path = frozen_pdf_path(document)
    assert saved_path is not None
    assert saved_path.read_bytes() == original_bytes
    assert not original_path.exists()
    assert views._display_full_path(document) == str(saved_path)

    page = local_client.get(reverse("bitbucket_search:index"))
    html = page.content.decode()
    assert '<span class="bb-pdf-policy-badge">Excluded</span>' in html
    assert 'data-tooltip="Include in refresh"' in html
    assert 'data-tooltip="Delete PDF"' in html
    assert f'data-pdf-local-path="{saved_path}"' in html


def test_include_queues_worker_and_renders_awaiting_refresh(local_client, policy_pdf, monkeypatch):
    document, _path = policy_pdf
    exclude_registered_pdf(document.pk)
    wake = Mock(return_value=(1, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", wake)

    response = local_client.post(
        reverse("bitbucket_search:document_resume", args=(document.pk,)),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    assert response.json()["state"] == PDFLocalPolicyState.RESUMING
    document.refresh_from_db()
    assert document.local_policy.state == PDFLocalPolicyState.RESUMING
    job = RepositorySyncJob.objects.get(repository=document.repository)
    wake.assert_called_once_with(job_ids=(job.pk,))
    html = local_client.get(reverse("bitbucket_search:index")).content.decode()
    assert '<span class="bb-pdf-policy-badge">Awaiting refresh</span>' in html
    assert 'data-tooltip="Awaiting refresh"' in html
    assert 'aria-label="Include in refresh: Architecture.pdf" disabled aria-busy="true"' in html


@pytest.mark.parametrize("failure", ["database", "worker"])
def test_include_queue_failure_is_reported_as_pending(
    failure, local_client, policy_pdf, monkeypatch
):
    document, _path = policy_pdf
    exclude_registered_pdf(document.pk)
    if failure == "database":
        monkeypatch.setattr(views, "queue_repository_refresh", Mock(side_effect=OperationalError))
    else:
        monkeypatch.setattr(views, "_wake_queued_repository_workers", Mock(return_value=(0, True)))

    response = local_client.post(
        reverse("bitbucket_search:document_resume", args=(document.pk,)),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 503
    assert response.json()["state"] == "worker_unavailable"
    assert "awaiting refresh" in response.json()["detail"]
    document.refresh_from_db()
    assert document.local_policy.state == PDFLocalPolicyState.RESUMING


def test_delete_removes_local_copies_and_document_but_keeps_tombstone(local_client, policy_pdf):
    document, original_path = policy_pdf
    exclude_registered_pdf(document.pk)
    document.refresh_from_db()
    saved_path = frozen_pdf_path(document)
    assert saved_path is not None
    document_id = document.pk

    response = local_client.post(
        reverse("bitbucket_search:document_delete", args=(document_id,)),
        {"confirmed": "yes"},
    )

    assert response.status_code == 302
    assert response.url == reverse("bitbucket_search:index")
    assert not PDFDocument.objects.filter(pk=document_id).exists()
    assert not original_path.exists()
    assert not saved_path.exists()
    policy = PDFLocalPolicy.objects.get(repository=document.repository)
    assert policy.state == PDFLocalPolicyState.DELETED
    assert policy.document_id is None
    assert (managed_repository_path(document.repository) / ".git").is_dir()


def test_exclude_failure_is_honest_and_does_not_redirect_off_site(
    local_client, policy_pdf, monkeypatch
):
    document, _path = policy_pdf
    monkeypatch.setattr(
        views,
        "exclude_registered_pdf",
        Mock(
            side_effect=DocumentActionError("repository_refresh_in_progress", "Wait for refresh.")
        ),
    )
    response = local_client.post(
        reverse("bitbucket_search:document_exclude", args=(document.pk,)),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Wait for refresh."

    confirmation = local_client.get(
        reverse("bitbucket_search:document_delete", args=(document.pk,)),
        {"return_to": "https://untrusted.example.invalid/"},
    )
    assert confirmation.context["cancel_url"] == f"/pdfs/#pdf-document-{document.pk}"


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


def test_loaded_timeline_rows_keep_policy_forms_and_no_javascript_delete_confirmation(
    local_client, policy_pdf
):
    document, _path = policy_pdf
    response = local_client.get(
        reverse("bitbucket_search:document_page"),
        {"page": "1"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    html = response.json()["html"]
    assert f'method="post" action="/pdfs/documents/{document.pk}/exclude/"' in html
    assert f'method="get" action="/pdfs/documents/{document.pk}/delete/"' in html
    assert html.count('name="csrfmiddlewaretoken"') == 3
    assert html.count('name="return_page" value="1"') == 4
