from __future__ import annotations

import re

import pytest
from django.forms import PasswordInput
from django.test import Client
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketHTTPSCredential,
    BitbucketHTTPSCredentialKind,
    BitbucketRepository,
)
from bitbucket_search.services.https_credentials import (
    CLOUD_ORIGIN,
    reset_https_secret_store_cache,
)

pytestmark = pytest.mark.django_db

SYNTHETIC_VALUE = "view-only-bitbucket-token-never-valid-4917"
INVALID_POST_VALUE = "invalid-form-token-must-not-render-8642"
SYNTHETIC_USERNAME = "submitted-cloud-username-must-not-persist"


@pytest.fixture(autouse=True)
def database_https_credential_backend(settings):
    settings.BITBUCKET_ALLOWED_HOSTS = ("bitbucket.org",)
    settings.BITBUCKET_SECRET_BACKEND = "database"
    settings.SECRET_KEY = "synthetic-test-secret-key-only-not-for-real-use-settings-view"
    reset_https_secret_store_cache()
    yield
    reset_https_secret_store_cache()


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _html(response) -> str:
    return response.content.decode()


def _input_tag(html: str, name: str) -> str:
    match = re.search(rf'<input\b[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match is not None
    return match.group(0)


def _csrf_client() -> Client:
    return Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )


def _settings_csrf_token(client: Client) -> str:
    response = client.get(reverse("bookmark_manager:settings"))
    assert response.status_code == 200
    return response.cookies["csrftoken"].value


def _save_payload(private_value: str = SYNTHETIC_VALUE) -> dict[str, str]:
    return {
        "origin": CLOUD_ORIGIN,
        "credential_kind": BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
        "account_name": SYNTHETIC_USERNAME,
        "token": private_value,
        "return_to": "settings",
    }


def test_home_topbar_settings_gear_links_to_the_shared_settings_page(loopback_client):
    response = loopback_client.get(reverse("core:dashboard"))
    html = _html(response)
    topbar_start = html.index('<header class="knowledge-topbar">')
    main_content_start = html.index('<div id="main-content"', topbar_start)
    topbar = html[topbar_start:main_content_start]
    settings_url = reverse("bookmark_manager:settings")

    assert response.status_code == 200
    assert re.search(
        rf'<a\b[^>]*class="[^"]*knowledge-theme-toggle--compact[^"]*"'
        rf'[^>]*href="{re.escape(settings_url)}"[^>]*aria-label="Settings"',
        topbar,
    )
    assert '<span aria-hidden="true">⚙</span>' in topbar


def test_settings_keeps_confluence_and_bookmark_data_controls(loopback_client):
    response = loopback_client.get(reverse("bookmark_manager:settings"))
    html = _html(response)
    confluence_token = _input_tag(html, "personal_access_token")

    assert response.status_code == 200
    assert "personal_access_token" in response.context["settings_form"].fields
    assert 'type="password"' in confluence_token
    assert f'action="{reverse("bookmark_manager:export")}"' in html
    assert ">Export JSON</button>" in html
    assert f'action="{reverse("bookmark_manager:import")}"' in html
    assert ">Import bookmarks</button>" in html


@pytest.mark.parametrize(
    "view_name",
    ("bookmark_manager:index", "bookmark_manager:settings"),
)
def test_bitbucket_settings_fields_have_unique_namespaced_dom_ids(loopback_client, view_name):
    response = loopback_client.get(reverse(view_name))
    html = _html(response)
    element_ids = re.findall(r'\sid="([^"]+)"', html)

    assert response.status_code == 200
    assert len(element_ids) == len(set(element_ids))
    assert {
        "id_bitbucket_https_origin",
        "id_bitbucket_https_credential_kind",
        "id_bitbucket_https_account_name",
        "id_bitbucket_https_token",
    }.issubset(element_ids)


def test_settings_describes_each_supported_bitbucket_https_credential(loopback_client):
    response = loopback_client.get(reverse("bookmark_manager:settings"))
    html = _html(response)
    form = response.context["bitbucket_credential_form"]

    assert response.status_code == 200
    assert tuple(form.fields["credential_kind"].choices) == (
        (
            BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
            "Atlassian API token (recommended for Bitbucket Cloud)",
        ),
        (
            BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
            "Repository, project, or workspace access token (Bitbucket Cloud)",
        ),
        (
            BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
            "Account name + HTTP access token (Bitbucket Data Center)",
        ),
    )
    assert tuple(form.fields["origin"].choices) == ((CLOUD_ORIGIN, CLOUD_ORIGIN),)
    assert isinstance(form.fields["token"].widget, PasswordInput)
    assert form.fields["token"].widget.render_value is False
    assert "encrypts this token locally" in form.fields["token"].help_text
    assert "never writes it into a repository URL" in form.fields["token"].help_text
    assert "exact host" in form.fields["token"].help_text
    assert "repository read is sufficient" in html
    assert "Do not grant write, admin, or delete" in html


