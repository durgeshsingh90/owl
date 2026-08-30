from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Protocol

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, OperationalError, ProgrammingError, transaction
from django.utils import timezone

from bookmark_manager.models import (
    ConfluenceConfiguration,
    ConnectionStatus,
    CredentialSource,
)
from bookmark_manager.services.confluence_adapter import (
    ConfluenceAdapter,
    ConfluenceResult,
    ConfluenceResultCode,
)
from bookmark_manager.services.confluence_validation import (
    CanonicalOrigin,
    OriginValidationError,
    validate_confluence_origin,
)
from bookmark_manager.services.logging_events import get_logger, log_event, logging_context
from bookmark_manager.services.secret_store import (
    SecretStore,
    SecretStoreError,
    credential_source_for_store,
    get_secret_store,
)

logger = get_logger("configuration")

VERIFICATION_CACHE_PREFIX = "owl:confluence:verification:"
VERIFICATION_TTL_SECONDS = 10 * 60
UI_CREDENTIAL_ENVELOPE_VERSION = 1
MAX_UI_CREDENTIAL_ENVELOPE_BYTES = 64 * 1024


class ConfigurationUnavailable(RuntimeError):
    """A safe configuration problem for server-side integration callers."""


class CredentialEnvelopeError(ConfigurationUnavailable):
    """A stored UI credential is absent, malformed, or bound to another profile."""


class ConnectionTester(Protocol):
    def test_connection(self) -> ConfluenceResult: ...


type TesterFactory = Callable[[CanonicalOrigin, str, str], ConnectionTester]


@dataclass(frozen=True, slots=True)
class ConfigurationSummary:
    source: str
    complete: bool
    state: str
    label: str
    detail: str
    last_verified_at: datetime | None = None
    has_stored_credential: bool = False
    managed_externally: bool = False


@dataclass(frozen=True, slots=True)
class ConfigurationActionResult:
    success: bool
    state: str
    label: str
    detail: str
    verified_at: datetime | None = None
    verification_receipt: str = ""


@dataclass(frozen=True, slots=True)
class ActiveConfluenceProfile:
    origin: CanonicalOrigin = field(repr=False)
    token: str = field(repr=False)
    auth_mode: str
    source: str


@dataclass(frozen=True, slots=True)
class _BoundCredential:
    origin: str
    auth_mode: str
    token: str = field(repr=False)


