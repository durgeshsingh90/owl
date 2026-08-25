from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Final

from django.conf import settings

KEYRING_SERVICE: Final = "owl.confluence"
KEYRING_ACCOUNT: Final = "active"


class SecretStoreError(RuntimeError):
    """Base error that is safe for callers to classify without exposing a secret."""


class SecretStoreUnavailable(SecretStoreError):
    """The configured credential backend cannot be used."""


class SecretStoreOperationError(SecretStoreError):
    """A credential operation failed without exposing the credential value."""


class SecretStore(ABC):
    @abstractmethod
    def is_available(self) -> bool:
        """Return whether the backend can accept secret operations."""

    @abstractmethod
    def get(self) -> str | None:
        """Return the stored secret to server-side integration code only."""

    @abstractmethod
    def set(self, value: str) -> None:
        """Store a non-empty secret."""

    @abstractmethod
    def delete(self) -> None:
        """Delete the secret. Deleting an absent value is idempotent."""


class KeyringSecretStore(SecretStore):
    """Operating-system credential store backed by the `keyring` package."""

    def _keyring(self):
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as exc:  # pragma: no cover - dependency installation failure
            raise SecretStoreUnavailable("Credential-store support is unavailable.") from exc
        return keyring, KeyringError

    def is_available(self) -> bool:
        try:
            keyring, _ = self._keyring()
            backend = keyring.get_keyring()
            return bool(getattr(backend, "priority", 0))
        except SecretStoreError:
            return False
        except Exception:
            return False

    def get(self) -> str | None:
        keyring, keyring_error = self._keyring()
        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except keyring_error as exc:
            raise SecretStoreOperationError("The saved credential could not be read.") from exc

    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        keyring, keyring_error = self._keyring()
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, value)
        except keyring_error as exc:
            raise SecretStoreOperationError("The credential could not be stored.") from exc

    def delete(self) -> None:
        keyring, keyring_error = self._keyring()
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except keyring_error as exc:
            # Different backends use the same base exception for an absent value and an
            # actual failure. Check first so deletion remains idempotent.
            try:
                if keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is None:
                    return
            except keyring_error:
                pass
            raise SecretStoreOperationError("The credential could not be removed.") from exc


class InMemorySecretStore(SecretStore):
    """Explicitly injectable fake used by automated tests only."""

    def __init__(self) -> None:
        self._value: str | None = None
        self.available = True
        self.fail_reads = False
        self.fail_writes = False
        self.fail_deletes = False

    def is_available(self) -> bool:
        return self.available

    def get(self) -> str | None:
        if not self.available or self.fail_reads:
            raise SecretStoreOperationError("The fake credential could not be read.")
        return self._value

    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        if not self.available or self.fail_writes:
            raise SecretStoreOperationError("The fake credential could not be stored.")
        self._value = value

    def delete(self) -> None:
        if not self.available or self.fail_deletes:
            raise SecretStoreOperationError("The fake credential could not be removed.")
        self._value = None


@lru_cache(maxsize=1)
def get_secret_store() -> SecretStore:
    backend = settings.CONFLUENCE_SECRET_BACKEND.casefold()
    if backend == "keyring":
        return KeyringSecretStore()
    if backend == "memory" and settings.OWL_ALLOW_IN_MEMORY_SECRET_STORE:
        return InMemorySecretStore()
    raise SecretStoreUnavailable("The configured credential backend is not permitted.")


def reset_secret_store_cache() -> None:
    """Test helper for settings overrides; it never reads or deletes a real secret."""

    get_secret_store.cache_clear()
