from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketHTTPSCredential,
    BitbucketHTTPSCredentialKind,
    BitbucketHTTPSCredentialSource,
    BitbucketHTTPSCredentialState,
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import RepositorySyncError
from bitbucket_search.services.https_credentials import (
    CLOUD_ACCESS_TOKEN_USERNAME,
    CLOUD_API_TOKEN_USERNAME,
    CLOUD_ORIGIN,
    DatabaseHTTPSSecretStore,
    HTTPSCredentialUnavailable,
    InMemoryHTTPSSecretStore,
    KeyringHTTPSSecretStore,
    available_https_origins,
    get_https_credential_summary,
    normalize_https_origin,
    remove_https_credential,
    reset_https_secret_store_cache,
    resolve_https_credential,
    save_https_credential,
)
from bitbucket_search.services.repository_sync import execute_claimed_job

pytestmark = pytest.mark.django_db

SYNTHETIC_TOKEN = "not-a-real-token"
OTHER_SYNTHETIC_TOKEN = "not-a-real-secret"


@pytest.fixture(autouse=True)
def isolated_https_store_cache():
    reset_https_secret_store_cache()
    yield
    reset_https_secret_store_cache()


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",),
    BITBUCKET_SECRET_BACKEND="database",
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-bitbucket-https",
)
def test_cloud_api_token_is_encrypted_and_resolves_with_derived_username():
    result = save_https_credential(
        "https://BITBUCKET.org/workspace/repository.git",
        BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
        SYNTHETIC_TOKEN,
        username="ignored-user",
    )

    assert result.success is True
    assert result.origin == CLOUD_ORIGIN
    assert result.state == BitbucketHTTPSCredentialState.STORED_UNVERIFIED
    record = BitbucketHTTPSCredential.objects.get()
    assert record.origin == CLOUD_ORIGIN
    assert record.credential_source == BitbucketHTTPSCredentialSource.DATABASE
    assert record.credential_ciphertext
    assert SYNTHETIC_TOKEN not in record.credential_ciphertext
    assert "ignored-user" not in record.credential_ciphertext

    resolved = resolve_https_credential("https://bitbucket.org/other/repository.git")
    assert resolved is not None
    assert resolved.username == CLOUD_API_TOKEN_USERNAME
    assert resolved.token == SYNTHETIC_TOKEN
    assert SYNTHETIC_TOKEN not in repr(resolved)
    assert CLOUD_API_TOKEN_USERNAME not in repr(resolved)


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",),
    BITBUCKET_SECRET_BACKEND="memory",
    OWL_ALLOW_IN_MEMORY_SECRET_STORE=True,
)
def test_cloud_access_token_uses_separate_derived_username_in_memory_store():
    saved = save_https_credential(
        "bitbucket.org",
        BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
        SYNTHETIC_TOKEN,
    )
    resolved = resolve_https_credential(CLOUD_ORIGIN)

    assert saved.success is True
    assert resolved is not None
    assert resolved.username == CLOUD_ACCESS_TOKEN_USERNAME
    assert resolved.token == SYNTHETIC_TOKEN


@pytest.mark.parametrize(
    "kind",
    (
        BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
        BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
    ),
)
@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_cloud_kinds_are_rejected_for_non_cloud_origins(kind):
    result = save_https_credential(
        "https://scm.example.invalid",
        kind,
        SYNTHETIC_TOKEN,
        secret_store=InMemoryHTTPSSecretStore("https://scm.example.invalid:443"),
    )

    assert result.success is False
    assert result.state == "configuration_error"
    assert not BitbucketHTTPSCredential.objects.exists()


@pytest.mark.parametrize("username", ("", "name:token", "name\nforged"))
@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_username_token_requires_one_safe_explicit_username(username):
    result = save_https_credential(
        "https://scm.example.invalid",
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username=username,
        secret_store=InMemoryHTTPSSecretStore("https://scm.example.invalid:443"),
    )

    assert result.success is False
    assert result.state == "configuration_error"


@pytest.mark.parametrize("token", ("", " not-a-real-token", "not-a-real-token ", "bad\ntoken"))
@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_new_profile_requires_one_unambiguous_control_free_token(token):
    result = save_https_credential(
        "https://scm.example.invalid",
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        token,
        username="owl-reader",
        secret_store=InMemoryHTTPSSecretStore("https://scm.example.invalid:443"),
    )

    assert result.success is False
    assert result.state == "configuration_error"


@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_exact_effective_port_is_part_of_the_credential_identity():
    origin = "https://scm.example.invalid:8443"
    store = InMemoryHTTPSSecretStore(origin)
    saved = save_https_credential(
        f"{origin}/stash/scm/adr/repository.git",
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )

    assert saved.origin == origin
    assert (
        resolve_https_credential(f"{origin}/stash/scm/other/repository.git", secret_store=store)
        is not None
    )
    assert resolve_https_credential("https://scm.example.invalid/repository.git") is None
    assert normalize_https_origin("scm.example.invalid") == "https://scm.example.invalid:443"


@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_blank_token_retains_only_an_unchanged_profile_binding():
    origin = "https://scm.example.invalid:443"
    store = InMemoryHTTPSSecretStore(origin)
    first = save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )
    retained = save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        "",
        username="owl-reader",
        secret_store=store,
    )
    rejected = save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        "",
        username="different-reader",
        secret_store=store,
    )

    assert first.success is True
    assert retained.success is True
    assert rejected.success is False
    resolved = resolve_https_credential(origin, secret_store=store)
    assert resolved is not None
    assert resolved.username == "owl-reader"
    assert resolved.token == SYNTHETIC_TOKEN


