from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import DatabaseError, IntegrityError, OperationalError

from bookmark_manager.models import (
    BookmarkImportFailure,
    BookmarkImportRun,
    BookmarkImportStatus,
    ConfluenceConfiguration,
    ConfluencePageNode,
    ConnectionStatus,
)
from bookmark_manager.services import configuration, import_export, secret_store
from bookmark_manager.services.bookmark_application import BookmarkActionError
from bookmark_manager.services.confluence_adapter import ConfluenceResult, ConfluenceResultCode
from bookmark_manager.services.logging_events import log_event

pytestmark = pytest.mark.django_db
PRIVATE_ORIGIN = "https://private.example.invalid/wiki"
PRIVATE_TEXT = "private-bookmark-content-never-log"
PRIVATE_FILENAME = "private-import-file.json"
SYNTHETIC_CREDENTIAL = "not-a-real-secret"


@pytest.fixture
def events(caplog, monkeypatch):
    boundary = logging.getLogger("owl.bookmarks")
    caplog.set_level(logging.DEBUG, logger=boundary.name)
    monkeypatch.setattr(boundary, "propagate", False)
    boundary.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        boundary.removeHandler(caplog.handler)


def _events(events, name):
    return [
        record
        for record in events.records
        if record.getMessage().split(" ", 1)[0] == f"event={name}"
    ]


def _event(events, name):
    matches = _events(events, name)
    assert matches, f"No event {name} in {events.text}"
    return matches[-1]


def _assert_private(events):
    for value in (PRIVATE_ORIGIN, PRIVATE_TEXT, PRIVATE_FILENAME, SYNTHETIC_CREDENTIAL):
        assert value not in events.text
    assert "Traceback" not in events.text
    assert all(record.exc_info is None for record in events.records)


def _record(page_id="200"):
    return {
        "pageId": page_id,
        "pageTitle": PRIVATE_TEXT,
        "pageUrl": f"{PRIVATE_ORIGIN}/pages/{page_id}/{PRIVATE_TEXT}",
    }


def _save_profile(store, value=SYNTHETIC_CREDENTIAL):
    return configuration.save_ui_configuration(
        base_url=PRIVATE_ORIGIN,
        personal_access_token=value,
        secret_store=store,
    )


def test_import_and_export_lifecycle_counts_do_not_include_content(events):
    result = import_export.import_bookmarks_document(
        [_record()], filename=PRIVATE_FILENAME, batch_size=1
    )
    assert result.imported_records == 1
    created = _event(events, "bookmark_import_run_created")
    assert created.levelno == logging.INFO
    assert f"run_id={result.run.pk}" in created.getMessage()
    progress = _event(events, "bookmark_import_progress")
    assert progress.levelno == logging.DEBUG
    assert "processed_count=1" in progress.getMessage()
    completed = _event(events, "bookmark_transfer_completed")
    assert completed.levelno == logging.INFO
    assert "imported_count=1" in completed.getMessage()
    assert "elapsed_ms=" in completed.getMessage()

    serialized = import_export.export_bookmarks_json()
    assert json.loads(serialized)["record_count"] == 1
    completed = _event(events, "bookmark_transfer_completed")
    assert "operation=export_document" in completed.getMessage()
    assert "count=1" in completed.getMessage()
    assert _event(events, "bookmark_export_serialized").levelno == logging.DEBUG
    _assert_private(events)


def test_text_import_progress_is_bounded_and_nested_saves_keep_run_identity(events):
    def saver(_url):
        log_event(import_export.logger, logging.DEBUG, "synthetic_save_stage")
        return SimpleNamespace(created=True)

    result = import_export.import_bookmarks_text(
        "\n".join(f"https://example.invalid/{index}" for index in range(101)),
        bookmark_saver=saver,
    )
    assert result.imported_records == 101
    progress = _events(events, "bookmark_import_progress")
    assert 1 <= len(progress) <= 20
    assert "processed_count=101" in progress[-1].getMessage()
    nested = _events(events, "synthetic_save_stage")
    assert len(nested) == 101
    assert all(f"run_id={result.run.pk}" in record.getMessage() for record in nested)
    assert "record_number=101" in nested[-1].getMessage()


@pytest.mark.parametrize("kind", ["json", "text"])
def test_invalid_import_document_is_warning_without_persistence(events, kind):
    importer = (
        import_export.import_bookmarks_document
        if kind == "json"
        else import_export.import_bookmarks_text
    )
    with pytest.raises(import_export.ImportDocumentError):
        importer(PRIVATE_TEXT, filename=PRIVATE_FILENAME)
    assert _event(events, "bookmark_transfer_rejected").levelno == logging.WARNING
    assert not BookmarkImportRun.objects.exists()
    assert not any(record.levelno >= logging.ERROR for record in events.records)
    _assert_private(events)


