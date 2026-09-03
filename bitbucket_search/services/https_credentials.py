"""Secure, origin-bound credentials for non-interactive Bitbucket HTTPS Git."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache, wraps
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import DatabaseError, OperationalError, ProgrammingError, transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketHTTPSCredential,
    BitbucketHTTPSCredentialKind,
    BitbucketHTTPSCredentialSource,
    BitbucketHTTPSCredentialState,
    BitbucketRepository,
)
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.repository_hosts import (
    RepositoryHostNotAllowed,
    RepositoryHostValidationError,
    effective_repository_host_policy,
    https_origin_from_repository_url,
    normalize_repository_host_origin,
    require_repository_https_origin_allowed,
)
from bookmark_manager.services.secret_store import (
    SecretStore,
    SecretStoreError,
    SecretStoreOperationError,
    SecretStoreUnavailable,
)

logger = get_logger("https_credentials")

KEYRING_SERVICE = "owl.bitbucket.https"
CLOUD_ORIGIN = "https://bitbucket.org:443"
CLOUD_API_TOKEN_USERNAME = "x-bitbucket-api-token-auth"
CLOUD_ACCESS_TOKEN_USERNAME = "x-token-auth"
ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_TOKEN_CHARACTERS = 16_384
MAX_USERNAME_CHARACTERS = 320

NOT_CONFIGURED = "not_configured"
CREDENTIAL_STORE_UNAVAILABLE = "credential_store_unavailable"
CONFIGURATION_ERROR = "configuration_error"


class HTTPSCredentialError(RuntimeError):
    """Base class for safe Bitbucket HTTPS credential errors."""


class HTTPSCredentialValidationError(HTTPSCredentialError):
    """One credential profile value is outside OWL's safe boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HTTPSCredentialUnavailable(HTTPSCredentialError):
    """A configured HTTPS credential cannot be retrieved securely."""


@dataclass(frozen=True, slots=True)
class HTTPSCredentialSummary:
    origin: str
    kind: str
    source: str
    state: str
    label: str
    detail: str
    has_stored_credential: bool = False


@dataclass(frozen=True, slots=True)
class HTTPSCredentialActionResult:
    success: bool
    state: str
    label: str
    detail: str
    origin: str = ""
    kind: str = ""


@dataclass(frozen=True, slots=True)
class ActiveBitbucketHTTPSCredential:
    """A resolved credential that must remain inside server-side Git code."""

    origin: str
    kind: str
    username: str = field(repr=False)
    token: str = field(repr=False)
    source: str

    def authorization_header(self) -> str:
        value = base64.b64encode(f"{self.username}:{self.token}".encode()).decode("ascii")
        return f"Basic {value}"


@dataclass(frozen=True, slots=True)
class _BoundCredential:
    origin: str
    kind: str
    username: str = field(repr=False)
    token: str = field(repr=False)


def _normalized_hostname(value: object) -> str:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().rstrip(".")
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        raise HTTPSCredentialValidationError(
            "invalid_https_origin", "Enter a valid Bitbucket HTTPS origin."
        )
    try:
        hostname = raw.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise HTTPSCredentialValidationError(
            "invalid_https_origin", "Enter a valid Bitbucket HTTPS origin."
        ) from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise HTTPSCredentialValidationError(
            "invalid_https_origin", "Enter a valid Bitbucket HTTPS origin."
        )
    return hostname


def _allowed_hosts() -> frozenset[str]:
    return effective_repository_host_policy().hostnames


def normalize_https_origin(value: object) -> str:
    """Return ``https://host:effective-port`` for a safe approved origin.

    Existing credential call sites historically passed either an origin or a
    complete HTTPS repository clone URL. Keep accepting the latter while the
    Settings host-approval form uses the stricter pathless normalizer directly.
    """

    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw or len(raw) > 2048 or any(ord(character) < 32 for character in raw):
        raise HTTPSCredentialValidationError(
            "invalid_https_origin", "Enter a valid Bitbucket HTTPS origin."
        )
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
        normalized = (
            https_origin_from_repository_url(candidate)
            if parsed.path not in {"", "/"}
            else normalize_repository_host_origin(candidate)
        )
        require_repository_https_origin_allowed(normalized.canonical_origin)
    except (RepositoryHostValidationError, RepositoryHostNotAllowed) as exc:
        raise HTTPSCredentialValidationError(
            "invalid_https_origin", "Enter a valid Bitbucket HTTPS origin."
        ) from exc
    return normalized.canonical_origin


