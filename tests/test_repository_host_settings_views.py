from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from django.apps import apps
from django.test import Client
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketHTTPSCredentialKind,
    BitbucketRepository,
    RepositorySyncJob,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def ui_managed_repository_host_policy(settings):
    settings.BITBUCKET_ALLOWED_HOSTS = ("bitbucket.org", "github.com")
    settings.BITBUCKET_ALLOWED_HOSTS_EXPLICIT = False
    settings.BITBUCKET_ALLOWED_HOSTS_SOURCE = "unset"
    settings.CONFLUENCE_ACTION_COOLDOWN_SECONDS = 0
    settings.OWL_ALLOW_NON_LOOPBACK = False


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _host_model():
    return apps.get_model("bitbucket_search", "TrustedRepositoryHost")


def _html(response) -> str:
    return response.content.decode()


def test_settings_information_architecture_is_deep_linked_and_observation_only(
    loopback_client,
):
    initial_counts = (
        BitbucketRepository.objects.count(),
        RepositorySyncJob.objects.count(),
    )
    settings_url = reverse("bookmark_manager:settings")
    expected_sections = (
        ("overview", "Overview"),
        ("confluence", "Confluence"),
        ("repository-sources", "Repository sources"),
        ("bookmark-data", "Bookmark data"),
    )

    for section, label in expected_sections:
        response = loopback_client.get(settings_url, {"section": section})
        html = _html(response)

        assert response.status_code == 200
        assert response.context["selected_settings_section"] == section
        assert f'aria-current="page">{label}</a>' in html
        assert html.count('aria-current="page"') == 1
        assert "bookmark-app-sidebar" not in html
        assert "data-notification-center" not in html
        assert "data-bitbucket-schedule-tick-form" not in html
        assert "refresh/schedule/tick" not in html
        assert "repository/schedule/tick" not in html

    assert initial_counts == (
        BitbucketRepository.objects.count(),
        RepositorySyncJob.objects.count(),
    )


def test_settings_tasks_expand_only_the_requested_main_form(loopback_client):
    settings_url = reverse("bookmark_manager:settings")
    overview = _html(loopback_client.get(settings_url))
    confluence = _html(
        loopback_client.get(settings_url, {"section": "confluence", "task": "confluence"})
    )
    hosts = _html(
        loopback_client.get(
            settings_url,
            {"section": "repository-sources", "task": "host"},
        )
    )

    assert "data-confluence-settings-form" not in overview
    assert "data-repository-host-form" not in overview
    assert "data-bitbucket-credential-form" not in overview
    assert "data-confluence-settings-form" in confluence
    assert "data-repository-host-form" not in confluence
    assert "data-repository-host-form" in hosts
    assert "data-confluence-settings-form" not in hosts


def test_repository_host_add_is_canonical_idempotent_and_does_not_start_work(
    loopback_client,
    monkeypatch,
):
    marker = "https://SCM.Company.Example.:8443/"
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("host approval must not perform DNS"),
    )

    first = loopback_client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": marker},
    )

    assert first.status_code == 302
    first_url = urlparse(first.url)
    assert first_url.path == reverse("bookmark_manager:settings")
    assert parse_qs(first_url.query) == {
        "section": ["repository-sources"],
        "action": ["host-added"],
    }
    host = _host_model().objects.get()
    assert host.canonical_origin == "https://scm.company.example:8443"
    assert host.hostname == "scm.company.example"
    assert host.port == 8443
    assert not BitbucketRepository.objects.exists()
    assert not RepositorySyncJob.objects.exists()

    duplicate = loopback_client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": "https://scm.company.example:8443"},
    )
    assert duplicate.status_code == 302
    assert parse_qs(urlparse(duplicate.url).query)["action"] == ["host-unchanged"]
    assert _host_model().objects.count() == 1


def test_invalid_repository_host_never_reflects_credential_like_input(
    loopback_client,
    caplog,
):
    secret_marker = "host-view-secret-marker-4830"
    rejected = f"https://reader:{secret_marker}@scm.example.invalid"

    response = loopback_client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": rejected},
    )
    html = _html(response)

    assert response.status_code == 400
    assert "no-store" in response.headers["Cache-Control"]
    assert "credential-free HTTPS repository host URL" in html
    assert secret_marker not in html
    assert secret_marker not in response.headers.get("Location", "")
    assert secret_marker not in repr(dict(loopback_client.session.items()))
    assert secret_marker not in caplog.text
    assert response.context["repository_host_form"].is_bound is False
    assert 'id="id_repository_host_url"' in html
    assert "autofocus" in html
    assert not _host_model().objects.exists()


@pytest.mark.parametrize(
    ("host", "remote_addr"),
    (
        ("127.0.0.1", "198.51.100.27"),
        ("owl.example.test", "127.0.0.1"),
    ),
)
def test_repository_host_mutations_require_loopback_host_and_peer(
    settings,
    host,
    remote_addr,
):
    settings.OWL_ALLOW_NON_LOOPBACK = True
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "owl.example.test"]
    client = Client(HTTP_HOST=host, REMOTE_ADDR=remote_addr)

    response = client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": "https://scm.example.invalid"},
    )

    assert response.status_code == 403
    assert "only from the local OWL application" in _html(response)
    assert not _host_model().objects.exists()