def test_export_invalid_time_is_warning_not_an_operational_error(events):
    with pytest.raises(ValueError):
        import_export.export_bookmarks_document(generated_at=datetime(2026, 1, 1))
    assert _event(events, "bookmark_export_invalid_time").levelno == logging.WARNING
    assert not any(record.levelno >= logging.ERROR for record in events.records)


@pytest.mark.parametrize(
    "failure",
    [
        BookmarkActionError(PRIVATE_TEXT, PRIVATE_TEXT),
        IntegrityError(PRIVATE_TEXT),
        OperationalError(PRIVATE_TEXT),
        RuntimeError(PRIVATE_TEXT),
    ],
)
def test_failed_json_records_are_error_and_continue_with_static_codes(events, failure):
    def saver(_value):
        raise failure

    result = import_export.import_bookmarks_document([_record()], bookmark_saver=saver)
    assert result.failed_records == 1
    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    record = _event(events, "bookmark_import_record_failed")
    assert record.levelno == logging.ERROR
    assert f"run_id={result.run.pk}" in record.getMessage()
    assert "record_number=1" in record.getMessage()
    assert "error_type=" in record.getMessage()
    _assert_private(events)


def test_record_error_is_emitted_before_failure_report_database_write(events, monkeypatch):
    def fail_write(*_args, **_kwargs):
        assert _event(events, "bookmark_import_record_failed").levelno == logging.ERROR
        raise OperationalError(PRIVATE_TEXT)

    monkeypatch.setattr(BookmarkImportFailure.objects, "update_or_create", fail_write)
    with pytest.raises(OperationalError):
        import_export.import_bookmarks_document([False])
    persisted = _event(events, "bookmark_import_failure_persistence_failed")
    assert persisted.levelno == logging.ERROR
    assert "record_number=1" in persisted.getMessage()
    assert _event(events, "bookmark_transfer_failed").levelno == logging.ERROR
    _assert_private(events)


def test_short_database_lock_is_retried_before_import_failure_is_persisted(monkeypatch):
    attempts = 0
    monkeypatch.setattr(import_export, "DATABASE_LOCK_RETRY_DELAYS", (0, 0))

    def temporarily_locked():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("database is locked")
        return "saved"

    assert import_export._retry_database_lock(temporarily_locked) == "saved"
    assert attempts == 3


def test_unexpected_normalization_failure_is_logged_even_if_handled(events, monkeypatch):
    monkeypatch.setattr(
        import_export, "_normalize_record", Mock(side_effect=RuntimeError(PRIVATE_TEXT))
    )
    result = import_export.import_bookmarks_document([False])
    assert result.failed_records == 1
    assert _event(events, "bookmark_import_normalization_failed").levelno == logging.ERROR
    _assert_private(events)


def test_import_progress_persistence_failure_is_error_with_run_id(events, monkeypatch):
    original_save = BookmarkImportRun.save

    def fail_progress(self, *args, **kwargs):
        if "processed_records" in kwargs.get("update_fields", ()):
            raise DatabaseError(PRIVATE_TEXT)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(BookmarkImportRun, "save", fail_progress)
    with pytest.raises(DatabaseError):
        import_export.import_bookmarks_text(
            "https://example.invalid/one", bookmark_saver=lambda _url: SimpleNamespace(created=True)
        )
    failed = _event(events, "bookmark_import_progress_persistence_failed")
    assert failed.levelno == logging.ERROR
    assert "run_id=1" in failed.getMessage()
    _assert_private(events)


def test_export_database_failure_is_error_without_query_or_content(events, monkeypatch):
    monkeypatch.setattr(
        ConfluencePageNode.objects, "all", Mock(side_effect=OperationalError(PRIVATE_TEXT))
    )
    with pytest.raises(OperationalError):
        import_export.export_bookmarks_document()
    record = _event(events, "bookmark_transfer_failed")
    assert record.levelno == logging.ERROR
    assert "operation=export_document" in record.getMessage()
    _assert_private(events)


def test_unconfigured_and_missing_credential_reads_remain_quiet(events):
    store = secret_store.InMemorySecretStore()
    assert not configuration.get_configuration_summary(secret_store=store).complete
    with pytest.raises(configuration.ConfigurationUnavailable):
        configuration.get_active_profile(secret_store=store)
    assert not events.records

    ConfluenceConfiguration.objects.create(base_url=PRIVATE_ORIGIN)
    assert not configuration.get_configuration_summary(secret_store=store).complete
    with pytest.raises(configuration.CredentialEnvelopeError):
        configuration.get_active_profile(secret_store=store)
    assert secret_store.DatabaseSecretStore().get() is None
    assert not events.records


