from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.apps import apps
from django.db import OperationalError
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
)
from bitbucket_search.services import repository_hosts
from bitbucket_search.services.repository_hosts import (
    RepositoryHostConflict,
    RepositoryHostPolicyManagedExternally,
    RepositoryHostReadOnly,
    RepositoryHostValidationError,
    add_trusted_repository_host,
    effective_repository_host_policy,
    is_repository_hostname_allowed,
    is_repository_https_origin_allowed,
    list_repository_host_summaries,
    normalize_repository_host_origin,
    remove_trusted_repository_host,
    repository_host_dependencies,
)

pytestmark = pytest.mark.django_db


def _host_model():
    return apps.get_model("bitbucket_search", "TrustedRepositoryHost")


@pytest.mark.parametrize(
    ("value", "origin", "hostname", "port"),
    (
        (
            "https://BITBUCKET.org",
            "https://bitbucket.org:443",
            "bitbucket.org",
            443,
        ),
        (
            "https://SCM.Company.Example.:8443/",
            "https://scm.company.example:8443",
            "scm.company.example",
            8443,
        ),
        (
            "https://bücher.example/",
            "https://xn--bcher-kva.example:443",
            "xn--bcher-kva.example",
            443,
        ),
        (
            "https://XN--BCHER-KVA.example/",
            "https://xn--bcher-kva.example:443",
            "xn--bcher-kva.example",
            443,
        ),
        ("https://[2001:db8::1]/", "https://[2001:db8::1]:443", "2001:db8::1", 443),
    ),
)
def test_repository_host_origin_normalizes_exact_https_identity(value, origin, hostname, port):
    normalized = normalize_repository_host_origin(value)

    assert normalized.canonical_origin == origin
    assert normalized.hostname == hostname
    assert normalized.port == port


@pytest.mark.parametrize(
    "value",
    (
        "",
        "scm.example.invalid",
        "http://scm.example.invalid",
        "ssh://git@scm.example.invalid/team/repository.git",
        "file:///tmp/repository",
        "https://reader@scm.example.invalid",
        "https://reader:secret@scm.example.invalid",
        "https://scm.example.invalid/stash",
        "https://scm.example.invalid/%2F",
        "https://scm.example.invalid?team=owl",
        "https://scm.example.invalid#settings",
        "https://*.example.invalid",
        "https://scm..example.invalid",
        "https://-scm.example.invalid",
        "https://scm_.example.invalid",
        "https://127.1",
        "https://0177.0.0.1",
        "https://0x7f000001",
        "https://scm.example.invalid:",
        "https://scm.example.invalid:0",
        "https://scm.example.invalid:65536",
        "https://faß.example",
        "https://😀.example",
        " https://scm.example.invalid",
        "https://scm.example.invalid ",
        "https://scm.example.invalid\n",
        "https://scm.example.invalid\nforged",
        "https://scm.example.invalid\u200b",
        "https://" + "a" * 64 + ".example.invalid",
        "https://" + "a" * 2_049,
    ),
)
def test_repository_host_origin_rejects_every_non_origin_shape(value):
    with pytest.raises(RepositoryHostValidationError) as captured:
        normalize_repository_host_origin(value)

    assert captured.value.code == "invalid_repository_host_origin"


def test_rejected_credential_like_host_input_is_never_repeated_in_safe_error():
    private_value = "synthetic-user:synthetic-secret-never-valid"

    with pytest.raises(RepositoryHostValidationError) as captured:
        normalize_repository_host_origin(f"https://{private_value}@scm.example.invalid/path")

    assert private_value not in str(captured.value)
    assert private_value not in repr(captured.value)


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("SCM.EXAMPLE.INVALID",),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=True,
)
def test_explicit_environment_policy_is_authoritative_and_does_not_read_ui_hosts(monkeypatch):
    model_lookup = Mock(side_effect=AssertionError("UI registry must not broaden external policy"))
    monkeypatch.setattr(repository_hosts, "_trusted_host_model", model_lookup)

    policy = effective_repository_host_policy()

    assert policy.externally_managed is True
    assert policy.source == "environment"
    assert policy.hostnames == frozenset({"scm.example.invalid"})
    assert policy.allows_hostname("scm.example.invalid")
    assert policy.allows_https_origin("https://scm.example.invalid:8443")
    assert not policy.allows_hostname("bitbucket.org")
    model_lookup.assert_not_called()