def test_bitbucket_password_input_is_blank_on_get_and_invalid_post(loopback_client):
    initial = loopback_client.get(reverse("bookmark_manager:settings"))
    initial_html = _html(initial)
    initial_token_input = _input_tag(initial_html, "token")

    assert initial.status_code == 200
    assert 'type="password"' in initial_token_input
    assert 'autocomplete="new-password"' in initial_token_input
    assert "value=" not in initial_token_input
    assert SYNTHETIC_VALUE not in initial_html

    response = loopback_client.post(
        reverse("bookmark_manager:bitbucket_https_credential_save"),
        {
            "origin": CLOUD_ORIGIN,
            "credential_kind": BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
            "account_name": "",
            "token": INVALID_POST_VALUE,
            "return_to": "settings",
        },
    )
    html = _html(response)
    token_input = _input_tag(html, "token")

    assert response.status_code == 400
    assert "Enter the Bitbucket account name" in html
    assert "value=" not in token_input
    assert INVALID_POST_VALUE not in html
    assert INVALID_POST_VALUE not in repr(dict(loopback_client.session.items()))
    assert not BitbucketHTTPSCredential.objects.exists()


@pytest.mark.parametrize(
    ("view_name", "payload"),
    (
        ("bookmark_manager:bitbucket_https_credential_save", _save_payload()),
        (
            "bookmark_manager:bitbucket_https_credential_remove",
            {
                "origin": CLOUD_ORIGIN,
                "confirm": "remove-bitbucket-credential",
                "return_to": "settings",
            },
        ),
    ),
)
def test_bitbucket_credential_mutations_require_csrf(view_name, payload):
    client = _csrf_client()

    response = client.post(reverse(view_name), payload)

    assert response.status_code == 403
    assert not BitbucketHTTPSCredential.objects.exists()


@pytest.mark.parametrize(
    ("view_name", "payload"),
    (
        ("bookmark_manager:bitbucket_https_credential_save", _save_payload()),
        (
            "bookmark_manager:bitbucket_https_credential_remove",
            {
                "origin": CLOUD_ORIGIN,
                "confirm": "remove-bitbucket-credential",
                "return_to": "settings",
            },
        ),
    ),
)
def test_bitbucket_credential_mutations_are_loopback_only(loopback_client, view_name, payload):
    response = loopback_client.post(
        reverse(view_name),
        payload,
        REMOTE_ADDR="198.51.100.27",
    )

    assert response.status_code == 403
    assert "only from the local OWL application" in _html(response)
    assert not BitbucketHTTPSCredential.objects.exists()


def test_database_credential_save_reload_and_remove_is_secret_free_and_keeps_repository():
    repository = BitbucketRepository.objects.create(
        display_name="private-architecture",
        canonical_remote_key="bitbucket.org/owl/private-architecture",
        remote_url="https://bitbucket.org/owl/private-architecture.git",
    )
    client = _csrf_client()
    csrf_token = _settings_csrf_token(client)
    save_payload = _save_payload()
    save_payload["csrfmiddlewaretoken"] = csrf_token

    saved = client.post(
        reverse("bookmark_manager:bitbucket_https_credential_save"),
        save_payload,
    )

    assert saved.status_code == 302
    assert saved.url == reverse("bookmark_manager:settings")
    record = BitbucketHTTPSCredential.objects.get(origin=CLOUD_ORIGIN)
    assert record.credential_ciphertext
    assert SYNTHETIC_VALUE not in record.credential_ciphertext
    assert SYNTHETIC_USERNAME not in record.credential_ciphertext
    assert SYNTHETIC_VALUE not in repr(record.__dict__)
    assert SYNTHETIC_USERNAME not in repr(record.__dict__)
    assert "token" not in {field.name for field in record._meta.concrete_fields}
    assert "username" not in {field.name for field in record._meta.concrete_fields}
    assert SYNTHETIC_VALUE not in repr(dict(client.session.items()))
    assert SYNTHETIC_USERNAME not in repr(dict(client.session.items()))

    reloaded = client.get(reverse("bookmark_manager:settings"))
    reloaded_html = _html(reloaded)
    reloaded_token_input = _input_tag(reloaded_html, "token")

    assert reloaded.status_code == 200
    assert CLOUD_ORIGIN in reloaded_html
    assert "Stored" in reloaded_html
    assert "not verified" in reloaded_html
    assert SYNTHETIC_VALUE not in reloaded_html
    assert SYNTHETIC_USERNAME not in reloaded_html
    assert "value=" not in reloaded_token_input

    removed = client.post(
        reverse("bookmark_manager:bitbucket_https_credential_remove"),
        {
            "csrfmiddlewaretoken": csrf_token,
            "origin": CLOUD_ORIGIN,
            "confirm": "remove-bitbucket-credential",
            "return_to": "settings",
        },
    )

    assert removed.status_code == 302
    assert removed.url == reverse("bookmark_manager:settings")
    assert not BitbucketHTTPSCredential.objects.exists()
    repository.refresh_from_db()
    assert repository.remote_url == "https://bitbucket.org/owl/private-architecture.git"
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()

    after_remove = client.get(reverse("bookmark_manager:settings"))
    after_remove_html = _html(after_remove)
    assert after_remove.status_code == 200
    assert "No HTTPS credential saved" in after_remove_html
    assert SYNTHETIC_VALUE not in after_remove_html
    assert SYNTHETIC_USERNAME not in after_remove_html