def test_configuration_database_read_failure_is_error_before_safe_fallback(events, monkeypatch):
    monkeypatch.setattr(
        ConfluenceConfiguration.objects, "filter", Mock(side_effect=OperationalError(PRIVATE_TEXT))
    )
    assert not configuration.get_configuration_summary().complete
    with pytest.raises(configuration.ConfigurationUnavailable):
        configuration.get_active_profile()
    failed = _events(events, "configuration_database_read_failed")
    assert len(failed) == 2
    assert all(record.levelno == logging.ERROR for record in failed)
    _assert_private(events)


@pytest.mark.parametrize("code", list(ConfluenceResultCode))
def test_connection_outcomes_are_classified_without_logging_provider_message(events, code):
    result = configuration.test_candidate_connection(
        base_url=PRIVATE_ORIGIN,
        personal_access_token=SYNTHETIC_CREDENTIAL,
        tester_factory=lambda *_args: SimpleNamespace(
            test_connection=lambda: ConfluenceResult(code, PRIVATE_TEXT)
        ),
    )
    if code in (ConfluenceResultCode.CONNECTED, ConfluenceResultCode.SUCCESS):
        assert result.success
        assert _event(events, "configuration_action_completed").levelno == logging.INFO
        assert _event(events, "configuration_connection_test_completed").levelno == logging.DEBUG
    else:
        assert not result.success
        assert _event(events, "configuration_connection_test_failed").levelno == logging.ERROR
        assert _event(events, "configuration_action_failed").levelno == logging.ERROR
    _assert_private(events)


def test_connection_exception_is_error_while_original_safe_result_is_preserved(events):
    result = configuration.test_candidate_connection(
        base_url=PRIVATE_ORIGIN,
        personal_access_token=SYNTHETIC_CREDENTIAL,
        tester_factory=Mock(side_effect=RuntimeError(PRIVATE_TEXT)),
    )
    assert not result.success
    assert result.state == ConnectionStatus.UNREACHABLE
    failed = _event(events, "configuration_connection_test_failed")
    assert failed.levelno == logging.ERROR
    assert "error_type=RuntimeError" in failed.getMessage()
    _assert_private(events)


def test_configuration_rejected_input_is_warning(events):
    result = configuration.save_ui_configuration(
        base_url=PRIVATE_ORIGIN,
        personal_access_token="",
        secret_store=secret_store.InMemorySecretStore(),
    )
    assert not result.success
    assert _event(events, "configuration_action_failed").levelno == logging.WARNING
    assert not any(record.levelno >= logging.ERROR for record in events.records)


@pytest.mark.parametrize("compensation_fails", [False, True])
def test_configuration_save_logs_database_and_compensation_failures(
    events, monkeypatch, compensation_fails
):
    store = secret_store.InMemorySecretStore()
    assert _save_profile(store).success
    previous = store.get()
    events.clear()
    monkeypatch.setattr(
        ConfluenceConfiguration.objects,
        "update_or_create",
        Mock(side_effect=DatabaseError(PRIVATE_TEXT)),
    )
    original_set = store.set

    def restore(value):
        if compensation_fails and value == previous:
            raise secret_store.SecretStoreOperationError(PRIVATE_TEXT)
        original_set(value)

    monkeypatch.setattr(store, "set", restore)
    assert not _save_profile(store, "synthetic-replacement-never-valid").success
    assert _event(events, "configuration_save_failed").levelno == logging.ERROR
    assert _event(events, "configuration_credential_restore_started").levelno == logging.WARNING
    if compensation_fails:
        assert _event(events, "configuration_credential_restore_failed").levelno == logging.ERROR
    else:
        assert _event(events, "configuration_credential_restored").levelno == logging.INFO
        assert store.get() == previous
    assert ConfluenceConfiguration.objects.get(pk=1).base_url == PRIVATE_ORIGIN
    _assert_private(events)


def test_configuration_remove_compensation_failure_is_not_silent(events, monkeypatch):
    store = secret_store.InMemorySecretStore()
    assert _save_profile(store).success
    monkeypatch.setattr(
        ConfluenceConfiguration, "save", Mock(side_effect=DatabaseError(PRIVATE_TEXT))
    )
    monkeypatch.setattr(
        store, "set", Mock(side_effect=secret_store.SecretStoreOperationError(PRIVATE_TEXT))
    )
    result = configuration.remove_ui_configuration(secret_store=store)
    assert not result.success
    assert _event(events, "configuration_remove_failed").levelno == logging.ERROR
    assert _event(events, "configuration_credential_restore_failed").levelno == logging.ERROR
    _assert_private(events)


