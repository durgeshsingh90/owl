from __future__ import annotations

import re
from unittest.mock import Mock

import pytest
from django.test import Client
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.repository_lifecycle import RepositoryLifecycleError

pytestmark = pytest.mark.django_db


@pytest.fixture
def repository(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "staging"
    return BitbucketRepository.objects.create(
        display_name="engineering-availability-localization-sign-off",
        canonical_remote_key="example.invalid/adr/engineering",
        remote_url="https://example.invalid/stash/scm/adr/engineering.git",
        sync_state=RepositorySyncState.READY,
    )


@pytest.fixture
def local_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def test_repository_exclusion_toggle_is_explicit_and_keeps_files_searchable(
    local_client, repository
):
    document = PDFDocument.objects.create(
        repository=repository, filename="Guide.pdf", relative_path="docs/Guide.pdf"
    )
    url = reverse("bitbucket_search:repository_exclude", args=(repository.pk,))

    for value, expected in (("yes", True), ("no", False)):
        response = local_client.post(
            url, {"excluded": value}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        assert response.status_code == 200
        repository.refresh_from_db()
        assert repository.exclude_from_refresh is expected
        assert repository.enabled
        assert response.json()["repository"]["refreshExcluded"] is expected
        assert PDFDocument.objects.filter(pk=document.pk).exists()


@pytest.mark.parametrize("value", [None, "", "true", "unexpected"])
def test_repository_exclusion_rejects_ambiguous_values(local_client, repository, value):
    response = local_client.post(
        reverse("bitbucket_search:repository_exclude", args=(repository.pk,)),
        {} if value is None else {"excluded": value},
    )
    assert response.status_code == 400
    repository.refresh_from_db()
    assert not repository.exclude_from_refresh


def test_repository_exclusion_is_post_only_and_redirect_cannot_leave_owl(local_client, repository):
    url = reverse("bitbucket_search:repository_exclude", args=(repository.pk,))
    assert local_client.get(url).status_code == 405
    response = local_client.post(
        url, {"excluded": "yes", "return_to": "https://foreign.example.invalid/"}
    )
    assert response.status_code == 302
    assert response.url == reverse("bitbucket_search:index")


@pytest.mark.parametrize("route", ["repository_exclude", "repository_remove"])
@pytest.mark.parametrize(
    ("origin", "token", "remote", "allowed"),
    [
        ("null", True, "127.0.0.1", True),
        ("null", False, "127.0.0.1", False),
        ("https://foreign.example.invalid", True, "127.0.0.1", False),
        ("null", True, "192.0.2.25", False),
    ],
)
def test_repository_lifecycle_preserves_csrf_and_strict_loopback(
    repository, monkeypatch, settings, route, origin, token, remote, allowed
):
    settings.OWL_ALLOW_NON_LOOPBACK = True
    action = Mock(side_effect=RepositoryLifecycleError("repository_busy", "Synthetic busy repo"))
    service = (
        "remove_repository" if route == "repository_remove" else "set_repository_refresh_excluded"
    )
    monkeypatch.setattr(views, service, action)
    client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=remote)
    page = client.get(reverse("bitbucket_search:index"))
    data = {"excluded": "yes", "confirmed": "yes"}
    if token:
        data["csrfmiddlewaretoken"] = page.cookies["csrftoken"].value
    response = client.post(
        reverse(f"bitbucket_search:{route}", args=(repository.pk,)),
        data,
        HTTP_ORIGIN=origin,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == (409 if allowed else 403)
    assert action.call_count == int(allowed)


def test_repository_remove_confirmation_is_read_only_and_explains_local_scope(
    local_client, repository, monkeypatch
):
    remove = Mock()
    monkeypatch.setattr(views, "remove_repository", remove)
    url = reverse("bitbucket_search:repository_remove", args=(repository.pk,))
    response = local_client.get(url, {"return_to": "https://foreign.example.invalid/"})
    html = response.content.decode()
    assert response.status_code == 200
    assert repository.display_name in html
    assert "including local changes and saved PDF copies" in html
    assert "commit history records, and background jobs" in html
    assert "remote repository is not changed" in html
    assert 'name="confirmed" value="yes"' in html
    assert response.context["cancel_url"] == reverse("bitbucket_search:index")
    assert local_client.post(url).status_code == 400
    remove.assert_not_called()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()


@pytest.mark.parametrize("async_request", [False, True])
def test_confirmed_repository_remove_calls_service_then_safe_success(
    local_client, repository, monkeypatch, async_request
):
    remove = Mock()
    monkeypatch.setattr(views, "remove_repository", remove)
    response = local_client.post(
        reverse("bitbucket_search:repository_remove", args=(repository.pk,)),
        {"confirmed": "yes", "return_to": "https://foreign.example.invalid/"},
        **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if async_request else {}),
    )
    remove.assert_called_once_with(repository.pk, confirmed=True)
    if async_request:
        assert response.status_code == 200
        assert response.json()["state"] == "removed"
        assert "remote repository was not changed" in response.json()["detail"]
    else:
        assert response.status_code == 302
        assert response.url == reverse("bitbucket_search:index")


@pytest.mark.parametrize("phase", ["sync_queued", "sync_running", "pdf_queued", "pdf_running"])
def test_remove_controls_and_status_include_git_and_pdf_worker_activity(
    local_client, repository, phase
):
    if phase.startswith("sync"):
        RepositorySyncJob.objects.create(
            repository=repository,
            status=RepositorySyncJobStatus.QUEUED
            if phase.endswith("queued")
            else RepositorySyncJobStatus.RUNNING,
        )
    else:
        document = PDFDocument.objects.create(
            repository=repository, filename="Guide.pdf", relative_path="Guide.pdf"
        )
        PDFExtractionJob.objects.create(
            document=document,
            target_git_blob_id="a" * 40,
            target_source_commit="b" * 40,
            target_relative_path=document.relative_path,
            target_extractor_version="test",
            status=PDFExtractionJobStatus.QUEUED
            if phase.endswith("queued")
            else PDFExtractionJobStatus.RUNNING,
        )
    html = local_client.get(reverse("bitbucket_search:index")).content.decode()
    assert 'data-repository-active-work="true"' in html
    assert re.search(r"<button[^>]+data-selected-remove[^>]+disabled", html)
    assert re.search(r"<button[^>]+data-selected-exclude[^>]+disabled", html)
    assert "data-repository-remove-button" not in html
    refresh_form = re.search(r'<form\s+class="bb-refresh-all[^\"]*".*?</form>', html, re.DOTALL)
    assert refresh_form
    assert 'disabled aria-busy="true"' in refresh_form.group()
    assert "data-refresh-all-icon hidden" not in refresh_form.group()
    assert "data-refresh-all-spinner hidden" in refresh_form.group()
    assert "data-refresh-all-visual" in refresh_form.group()
    assert "data-overall-progress" in refresh_form.group()
    assert "data-repository-run-timer" not in html
    confirmation = local_client.get(
        reverse("bitbucket_search:repository_remove", args=(repository.pk,))
    )
    if phase.startswith("sync"):
        assert confirmation.context["repository_git_busy"]
        assert not confirmation.context["repository_pdf_indexing"]
        assert "Git work is active" in confirmation.content.decode()
    else:
        assert confirmation.context["repository_pdf_indexing"]
        assert not confirmation.context["repository_git_busy"]
        assert "Active PDF indexing will be stopped" in confirmation.content.decode()
    assert not re.search(
        r'<button class="button button--danger"[^>]+disabled',
        confirmation.content.decode(),
    )
    status = local_client.get(reverse("bitbucket_search:repository_status"))
    assert status.json()["repositories"][0]["hasActiveWork"]


def test_excluded_sidebar_repository_has_selection_badge_and_shared_refresh(
    local_client, repository
):
    repository.exclude_from_refresh = True
    repository.save()
    document = PDFDocument.objects.create(
        repository=repository, filename="Guide.pdf", relative_path="Guide.pdf"
    )
    page = local_client.get(reverse("bitbucket_search:index"))
    html = page.content.decode()
    assert f'aria-label="Select {repository.display_name}"' in html
    assert 'data-repository-refresh-excluded="true"' in html
    assert 'form="bb-repository-selection-form"' in html
    assert "data-repository-menu" not in html
    assert "Refresh excluded" in html
    assert page.context["enabled_repository_count"] == 0
    assert html.count("no repositories are included") == 1
    refresh_button = re.search(r"<button[^>]+data-selected-refresh[^>]*>", html)
    assert refresh_button and "disabled" in refresh_button.group()
    assert "Guide.pdf" in html
    assert f"/documents/{document.pk}/exclude/" not in html
    assert f"/documents/{document.pk}/include/" not in html
    assert f"/documents/{document.pk}/delete/" not in html
    assert f"/repositories/{repository.pk}/remove/" in html


def test_incomplete_removal_is_visible_and_can_retry_without_live_repository(
    local_client, monkeypatch
):
    recovery = RepositoryRemovalRecovery.objects.create(
        repository_id=773,
        display_name="Removal pending",
        quarantine_manifest=[],
        database_deleted=True,
    )
    html = local_client.get(reverse("bitbucket_search:index")).content.decode()
    assert "Removal incomplete" in html
    assert "Retry removal" in html
    url = reverse("bitbucket_search:repository_remove", args=(recovery.repository_id,))
    assert url in html
    confirmation = local_client.get(url)
    assert confirmation.status_code == 200
    assert "Finish removing this repository?" in confirmation.content.decode()
    assert "Database removal completed" in confirmation.content.decode()
    remove = Mock()
    monkeypatch.setattr(views, "remove_repository", remove)
    response = local_client.post(url, {"confirmed": "yes"})
    assert response.status_code == 302
    remove.assert_called_once_with(773, confirmed=True)


def test_interrupted_removal_locks_regular_actions_and_describes_pending_database_cleanup(
    local_client, repository
):
    RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk,
        display_name=repository.display_name,
        quarantine_manifest=[],
        database_deleted=False,
    )
    page = local_client.get(reverse("bitbucket_search:index"))
    html = page.content.decode()
    assert page.context["enabled_repository_count"] == 0
    assert 'data-repository-removal-pending="true"' in html
    assert re.search(r"<button[^>]+data-selected-remove[^>]+disabled", html)
    assert re.search(r"<button[^>]+data-selected-exclude[^>]+disabled", html)
    refresh_button = re.search(r"<button[^>]+data-selected-refresh[^>]*>", html)
    assert refresh_button and "disabled" in refresh_button.group()
    confirmation = local_client.get(
        reverse("bitbucket_search:repository_remove", args=(repository.pk,))
    )
    assert "previous removal was interrupted" in confirmation.content.decode()
    assert "Database removal completed" not in confirmation.content.decode()
    status = local_client.get(reverse("bitbucket_search:repository_status"))
    assert status.json()["repositories"][0]["hasRemovalPending"]