@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_replacement_write_failure_preserves_previous_profile_and_secret():
    origin = "https://scm.example.invalid:443"
    store = InMemoryHTTPSSecretStore(origin)
    save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )
    store.fail_writes = True

    result = save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        OTHER_SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )

    assert result.success is False
    store.fail_writes = False
    resolved = resolve_https_credential(origin, secret_store=store)
    assert resolved is not None
    assert resolved.token == SYNTHETIC_TOKEN
    assert BitbucketHTTPSCredential.objects.get().kind == (
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN
    )


@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_remove_failure_preserves_profile_then_success_keeps_repositories():
    origin = "https://scm.example.invalid:443"
    store = InMemoryHTTPSSecretStore(origin)
    save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )
    repository = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="scm.example.invalid/team/architecture",
        remote_url="https://scm.example.invalid/team/architecture.git",
    )
    store.fail_deletes = True

    failed = remove_https_credential(origin, secret_store=store)

    assert failed.success is False
    assert BitbucketHTTPSCredential.objects.filter(origin=origin).exists()
    store.fail_deletes = False
    removed = remove_https_credential(origin, secret_store=store)
    assert removed.success is True
    assert not BitbucketHTTPSCredential.objects.filter(origin=origin).exists()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert store.get() is None


@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_summary_never_contains_encrypted_envelope_username_or_token():
    origin = "https://scm.example.invalid:443"
    store = InMemoryHTTPSSecretStore(origin)
    save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="private-reader",
        secret_store=store,
    )

    summary = get_https_credential_summary(origin, secret_store=store)

    assert summary.has_stored_credential is True
    assert summary.state == BitbucketHTTPSCredentialState.STORED_UNVERIFIED
    assert SYNTHETIC_TOKEN not in repr(summary)
    assert "private-reader" not in repr(summary)
    assert store.value not in repr(summary)


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",),
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-first-installation",
)
def test_database_ciphertext_cannot_be_resolved_with_another_installation_key():
    origin = "https://scm.example.invalid:443"
    store = DatabaseHTTPSSecretStore(origin)
    save_https_credential(
        origin,
        BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        SYNTHETIC_TOKEN,
        username="owl-reader",
        secret_store=store,
    )

    with (
        override_settings(
            SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-second-installation"
        ),
        pytest.raises(HTTPSCredentialUnavailable),
    ):
        resolve_https_credential(origin, secret_store=DatabaseHTTPSSecretStore(origin))


class _FakeKeyringError(Exception):
    pass


class _FakeKeyring:
    def __init__(self):
        self.backend = SimpleNamespace(priority=1)
        self.values = {}

    def get_keyring(self):
        return self.backend

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        if self.values.pop((service, account), None) is None:
            raise _FakeKeyringError("absent")


def test_keyring_accounts_are_scoped_to_the_exact_origin(monkeypatch):
    backend = _FakeKeyring()
    first = KeyringHTTPSSecretStore("https://scm.example.invalid:443")
    second = KeyringHTTPSSecretStore("https://scm.example.invalid:8443")
    monkeypatch.setattr(
        KeyringHTTPSSecretStore,
        "_keyring",
        staticmethod(lambda: (backend, _FakeKeyringError)),
    )

    first.set("first-placeholder")
    second.set("second-placeholder")

    assert first.account != second.account
    assert first.get() == "first-placeholder"
    assert second.get() == "second-placeholder"
    first.delete()
    assert first.get() is None
    assert second.get() == "second-placeholder"


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "scm.example.invalid"))
def test_available_origins_include_default_ports_and_registered_custom_port():
    BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="scm.example.invalid/team/architecture",
        remote_url="https://scm.example.invalid:8443/team/architecture.git",
    )

    assert available_https_origins() == (
        "https://bitbucket.org:443",
        "https://scm.example.invalid:443",
        "https://scm.example.invalid:8443",
    )


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",),
    BITBUCKET_SECRET_BACKEND="database",
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-worker-integration",
)
def test_repository_worker_resolves_saved_https_credential_for_the_full_git_sync(monkeypatch):
    private_value = "not-a-real-token"
    saved = save_https_credential(
        CLOUD_ORIGIN,
        BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
        private_value,
    )
    assert saved.success is True
    repository = BitbucketRepository.objects.create(
        display_name="private-documents",
        canonical_remote_key="bitbucket.org/workspace/private-documents",
        remote_url="https://bitbucket.org/workspace/private-documents.git",
        sync_state=RepositorySyncState.CLONING,
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )
    observed = {}

    def capture_credential(
        received_repository,
        *,
        operation,
        progress_callback,
        https_credential,
    ):
        observed.update(
            repository=received_repository.pk,
            operation=operation,
            username=https_credential.username,
            private_value=https_credential.token,
        )
        raise RepositorySyncError("synthetic_stop", "Synthetic sync stopped after capture.")

    monkeypatch.setattr(
        "bitbucket_search.services.repository_sync.synchronize_repository",
        capture_credential,
    )

    completed = execute_claimed_job(job.pk)

    assert observed == {
        "repository": repository.pk,
        "operation": RepositorySyncOperation.CLONE,
        "username": CLOUD_API_TOKEN_USERNAME,
        "private_value": private_value,
    }
    assert completed.status == RepositorySyncJobStatus.FAILED
    repository.refresh_from_db()
    assert private_value not in repository.remote_url
    assert private_value not in repository.last_error_summary