@override_settings(BITBUCKET_ALLOWED_HOSTS=(), BITBUCKET_ALLOWED_HOSTS_EXPLICIT=True)
def test_explicit_blank_environment_policy_is_deny_all():
    policy = effective_repository_host_policy()

    assert policy.externally_managed is True
    assert policy.entries == ()
    assert not is_repository_hostname_allowed("bitbucket.org")
    assert not is_repository_https_origin_allowed("https://bitbucket.org")


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_unset_policy_unions_builtins_with_enabled_ui_hosts_and_honors_exact_ui_port():
    _host_model().objects.create(
        canonical_origin="https://scm.example.invalid:8443",
        hostname="scm.example.invalid",
        port=8443,
        enabled=True,
    )
    _host_model().objects.create(
        canonical_origin="https://disabled.example.invalid:443",
        hostname="disabled.example.invalid",
        port=443,
        enabled=False,
    )

    policy = effective_repository_host_policy()

    assert policy.externally_managed is False
    assert policy.database_available is True
    assert policy.hostnames == frozenset({"bitbucket.org", "github.com", "scm.example.invalid"})
    assert policy.allows_https_origin("https://scm.example.invalid:8443")
    assert not policy.allows_https_origin("https://scm.example.invalid:9443")
    assert not policy.allows_hostname("disabled.example.invalid")


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_ui_registry_database_failure_keeps_builtins_and_fails_closed_for_ui(monkeypatch):
    class BrokenManager:
        def filter(self, **_kwargs):
            raise OperationalError("synthetic unavailable registry")

    class BrokenModel:
        objects = BrokenManager()

    monkeypatch.setattr(repository_hosts, "_trusted_host_model", lambda: BrokenModel)

    policy = effective_repository_host_policy()

    assert policy.database_available is False
    assert policy.allows_hostname("bitbucket.org")
    assert not policy.allows_hostname("scm.example.invalid")


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_add_host_is_idempotent_reenables_a_row_and_never_queues_other_state():
    first = add_trusted_repository_host("https://SCM.example.invalid:8443/")
    second = add_trusted_repository_host("https://scm.example.invalid:8443")
    record = _host_model().objects.get()

    assert first.created is True
    assert second.created is False
    assert second.changed is False
    assert record.canonical_origin == "https://scm.example.invalid:8443"
    assert record.hostname == "scm.example.invalid"
    assert record.port == 8443
    assert not BitbucketRepository.objects.exists()
    assert not RepositorySyncJob.objects.exists()

    record.enabled = False
    record.save(update_fields=("enabled", "updated_at"))
    reenabled = add_trusted_repository_host("https://scm.example.invalid:8443")

    assert reenabled.created is False
    assert reenabled.changed is True
    record.refresh_from_db()
    assert record.enabled is True


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_adding_an_existing_builtin_is_an_idempotent_read_only_noop():
    result = add_trusted_repository_host("https://BITBUCKET.org/")

    assert result.created is False
    assert result.changed is False
    assert not _host_model().objects.exists()


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("scm.external.invalid",),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=True,
)
def test_external_policy_rejects_ui_add_and_remove_without_mutating_stored_rows():
    record = _host_model().objects.create(
        canonical_origin="https://old-ui.example.invalid:443",
        hostname="old-ui.example.invalid",
        port=443,
        enabled=True,
    )

    with pytest.raises(RepositoryHostPolicyManagedExternally):
        add_trusted_repository_host("https://new-ui.example.invalid")
    with pytest.raises(RepositoryHostPolicyManagedExternally):
        remove_trusted_repository_host(record.canonical_origin)

    record.refresh_from_db()
    assert record.enabled is True
    assert _host_model().objects.count() == 1


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_unused_ui_host_can_be_removed_but_builtin_cannot():
    add_trusted_repository_host("https://scm.example.invalid:8443")

    result = remove_trusted_repository_host("https://scm.example.invalid:8443/")

    assert result.removed is True
    assert not _host_model().objects.exists()
    with pytest.raises(RepositoryHostReadOnly):
        remove_trusted_repository_host("https://bitbucket.org")


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_host_removal_reports_safe_dependency_counts_and_never_cascades():
    origin = "https://scm.example.invalid:8443"
    add_trusted_repository_host(origin)
    repository = BitbucketRepository.objects.create(
        display_name="synthetic-repository",
        canonical_remote_key="scm.example.invalid/team/synthetic-repository",
        remote_url=f"{origin}/team/synthetic-repository.git",
        last_sync_successful_at=timezone.now(),
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        status=RepositorySyncJobStatus.QUEUED,
    )
    BitbucketHTTPSCredential.objects.create(
        origin=origin,
        kind=BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        credential_source=BitbucketHTTPSCredentialSource.DATABASE,
        credential_ciphertext="synthetic-envelope-with-no-real-secret",
        state=BitbucketHTTPSCredentialState.CONNECTED,
    )

    with pytest.raises(RepositoryHostConflict) as captured:
        remove_trusted_repository_host(origin)

    assert captured.value.dependencies.repository_count == 1
    assert captured.value.dependencies.active_job_count == 1
    assert captured.value.dependencies.credential_count == 1
    assert "synthetic-repository" not in str(captured.value)
    assert _host_model().objects.filter(canonical_origin=origin).exists()
    assert BitbucketRepository.objects.filter(pk=repository.pk).exists()
    assert RepositorySyncJob.objects.filter(repository=repository).exists()
    assert BitbucketHTTPSCredential.objects.filter(origin=origin).exists()


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "github.com"),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=False,
)
def test_ssh_repository_is_a_dependency_of_the_exact_ui_hostname():
    origin = "https://scm.example.invalid:8443"
    add_trusted_repository_host(origin)
    BitbucketRepository.objects.create(
        display_name="ssh-repository",
        canonical_remote_key="scm.example.invalid/team/ssh-repository",
        remote_url="ssh://git@scm.example.invalid/team/ssh-repository.git",
    )

    dependencies = repository_host_dependencies(origin)

    assert dependencies.repository_count == 1
    with pytest.raises(RepositoryHostConflict):
        remove_trusted_repository_host(origin)


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("scm.external.invalid",),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=True,
)
def test_summaries_retain_ui_rows_as_unavailable_under_external_policy():
    _host_model().objects.create(
        canonical_origin="https://old-ui.example.invalid:8443",
        hostname="old-ui.example.invalid",
        port=8443,
        enabled=True,
    )

    summaries = {summary.canonical_origin: summary for summary in list_repository_host_summaries()}

    assert summaries["https://scm.external.invalid:443"].source == "environment"
    assert summaries["https://scm.external.invalid:443"].state == "managed_externally"
    retained = summaries["https://old-ui.example.invalid:8443"]
    assert retained.source == "ui"
    assert retained.enabled is True
    assert retained.available is False
    assert retained.state == "unavailable"