@pytest.mark.parametrize("action", ["repository_add", "repository_refresh"])
def test_incomplete_removal_rejects_refresh_and_readding_with_clear_response(
    local_client, repository, action, settings, monkeypatch
):
    settings.BITBUCKET_ALLOWED_HOSTS = ("example.invalid",)
    repository.canonical_remote_key = "example.invalid/stash/scm/adr/engineering"
    repository.save(update_fields=("canonical_remote_key",))
    wake = Mock(return_value=(0, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", wake)
    RepositoryRemovalRecovery.objects.create(
        repository_id=repository.pk,
        display_name=repository.display_name,
        quarantine_manifest=[],
        database_deleted=False,
    )
    url = reverse(
        f"bitbucket_search:{action}",
        args=(repository.pk,) if action == "repository_refresh" else (),
    )
    response = local_client.post(
        url,
        {"repository_url": repository.remote_url},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "repository_removal_pending"
    assert "Retry removal" in response.json()["detail"]
    assert not RepositorySyncJob.objects.exists()
    wake.assert_not_called()


@pytest.mark.parametrize("route", ["repository_remove", "repository_exclude"])
def test_repository_lifecycle_missing_target_is_404(local_client, route):
    response = local_client.post(
        reverse(f"bitbucket_search:{route}", args=(7654321,)),
        {"confirmed": "yes", "excluded": "yes"},
    )
    assert response.status_code == 404


def test_repository_remove_error_is_shown_without_claiming_success(
    local_client, repository, monkeypatch
):
    monkeypatch.setattr(
        views,
        "remove_repository",
        Mock(side_effect=RepositoryLifecycleError("pdf_extraction_busy", "Wait for PDF workers.")),
    )
    response = local_client.post(
        reverse("bitbucket_search:repository_remove", args=(repository.pk,)),
        {"confirmed": "yes"},
    )
    assert response.status_code == 409
    assert "Wait for PDF workers." in response.content.decode()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()
