from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.db import DatabaseError
from django.test import override_settings

from bookmark_manager.models import (
    Bookmark,
    ConfluenceConfiguration,
    ConfluencePageNode,
    ConnectionStatus,
    CredentialSource,
)
from bookmark_manager.services.configuration import (
    UI_CREDENTIAL_ENVELOPE_VERSION,
    VERIFICATION_CACHE_PREFIX,
    ConfigurationUnavailable,
    get_active_profile,
    get_configuration_summary,
    remove_ui_configuration,
    save_ui_configuration,
)
from bookmark_manager.services.configuration import (
    test_candidate_connection as check_candidate_connection,
)
from bookmark_manager.services.confluence_adapter import (
    ConfluenceResult,
    ConfluenceResultCode,
)
from bookmark_manager.services.secret_store import (
    InMemorySecretStore,
    SecretStoreOperationError,
)

pytestmark = pytest.mark.django_db

SYNTHETIC_ORIGIN = "https://confluence.example.invalid/wiki"
SECOND_SYNTHETIC_ORIGIN = "https://knowledge.example.invalid/confluence"


class StaticConnectionTester:
    def __init__(self, result: ConfluenceResult) -> None:
        self._result = result

    def test_connection(self) -> ConfluenceResult:
        return self._result


class RecordingTesterFactory:
    """Verify the candidate credential without retaining it in repr or call records."""

    def __init__(self, expected_credential: str, result: ConfluenceResult) -> None:
        self._expected_digest = hashlib.sha256(expected_credential.encode()).digest()
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, origin, credential: str, auth_mode: str) -> StaticConnectionTester:
        assert hashlib.sha256(credential.encode()).digest() == self._expected_digest
        self.calls.append((origin.base_url, auth_mode))
        return StaticConnectionTester(self._result)


class CompensationFailureStore(InMemorySecretStore):
    """Allow a replacement write, then fail only when restoring the old envelope."""

    def __init__(self) -> None:
        super().__init__()
        self._blocked_restore_value: str | None = None

    def block_restoration_of_current_value(self) -> None:
        self._blocked_restore_value = self.get()

    def set(self, value: str) -> None:
        if self._blocked_restore_value is not None and value == self._blocked_restore_value:
            raise SecretStoreOperationError("Synthetic compensation failure.")
        super().set(value)


@pytest.fixture(autouse=True)
def isolated_verification_receipts() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def secure_store() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def unique_credential() -> str:
    return f"owl-{uuid.uuid4().hex}-synthetic-never-valid"


@pytest.fixture
def second_unique_credential() -> str:
    return f"owl-{uuid.uuid4().hex}-replacement-never-valid"


def connected_result(message: str = "Synthetic connection succeeded.") -> ConfluenceResult:
    return ConfluenceResult(ConfluenceResultCode.CONNECTED, message)


def save_local_profile(
    secure_store: InMemorySecretStore,
    credential: str,
    *,
    base_url: str = SYNTHETIC_ORIGIN,
    verification_receipt: str = "",
):
    return save_ui_configuration(
        base_url=base_url,
        personal_access_token=credential,
        verification_receipt=verification_receipt,
        secret_store=secure_store,
    )


