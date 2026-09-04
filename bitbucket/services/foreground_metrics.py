"""Bounded process-local request latency telemetry for PDF pipeline guardrails.

The web process owns these observations.  They are merged into the supervisor's
redacted pipeline snapshot at response time, so the metrics collector never
needs to write browser/search timings into SQLite.
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
from django.http import HttpRequest, HttpResponse

_MAX_SAMPLES = 512
_LOCK = threading.Lock()
_SAMPLES: dict[str, deque[float]] = {
    "dashboard": deque(maxlen=_MAX_SAMPLES),
    "exact_search": deque(maxlen=_MAX_SAMPLES),
    "representative": deque(maxlen=_MAX_SAMPLES),
}
_LAST_PERSISTED_MONOTONIC = 0.0
_PERSIST_INTERVAL_SECONDS = 2.0
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


def record_foreground_latency(kind: str, duration_ms: float) -> None:
    """Record one bounded non-negative request duration without request data."""

    if kind not in _SAMPLES or not math.isfinite(duration_ms) or duration_ms < 0:
        return
    with _LOCK:
        _SAMPLES[kind].append(float(duration_ms))
    _persist_snapshot_if_due()


def _summary(values: tuple[float, ...]) -> dict[str, int | float | None]:
    return {
        "sampleCount": len(values),
        "p50Ms": round(_percentile(values, 50), 3) if values else None,
        "p95Ms": round(_percentile(values, 95), 3) if values else None,
    }


def _local_snapshot() -> dict[str, Any]:
    with _LOCK:
        samples = {kind: tuple(values) for kind, values in _SAMPLES.items()}
    dashboard = _summary(samples["dashboard"])
    exact_search = _summary(samples["exact_search"])
    representative = _summary(samples["representative"])
    return {
        "exactSearchAvailable": True,
        "exactSearchP50Ms": exact_search["p50Ms"],
        "exactSearchP95Ms": exact_search["p95Ms"],
        "exactSearchSampleCount": exact_search["sampleCount"],
        "dashboardP50Ms": dashboard["p50Ms"],
        "dashboardP95Ms": dashboard["p95Ms"],
        "dashboardSampleCount": dashboard["sampleCount"],
        "representativeRequestP50Ms": representative["p50Ms"],
        "representativeRequestP95Ms": representative["p95Ms"],
        "representativeRequestSampleCount": representative["sampleCount"],
        "availability": {
            **(
                {}
                if exact_search["sampleCount"]
                else {"exactSearchLatency": "no_requests_observed_in_web_process"}
            ),
            **(
                {}
                if dashboard["sampleCount"]
                else {"dashboardLatency": "no_requests_observed_in_web_process"}
            ),
            **(
                {}
                if representative["sampleCount"]
                else {"representativeRequestLatency": "no_requests_observed_in_web_process"}
            ),
        },
    }


def _snapshot_path() -> Path:
    return Path(settings.BITBUCKET_APP_PIPELINE_STATE_ROOT).resolve() / "foreground-metrics-v1.json"


def _persist_snapshot_if_due() -> None:
    global _LAST_PERSISTED_MONOTONIC
    now = time.monotonic()
    with _LOCK:
        if now - _LAST_PERSISTED_MONOTONIC < _PERSIST_INTERVAL_SECONDS:
            return
        _LAST_PERSISTED_MONOTONIC = now
    target = _snapshot_path()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schemaVersion": 1,
            "writtenAtUnixSeconds": time.time(),
            "foreground": _local_snapshot(),
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


def _persisted_snapshot() -> dict[str, Any] | None:
    try:
        payload = json.loads(_snapshot_path().read_text(encoding="utf-8"))
        age = time.time() - float(payload["writtenAtUnixSeconds"])
        foreground = payload["foreground"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("schemaVersion") != 1 or not 0 <= age <= _PERSISTED_MAX_AGE_SECONDS:
        return None
    if not isinstance(foreground, dict):
        return None
    return foreground


def foreground_latency_snapshot() -> dict[str, Any]:
    """Return local observations, or the latest bounded web-process snapshot."""

    local = _local_snapshot()
    if any(
        int(local.get(field) or 0)
        for field in (
            "exactSearchSampleCount",
            "dashboardSampleCount",
            "representativeRequestSampleCount",
        )
    ):
        return local
    return _persisted_snapshot() or local


def _is_exact_search(request: HttpRequest) -> bool:
    if request.method != "GET" or request.path.rstrip("/") != "/bitbucket":
        return False
    query = request.GET
    return bool(
        query.get("q")
        or query.getlist("chip")
        or query.getlist("repository")
        or query.getlist("index_state")
        or query.getlist("committer")
        or query.getlist("people_group")
    )


def _is_dashboard(request: HttpRequest) -> bool:
    return request.method == "GET" and request.path in {
        "/",
        "/bitbucket/logs/",
        "/bitbucket/pipeline/metrics/",
    }


class ForegroundLatencyMiddleware:
    """Measure selected GET latency with a monotonic clock and no identifiers."""

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        response = self.get_response(request)
        elapsed_ms = (time.monotonic() - started) * 1_000
        if request.method == "GET":
            record_foreground_latency("representative", elapsed_ms)
        if _is_dashboard(request):
            record_foreground_latency("dashboard", elapsed_ms)
        if _is_exact_search(request):
            record_foreground_latency("exact_search", elapsed_ms)
        return response