def available_https_origins() -> tuple[str, ...]:
    """List safe default origins plus exact origins used by registered HTTPS remotes."""

    origins = {entry.canonical_origin for entry in effective_repository_host_policy().entries}
    try:
        remote_urls = BitbucketRepository.objects.filter(
            remote_url__istartswith="https://"
        ).values_list("remote_url", flat=True)
        for remote_url in remote_urls:
            try:
                remote_origin = https_origin_from_repository_url(remote_url)
                require_repository_https_origin_allowed(remote_origin.canonical_origin)
                origins.add(remote_origin.canonical_origin)
            except (RepositoryHostValidationError, RepositoryHostNotAllowed):
                continue
    except (OperationalError, ProgrammingError):
        pass
    return tuple(sorted(origins))


def _credential_username(kind: str, username: object = "") -> str:
    if kind == BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN:
        return CLOUD_API_TOKEN_USERNAME
    if kind == BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN:
        return CLOUD_ACCESS_TOKEN_USERNAME
    if kind != BitbucketHTTPSCredentialKind.USERNAME_TOKEN:
        raise HTTPSCredentialValidationError(
            "unsupported_https_credential_kind",
            "Choose a supported Bitbucket HTTPS credential type.",
        )
    normalized = unicodedata.normalize("NFKC", str(username or "")).strip()
    if (
        not normalized
        or len(normalized) > MAX_USERNAME_CHARACTERS
        or ":" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise HTTPSCredentialValidationError(
            "https_username_required",
            "Enter a username without colons or control characters.",
        )
    return normalized


def _validated_profile(origin: object, kind: object, username: object = "") -> tuple[str, str, str]:
    normalized_origin = normalize_https_origin(origin)
    normalized_kind = str(kind or "").strip().casefold()
    if (
        normalized_kind
        in {
            BitbucketHTTPSCredentialKind.CLOUD_API_TOKEN,
            BitbucketHTTPSCredentialKind.CLOUD_ACCESS_TOKEN,
        }
        and normalized_origin != CLOUD_ORIGIN
    ):
        raise HTTPSCredentialValidationError(
            "cloud_credential_origin_required",
            "Bitbucket Cloud credential types can be saved only for bitbucket.org HTTPS.",
        )
    normalized_username = _credential_username(normalized_kind, username)
    return normalized_origin, normalized_kind, normalized_username


def _validated_token(value: object) -> str:
    raw_token = str(value or "")
    token = raw_token.strip()
    if (
        not token
        or token != raw_token
        or len(token) > MAX_TOKEN_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in token)
    ):
        raise HTTPSCredentialValidationError(
            "https_token_required", "Enter an HTTPS access or API token without control characters."
        )
    return token


