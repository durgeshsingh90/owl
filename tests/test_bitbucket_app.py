from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.contrib.staticfiles import finders
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from bitbucket import views
from bitbucket.models import (
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
    TrustedRepositoryHost,
)
from bitbucket.services import git_sync
from bitbucket.services.pdf_search_query import (
    DEFAULT_SEARCH_PAGE_SIZE,
    MAX_SEARCH_PAGE_SIZE,
)
from bitbucket.services.repository_sync import (
    managed_repository_path,
    queue_due_daily_repository_refreshes,
)
from bitbucket_search.models import BitbucketRepository as SearchRepository

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    client.defaults["REMOTE_ADDR"] = "127.0.0.1"
    return client


def test_bitbucket_is_a_separate_django_app_alongside_bitbucket_search():
    assert reverse("bitbucket:index") == "/bitbucket/"
    assert reverse("bitbucket_search:index") == "/pdfs/"
    assert BitbucketRepository._meta.app_label == "bitbucket"
    assert SearchRepository._meta.app_label == "bitbucket_search"
    assert BitbucketRepository._meta.db_table != SearchRepository._meta.db_table


def test_bitbucket_workspace_exposes_requested_inventory_controls(loopback_client):
    response = loopback_client.get("/bitbucket/")
    html = response.content.decode()

    assert response.status_code == 200
    assert response.context["active_app"] == "bitbucket_app"
    assert 'class="bb-stage" aria-label="Bitbucket workspace"' in html
    assert 'action="/bitbucket/repositories/add/"' in html
    assert 'action="/bitbucket/repositories/schedule/tick/"' in html
    assert "PDF files" in html
    assert "VSDX files" in html
    assert "Copy selected paths" in html
    assert "Open selected (0)" in html
    assert 'data-select-all-pdfs aria-label="Select all PDFs on this page"' in html
    assert ">Project<" in html
    assert ">Repository<" in html
    assert ">Date added to repo<" in html
    assert ">Added by<" in html
    assert ">Commit ID<" in html
    assert ">Opens<" in html
    assert ">Actions<" in html
    assert ">People<" in html
    assert "500 PDFs per page" not in html  # Empty inventory has no misleading page claim.


def test_bitbucket_workspace_assets_are_available():
    assert finders.find("bitbucket/bitbucket.css") is not None
    assert finders.find("bitbucket/bitbucket.js") is not None


def test_bitbucket_schedule_tick_accepts_only_local_tokened_opaque_origin():
    client = Client(enforce_csrf_checks=True)
    page = client.get("/bitbucket/", HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    assert page.status_code == 200
    token = client.cookies["csrftoken"].value

    local = client.post(
        "/bitbucket/repositories/schedule/tick/",
        {"csrfmiddlewaretoken": token},
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
        HTTP_ORIGIN="null",
    )
    foreign = client.post(
        "/bitbucket/repositories/schedule/tick/",
        {"csrfmiddlewaretoken": token},
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
        HTTP_ORIGIN="https://attacker.example",
    )

    assert local.status_code == 200
    assert foreign.status_code == 403


def test_server_clone_route_extracts_project_and_repository_labels():
    repository = BitbucketRepository(
        remote_url="https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
        display_name="engineering-sign-off",
    )

    assert views._project_label(repository) == "adr"
    assert repository.display_name == "engineering-sign-off"


@override_settings(BITBUCKET_ALLOWED_HOSTS=())
def test_repository_add_approves_exact_https_origin_and_queues_first_clone(
    loopback_client,
    monkeypatch,
):
    worker_wakeup = Mock(return_value=(1, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", worker_wakeup)
    remote_url = "https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git"

    response = loopback_client.post(
        "/bitbucket/repositories/add/",
        {"repository_url": remote_url},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    repository = BitbucketRepository.objects.get()
    assert repository.remote_url == remote_url
    assert repository.display_name == "engineering-sign-off"
    assert repository.canonical_remote_key.endswith("/stash/scm/adr/engineering-sign-off")
    assert not SearchRepository.objects.exists()
    assert TrustedRepositoryHost.objects.get().canonical_origin == "https://scm.example.invalid:443"
    job = RepositorySyncJob.objects.get(repository=repository)
    assert job.operation == RepositorySyncOperation.CLONE
    worker_wakeup.assert_called_once_with(job_ids=(job.pk,))


def test_connection_preflight_uses_git_ls_remote_before_transport(monkeypatch):
    repository = BitbucketRepository(
        remote_url="https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
        canonical_remote_key="scm.example.invalid/stash/scm/adr/engineering-sign-off",
    )
    run_capture = Mock(return_value="")
    monkeypatch.setattr(git_sync, "_run_capture", run_capture)

    git_sync._check_connection(repository, lambda *_args: None)

    assert run_capture.call_args.args[0] == (
        "git",
        "ls-remote",
        "--symref",
        "--",
        repository.remote_url,
        "HEAD",
    )


def test_daily_pull_is_queued_only_once_after_the_daily_boundary(settings, tmp_path):
    settings.BITBUCKET_APP_REPOSITORIES_ROOT = tmp_path / "repositories"
    observed_at = datetime(2026, 9, 4, 12, tzinfo=UTC)
    TrustedRepositoryHost.objects.create(
        canonical_origin="https://scm.example.invalid:443",
        hostname="scm.example.invalid",
        port=443,
    )
    repository = BitbucketRepository.objects.create(
        remote_url="https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
        canonical_remote_key="scm.example.invalid/stash/scm/adr/engineering-sign-off",
        display_name="engineering-sign-off",
        sync_state=RepositorySyncState.READY,
        last_sync_successful_at=observed_at - timedelta(days=1),
    )
    (managed_repository_path(repository) / ".git").mkdir(parents=True)

    first = queue_due_daily_repository_refreshes(at=observed_at)
    second = queue_due_daily_repository_refreshes(at=observed_at + timedelta(minutes=5))

    assert len(first) == 1
    assert first[0].job.operation == RepositorySyncOperation.REFRESH
    assert first[0].job.scheduled_day == timezone.localdate(observed_at)
    assert second == ()
    assert (
        RepositorySyncJob.objects.filter(
            repository=repository,
            status=RepositorySyncJobStatus.QUEUED,
        ).count()
        == 1
    )


@override_settings(BITBUCKET_APP_PDF_PAGE_SIZE=500, BITBUCKET_APP_SEARCH_PAGE_SIZE=500)
def test_bitbucket_inventory_and_search_page_size_is_500(settings):
    assert settings.BITBUCKET_APP_PDF_PAGE_SIZE == 500
    assert settings.BITBUCKET_APP_SEARCH_PAGE_SIZE == 500
    assert DEFAULT_SEARCH_PAGE_SIZE == 500
    assert MAX_SEARCH_PAGE_SIZE == 500