def test_repository_host_mutations_are_post_only_and_csrf_protected(loopback_client):
    add_url = reverse("bookmark_manager:repository_host_add")
    remove_url = reverse("bookmark_manager:repository_host_remove")
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )

    assert loopback_client.get(add_url).status_code == 405
    assert loopback_client.get(remove_url).status_code == 405
    assert (
        csrf_client.post(
            add_url,
            {"repository_host_url": "https://scm.example.invalid"},
        ).status_code
        == 403
    )
    assert not _host_model().objects.exists()


def test_repository_host_add_accepts_only_the_narrow_local_null_origin_case():
    client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = client.get(
        reverse("bookmark_manager:settings"),
        {"section": "repository-sources", "task": "host"},
    )
    token = page.cookies["csrftoken"].value
    accepted = client.post(
        reverse("bookmark_manager:repository_host_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_host_url": "https://scm.example.invalid",
        },
        HTTP_ORIGIN="null",
    )

    assert accepted.status_code == 302
    assert _host_model().objects.filter(hostname="scm.example.invalid").exists()

    rejected = client.post(
        reverse("bookmark_manager:repository_host_add"),
        {
            "csrfmiddlewaretoken": token,
            "repository_host_url": "https://other.example.invalid",
        },
        HTTP_ORIGIN="https://foreign.example.invalid",
    )
    assert rejected.status_code == 403
    assert not _host_model().objects.filter(hostname="other.example.invalid").exists()


def test_externally_managed_host_policy_is_read_only_in_settings(
    loopback_client,
    settings,
):
    settings.BITBUCKET_ALLOWED_HOSTS = ()
    settings.BITBUCKET_ALLOWED_HOSTS_EXPLICIT = True
    settings.BITBUCKET_ALLOWED_HOSTS_SOURCE = "explicit_blank"
    page = loopback_client.get(
        reverse("bookmark_manager:settings"), {"section": "repository-sources"}
    )
    html = _html(page)

    assert page.status_code == 200
    assert "Managed externally" in html
    assert ">Add host</a>" not in html
    assert "data-repository-host-form" not in html

    crafted = loopback_client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": "https://scm.example.invalid"},
    )
    assert crafted.status_code == 409
    assert "managed by the OWL environment" in _html(crafted)
    assert not _host_model().objects.exists()


def test_repository_host_removal_reports_dependencies_and_never_cascades(
    loopback_client,
):
    host = _host_model().objects.create(
        canonical_origin="https://scm.example.invalid:443",
        hostname="scm.example.invalid",
        port=443,
    )
    repository_marker = "private-repository-name-must-not-render"
    repository = BitbucketRepository.objects.create(
        display_name=repository_marker,
        canonical_remote_key="scm.example.invalid/team/private",
        remote_url="https://scm.example.invalid/team/private.git",
    )
    removal_url = reverse("bookmark_manager:repository_host_remove")

    missing_confirmation = loopback_client.post(
        removal_url,
        {"canonical_origin": host.canonical_origin},
    )
    assert missing_confirmation.status_code == 400

    conflict = loopback_client.post(
        removal_url,
        {
            "canonical_origin": host.canonical_origin,
            "confirm": "remove-repository-host",
        },
    )
    conflict_html = _html(conflict)
    assert conflict.status_code == 409
    assert "1 dependent repositories" in conflict_html
    assert repository_marker not in conflict_html
    assert _host_model().objects.filter(pk=host.pk).exists()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()

    repository.delete()
    removed = loopback_client.post(
        removal_url,
        {
            "canonical_origin": host.canonical_origin,
            "confirm": "remove-repository-host",
        },
    )
    assert removed.status_code == 302
    assert parse_qs(urlparse(removed.url).query)["action"] == ["host-removed"]
    assert not _host_model().objects.filter(pk=host.pk).exists()


def test_custom_host_is_immediately_an_exact_generic_credential_choice(loopback_client):
    loopback_client.post(
        reverse("bookmark_manager:repository_host_add"),
        {"repository_host_url": "https://scm.example.invalid:8443"},
    )
    page = loopback_client.get(
        reverse("bookmark_manager:settings"),
        {
            "section": "repository-sources",
            "task": "credential",
            "origin": "https://scm.example.invalid:8443",
        },
    )
    form = page.context["bitbucket_credential_form"]
    html = _html(page)

    assert page.status_code == 200
    assert form.initial["origin"] == "https://scm.example.invalid:8443"
    assert tuple(form.fields["credential_kind"].choices) == (
        (
            BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
            "Account name + HTTPS access token",
        ),
    )
    assert "https://scm.example.invalid:8443" in dict(form.fields["origin"].choices)
    assert "https://scm.example.invalid:9443" not in dict(form.fields["origin"].choices)
    assert "use your existing operating-system SSH agent" in html
    assert 'href="/pdfs/repositories/">Add repository</a>' in html