def _logged_configuration_action(operation: str):
    """Log settings I/O without placing form values or receipt data in events."""

    def decorate(function):
        @wraps(function)
        def observed(*args, **kwargs):
            started = time.monotonic()
            with logging_context(operation=operation):
                log_event(logger, logging.INFO, "configuration_action_started")
                try:
                    result = function(*args, **kwargs)
                except Exception as exc:
                    log_event(
                        logger,
                        logging.ERROR,
                        "configuration_action_failed",
                        error=exc,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                    )
                    raise
                level = logging.INFO
                if not result.success:
                    level = (
                        logging.WARNING
                        if result.state
                        in (
                            ConnectionStatus.CONFIGURATION_ERROR,
                            ConnectionStatus.MANAGED_EXTERNALLY,
                        )
                        else logging.ERROR
                    )
                log_event(
                    logger,
                    level,
                    "configuration_action_completed"
                    if result.success
                    else "configuration_action_failed",
                    status=result.state,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                return result

        return observed

    return decorate


def _logged_configuration_read(function):
    @wraps(function)
    def observed(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ConfigurationUnavailable:
            # Expected absence is quiet; concrete database/store errors are logged
            # where they occur before being translated into this safe exception.
            raise
        except Exception as exc:
            log_event(logger, logging.ERROR, "configuration_read_failed", error=exc)
            raise

    return observed


def _credential_envelope(origin: CanonicalOrigin, auth_mode: str, token: str) -> str:
    """Create the versioned value written only to the operating-system secret store."""

    stored_value = json.dumps(
        {
            "auth_mode": auth_mode,
            "credential": token,
            "origin": origin.base_url,
            "version": UI_CREDENTIAL_ENVELOPE_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(stored_value.encode("utf-8")) > MAX_UI_CREDENTIAL_ENVELOPE_BYTES:
        raise CredentialEnvelopeError("The credential is too long to store securely.")
    return stored_value


def _bound_credential(
    stored_value: object,
    *,
    expected_origin: str,
    expected_auth_mode: str,
) -> _BoundCredential:
    """Decode one envelope and reject raw, malformed, or cross-profile values."""

    if not isinstance(stored_value, str) or not stored_value:
        raise CredentialEnvelopeError("The securely stored credential is missing.")
    if len(stored_value.encode("utf-8")) > MAX_UI_CREDENTIAL_ENVELOPE_BYTES:
        raise CredentialEnvelopeError("The securely stored credential is invalid.")
    try:
        payload = json.loads(stored_value)
    except (TypeError, ValueError) as exc:
        raise CredentialEnvelopeError("The securely stored credential is invalid.") from exc
    expected_keys = {"auth_mode", "credential", "origin", "version"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise CredentialEnvelopeError("The securely stored credential is invalid.")

    origin = payload.get("origin")
    auth_mode = payload.get("auth_mode")
    token = payload.get("credential")
    version = payload.get("version")
    valid_types = (
        isinstance(origin, str)
        and isinstance(auth_mode, str)
        and isinstance(token, str)
        and bool(token)
        and type(version) is int
    )
    if not valid_types or version != UI_CREDENTIAL_ENVELOPE_VERSION:
        raise CredentialEnvelopeError("The securely stored credential is invalid.")
    if origin != expected_origin or auth_mode != expected_auth_mode:
        raise CredentialEnvelopeError(
            "The securely stored credential does not match the configured Confluence profile."
        )
    return _BoundCredential(origin=origin, auth_mode=auth_mode, token=token)


def _environment_profile_state() -> tuple[bool, bool]:
    return bool(settings.CONFLUENCE_BASE_URL), bool(settings.CONFLUENCE_PAT)


def _configuration_error(detail: str) -> ConfigurationSummary:
    return ConfigurationSummary(
        source=CredentialSource.ENVIRONMENT,
        complete=False,
        state=ConnectionStatus.CONFIGURATION_ERROR,
        label="Incomplete environment configuration",
        detail=detail,
        managed_externally=True,
    )


def _store_or_unavailable(secret_store: SecretStore | None = None) -> SecretStore:
    try:
        store = secret_store or get_secret_store()
    except SecretStoreError as exc:
        log_event(logger, logging.ERROR, "configuration_store_unavailable", error=exc)
        raise ConfigurationUnavailable(
            "The configured credential store is unavailable. Enable it or use a complete "
            "ignored environment profile."
        ) from exc
    if not store.is_available():
        log_event(
            logger,
            logging.ERROR,
            "configuration_store_unavailable",
            reason="availability_check_failed",
        )
        raise ConfigurationUnavailable(
            "The configured credential store is unavailable. Enable it or use a complete "
            "ignored environment profile."
        )
    return store


@_logged_configuration_read
def get_configuration_summary(*, secret_store: SecretStore | None = None) -> ConfigurationSummary:
    """Describe the active source without returning an origin or retrieving a PAT for HTML."""

    has_environment_url, has_environment_pat = _environment_profile_state()
    if has_environment_url != has_environment_pat:
        log_event(
            logger, logging.WARNING, "configuration_environment_incomplete", operation="summary"
        )
        return _configuration_error("Set both Confluence environment values or remove both.")

    if has_environment_url and has_environment_pat:
        return ConfigurationSummary(
            source=CredentialSource.ENVIRONMENT,
            complete=True,
            state=ConnectionStatus.MANAGED_EXTERNALLY,
            label="Managed externally",
            detail="A complete environment-managed profile is active. Change it outside OWL.",
            has_stored_credential=True,
            managed_externally=True,
        )

    try:
        configuration = ConfluenceConfiguration.objects.filter(pk=1).first()
    except (OperationalError, ProgrammingError) as exc:
        log_event(
            logger,
            logging.ERROR,
            "configuration_database_read_failed",
            error=exc,
            operation="summary",
        )
        return ConfigurationSummary(
            source=CredentialSource.NONE,
            complete=False,
            state=ConnectionStatus.NOT_CONFIGURED,
            label="Database setup required",
            detail="Run database migrations before configuring Confluence.",
        )

    if configuration is None or not configuration.base_url:
        try:
            _store_or_unavailable(secret_store)
        except ConfigurationUnavailable:
            return ConfigurationSummary(
                source=CredentialSource.NONE,
                complete=False,
                state=ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
                label="Credential store unavailable",
                detail="Enable the operating-system credential store or use a complete "
                "ignored environment profile.",
            )
        return ConfigurationSummary(
            source=CredentialSource.NONE,
            complete=False,
            state=ConnectionStatus.NOT_CONFIGURED,
            label="Not configured",
            detail="Open Confluence settings to connect Bookmark Manager.",
        )

    stored_value = None
    try:
        store = _store_or_unavailable(secret_store)
        stored_value = store.get()
        _bound_credential(
            stored_value,
            expected_origin=configuration.base_url,
            expected_auth_mode=configuration.auth_mode,
        )
    except (ConfigurationUnavailable, SecretStoreError) as exc:
        if isinstance(exc, SecretStoreError) or (
            isinstance(exc, CredentialEnvelopeError) and stored_value
        ):
            log_event(
                logger,
                logging.ERROR,
                "configuration_credential_read_failed",
                error=exc,
                operation="summary",
            )
        return ConfigurationSummary(
            source=configuration.credential_source,
            complete=False,
            state=ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
            label="Credential store unavailable",
            detail="The saved credential is missing, invalid, or does not match this "
            "profile. Replace it or use a complete ignored environment profile.",
        )

    return ConfigurationSummary(
        source=configuration.credential_source,
        complete=True,
        state=configuration.connection_status,
        label=configuration.get_connection_status_display(),
        detail="The Confluence profile is stored encrypted on this computer.",
        last_verified_at=configuration.last_verified_at,
        has_stored_credential=True,
    )


def _validate_origin(value: str) -> CanonicalOrigin:
    return validate_confluence_origin(
        value,
        allow_test_targets=settings.OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS,
    )


@_logged_configuration_read
def get_active_profile(*, secret_store: SecretStore | None = None) -> ActiveConfluenceProfile:
    """Resolve credentials for explicit server-side requests; never pass this to templates."""

    has_environment_url, has_environment_pat = _environment_profile_state()
    if has_environment_url != has_environment_pat:
        log_event(logger, logging.WARNING, "configuration_environment_incomplete")
        raise ConfigurationUnavailable("The environment-managed Confluence profile is incomplete.")
    if has_environment_url and has_environment_pat:
        try:
            origin = _validate_origin(settings.CONFLUENCE_BASE_URL)
        except OriginValidationError as exc:
            log_event(
                logger,
                logging.WARNING,
                "configuration_origin_invalid",
                error=exc,
                stage="environment",
            )
            raise ConfigurationUnavailable(str(exc)) from exc
        return ActiveConfluenceProfile(
            origin=origin,
            token=settings.CONFLUENCE_PAT,
            auth_mode=settings.CONFLUENCE_AUTH_MODE,
            source=CredentialSource.ENVIRONMENT,
        )

    try:
        configuration = ConfluenceConfiguration.objects.filter(pk=1).first()
    except (OperationalError, ProgrammingError) as exc:
        log_event(logger, logging.ERROR, "configuration_database_read_failed", error=exc)
        raise ConfigurationUnavailable("Run database migrations before using Confluence.") from exc
    if configuration is None or not configuration.base_url:
        raise ConfigurationUnavailable("Configure Confluence before using this action.")

    try:
        origin = _validate_origin(configuration.base_url)
    except OriginValidationError as exc:
        log_event(logger, logging.ERROR, "configuration_origin_invalid", error=exc, stage="stored")
        raise ConfigurationUnavailable(str(exc)) from exc
    store = _store_or_unavailable(secret_store)
    try:
        stored_value = store.get()
    except SecretStoreError as exc:
        log_event(logger, logging.ERROR, "configuration_credential_read_failed", error=exc)
        raise ConfigurationUnavailable("The securely stored credential could not be read.") from exc
    try:
        bound = _bound_credential(
            stored_value,
            expected_origin=origin.base_url,
            expected_auth_mode=configuration.auth_mode,
        )
    except CredentialEnvelopeError as exc:
        if stored_value:
            log_event(logger, logging.ERROR, "configuration_credential_invalid", error=exc)
        raise
    return ActiveConfluenceProfile(
        origin=origin,
        token=bound.token,
        auth_mode=configuration.auth_mode,
        source=configuration.credential_source,
    )


def _default_tester_factory(
    origin: CanonicalOrigin, token: str, auth_mode: str
) -> ConnectionTester:
    return ConfluenceAdapter(
        origin,
        token,
        auth_mode=auth_mode,
        timeout_seconds=settings.CONFLUENCE_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes=settings.CONFLUENCE_MAX_RESPONSE_BYTES,
    )


def _token_digest(token: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _issue_verification_receipt(
    origin: CanonicalOrigin, auth_mode: str, token: str, verified_at: datetime
) -> str:
    receipt = secrets.token_urlsafe(32)
    cache.set(
        f"{VERIFICATION_CACHE_PREFIX}{receipt}",
        {
            "origin": origin.base_url,
            "auth_mode": auth_mode,
            "digest": _token_digest(token),
            "verified_at": verified_at.isoformat(),
        },
        timeout=VERIFICATION_TTL_SECONDS,
    )
    return receipt


def _consume_verification_receipt(
    receipt: str, origin: CanonicalOrigin, auth_mode: str, token: str
) -> datetime | None:
    if not receipt or len(receipt) > 256:
        return None
    key = f"{VERIFICATION_CACHE_PREFIX}{receipt}"
    record = cache.get(key)
    cache.delete(key)
    if not isinstance(record, dict):
        return None
    values_match = (
        record.get("origin") == origin.base_url
        and record.get("auth_mode") == auth_mode
        and isinstance(record.get("digest"), str)
        and hmac.compare_digest(record["digest"], _token_digest(token))
    )
    if not values_match:
        return None
    try:
        verified_at = datetime.fromisoformat(str(record["verified_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return verified_at if timezone.is_aware(verified_at) else timezone.make_aware(verified_at)


def _result_state(result: ConfluenceResult) -> str:
    mapping = {
        ConfluenceResultCode.CONNECTED: ConnectionStatus.CONNECTED,
        ConfluenceResultCode.SUCCESS: ConnectionStatus.CONNECTED,
        ConfluenceResultCode.INVALID_CREDENTIAL: ConnectionStatus.INVALID_CREDENTIAL,
        ConfluenceResultCode.ACCESS_DENIED: ConnectionStatus.ACCESS_DENIED,
        ConfluenceResultCode.RATE_LIMITED: ConnectionStatus.RATE_LIMITED,
        ConfluenceResultCode.UNREACHABLE: ConnectionStatus.UNREACHABLE,
        ConfluenceResultCode.UNSUPPORTED_RESPONSE: ConnectionStatus.UNSUPPORTED_RESPONSE,
        ConfluenceResultCode.NOT_FOUND: ConnectionStatus.UNSUPPORTED_RESPONSE,
    }
    return mapping[result.code]


@_logged_configuration_action("test_connection")
def test_candidate_connection(
    *,
    base_url: str,
    personal_access_token: str,
    auth_mode: str = "bearer",
    secret_store: SecretStore | None = None,
    tester_factory: TesterFactory | None = None,
) -> ConfigurationActionResult:
    """Test current form values once without persisting configuration or credentials."""

    try:
        origin = _validate_origin(base_url)
    except OriginValidationError as exc:
        return ConfigurationActionResult(
            False,
            ConnectionStatus.CONFIGURATION_ERROR,
            "Configuration error",
            str(exc),
        )
    normalized_auth_mode = auth_mode.strip().casefold()
    if normalized_auth_mode != "bearer":
        return ConfigurationActionResult(
            False,
            ConnectionStatus.CONFIGURATION_ERROR,
            "Configuration error",
            "Only Bearer authentication is supported.",
        )

    token = personal_access_token.strip()
    if not token:
        stored_value = None
        try:
            current = ConfluenceConfiguration.objects.filter(pk=1).first()
            if current is None or current.base_url != origin.base_url:
                raise ConfigurationUnavailable("Enter a PAT for this Confluence origin.")
            store = _store_or_unavailable(secret_store)
            stored_value = store.get()
            token = _bound_credential(
                stored_value,
                expected_origin=origin.base_url,
                expected_auth_mode=normalized_auth_mode,
            ).token
        except (ConfigurationUnavailable, SecretStoreError) as exc:
            if isinstance(exc, SecretStoreError) or (
                isinstance(exc, CredentialEnvelopeError) and stored_value
            ):
                log_event(logger, logging.ERROR, "configuration_credential_read_failed", error=exc)
            token = ""
    if not token:
        return ConfigurationActionResult(
            False,
            ConnectionStatus.CONFIGURATION_ERROR,
            "PAT required",
            "Enter a PAT to test this Confluence origin.",
        )

    log_event(logger, logging.DEBUG, "configuration_connection_test_started")
    try:
        tester = (tester_factory or _default_tester_factory)(origin, token, normalized_auth_mode)
        result = tester.test_connection()
    except Exception as exc:
        log_event(logger, logging.ERROR, "configuration_connection_test_failed", error=exc)
        return ConfigurationActionResult(
            False,
            ConnectionStatus.UNREACHABLE,
            "Unreachable",
            "OWL could not complete the bounded read-only connection test.",
        )

    state = _result_state(result)
    log_event(
        logger,
        logging.DEBUG if state == ConnectionStatus.CONNECTED else logging.ERROR,
        "configuration_connection_test_completed"
        if state == ConnectionStatus.CONNECTED
        else "configuration_connection_test_failed",
        status=state,
    )
    verified_at = timezone.now() if state == ConnectionStatus.CONNECTED else None
    receipt = (
        _issue_verification_receipt(origin, normalized_auth_mode, token, verified_at)
        if verified_at is not None
        else ""
    )
    return ConfigurationActionResult(
        state == ConnectionStatus.CONNECTED,
        state,
        ConnectionStatus(state).label,
        result.message,
        verified_at=verified_at,
        verification_receipt=receipt,
    )


def _action_failure(state: str, label: str, detail: str) -> ConfigurationActionResult:
    return ConfigurationActionResult(False, state, label, detail)


@_logged_configuration_action("save")
def save_ui_configuration(
    *,
    base_url: str,
    personal_access_token: str,
    auth_mode: str = "bearer",
    verification_receipt: str = "",
    secret_store: SecretStore | None = None,
) -> ConfigurationActionResult:
    """Atomically replace UI-managed non-secret state and the secure credential."""

    if any(_environment_profile_state()):
        return _action_failure(
            ConnectionStatus.MANAGED_EXTERNALLY,
            "Managed externally",
            "Change the complete environment-managed profile outside OWL.",
        )
    try:
        origin = _validate_origin(base_url)
    except OriginValidationError as exc:
        return _action_failure(
            ConnectionStatus.CONFIGURATION_ERROR, "Configuration error", str(exc)
        )
    normalized_auth_mode = auth_mode.strip().casefold()
    if normalized_auth_mode != "bearer":
        return _action_failure(
            ConnectionStatus.CONFIGURATION_ERROR,
            "Configuration error",
            "Only Bearer authentication is supported.",
        )
    current = ConfluenceConfiguration.objects.filter(pk=1).first()
    try:
        store = _store_or_unavailable(secret_store)
        previous_stored_value = store.get()
    except (ConfigurationUnavailable, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "configuration_credential_read_failed", error=exc)
        return _action_failure(
            ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
            "Credential store unavailable",
            "The credential was not saved. Enable the secure store or use a complete "
            "ignored environment profile.",
        )

    previous_bound: _BoundCredential | None = None
    if previous_stored_value:
        if current is None or not current.base_url:
            return _action_failure(
                ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
                "Credential store unavailable",
                "The stored credential has no matching local profile. Remove it before "
                "saving a new profile.",
            )
        try:
            previous_bound = _bound_credential(
                previous_stored_value,
                expected_origin=current.base_url,
                expected_auth_mode=current.auth_mode,
            )
        except CredentialEnvelopeError as exc:
            log_event(logger, logging.ERROR, "configuration_credential_invalid", error=exc)
            return _action_failure(
                ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
                "Credential store unavailable",
                "The stored credential is invalid or does not match the local profile. "
                "Remove it before saving a replacement.",
            )

    submitted_secret = personal_access_token.strip()
    origin_changed = bool(current and current.base_url and current.base_url != origin.base_url)
    if (current is None or not current.base_url or origin_changed) and not submitted_secret:
        return _action_failure(
            ConnectionStatus.CONFIGURATION_ERROR,
            "PAT required",
            "Enter a new PAT when configuring or changing the Confluence origin.",
        )
    previous_token = previous_bound.token if previous_bound is not None else ""
    next_secret = submitted_secret or previous_token
    if not next_secret:
        return _action_failure(
            ConnectionStatus.CONFIGURATION_ERROR,
            "PAT required",
            "Enter a PAT before saving this profile.",
        )

    token_changed = bool(submitted_secret) and not (
        previous_token and secrets.compare_digest(submitted_secret, previous_token)
    )
    auth_changed = bool(current and current.auth_mode != normalized_auth_mode)
    profile_changed = current is None or origin_changed or auth_changed or token_changed
    credential_binding_changed = current is None or origin_changed or auth_changed
    credential_write_required = bool(submitted_secret) and (
        token_changed or credential_binding_changed
    )
    verified_at = _consume_verification_receipt(
        verification_receipt, origin, normalized_auth_mode, next_secret
    )
    retain_connected = bool(
        current
        and not profile_changed
        and current.connection_status == ConnectionStatus.CONNECTED
        and current.last_verified_at
    )
    connection_status = (
        ConnectionStatus.CONNECTED
        if verified_at is not None or retain_connected
        else ConnectionStatus.STORED_UNVERIFIED
    )
    if retain_connected:
        verified_at = current.last_verified_at

    credential_written = False
    credential_source = credential_source_for_store(store)
    log_event(
        logger,
        logging.DEBUG,
        "configuration_save_prepared",
        stage="replace_credential" if credential_write_required else "retain_credential",
        status=connection_status,
    )
    try:
        if credential_write_required:
            store.set(_credential_envelope(origin, normalized_auth_mode, submitted_secret))
            credential_written = True
        with transaction.atomic():
            locked = ConfluenceConfiguration.objects.select_for_update().filter(pk=1).first()
            configured_at = (
                timezone.now()
                if profile_changed
                else (locked.configured_at if locked else timezone.now())
            )
            ConfluenceConfiguration.objects.update_or_create(
                pk=1,
                defaults={
                    "base_url": origin.base_url,
                    "auth_mode": normalized_auth_mode,
                    "credential_source": credential_source,
                    "connection_status": connection_status,
                    "configured_at": configured_at,
                    "last_test_attempt_at": verified_at,
                    "last_verified_at": verified_at,
                    "last_error_code": "",
                    "last_error_message": "",
                },
            )
    except (CredentialEnvelopeError, DatabaseError, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "configuration_save_failed", error=exc)
        if credential_written:
            log_event(logger, logging.WARNING, "configuration_credential_restore_started")
            try:
                if previous_stored_value:
                    store.set(previous_stored_value)
                else:
                    store.delete()
            except SecretStoreError as restore_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "configuration_credential_restore_failed",
                    error=restore_error,
                )
            else:
                log_event(logger, logging.INFO, "configuration_credential_restored")
        return _action_failure(
            ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
            "Not saved",
            "The replacement could not be committed. The prior profile remains selected.",
        )

    return ConfigurationActionResult(
        True,
        connection_status,
        ConnectionStatus(connection_status).label,
        "The Confluence profile was stored encrypted.",
        verified_at=verified_at,
    )


@_logged_configuration_action("remove")
def remove_ui_configuration(
    *, secret_store: SecretStore | None = None
) -> ConfigurationActionResult:
    """Remove the secure credential first, preserving local bookmarks in every outcome."""

    if any(_environment_profile_state()):
        return _action_failure(
            ConnectionStatus.MANAGED_EXTERNALLY,
            "Managed externally",
            "Environment-managed credentials cannot be removed through OWL.",
        )
    current = ConfluenceConfiguration.objects.filter(pk=1).first()
    if current is None or not current.base_url:
        return ConfigurationActionResult(
            True,
            ConnectionStatus.NOT_CONFIGURED,
            "Not configured",
            "No UI-managed credential is stored.",
        )
    try:
        store = _store_or_unavailable(secret_store)
        previous_stored_value = store.get()
        store.delete()
    except (ConfigurationUnavailable, SecretStoreError) as exc:
        log_event(logger, logging.ERROR, "configuration_credential_remove_failed", error=exc)
        return _action_failure(
            ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
            "Credential not removed",
            "The secure store could not remove the credential. The local profile is unchanged.",
        )

    try:
        with transaction.atomic():
            locked = ConfluenceConfiguration.objects.select_for_update().get(pk=1)
            locked.base_url = ""
            locked.auth_mode = "bearer"
            locked.credential_source = CredentialSource.NONE
            locked.connection_status = ConnectionStatus.NOT_CONFIGURED
            locked.configured_at = None
            locked.last_test_attempt_at = None
            locked.last_verified_at = None
            locked.last_error_code = ""
            locked.last_error_message = ""
            locked.save()
    except DatabaseError as exc:
        log_event(logger, logging.ERROR, "configuration_remove_failed", error=exc)
        if previous_stored_value:
            log_event(logger, logging.WARNING, "configuration_credential_restore_started")
            try:
                store.set(previous_stored_value)
            except SecretStoreError as restore_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "configuration_credential_restore_failed",
                    error=restore_error,
                )
            else:
                log_event(logger, logging.INFO, "configuration_credential_restored")
        return _action_failure(
            ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE,
            "Credential not removed",
            "The local profile could not be cleared. OWL attempted to restore the prior credential.",
        )

    return ConfigurationActionResult(
        True,
        ConnectionStatus.NOT_CONFIGURED,
        "Credential removed",
        "The secure credential was removed. Local bookmarks remain available.",
    )
