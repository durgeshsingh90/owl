import pytest
from django.test import override_settings

from bookmark_manager.services.secret_store import (
    DatabaseSecretStore,
    InMemorySecretStore,
    KeyringSecretStore,
    SecretStoreOperationError,
    SecretStoreUnavailable,
    get_secret_store,
    reset_secret_store_cache,
)


def test_memory_secret_store_lifecycle_does_not_expose_value_in_repr():
    store = InMemorySecretStore()
    synthetic_value = "owl-test-pat-never-valid"

    assert store.is_available()
    assert store.get() is None
    store.set(synthetic_value)
    assert store.get() == synthetic_value
    assert synthetic_value not in repr(store)
    store.delete()
    assert store.get() is None


def test_memory_secret_store_failure_modes_preserve_existing_value():
    store = InMemorySecretStore()
    store.set("old-synthetic-value")
    store.fail_writes = True

    with pytest.raises(SecretStoreOperationError):
        store.set("new-synthetic-value")

    store.fail_writes = False
    assert store.get() == "old-synthetic-value"


@override_settings(
    CONFLUENCE_SECRET_BACKEND="memory",
    OWL_ALLOW_IN_MEMORY_SECRET_STORE=True,
)
def test_memory_backend_requires_explicit_test_permission():
    reset_secret_store_cache()
    try:
        assert isinstance(get_secret_store(), InMemorySecretStore)
    finally:
        reset_secret_store_cache()


@override_settings(
    CONFLUENCE_SECRET_BACKEND="memory",
    OWL_ALLOW_IN_MEMORY_SECRET_STORE=False,
)
def test_memory_backend_is_rejected_without_explicit_permission():
    reset_secret_store_cache()
    try:
        with pytest.raises(SecretStoreUnavailable):
            get_secret_store()
    finally:
        reset_secret_store_cache()


@pytest.mark.django_db
@override_settings(
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-database-encryption",
    CONFLUENCE_SECRET_BACKEND="database",
)
def test_database_store_encrypts_credential_at_rest():
    from bookmark_manager.models import ConfluenceConfiguration

    marker = "synthetic-database-pat-never-valid"
    reset_secret_store_cache()
    try:
        store = get_secret_store()
        assert isinstance(store, DatabaseSecretStore)
        assert store.is_available()
        assert store.get() is None

        store.set(marker)

        ciphertext = ConfluenceConfiguration.objects.get(pk=1).credential_ciphertext
        assert ciphertext
        assert marker not in ciphertext
        assert store.get() == marker

        store.delete()
        assert store.get() is None
    finally:
        reset_secret_store_cache()


@pytest.mark.django_db
@override_settings(
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-database-encryption",
    CONFLUENCE_SECRET_BACKEND="database",
)
def test_database_store_rejects_ciphertext_from_another_installation():
    marker = "synthetic-database-pat-never-valid"
    store = DatabaseSecretStore()
    store.set(marker)

    with (
        override_settings(
            SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-other-installation"
        ),
        pytest.raises(SecretStoreOperationError),
    ):
        DatabaseSecretStore().get()


@pytest.mark.django_db
@override_settings(CONFLUENCE_SECRET_BACKEND="auto")
def test_auto_backend_falls_back_to_database(monkeypatch):
    monkeypatch.setattr(KeyringSecretStore, "is_available", lambda self: False)
    reset_secret_store_cache()
    try:
        assert isinstance(get_secret_store(), DatabaseSecretStore)
    finally:
        reset_secret_store_cache()


@pytest.mark.django_db
@override_settings(CONFLUENCE_SECRET_BACKEND="auto")
def test_auto_backend_prefers_available_operating_system_store(monkeypatch):
    monkeypatch.setattr(KeyringSecretStore, "is_available", lambda self: True)
    reset_secret_store_cache()
    try:
        assert isinstance(get_secret_store(), KeyringSecretStore)
    finally:
        reset_secret_store_cache()
