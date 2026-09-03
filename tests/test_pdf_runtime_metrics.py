from __future__ import annotations

from collections import deque

from bitbucket_search.services import pdf_runtime_metrics


def _reset(monkeypatch, tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path
    monkeypatch.setattr(
        pdf_runtime_metrics,
        "_EVENTS",
        {
            "lock_wait": deque(maxlen=512),
            "transaction": deque(maxlen=512),
        },
    )
    monkeypatch.setattr(pdf_runtime_metrics, "_LAST_PERSISTED_MONOTONIC", 0.0)


def test_publisher_runtime_metrics_partition_lock_and_transaction_evidence(
    monkeypatch, tmp_path, settings
):
    _reset(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(pdf_runtime_metrics, "_persist_if_due", lambda **kwargs: None)
    pdf_runtime_metrics.record_sqlite_lock_wait(10, succeeded=True)
    pdf_runtime_metrics.record_sqlite_lock_wait(30, succeeded=False)
    pdf_runtime_metrics.record_publication_transaction(100, succeeded=True)
    pdf_runtime_metrics.record_publication_transaction(300, succeeded=False)

    snapshot = pdf_runtime_metrics.publisher_runtime_snapshot(window_seconds=60)

    assert snapshot["sqliteLockWaitP50Ms"] == 20
    assert snapshot["sqliteLockWaitP95Ms"] == 29
    assert snapshot["sqliteBusyErrors"] == 1
    assert snapshot["sqliteTransactionP50Ms"] == 200
    assert snapshot["successfulPublications"] == 1
    assert snapshot["failedPublications"] == 1


def test_publisher_runtime_metrics_are_unavailable_without_observations(
    monkeypatch, tmp_path, settings
):
    _reset(monkeypatch, tmp_path, settings)

    snapshot = pdf_runtime_metrics.publisher_runtime_snapshot(window_seconds=60)

    assert snapshot["sqliteLockWaitP95Ms"] is None
    assert snapshot["sqliteBusyErrors"] is None
    assert snapshot["sqliteTransactionPct"] is None
    assert snapshot["availability"] == {
        "sqliteLockTiming": "no_lock_acquisitions_in_window",
        "sqliteTransactionTiming": "no_publications_in_window",
    }


def test_supervisor_reads_recent_private_writer_snapshot(monkeypatch, tmp_path, settings):
    _reset(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(pdf_runtime_metrics, "_PERSIST_INTERVAL_SECONDS", 0.0)
    pdf_runtime_metrics.record_sqlite_lock_wait(12, succeeded=True)
    assert pdf_runtime_metrics._snapshot_path().stat().st_mode & 0o777 == 0o600

    _reset(monkeypatch, tmp_path, settings)
    snapshot = pdf_runtime_metrics.publisher_runtime_snapshot(window_seconds=60)

    assert snapshot["sqliteLockWaitP50Ms"] == 12
    assert snapshot["runtimeSampleCounts"]["lockWait"] == 1


def test_supervisor_aggregates_process_snapshots_without_clobbering(
    monkeypatch, tmp_path, settings
):
    _reset(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(pdf_runtime_metrics, "_PERSIST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(pdf_runtime_metrics.os, "getpid", lambda: 101)
    pdf_runtime_metrics.record_sqlite_lock_wait(12, succeeded=True)

    _reset(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(pdf_runtime_metrics.os, "getpid", lambda: 202)
    pdf_runtime_metrics.record_publication_transaction(34, succeeded=True)

    _reset(monkeypatch, tmp_path, settings)
    snapshot = pdf_runtime_metrics.publisher_runtime_snapshot(window_seconds=60)

    assert snapshot["runtimeSampleCounts"] == {"lockWait": 1, "transaction": 1}
    assert snapshot["sqliteLockWaitP50Ms"] == 12
    assert snapshot["sqliteTransactionP50Ms"] == 34


def test_flush_forces_final_writer_snapshot(monkeypatch, tmp_path, settings):
    _reset(monkeypatch, tmp_path, settings)
    calls = []
    monkeypatch.setattr(
        pdf_runtime_metrics,
        "_persist_if_due",
        lambda *, force=False: calls.append(force),
    )

    pdf_runtime_metrics.flush_publisher_runtime_metrics()

    assert calls == [True]
