from types import SimpleNamespace

import pytest

from bookmark_manager.services.secret_store import (
    KeyringSecretStore,
    SecretStoreOperationError,
)


class FakeKeyringError(Exception):
    pass


class FakeKeyring:
    def __init__(self):
        self.backend = SimpleNamespace(priority=1)
        self.value = None
        self.fail_get = False
        self.fail_set = False
        self.fail_delete = False

    def get_keyring(self):
        return self.backend

    def get_password(self, service, account):
        if self.fail_get:
            raise FakeKeyringError("synthetic read failure")
        return self.value

    def set_password(self, service, account, value):
        if self.fail_set:
            raise FakeKeyringError("synthetic write failure")
        self.value = value

    def delete_password(self, service, account):
        if self.fail_delete or self.value is None:
            raise FakeKeyringError("synthetic delete failure")
        self.value = None


@pytest.fixture
def keyring_store(monkeypatch):
    backend = FakeKeyring()
    store = KeyringSecretStore()
    monkeypatch.setattr(store, "_keyring", lambda: (backend, FakeKeyringError))
    return store, backend


def test_keyring_store_reports_backend_availability_without_reading_a_secret(keyring_store):
    store, backend = keyring_store
    assert store.is_available() is True

    backend.backend.priority = 0
    assert store.is_available() is False


def test_keyring_store_round_trip_and_idempotent_absent_delete(keyring_store):
    store, backend = keyring_store
    marker = "synthetic-keyring-value-never-valid"

    store.set(marker)
    assert store.get() == marker
    store.delete()
    assert store.get() is None
    store.delete()

    with pytest.raises(ValueError):
        store.set("")

    assert backend.value is None


@pytest.mark.parametrize(("operation", "failure_flag"), [("get", "fail_get"), ("set", "fail_set")])
def test_keyring_store_maps_backend_read_and_write_errors(keyring_store, operation, failure_flag):
    store, backend = keyring_store
    marker = "synthetic-keyring-error-value-never-valid"
    setattr(backend, failure_flag, True)

    with pytest.raises(SecretStoreOperationError) as captured:
        getattr(store, operation)(marker) if operation == "set" else getattr(store, operation)()

    assert marker not in str(captured.value)


def test_keyring_store_distinguishes_failed_delete_from_an_absent_value(keyring_store):
    store, backend = keyring_store
    backend.value = "synthetic-existing-value-never-valid"
    backend.fail_delete = True

    with pytest.raises(SecretStoreOperationError):
        store.delete()

    backend.value = None
    backend.fail_get = False
    store.delete()
