from __future__ import annotations

import json
from collections import deque

from django.http import HttpResponse
from django.test import RequestFactory

from bitbucket_search.services import foreground_metrics


def _clear_samples(monkeypatch, tmp_path, settings):
    settings.PDF_PIPELINE_STATE_ROOT = tmp_path
    monkeypatch.setattr(
        foreground_metrics,
        "_SAMPLES",
        {
            "dashboard": deque(maxlen=512),
            "exact_search": deque(maxlen=512),
            "representative": deque(maxlen=512),
        },
    )
    monkeypatch.setattr(foreground_metrics, "_LAST_PERSISTED_MONOTONIC", 0.0)


def test_latency_snapshot_reports_p50_p95_and_no_request_data(monkeypatch, tmp_path, settings):
    _clear_samples(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(foreground_metrics, "_PERSIST_INTERVAL_SECONDS", 0.0)

    for value in (10.0, 20.0, 30.0, 40.0, 50.0):
        foreground_metrics.record_foreground_latency("exact_search", value)

    snapshot = foreground_metrics.foreground_latency_snapshot()
    assert snapshot["exactSearchSampleCount"] == 5
    assert snapshot["exactSearchP50Ms"] == 30.0
    assert snapshot["exactSearchP95Ms"] == 48.0
    serialized = json.dumps(snapshot)
    assert "query" not in serialized
    assert "path" not in serialized


def test_middleware_classifies_dashboard_and_exact_search(monkeypatch, tmp_path, settings):
    _clear_samples(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(foreground_metrics, "_persist_snapshot_if_due", lambda: None)
    middleware = foreground_metrics.ForegroundLatencyMiddleware(lambda request: HttpResponse("ok"))
    factory = RequestFactory()

    middleware(factory.get("/"))
    middleware(factory.get("/pdfs/", {"q": "durable publication"}))

    snapshot = foreground_metrics.foreground_latency_snapshot()
    assert snapshot["dashboardSampleCount"] == 1
    assert snapshot["exactSearchSampleCount"] == 1
    assert snapshot["representativeRequestSampleCount"] == 2


def test_supervisor_process_can_read_recent_web_snapshot(monkeypatch, tmp_path, settings):
    _clear_samples(monkeypatch, tmp_path, settings)
    monkeypatch.setattr(foreground_metrics, "_PERSIST_INTERVAL_SECONDS", 0.0)
    foreground_metrics.record_foreground_latency("dashboard", 25.0)

    persisted = foreground_metrics._snapshot_path()
    assert persisted.stat().st_mode & 0o777 == 0o600

    _clear_samples(monkeypatch, tmp_path, settings)
    snapshot = foreground_metrics.foreground_latency_snapshot()
    assert snapshot["dashboardP50Ms"] == 25.0
    assert snapshot["dashboardSampleCount"] == 1