def assert_store_binding(
    secure_store: InMemorySecretStore,
    credential: str,
    *,
    origin: str = SYNTHETIC_ORIGIN,
    auth_mode: str = "bearer",
) -> dict[str, object]:
    stored_value = secure_store.get()
    assert stored_value is not None
    assert stored_value != credential
    envelope = json.loads(stored_value)
    assert envelope == {
        "auth_mode": auth_mode,
        "credential": credential,
        "origin": origin,
        "version": UI_CREDENTIAL_ENVELOPE_VERSION,
    }
    return envelope


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_in_memory_store_round_trip_never_places_credential_in_database(
    secure_store,
    unique_credential,
):
    result = save_local_profile(secure_store, unique_credential)

    assert result.success is True
    assert result.state == ConnectionStatus.STORED_UNVERIFIED
    assert_store_binding(secure_store, unique_credential)
    configuration = ConfluenceConfiguration.objects.get(pk=1)
    assert configuration.base_url == SYNTHETIC_ORIGIN
    assert configuration.credential_source == CredentialSource.KEYRING
    assert unique_credential not in repr(result)
    assert unique_credential not in repr(configuration.__dict__)


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_connection_receipt_is_issued_without_saving_form_values(
    secure_store,
    unique_credential,
):
    factory = RecordingTesterFactory(unique_credential, connected_result())

    result = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=factory,
    )

    assert result.success is True
    assert result.state == ConnectionStatus.CONNECTED
    assert result.verified_at is not None
    assert result.verification_receipt
    assert factory.calls == [(SYNTHETIC_ORIGIN, "bearer")]
    assert ConfluenceConfiguration.objects.count() == 0
    assert secure_store.get() is None
    assert unique_credential not in repr(result)

    cached_receipt = cache.get(f"{VERIFICATION_CACHE_PREFIX}{result.verification_receipt}")
    assert isinstance(cached_receipt, dict)
    assert unique_credential not in repr(cached_receipt)
    assert cached_receipt["digest"] != unique_credential


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_matching_receipt_marks_unchanged_values_connected(
    secure_store,
    unique_credential,
):
    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=RecordingTesterFactory(unique_credential, connected_result()),
    )

    saved = save_local_profile(
        secure_store,
        unique_credential,
        verification_receipt=tested.verification_receipt,
    )

    assert saved.success is True
    assert saved.state == ConnectionStatus.CONNECTED
    assert saved.verified_at == tested.verified_at
    configuration = ConfluenceConfiguration.objects.get(pk=1)
    assert configuration.connection_status == ConnectionStatus.CONNECTED
    assert configuration.last_test_attempt_at == tested.verified_at
    assert configuration.last_verified_at == tested.verified_at


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_receipt_token_mismatch_is_consumed_and_cannot_verify_a_later_save(
    secure_store,
    unique_credential,
    second_unique_credential,
):
    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=RecordingTesterFactory(unique_credential, connected_result()),
    )

    mismatched = save_local_profile(
        secure_store,
        second_unique_credential,
        verification_receipt=tested.verification_receipt,
    )
    reused = save_local_profile(
        secure_store,
        unique_credential,
        verification_receipt=tested.verification_receipt,
    )

    assert mismatched.success is True
    assert mismatched.state == ConnectionStatus.STORED_UNVERIFIED
    assert reused.success is True
    assert reused.state == ConnectionStatus.STORED_UNVERIFIED
    assert cache.get(f"{VERIFICATION_CACHE_PREFIX}{tested.verification_receipt}") is None


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_receipt_origin_mismatch_does_not_carry_verification_to_new_origin(
    secure_store,
    unique_credential,
):
    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=RecordingTesterFactory(unique_credential, connected_result()),
    )

    saved = save_local_profile(
        secure_store,
        unique_credential,
        base_url=SECOND_SYNTHETIC_ORIGIN,
        verification_receipt=tested.verification_receipt,
    )

    assert saved.success is True
    assert saved.state == ConnectionStatus.STORED_UNVERIFIED
    assert saved.verified_at is None


