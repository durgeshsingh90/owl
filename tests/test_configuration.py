import pytest
from django.test import override_settings

from bookmark_manager.models import (
    ConfluenceConfiguration,
    ConnectionStatus,
    CredentialSource,
)
from bookmark_manager.services.configuration import get_configuration_summary

pytestmark = pytest.mark.django_db


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_empty_profile_is_not_configured():
    summary = get_configuration_summary()

    assert summary.complete is False
    assert summary.state == ConnectionStatus.NOT_CONFIGURED
    assert summary.source == CredentialSource.NONE


@override_settings(
    CONFLUENCE_BASE_URL="https://confluence.example.invalid",
    CONFLUENCE_PAT="",
)
def test_partial_environment_profile_is_rejected_without_mixing_sources():
    ConfluenceConfiguration.objects.create(
        base_url="https://stored.example.invalid",
        credential_source=CredentialSource.KEYRING,
        connection_status=ConnectionStatus.CONNECTED,
    )

    summary = get_configuration_summary()

    assert summary.complete is False
    assert summary.state == ConnectionStatus.CONFIGURATION_ERROR
    assert summary.source == CredentialSource.ENVIRONMENT


@override_settings(
    CONFLUENCE_BASE_URL="https://confluence.example.invalid",
    CONFLUENCE_PAT="synthetic-value-never-returned",
)
def test_complete_environment_profile_takes_precedence_without_returning_pat():
    summary = get_configuration_summary()

    assert summary.complete is True
    assert summary.state == ConnectionStatus.MANAGED_EXTERNALLY
    assert "synthetic-value" not in repr(summary)


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_local_configuration_model_contains_no_secret_field():
    field_names = {field.name for field in ConfluenceConfiguration._meta.get_fields()}

    assert "pat" not in field_names
    assert "token" not in field_names
    assert "secret" not in field_names
    assert "secret_reference" not in field_names


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_singleton_model_forces_primary_key_one():
    configuration = ConfluenceConfiguration(
        id=7,
        base_url="https://confluence.example.invalid",
        credential_source=CredentialSource.KEYRING,
        connection_status=ConnectionStatus.STORED_UNVERIFIED,
    )
    configuration.save()

    assert configuration.pk == 1
    assert ConfluenceConfiguration.objects.count() == 1