def test_invalid_stored_envelope_is_error_without_credential_material(events):
    store = secret_store.InMemorySecretStore()
    ConfluenceConfiguration.objects.create(base_url=PRIVATE_ORIGIN)
    store.set(SYNTHETIC_CREDENTIAL)
    assert not configuration.get_configuration_summary(secret_store=store).complete
    with pytest.raises(configuration.CredentialEnvelopeError):
        configuration.get_active_profile(secret_store=store)
    assert _event(events, "configuration_credential_read_failed").levelno == logging.ERROR
    assert _event(events, "configuration_credential_invalid").levelno == logging.ERROR
    _assert_private(events)


@pytest.mark.parametrize("operation", ["get", "set", "delete"])
def test_keyring_unexpected_backend_exception_is_logged_and_unchanged(
    events, monkeypatch, operation
):
    class BackendError(Exception):
        pass

    store = secret_store.KeyringSecretStore()
    backend = SimpleNamespace(
        get_password=Mock(side_effect=RuntimeError(PRIVATE_TEXT)),
        set_password=Mock(side_effect=RuntimeError(PRIVATE_TEXT)),
        delete_password=Mock(side_effect=RuntimeError(PRIVATE_TEXT)),
    )
    monkeypatch.setattr(store, "_keyring", lambda: (backend, BackendError))
    with pytest.raises(RuntimeError):
        getattr(store, operation)(SYNTHETIC_CREDENTIAL) if operation == "set" else getattr(
            store, operation
        )()
    failed = _event(events, "credential_store_operation_failed")
    assert failed.levelno == logging.ERROR
    assert f"operation={operation}" in failed.getMessage()
    _assert_private(events)


def test_keyring_availability_exception_and_auto_fallback_have_distinct_levels(
    events, monkeypatch, settings
):
    settings.CONFLUENCE_SECRET_BACKEND = "auto"
    monkeypatch.setattr(
        secret_store.KeyringSecretStore, "_keyring", Mock(side_effect=RuntimeError(PRIVATE_TEXT))
    )
    assert isinstance(secret_store.get_secret_store(), secret_store.DatabaseSecretStore)
    assert _event(events, "credential_store_availability_failed").levelno == logging.ERROR
    assert _event(events, "credential_store_fallback_selected").levelno == logging.WARNING
    _assert_private(events)


def test_keyring_absent_read_and_idempotent_delete_are_not_errors(events, monkeypatch):
    class BackendError(Exception):
        pass

    store = secret_store.KeyringSecretStore()
    backend = SimpleNamespace(
        get_password=lambda *_args: None,
        delete_password=Mock(side_effect=BackendError(PRIVATE_TEXT)),
    )
    monkeypatch.setattr(store, "_keyring", lambda: (backend, BackendError))
    assert store.get() is None
    assert not events.records
    store.delete()
    assert not any(record.levelno >= logging.WARNING for record in events.records)
    _assert_private(events)


def test_database_store_decryption_failure_is_error_and_missing_value_is_quiet(events):
    store = secret_store.DatabaseSecretStore()
    assert store.get() is None
    assert not events.records
    ConfluenceConfiguration.objects.create(credential_ciphertext=PRIVATE_TEXT)
    with pytest.raises(secret_store.SecretStoreOperationError):
        store.get()
    failed = _event(events, "credential_store_operation_failed")
    assert failed.levelno == logging.ERROR
    assert "stage=database" in failed.getMessage()
    _assert_private(events)


def test_database_store_availability_failure_is_error(events, monkeypatch):
    store = secret_store.DatabaseSecretStore()
    monkeypatch.setattr(
        ConfluenceConfiguration.objects,
        "values_list",
        Mock(side_effect=OperationalError(PRIVATE_TEXT)),
    )
    assert not store.is_available()
    assert _event(events, "credential_store_availability_failed").levelno == logging.ERROR
    _assert_private(events)


def test_database_credential_store_success_logs_no_value(events):
    store = secret_store.DatabaseSecretStore()
    store.set(SYNTHETIC_CREDENTIAL)
    assert store.get() == SYNTHETIC_CREDENTIAL
    store.delete()
    records = _events(events, "credential_store_operation_completed")
    assert [record.levelno for record in records] == [logging.INFO, logging.DEBUG, logging.INFO]
    _assert_private(events)
