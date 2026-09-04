from __future__ import annotations

import base64
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from functools import lru_cache, wraps
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import DatabaseError

from bookmark_manager.services.logging_events import get_logger, log_event

logger = get_logger("secret_store")
KEYRING_SERVICE: Final = "owl.confluence"
KEYRING_ACCOUNT: Final = "active"


class SecretStoreError(RuntimeError):
    """Base error that is safe for callers to classify without exposing a secret."""


class SecretStoreUnavailable(SecretStoreError):
    """The configured credential backend cannot be used."""


class SecretStoreOperationError(SecretStoreError):
    """A credential operation failed without exposing the credential value."""


def _logged_secret_operation(operation: str, backend: str):
    """Record backend outcomes without inspecting credentials or backend messages."""

    def decorate(function):
        @wraps(function)
        def observed(self, *args, **kwargs):
            started = time.monotonic()
            try:
                result = function(self, *args, **kwargs)
            except Exception as exc:
                missing_input = operation == "set" and not (
                    args[0] if args else kwargs.get("value")
                )
                log_event(
                    logger,
                    logging.WARNING if missing_input else logging.ERROR,
                    "credential_store_operation_rejected"
                    if missing_input
                    else "credential_store_operation_failed",
                    error=exc.__cause__
                    if isinstance(exc, SecretStoreError) and exc.__cause__ is not None
                    else exc,
                    operation=operation,
                    stage=backend,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise
            # An absent credential is a normal first-run state, not a failure.
            if operation != "get" or result is not None:
                log_event(
                    logger,
                    logging.DEBUG if operation == "get" else logging.INFO,
                    "credential_store_operation_completed",
                    operation=operation,
                    stage=backend,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            return result

        return observed

    return decorate


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
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "credential_store_availability_failed",
                error=exc,
                stage="keyring",
            )
            return False

    @_logged_secret_operation("get", "keyring")
    def get(self) -> str | None:
        keyring, keyring_error = self._keyring()
        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except keyring_error as exc:
            raise SecretStoreOperationError("The saved credential could not be read.") from exc

    @_logged_secret_operation("set", "keyring")
    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        keyring, keyring_error = self._keyring()
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, value)
        except keyring_error as exc:
            raise SecretStoreOperationError("The credential could not be stored.") from exc

    @_logged_secret_operation("delete", "keyring")
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
            except keyring_error as read_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "credential_store_absence_check_failed",
                    error=read_error,
                    operation="delete",
                    stage="keyring",
                )
            raise SecretStoreOperationError("The credential could not be removed.") from exc


class DatabaseSecretStore(SecretStore):
    """Encrypted credential storage in OWL's single local configuration row."""

    @staticmethod
    def _configuration_model():
        from bookmark_manager.models import ConfluenceConfiguration

        return ConfluenceConfiguration

    @staticmethod
    def _fernet() -> Fernet:
        digest = hashlib.sha256(
            f"owl.database-credential.v1:{settings.SECRET_KEY}".encode()
        ).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def is_available(self) -> bool:
        try:
            (
                self._configuration_model()
                .objects.values_list("credential_ciphertext", flat=True)
                .first()
            )
        except DatabaseError as exc:
            log_event(
                logger,
                logging.ERROR,
                "credential_store_availability_failed",
                error=exc,
                stage="database",
            )
            return False
        return True

    @_logged_secret_operation("get", "database")
    def get(self) -> str | None:
        try:
            ciphertext = (
                self._configuration_model()
                .objects.filter(pk=1)
                .values_list("credential_ciphertext", flat=True)
                .first()
            )
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted database credential could not be read."
            ) from exc
        if not ciphertext:
            return None
        try:
            return self._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreOperationError(
                "The encrypted database credential could not be read."
            ) from exc

    @_logged_secret_operation("set", "database")
    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        ciphertext = self._fernet().encrypt(value.encode("utf-8")).decode("ascii")
        try:
            self._configuration_model().objects.update_or_create(
                pk=1,
                defaults={"credential_ciphertext": ciphertext},
            )
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted database credential could not be stored."
            ) from exc

    @_logged_secret_operation("delete", "database")
    def delete(self) -> None:
        try:
            self._configuration_model().objects.filter(pk=1).update(credential_ciphertext="")
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted database credential could not be removed."
            ) from exc


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
    if backend == "auto":
        keyring_store = KeyringSecretStore()
        if keyring_store.is_available():
            log_event(logger, logging.DEBUG, "credential_store_selected", stage="keyring")
            return keyring_store
        log_event(
            logger,
            logging.WARNING,
            "credential_store_fallback_selected",
            stage="database",
            reason="keyring_unavailable",
        )
        return DatabaseSecretStore()
    if backend == "keyring":
        log_event(logger, logging.DEBUG, "credential_store_selected", stage="keyring")
        return KeyringSecretStore()
    if backend == "database":
        log_event(logger, logging.DEBUG, "credential_store_selected", stage="database")
        return DatabaseSecretStore()
    if backend == "memory" and settings.OWL_ALLOW_IN_MEMORY_SECRET_STORE:
        return InMemorySecretStore()
    log_event(
        logger,
        logging.ERROR,
        "credential_store_selection_failed",
        reason="backend_not_permitted",
    )
    raise SecretStoreUnavailable("The configured credential backend is not permitted.")


def credential_source_for_store(store: SecretStore) -> str:
    """Return the persisted source label without exposing any stored value."""

    return "database" if isinstance(store, DatabaseSecretStore) else "keyring"


def reset_secret_store_cache() -> None:
    """Test helper for settings overrides; it never reads or deletes a real secret."""

    get_secret_store.cache_clear()
