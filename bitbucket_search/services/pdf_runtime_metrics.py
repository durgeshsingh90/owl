"""Cross-process, bounded timing gauges for the sole PDF publisher.

Only numeric timestamps, durations, and success flags are retained.  Writer
processes atomically refresh one private JSON snapshot; the supervisor reads it
without adding high-frequency rows to SQLite.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from django.conf import settings

_LOCK = threading.Lock()
_MAX_EVENTS = 512
_EVENTS: dict[str, deque[tuple[float, float, bool]]] = {
    "lock_wait": deque(maxlen=_MAX_EVENTS),
    "transaction": deque(maxlen=_MAX_EVENTS),
}
_LAST_PERSISTED_MONOTONIC = 0.0
_PERSIST_INTERVAL_SECONDS = 1.0
_PERSISTED_MAX_AGE_SECONDS = 120.0


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * percentile / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _snapshot_path(*, process_id: int | None = None) -> Path:
    """Keep one bounded snapshot per process so writers cannot clobber peers."""

    resolved_process_id = os.getpid() if process_id is None else int(process_id)
    return (
        Path(settings.PDF_PIPELINE_STATE_ROOT).resolve()
        / f"publisher-metrics-v1-{resolved_process_id}.json"
    )


def _serialized_events() -> dict[str, list[list[float | bool]]]:
    with _LOCK:
        return {
            kind: [[at, duration, succeeded] for at, duration, succeeded in values]
            for kind, values in _EVENTS.items()
        }


def _persist_if_due(*, force: bool = False) -> None:
    global _LAST_PERSISTED_MONOTONIC
    now = time.monotonic()
    with _LOCK:
        if not force and now - _LAST_PERSISTED_MONOTONIC < _PERSIST_INTERVAL_SECONDS:
            return
        _LAST_PERSISTED_MONOTONIC = now
    target = _snapshot_path()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schemaVersion": 1,
            "processId": os.getpid(),
            "writtenAtUnixSeconds": time.time(),
            "events": _serialized_events(),
        }
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError):
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def record_sqlite_lock_wait(duration_ms: float, *, succeeded: bool) -> None:
    if not math.isfinite(duration_ms) or duration_ms < 0:
        return
    with _LOCK:
        _EVENTS["lock_wait"].append((time.time(), float(duration_ms), bool(succeeded)))
    _persist_if_due(force=not succeeded)


def record_publication_transaction(duration_ms: float, *, succeeded: bool) -> None:
    if not math.isfinite(duration_ms) or duration_ms < 0:
        return
    with _LOCK:
        _EVENTS["transaction"].append((time.time(), float(duration_ms), bool(succeeded)))
    _persist_if_due(force=not succeeded)


def flush_publisher_runtime_metrics() -> None:
    """Persist the final bounded ring before a short-lived writer exits."""

    _persist_if_due(force=True)


def _validated_events(value: object) -> dict[str, tuple[tuple[float, float, bool], ...]] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, tuple[tuple[float, float, bool], ...]] = {}
    for kind in _EVENTS:
        rows = value.get(kind)
        if not isinstance(rows, list) or len(rows) > _MAX_EVENTS:
            return None
        accepted: list[tuple[float, float, bool]] = []
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 3
                or isinstance(row[0], bool)
                or isinstance(row[1], bool)
                or not isinstance(row[0], (int, float))
                or not isinstance(row[1], (int, float))
                or not math.isfinite(float(row[0]))
                or not math.isfinite(float(row[1]))
                or float(row[1]) < 0
                or type(row[2]) is not bool
            ):
                return None
            accepted.append((float(row[0]), float(row[1]), row[2]))
        normalized[kind] = tuple(accepted)
    return normalized


def _persisted_snapshot(
    path: Path,
) -> dict[str, tuple[tuple[float, float, bool], ...]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload["writtenAtUnixSeconds"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("schemaVersion") != 1 or not 0 <= age <= _PERSISTED_MAX_AGE_SECONDS:
        return None
    return _validated_events(payload.get("events"))


def _persisted_events() -> dict[str, tuple[tuple[float, float, bool], ...]] | None:
    root = Path(settings.PDF_PIPELINE_STATE_ROOT).resolve()
    combined: dict[str, list[tuple[float, float, bool]]] = {kind: [] for kind in _EVENTS}
    found = False
    try:
        paths = tuple(root.glob("publisher-metrics-v1-*.json"))
    except OSError:
        return None
    for path in paths:
        events = _persisted_snapshot(path)
        if events is None:
            try:
                if time.time() - path.stat().st_mtime > _PERSISTED_MAX_AGE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        found = True
        for kind, rows in events.items():
            combined[kind].extend(rows)
    if not found:
        return None
    return {
        kind: tuple(sorted(rows, key=lambda row: row[0])[-_MAX_EVENTS:])
        for kind, rows in combined.items()
    }


def publisher_runtime_snapshot(*, window_seconds: int) -> dict[str, Any]:
    """Return rolling lock/transaction measures for classifier and dashboard use."""

    with _LOCK:
        local = {kind: tuple(values) for kind, values in _EVENTS.items()}
    persisted = _persisted_events()
    if persisted is None:
        events = local
    else:
        # The current process can have a newer in-memory tail than its latest
        # rate-limited file. Merge it with peer snapshots and remove only exact
        # copies already persisted by this process.
        events = {
            kind: tuple(
                sorted(
                    set((*persisted[kind], *local[kind])),
                    key=lambda row: row[0],
                )[-_MAX_EVENTS:]
            )
            for kind in _EVENTS
        }
    now = time.time()
    window = max(1, int(window_seconds))
    cutoff = now - window
    waits = tuple(row for row in events["lock_wait"] if cutoff <= row[0] <= now)
    transactions = tuple(row for row in events["transaction"] if cutoff <= row[0] <= now)
    wait_values = tuple(row[1] for row in waits)
    transaction_values = tuple(row[1] for row in transactions)
    busy_errors = sum(1 for row in waits if not row[2])
    successful_publications = sum(1 for row in transactions if row[2])
    failed_publications = len(transactions) - successful_publications
    return {
        "sqliteTransactionPct": round(min(100.0, sum(transaction_values) / (window * 10)), 3)
        if transactions
        else None,
        "sqliteTransactionP50Ms": (
            round(_percentile(transaction_values, 50), 3) if transactions else None
        ),
        "sqliteTransactionP95Ms": (
            round(_percentile(transaction_values, 95), 3) if transactions else None
        ),
        "sqliteLockWaitP50Ms": round(_percentile(wait_values, 50), 3) if waits else None,
        "sqliteLockWaitP95Ms": round(_percentile(wait_values, 95), 3) if waits else None,
        "sqliteBusyErrors": busy_errors if waits else None,
        "sqliteBusyErrorsPerSecond": round(busy_errors / window, 6) if waits else None,
        "successfulPublications": successful_publications,
        "failedPublications": failed_publications,
        "runtimeWindowSeconds": window,
        "runtimeSampleCounts": {
            "lockWait": len(waits),
            "transaction": len(transactions),
        },
        "availability": {
            **({} if waits else {"sqliteLockTiming": "no_lock_acquisitions_in_window"}),
            **({} if transactions else {"sqliteTransactionTiming": "no_publications_in_window"}),
        },
    }