def _credential_envelope(origin: str, kind: str, username: str, token: str) -> str:
    stored_value = json.dumps(
        {
            "kind": kind,
            "origin": origin,
            "token": token,
            "username": username,
            "version": ENVELOPE_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(stored_value.encode()) > MAX_ENVELOPE_BYTES:
        raise HTTPSCredentialValidationError(
            "https_credential_too_long", "The HTTPS credential is too long to store securely."
        )
    return stored_value


def _bound_credential(
    stored_value: object,
    *,
    expected_origin: str,
    expected_kind: str,
) -> _BoundCredential:
    if not isinstance(stored_value, str) or not stored_value:
        raise HTTPSCredentialUnavailable("The securely stored HTTPS credential is missing.")
    if len(stored_value.encode()) > MAX_ENVELOPE_BYTES:
        raise HTTPSCredentialUnavailable("The securely stored HTTPS credential is invalid.")
    try:
        payload = json.loads(stored_value)
    except (TypeError, ValueError) as exc:
        raise HTTPSCredentialUnavailable(
            "The securely stored HTTPS credential is invalid."
        ) from exc
    expected_keys = {"kind", "origin", "token", "username", "version"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise HTTPSCredentialUnavailable("The securely stored HTTPS credential is invalid.")
    origin = payload.get("origin")
    kind = payload.get("kind")
    username = payload.get("username")
    token = payload.get("token")
    version = payload.get("version")
    if not (
        isinstance(origin, str)
        and isinstance(kind, str)
        and isinstance(username, str)
        and isinstance(token, str)
        and bool(token)
        and type(version) is int
        and version == ENVELOPE_VERSION
        and origin == expected_origin
        and kind == expected_kind
    ):
        raise HTTPSCredentialUnavailable("The securely stored HTTPS credential is invalid.")
    try:
        expected_username = _credential_username(kind, username)
        expected_token = _validated_token(token)
    except HTTPSCredentialValidationError as exc:
        raise HTTPSCredentialUnavailable(
            "The securely stored HTTPS credential is invalid."
        ) from exc
    if username != expected_username or token != expected_token:
        raise HTTPSCredentialUnavailable(
            "The securely stored HTTPS credential does not match its profile."
        )
    return _BoundCredential(origin=origin, kind=kind, username=username, token=token)


def _logged_secret_operation(operation: str, backend: str):
    def decorate(function):
        @wraps(function)
        def observed(self, *args, **kwargs):
            started = time.monotonic()
            try:
                result = function(self, *args, **kwargs)
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "https_credential_store_operation_failed",
                    error=exc.__cause__
                    if isinstance(exc, SecretStoreError) and exc.__cause__ is not None
                    else exc,
                    operation=operation,
                    stage=backend,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise
            if operation != "get" or result is not None:
                log_event(
                    logger,
                    logging.DEBUG if operation == "get" else logging.INFO,
                    "https_credential_store_operation_completed",
                    operation=operation,
                    stage=backend,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            return result

        return observed

    return decorate


class KeyringHTTPSSecretStore(SecretStore):
    """One exact HTTPS origin in the operating-system credential store."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.account = hashlib.sha256(origin.encode()).hexdigest()

    @staticmethod
    def _keyring():
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as exc:  # pragma: no cover - dependency installation failure
            raise SecretStoreUnavailable("Credential-store support is unavailable.") from exc
        return keyring, KeyringError

    def is_available(self) -> bool:
        try:
            keyring, _ = self._keyring()
            return bool(getattr(keyring.get_keyring(), "priority", 0))
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "https_credential_store_availability_failed",
                error=exc,
                stage="keyring",
            )
            return False

    @_logged_secret_operation("get", "keyring")
    def get(self) -> str | None:
        keyring, keyring_error = self._keyring()
        try:
            return keyring.get_password(KEYRING_SERVICE, self.account)
        except keyring_error as exc:
            raise SecretStoreOperationError(
                "The saved HTTPS credential could not be read."
            ) from exc

    @_logged_secret_operation("set", "keyring")
    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        keyring, keyring_error = self._keyring()
        try:
            keyring.set_password(KEYRING_SERVICE, self.account, value)
        except keyring_error as exc:
            raise SecretStoreOperationError("The HTTPS credential could not be stored.") from exc

    @_logged_secret_operation("delete", "keyring")
    def delete(self) -> None:
        keyring, keyring_error = self._keyring()
        try:
            keyring.delete_password(KEYRING_SERVICE, self.account)
        except keyring_error as exc:
            try:
                if keyring.get_password(KEYRING_SERVICE, self.account) is None:
                    return
            except keyring_error:
                pass
            raise SecretStoreOperationError("The HTTPS credential could not be removed.") from exc


class DatabaseHTTPSSecretStore(SecretStore):
    """One exact HTTPS origin encrypted in its Bitbucket configuration row."""

    def __init__(self, origin: str) -> None:
        self.origin = origin

    @staticmethod
    def _fernet() -> Fernet:
        digest = hashlib.sha256(
            f"owl.bitbucket-https-credential.v1:{settings.SECRET_KEY}".encode()
        ).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def is_available(self) -> bool:
        try:
            BitbucketHTTPSCredential.objects.values_list("credential_ciphertext", flat=True).first()
        except DatabaseError as exc:
            log_event(
                logger,
                logging.ERROR,
                "https_credential_store_availability_failed",
                error=exc,
                stage="database",
            )
            return False
        return True

    @_logged_secret_operation("get", "database")
    def get(self) -> str | None:
        try:
            ciphertext = (
                BitbucketHTTPSCredential.objects.filter(origin=self.origin)
                .values_list("credential_ciphertext", flat=True)
                .first()
            )
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted HTTPS credential could not be read."
            ) from exc
        if not ciphertext:
            return None
        try:
            return self._fernet().decrypt(ciphertext.encode("ascii")).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreOperationError(
                "The encrypted HTTPS credential could not be read."
            ) from exc

    @_logged_secret_operation("set", "database")
    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        ciphertext = self._fernet().encrypt(value.encode()).decode("ascii")
        try:
            updated = BitbucketHTTPSCredential.objects.filter(origin=self.origin).update(
                credential_ciphertext=ciphertext
            )
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted HTTPS credential could not be stored."
            ) from exc
        if updated != 1:
            raise SecretStoreOperationError(
                "The encrypted HTTPS credential profile is unavailable."
            )

    @_logged_secret_operation("delete", "database")
    def delete(self) -> None:
        try:
            BitbucketHTTPSCredential.objects.filter(origin=self.origin).update(
                credential_ciphertext=""
            )
        except DatabaseError as exc:
            raise SecretStoreOperationError(
                "The encrypted HTTPS credential could not be removed."
            ) from exc


class InMemoryHTTPSSecretStore(SecretStore):
    """Explicit per-origin fake used only by automated tests."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.value: str | None = None
        self.available = True
        self.fail_reads = False
        self.fail_writes = False
        self.fail_deletes = False

    def is_available(self) -> bool:
        return self.available

    def get(self) -> str | None:
        if not self.available or self.fail_reads:
            raise SecretStoreOperationError("The fake HTTPS credential could not be read.")
        return self.value

    def set(self, value: str) -> None:
        if not value:
            raise ValueError("A non-empty credential is required.")
        if not self.available or self.fail_writes:
            raise SecretStoreOperationError("The fake HTTPS credential could not be stored.")
        self.value = value

    def delete(self) -> None:
        if not self.available or self.fail_deletes:
            raise SecretStoreOperationError("The fake HTTPS credential could not be removed.")
        self.value = None


def _configured_backend() -> str:
    return (
        str(getattr(settings, "BITBUCKET_SECRET_BACKEND", settings.CONFLUENCE_SECRET_BACKEND))
        .strip()
        .casefold()
    )


@lru_cache(maxsize=64)
def get_https_secret_store(origin: str) -> SecretStore:
    normalized_origin = normalize_https_origin(origin)
    backend = _configured_backend()
    if backend == "auto":
        keyring_store = KeyringHTTPSSecretStore(normalized_origin)
        if keyring_store.is_available():
            return keyring_store
        return DatabaseHTTPSSecretStore(normalized_origin)
    if backend == "keyring":
        return KeyringHTTPSSecretStore(normalized_origin)
    if backend == "database":
        return DatabaseHTTPSSecretStore(normalized_origin)
    if backend == "memory" and settings.OWL_ALLOW_IN_MEMORY_SECRET_STORE:
        return InMemoryHTTPSSecretStore(normalized_origin)
    raise SecretStoreUnavailable("The configured HTTPS credential backend is not permitted.")


def reset_https_secret_store_cache() -> None:
    """Clear only cached Bitbucket stores; never read or remove a credential."""

    get_https_secret_store.cache_clear()


def _store_for_record(
    record: BitbucketHTTPSCredential | None,
    origin: str,
    supplied: SecretStore | None,
) -> SecretStore:
    if supplied is not None:
        store = supplied
    elif _configured_backend() == "memory" and settings.OWL_ALLOW_IN_MEMORY_SECRET_STORE:
        store = get_https_secret_store(origin)
    elif record is not None and record.credential_source == BitbucketHTTPSCredentialSource.KEYRING:
        store = KeyringHTTPSSecretStore(origin)
    elif record is not None and record.credential_source == BitbucketHTTPSCredentialSource.DATABASE:
        store = DatabaseHTTPSSecretStore(origin)
    else:
        store = get_https_secret_store(origin)
    if not store.is_available():
        raise HTTPSCredentialUnavailable("The configured HTTPS credential store is unavailable.")
    return store


def _source_for_store(store: SecretStore) -> str:
    return (
        BitbucketHTTPSCredentialSource.DATABASE
        if isinstance(store, DatabaseHTTPSSecretStore)
        else BitbucketHTTPSCredentialSource.KEYRING
    )


def _action_failure(state: str, label: str, detail: str) -> HTTPSCredentialActionResult:
    return HTTPSCredentialActionResult(False, state, label, detail)


def get_https_credential_summary(
    origin: object,
    *,
    secret_store: SecretStore | None = None,
) -> HTTPSCredentialSummary:
    """Describe one profile without returning its username, token, or envelope."""

    normalized_origin = normalize_https_origin(origin)
    try:
        record = BitbucketHTTPSCredential.objects.filter(origin=normalized_origin).first()
    except (OperationalError, ProgrammingError):
        return HTTPSCredentialSummary(
            normalized_origin,
            "",
            "",
            NOT_CONFIGURED,
            "Database setup required",
            "Run database migrations before saving Bitbucket HTTPS credentials.",
        )
    if record is None:
        return HTTPSCredentialSummary(
            normalized_origin,
            "",
            "",
            NOT_CONFIGURED,
            "Not configured",
            "No HTTPS credential is stored for this origin.",
        )
    try:
        store = _store_for_record(record, normalized_origin, secret_store)
        bound = _bound_credential(
            store.get(), expected_origin=normalized_origin, expected_kind=record.kind
        )
    except (HTTPSCredentialError, SecretStoreError) as exc:
        log_event(
            logger,
            logging.ERROR,
            "https_credential_read_failed",
            error=exc,
            stage="summary",
        )
        return HTTPSCredentialSummary(
            normalized_origin,
            record.kind,
            record.credential_source,
            CREDENTIAL_STORE_UNAVAILABLE,
            "Credential store unavailable",
            "The saved HTTPS credential is missing, invalid, or unavailable. Replace or remove it.",
        )
    del bound
    return HTTPSCredentialSummary(
        normalized_origin,
        record.kind,
        record.credential_source,
        record.state,
        record.get_state_display(),
        "The Bitbucket HTTPS credential is stored securely on this computer.",
        has_stored_credential=True,
    )


def list_https_credential_summaries() -> tuple[HTTPSCredentialSummary, ...]:
    """Return credential-free summaries ordered by exact HTTPS origin."""

    try:
        origins = tuple(
            BitbucketHTTPSCredential.objects.order_by("origin").values_list("origin", flat=True)
        )
    except (OperationalError, ProgrammingError):
        return ()
    return tuple(get_https_credential_summary(origin) for origin in origins)


def save_https_credential(
    origin: object,
    kind: object,
    token: object,
    username: object = "",
    *,
    secret_store: SecretStore | None = None,
) -> HTTPSCredentialActionResult:
    """Atomically save or replace one exact-origin credential as unverified."""

    try:
        normalized_origin, normalized_kind, normalized_username = _validated_profile(
            origin, kind, username
        )
    except HTTPSCredentialValidationError as exc:
        return _action_failure(CONFIGURATION_ERROR, "Configuration error", str(exc))
    current = BitbucketHTTPSCredential.objects.filter(origin=normalized_origin).first()
    try:
        store = _store_for_record(current, normalized_origin, secret_store)
        previous_stored_value = store.get()
    except (HTTPSCredentialError, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "https_credential_read_failed", error=exc, stage="save")
        return _action_failure(
            CREDENTIAL_STORE_UNAVAILABLE,
            "Credential store unavailable",
            "The HTTPS credential was not saved because the secure store is unavailable.",
        )

    previous_bound = None
    if previous_stored_value and current is not None:
        try:
            previous_bound = _bound_credential(
                previous_stored_value,
                expected_origin=normalized_origin,
                expected_kind=current.kind,
            )
        except HTTPSCredentialUnavailable as exc:
            log_event(logger, logging.ERROR, "https_credential_invalid", error=exc, stage="save")
            return _action_failure(
                CREDENTIAL_STORE_UNAVAILABLE,
                "Credential store unavailable",
                "The stored HTTPS credential is invalid. Remove it before saving a replacement.",
            )

    submitted_token = str(token or "")
    binding_unchanged = bool(
        current
        and previous_bound
        and current.kind == normalized_kind
        and previous_bound.username == normalized_username
    )
    if submitted_token:
        try:
            next_token = _validated_token(submitted_token)
        except HTTPSCredentialValidationError as exc:
            return _action_failure(CONFIGURATION_ERROR, "Configuration error", str(exc))
    elif binding_unchanged:
        next_token = previous_bound.token
    else:
        return _action_failure(
            CONFIGURATION_ERROR,
            "Token required",
            "Enter a new token when creating or changing this HTTPS credential profile.",
        )

    token_changed = not previous_bound or not secrets.compare_digest(
        next_token, previous_bound.token
    )
    credential_write_required = token_changed or not binding_unchanged
    envelope = _credential_envelope(
        normalized_origin, normalized_kind, normalized_username, next_token
    )
    credential_written = False
    source = _source_for_store(store)
    try:
        with transaction.atomic():
            BitbucketHTTPSCredential.objects.update_or_create(
                origin=normalized_origin,
                defaults={
                    "kind": normalized_kind,
                    "credential_source": source,
                    "state": BitbucketHTTPSCredentialState.STORED_UNVERIFIED,
                    "configured_at": timezone.now(),
                },
            )
            if credential_write_required:
                store.set(envelope)
                credential_written = True
    except (DatabaseError, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "https_credential_save_failed", error=exc)
        if credential_written:
            try:
                if previous_stored_value:
                    store.set(previous_stored_value)
                else:
                    store.delete()
            except SecretStoreError as restore_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "https_credential_restore_failed",
                    error=restore_error,
                )
        return _action_failure(
            CREDENTIAL_STORE_UNAVAILABLE,
            "Not saved",
            "The replacement could not be committed. The prior credential remains selected.",
        )
    return HTTPSCredentialActionResult(
        True,
        BitbucketHTTPSCredentialState.STORED_UNVERIFIED,
        BitbucketHTTPSCredentialState.STORED_UNVERIFIED.label,
        "The Bitbucket HTTPS credential was stored securely and has not been verified.",
        origin=normalized_origin,
        kind=normalized_kind,
    )


def remove_https_credential(
    origin: object,
    *,
    secret_store: SecretStore | None = None,
) -> HTTPSCredentialActionResult:
    """Remove one secure credential and its metadata without touching repositories."""

    try:
        normalized_origin = normalize_https_origin(origin)
    except HTTPSCredentialValidationError as exc:
        return _action_failure(CONFIGURATION_ERROR, "Configuration error", str(exc))
    current = BitbucketHTTPSCredential.objects.filter(origin=normalized_origin).first()
    if current is None:
        return HTTPSCredentialActionResult(
            True,
            NOT_CONFIGURED,
            "Not configured",
            "No HTTPS credential is stored for this origin.",
            origin=normalized_origin,
        )
    try:
        store = _store_for_record(current, normalized_origin, secret_store)
        previous_stored_value = store.get()
        with transaction.atomic():
            locked = BitbucketHTTPSCredential.objects.select_for_update().get(
                origin=normalized_origin
            )
            store.delete()
            locked.delete()
    except (HTTPSCredentialError, SecretStoreError, DatabaseError) as exc:
        log_event(logger, logging.ERROR, "https_credential_remove_failed", error=exc)
        if "previous_stored_value" in locals() and previous_stored_value:
            try:
                store.set(previous_stored_value)
            except SecretStoreError as restore_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "https_credential_restore_failed",
                    error=restore_error,
                )
        return _action_failure(
            CREDENTIAL_STORE_UNAVAILABLE,
            "Credential not removed",
            "The secure store could not remove the credential. The profile is unchanged.",
        )
    return HTTPSCredentialActionResult(
        True,
        NOT_CONFIGURED,
        "Credential removed",
        "The Bitbucket HTTPS credential was removed. Repositories and local files are unchanged.",
        origin=normalized_origin,
    )


def resolve_https_credential(
    remote_url_or_origin: object,
    *,
    secret_store: SecretStore | None = None,
) -> ActiveBitbucketHTTPSCredential | None:
    """Resolve only an exact HTTPS origin; SSH and unconfigured origins return ``None``."""

    raw = str(remote_url_or_origin or "").strip()
    try:
        supplied_scheme = urlsplit(raw).scheme.casefold() if "://" in raw else ""
    except ValueError:
        return None
    if supplied_scheme and supplied_scheme != "https":
        return None
    try:
        if supplied_scheme == "https" and urlsplit(raw).path not in {"", "/"}:
            remote_origin = https_origin_from_repository_url(raw)
            require_repository_https_origin_allowed(remote_origin.canonical_origin)
            normalized_origin = remote_origin.canonical_origin
        else:
            normalized_origin = normalize_https_origin(raw)
    except (
        HTTPSCredentialValidationError,
        RepositoryHostValidationError,
        RepositoryHostNotAllowed,
        ValueError,
    ):
        # Credential resolution is optional. Existing SSH, local fixtures, and
        # HTTPS origins removed from the allowlist must continue through Git's
        # normal credential path; an exact configured origin is still required
        # before OWL ever releases a saved token.
        return None
    record = BitbucketHTTPSCredential.objects.filter(origin=normalized_origin).first()
    if record is None:
        return None
    try:
        store = _store_for_record(record, normalized_origin, secret_store)
        bound = _bound_credential(
            store.get(), expected_origin=normalized_origin, expected_kind=record.kind
        )
    except (HTTPSCredentialError, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "https_credential_read_failed", error=exc, stage="resolve")
        raise HTTPSCredentialUnavailable(
            "The saved Bitbucket HTTPS credential could not be resolved."
        ) from exc
    return ActiveBitbucketHTTPSCredential(
        origin=normalized_origin,
        kind=record.kind,
        username=bound.username,
        token=bound.token,
        source=record.credential_source,
    )
