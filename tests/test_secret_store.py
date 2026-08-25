import pytest
from django.test import override_settings

from bookmark_manager.services.secret_store import (
    InMemorySecretStore,
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
    assert isinstance(get_secret_store(), InMemorySecretStore)


@override_settings(
    CONFLUENCE_SECRET_BACKEND="memory",
    OWL_ALLOW_IN_MEMORY_SECRET_STORE=False,
)
def test_memory_backend_is_rejected_without_explicit_permission():
    reset_secret_store_cache()
    with pytest.raises(SecretStoreUnavailable):
        get_secret_store()
