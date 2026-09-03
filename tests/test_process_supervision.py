from __future__ import annotations

from core import process_supervision


def test_standalone_worker_without_owner_remains_available(monkeypatch):
    monkeypatch.delenv(process_supervision.RESIDENT_SUPERVISOR_PID_ENV, raising=False)
    monkeypatch.setattr(process_supervision.os, "getppid", lambda: 999)

    assert process_supervision.resident_supervisor_is_alive()


def test_resident_worker_requires_its_direct_supervisor(monkeypatch):
    monkeypatch.setenv(process_supervision.RESIDENT_SUPERVISOR_PID_ENV, "123")
    monkeypatch.setattr(process_supervision.os, "getppid", lambda: 123)
    assert process_supervision.resident_supervisor_is_alive()

    monkeypatch.setattr(process_supervision.os, "getppid", lambda: 1)
    assert not process_supervision.resident_supervisor_is_alive()


def test_invalid_resident_worker_owner_fails_closed(monkeypatch):
    monkeypatch.setenv(process_supervision.RESIDENT_SUPERVISOR_PID_ENV, "not-a-pid")

    assert not process_supervision.resident_supervisor_is_alive()