@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        (ConfluenceResultCode.INVALID_CREDENTIAL, ConnectionStatus.INVALID_CREDENTIAL),
        (ConfluenceResultCode.ACCESS_DENIED, ConnectionStatus.ACCESS_DENIED),
        (ConfluenceResultCode.RATE_LIMITED, ConnectionStatus.RATE_LIMITED),
        (ConfluenceResultCode.UNREACHABLE, ConnectionStatus.UNREACHABLE),
        (ConfluenceResultCode.UNSUPPORTED_RESPONSE, ConnectionStatus.UNSUPPORTED_RESPONSE),
    ],
)
@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_failed_connection_results_are_sanitized_and_never_issue_receipts(
    secure_store,
    unique_credential,
    code,
    expected_state,
):
    factory = RecordingTesterFactory(
        unique_credential,
        ConfluenceResult(code, "Synthetic action-oriented failure."),
    )

    result = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=factory,
    )

    assert result.success is False
    assert result.state == expected_state
    assert result.verification_receipt == ""
    assert result.verified_at is None
    assert unique_credential not in repr(result)
    assert ConfluenceConfiguration.objects.count() == 0
    assert secure_store.get() is None


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_connection_exception_that_contains_credential_is_replaced_with_safe_result(
    secure_store,
    unique_credential,
):
    def exploding_factory(_origin, credential, _auth_mode):
        raise RuntimeError(credential)

    result = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=exploding_factory,
    )

    assert result.success is False
    assert result.state == ConnectionStatus.UNREACHABLE
    assert result.verification_receipt == ""
    assert unique_credential not in repr(result)
    assert unique_credential not in result.detail
    assert ConfluenceConfiguration.objects.count() == 0
    assert secure_store.get() is None


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_unchanged_connected_profile_can_save_with_empty_replacement_field(
    secure_store,
    unique_credential,
):
    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=RecordingTesterFactory(unique_credential, connected_result()),
    )
    connected = save_local_profile(
        secure_store,
        unique_credential,
        verification_receipt=tested.verification_receipt,
    )

    saved_again = save_local_profile(secure_store, "")

    assert connected.state == ConnectionStatus.CONNECTED
    assert saved_again.success is True
    assert saved_again.state == ConnectionStatus.CONNECTED
    assert saved_again.verified_at == connected.verified_at
    assert_store_binding(secure_store, unique_credential)


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_same_origin_credential_replacement_is_saved_but_becomes_unverified(
    secure_store,
    unique_credential,
    second_unique_credential,
):
    first = save_local_profile(secure_store, unique_credential)
    first_configuration = ConfluenceConfiguration.objects.get(pk=1)
    first_configured_at = first_configuration.configured_at

    replaced = save_local_profile(secure_store, second_unique_credential)

    assert first.success is True
    assert replaced.success is True
    assert replaced.state == ConnectionStatus.STORED_UNVERIFIED
    assert_store_binding(secure_store, second_unique_credential)
    first_configuration.refresh_from_db()
    assert first_configuration.base_url == SYNTHETIC_ORIGIN
    assert first_configuration.last_verified_at is None
    assert first_configuration.configured_at >= first_configured_at


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_origin_change_requires_new_credential_and_preserves_previous_profile_on_failure(
    secure_store,
    unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    prior = ConfluenceConfiguration.objects.get(pk=1)

    changed = save_ui_configuration(
        base_url=SECOND_SYNTHETIC_ORIGIN,
        personal_access_token="",
        secret_store=secure_store,
    )

    assert changed.success is False
    assert changed.state == ConnectionStatus.CONFIGURATION_ERROR
    prior.refresh_from_db()
    assert prior.base_url == SYNTHETIC_ORIGIN
    assert_store_binding(secure_store, unique_credential)


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_origin_change_with_new_credential_replaces_both_profile_parts(
    secure_store,
    unique_credential,
    second_unique_credential,
):
    save_local_profile(secure_store, unique_credential)

    changed = save_local_profile(
        secure_store,
        second_unique_credential,
        base_url=SECOND_SYNTHETIC_ORIGIN,
    )

    assert changed.success is True
    assert changed.state == ConnectionStatus.STORED_UNVERIFIED
    assert_store_binding(
        secure_store,
        second_unique_credential,
        origin=SECOND_SYNTHETIC_ORIGIN,
    )
    configuration = ConfluenceConfiguration.objects.get(pk=1)
    assert configuration.base_url == SECOND_SYNTHETIC_ORIGIN
    assert configuration.last_verified_at is None


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_origin_change_rebinds_envelope_even_if_submitted_credential_text_is_same(
    secure_store,
    unique_credential,
):
    save_local_profile(secure_store, unique_credential)

    changed = save_local_profile(
        secure_store,
        unique_credential,
        base_url=SECOND_SYNTHETIC_ORIGIN,
    )

    assert changed.success is True
    assert changed.state == ConnectionStatus.STORED_UNVERIFIED
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SECOND_SYNTHETIC_ORIGIN
    assert_store_binding(
        secure_store,
        unique_credential,
        origin=SECOND_SYNTHETIC_ORIGIN,
    )
    profile = get_active_profile(secret_store=secure_store)
    assert profile.origin.base_url == SECOND_SYNTHETIC_ORIGIN
    assert profile.token == unique_credential


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_secure_store_write_failure_preserves_previous_profile(
    secure_store,
    unique_credential,
    second_unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    secure_store.fail_writes = True

    result = save_local_profile(secure_store, second_unique_credential)

    assert result.success is False
    assert result.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE
    secure_store.fail_writes = False
    assert_store_binding(secure_store, unique_credential)
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SYNTHETIC_ORIGIN


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_database_failure_after_replacement_restores_previous_secure_credential(
    monkeypatch,
    secure_store,
    unique_credential,
    second_unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    prior = ConfluenceConfiguration.objects.get(pk=1)
    prior_configured_at = prior.configured_at

    def fail_database_write(*_args, **_kwargs):
        raise DatabaseError("synthetic database failure")

    monkeypatch.setattr(
        ConfluenceConfiguration.objects,
        "update_or_create",
        fail_database_write,
    )
    result = save_local_profile(secure_store, second_unique_credential)

    assert result.success is False
    assert_store_binding(secure_store, unique_credential)
    prior.refresh_from_db()
    assert prior.base_url == SYNTHETIC_ORIGIN
    assert prior.configured_at == prior_configured_at


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_db_and_compensation_failure_cannot_pair_new_credential_with_old_origin(
    monkeypatch,
    unique_credential,
    second_unique_credential,
):
    secure_store = CompensationFailureStore()
    save_local_profile(secure_store, unique_credential)
    secure_store.block_restoration_of_current_value()

    def fail_database_write(*_args, **_kwargs):
        raise DatabaseError("synthetic database failure")

    monkeypatch.setattr(
        ConfluenceConfiguration.objects,
        "update_or_create",
        fail_database_write,
    )

    result = save_local_profile(
        secure_store,
        second_unique_credential,
        base_url=SECOND_SYNTHETIC_ORIGIN,
    )

    assert result.success is False
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SYNTHETIC_ORIGIN
    assert_store_binding(
        secure_store,
        second_unique_credential,
        origin=SECOND_SYNTHETIC_ORIGIN,
    )

    summary = get_configuration_summary(secret_store=secure_store)
    assert summary.complete is False
    assert summary.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE
    with pytest.raises(ConfigurationUnavailable, match="does not match") as error:
        get_active_profile(secret_store=secure_store)
    assert unique_credential not in str(error.value)
    assert second_unique_credential not in str(error.value)
    assert second_unique_credential not in repr(result)
    assert second_unique_credential not in repr(summary)


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_initial_database_failure_removes_newly_written_secure_credential(
    monkeypatch,
    secure_store,
    unique_credential,
):
    def fail_database_write(*_args, **_kwargs):
        raise DatabaseError("synthetic database failure")

    monkeypatch.setattr(
        ConfluenceConfiguration.objects,
        "update_or_create",
        fail_database_write,
    )

    result = save_local_profile(secure_store, unique_credential)

    assert result.success is False
    assert secure_store.get() is None
    assert ConfluenceConfiguration.objects.count() == 0


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_remove_deletes_secure_credential_but_preserves_local_bookmarks(
    secure_store,
    unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    node = ConfluencePageNode.objects.create(
        page_id="700",
        title="Synthetic local bookmark",
        url=f"{SYNTHETIC_ORIGIN}/pages/700",
        space_key="SYN",
    )
    bookmark = Bookmark.objects.create(
        page_id="700",
        tree_node=node,
        title="Synthetic local bookmark",
        url=node.url,
        space_key="SYN",
    )

    result = remove_ui_configuration(secret_store=secure_store)

    assert result.success is True
    assert result.state == ConnectionStatus.NOT_CONFIGURED
    assert secure_store.get() is None
    configuration = ConfluenceConfiguration.objects.get(pk=1)
    assert configuration.base_url == ""
    assert configuration.credential_source == CredentialSource.NONE
    assert configuration.connection_status == ConnectionStatus.NOT_CONFIGURED
    assert Bookmark.objects.filter(pk=bookmark.pk).exists()
    assert ConfluencePageNode.objects.filter(pk=node.pk).exists()


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_secure_delete_failure_leaves_local_profile_and_credential_unchanged(
    secure_store,
    unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    secure_store.fail_deletes = True

    result = remove_ui_configuration(secret_store=secure_store)

    assert result.success is False
    secure_store.fail_deletes = False
    assert_store_binding(secure_store, unique_credential)
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SYNTHETIC_ORIGIN


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_database_failure_during_remove_restores_deleted_secure_credential(
    monkeypatch,
    secure_store,
    unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    original_save = ConfluenceConfiguration.save

    def fail_profile_clear(instance, *args, **kwargs):
        if not instance.base_url:
            raise DatabaseError("synthetic database failure")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(ConfluenceConfiguration, "save", fail_profile_clear)

    result = remove_ui_configuration(secret_store=secure_store)

    assert result.success is False
    assert_store_binding(secure_store, unique_credential)
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SYNTHETIC_ORIGIN


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_unavailable_store_never_falls_back_to_database_or_plaintext(
    secure_store,
    unique_credential,
):
    secure_store.available = False

    summary = get_configuration_summary(secret_store=secure_store)
    saved = save_local_profile(secure_store, unique_credential)

    assert summary.complete is False
    assert summary.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE
    assert saved.success is False
    assert saved.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE
    assert ConfluenceConfiguration.objects.count() == 0
    ConfluenceConfiguration.objects.create(
        base_url=SYNTHETIC_ORIGIN,
        credential_source=CredentialSource.KEYRING,
        connection_status=ConnectionStatus.STORED_UNVERIFIED,
    )
    with pytest.raises(ConfigurationUnavailable, match="credential store is unavailable"):
        get_active_profile(secret_store=secure_store)


@pytest.mark.parametrize(
    "invalid_envelope_kind",
    ["raw", "malformed", "version", "origin", "auth_mode"],
)
@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_raw_malformed_or_mismatched_stored_values_fail_closed_on_every_use(
    secure_store,
    unique_credential,
    invalid_envelope_kind,
):
    ConfluenceConfiguration.objects.create(
        base_url=SYNTHETIC_ORIGIN,
        auth_mode="bearer",
        credential_source=CredentialSource.KEYRING,
        connection_status=ConnectionStatus.CONNECTED,
    )
    valid_payload = {
        "auth_mode": "bearer",
        "credential": unique_credential,
        "origin": SYNTHETIC_ORIGIN,
        "version": UI_CREDENTIAL_ENVELOPE_VERSION,
    }
    if invalid_envelope_kind == "raw":
        stored_value = unique_credential
    elif invalid_envelope_kind == "malformed":
        stored_value = "{synthetic-invalid-envelope"
    else:
        if invalid_envelope_kind == "version":
            valid_payload["version"] = UI_CREDENTIAL_ENVELOPE_VERSION + 1
        elif invalid_envelope_kind == "origin":
            valid_payload["origin"] = SECOND_SYNTHETIC_ORIGIN
        else:
            valid_payload["auth_mode"] = "synthetic-unsupported-mode"
        stored_value = json.dumps(valid_payload)
    secure_store.set(stored_value)

    summary = get_configuration_summary(secret_store=secure_store)

    assert summary.complete is False
    assert summary.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE
    assert unique_credential not in repr(summary)
    with pytest.raises(ConfigurationUnavailable) as error:
        get_active_profile(secret_store=secure_store)
    assert unique_credential not in str(error.value)

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("An invalid envelope must not reach the connection adapter")

    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token="",
        secret_store=secure_store,
        tester_factory=forbidden_factory,
    )
    saved = save_local_profile(secure_store, unique_credential)
    assert tested.success is False
    assert tested.state == ConnectionStatus.CONFIGURATION_ERROR
    assert saved.success is False
    assert saved.state == ConnectionStatus.CREDENTIAL_STORE_UNAVAILABLE

    removed = remove_ui_configuration(secret_store=secure_store)
    assert removed.success is True
    assert secure_store.get() is None


@pytest.mark.parametrize(
    ("environment_origin", "environment_credential"),
    [(SYNTHETIC_ORIGIN, ""), ("", "synthetic-value-never-returned")],
)
def test_incomplete_environment_profile_blocks_mixing_with_ui_configuration(
    secure_store,
    unique_credential,
    environment_origin,
    environment_credential,
):
    save_local_profile(secure_store, unique_credential)

    with override_settings(
        CONFLUENCE_BASE_URL=environment_origin,
        CONFLUENCE_PAT=environment_credential,
    ):
        summary = get_configuration_summary(secret_store=secure_store)
        saved = save_local_profile(secure_store, unique_credential)
        removed = remove_ui_configuration(secret_store=secure_store)

        assert summary.complete is False
        assert summary.state == ConnectionStatus.CONFIGURATION_ERROR
        assert summary.source == CredentialSource.ENVIRONMENT
        assert saved.success is False
        assert saved.state == ConnectionStatus.MANAGED_EXTERNALLY
        assert removed.success is False
        assert removed.state == ConnectionStatus.MANAGED_EXTERNALLY
        with pytest.raises(ConfigurationUnavailable, match="incomplete"):
            get_active_profile(secret_store=secure_store)

    assert_store_binding(secure_store, unique_credential)
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == SYNTHETIC_ORIGIN


def test_complete_environment_profile_takes_precedence_without_reading_local_store(
    secure_store,
    unique_credential,
    second_unique_credential,
):
    save_local_profile(secure_store, unique_credential)
    secure_store.available = False

    with override_settings(
        CONFLUENCE_BASE_URL=SECOND_SYNTHETIC_ORIGIN,
        CONFLUENCE_PAT=second_unique_credential,
    ):
        summary = get_configuration_summary(secret_store=secure_store)
        profile = get_active_profile(secret_store=secure_store)

    assert summary.complete is True
    assert summary.managed_externally is True
    assert summary.state == ConnectionStatus.MANAGED_EXTERNALLY
    assert SECOND_SYNTHETIC_ORIGIN not in repr(summary)
    assert second_unique_credential not in repr(summary)
    assert second_unique_credential not in repr(profile)
    assert profile.origin.base_url == SECOND_SYNTHETIC_ORIGIN
    assert profile.token == second_unique_credential
    assert profile.source == CredentialSource.ENVIRONMENT


@override_settings(CONFLUENCE_BASE_URL="", CONFLUENCE_PAT="")
def test_all_configuration_result_and_database_surfaces_exclude_the_credential(
    secure_store,
    unique_credential,
):
    tested = check_candidate_connection(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=unique_credential,
        secret_store=secure_store,
        tester_factory=RecordingTesterFactory(unique_credential, connected_result()),
    )
    saved = save_local_profile(
        secure_store,
        unique_credential,
        verification_receipt=tested.verification_receipt,
    )
    summary = get_configuration_summary(secret_store=secure_store)
    profile = get_active_profile(secret_store=secure_store)
    database_values = list(
        ConfluenceConfiguration.objects.values_list(
            "base_url",
            "auth_mode",
            "credential_source",
            "connection_status",
            "last_error_code",
            "last_error_message",
        )
    )

    assert unique_credential not in repr(tested)
    assert unique_credential not in repr(saved)
    assert unique_credential not in repr(summary)
    assert unique_credential not in repr(profile)
    assert unique_credential not in repr(database_values)
    assert not any(
        name in {"pat", "token", "secret", "secret_reference"}
        for name in {field.name for field in ConfluenceConfiguration._meta.get_fields()}
    )