@override_settings(
    BITBUCKET_ALLOWED_HOSTS=("scm.external.invalid",),
    BITBUCKET_ALLOWED_HOSTS_EXPLICIT=True,
)
def test_hostname_managed_summary_counts_dependencies_on_registered_custom_ports():
    repository = BitbucketRepository.objects.create(
        display_name="custom-port-repository",
        canonical_remote_key="scm.external.invalid/team/custom-port-repository",
        remote_url="https://scm.external.invalid:8443/team/custom-port-repository.git",
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        status=RepositorySyncJobStatus.RUNNING,
    )
    BitbucketHTTPSCredential.objects.create(
        origin="https://scm.external.invalid:8443",
        kind=BitbucketHTTPSCredentialKind.USERNAME_TOKEN,
        credential_source=BitbucketHTTPSCredentialSource.DATABASE,
        credential_ciphertext="synthetic-envelope-with-no-real-secret",
        state=BitbucketHTTPSCredentialState.STORED_UNVERIFIED,
    )

    summary = list_repository_host_summaries()[0]

    assert summary.canonical_origin == "https://scm.external.invalid:443"
    assert summary.dependencies.repository_count == 1
    assert summary.dependencies.active_job_count == 1
    assert summary.dependencies.credential_count == 1
    assert summary.credential_state == "stored_unverified"
