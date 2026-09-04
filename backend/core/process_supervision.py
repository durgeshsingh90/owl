"""Small process-ownership checks shared by OWL's resident workers."""

from __future__ import annotations

import os

RESIDENT_SUPERVISOR_PID_ENV = "OWL_RESIDENT_SUPERVISOR_PID"


def resident_supervisor_is_alive() -> bool:
    """Return false when a supervised worker has outlived its direct parent.

    Standalone management commands intentionally have no ownership variable and
    remain unaffected. ``run_owl`` launches each resident controller directly,
    so a changed parent PID means that controller was orphaned and must stop
    before it can claim another durable job.
    """

    value = os.environ.get(RESIDENT_SUPERVISOR_PID_ENV)
    if value is None:
        return True
    try:
        expected_pid = int(value)
    except (TypeError, ValueError):
        return False
    return expected_pid > 0 and os.getppid() == expected_pid
