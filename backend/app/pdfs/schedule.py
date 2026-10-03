"""Durable background Bitbucket sync: once a day, retry failures every hour until one succeeds."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.core.config import load_settings
from app.core.database import connection
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient


INTERVAL = timedelta(days=1)
RETRY = timedelta(hours=1)


def setup():
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS bitbucket_sync_schedule (
            server TEXT PRIMARY KEY, next_attempt TEXT NOT NULL,
            job_id TEXT, last_success TEXT, error TEXT NOT NULL DEFAULT ''
        )""")
        # Bring a schedule saved under longer intervals forward to the current ones.
        for row in db.execute(
            "SELECT * FROM bitbucket_sync_schedule WHERE job_id IS NULL"
        ).fetchall():
            due = datetime.fromisoformat(row["next_attempt"])
            if row["error"] or not row["last_success"]:
                limit = datetime.now(timezone.utc) + RETRY
            else:
                limit = datetime.fromisoformat(row["last_success"]) + INTERVAL
            if due > limit:
                db.execute(
                    "UPDATE bitbucket_sync_schedule SET next_attempt=? WHERE server=?",
                    (limit.isoformat(), row["server"]),
                )


async def run_due(jobs, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        settings = load_settings()
    except ValueError:
        return
    server = settings.base_url
    with connection() as db:
        projects = [
            row[0]
            for row in db.execute(
                "SELECT id FROM tracked_projects WHERE server=?", (server,)
            )
        ]
        if not projects:
            return
        row = db.execute(
            "SELECT * FROM bitbucket_sync_schedule WHERE server=?", (server,)
        ).fetchone()
        if row is None:
            previous = db.execute(
                "SELECT last_completed_at FROM sync_metadata WHERE id=1"
            ).fetchone()
            baseline = (
                datetime.fromisoformat(previous[0]) if previous and previous[0] else now
            )
            db.execute(
                "INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES(?,?)",
                (server, (baseline + INTERVAL).isoformat()),
            )
            row = db.execute(
                "SELECT * FROM bitbucket_sync_schedule WHERE server=?", (server,)
            ).fetchone()
        if row["job_id"]:
            job = db.execute(
                "SELECT status,progress FROM jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
            if job and job["status"] in {"queued", "running", "paused"}:
                return
            progress = json.loads(job["progress"]) if job else {}
            success = (
                job
                and job["status"] == "succeeded"
                and not progress.get("failed")
                and not progress.get("repositories_failed")
            )
            completed = (
                datetime.fromisoformat(progress["completed_at"])
                if progress.get("completed_at")
                else now
            )
            next_attempt = (
                completed + (INTERVAL if success else RETRY)
            )
            db.execute(
                "UPDATE bitbucket_sync_schedule SET job_id=NULL,next_attempt=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,error=? WHERE server=?",
                (
                    next_attempt.isoformat(),
                    bool(success),
                    completed.isoformat(),
                    ""
                    if success
                    else "Sync did not complete successfully. Retrying in one hour.",
                    server,
                ),
            )
            return
        if datetime.fromisoformat(row["next_attempt"]) > now or jobs.active():
            return
    client = BitbucketClient(settings)
    try:
        await client.test()
        # A manual pull may have started while the connection check was in flight.
        if jobs.active():
            return
        job = jobs.start(projects)
        job["background"] = True
        jobs.save()
        with connection() as db:
            db.execute(
                "UPDATE bitbucket_sync_schedule SET job_id=?,error='' WHERE server=?",
                (job["id"], server),
            )
    except Exception as error:  # noqa: BLE001 - keep retrying without interrupting the UI
        event("bitbucket.schedule.failed", **error_details(error))
        with connection() as db:
            db.execute(
                "UPDATE bitbucket_sync_schedule SET next_attempt=?,error=? WHERE server=?",
                (
                    (now + RETRY).isoformat(),
                    "Connection or sync failed. Retrying in one hour.",
                    server,
                ),
            )
    finally:
        await client.close()


def status(jobs):
    """Automatic refresh state for this library, in the shape every OWL app shares."""
    state = {
        "status": "not_configured",
        "next_run": None,
        "last_success": None,
        "last_attempt": None,
        "message": "",
        "completed": 0,
        "total": 0,
        "failed": 0,
        "eta_seconds": None,
        "interval_hours": INTERVAL.total_seconds() / 3600,
        "retry_hours": RETRY.total_seconds() / 3600,
    }
    try:
        server = load_settings().base_url
    except ValueError:
        state["message"] = "Bitbucket is not connected. Add a connection in Settings."
        return state
    with connection() as db:
        if not db.execute(
            "SELECT 1 FROM tracked_projects WHERE server=? LIMIT 1", (server,)
        ).fetchone():
            state["status"] = "idle"
            state["message"] = "Add a repository to start automatic refreshes."
            return state
        row = db.execute(
            "SELECT * FROM bitbucket_sync_schedule WHERE server=?", (server,)
        ).fetchone()
        job = (
            db.execute(
                "SELECT status,progress FROM jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
            if row and row["job_id"]
            else None
        )

    def epoch(value):
        return datetime.fromisoformat(value).timestamp() if value else None

    state["status"] = "scheduled"
    if row:
        state["next_run"] = epoch(row["next_attempt"])
        state["last_success"] = epoch(row["last_success"])
        state["message"] = row["error"]
        if row["error"]:
            state["status"] = "retrying"
    current = jobs.current if jobs.active() else None
    if job and job["status"] in {"queued", "running", "paused"} or current:
        progress = current or json.loads(job["progress"])
        state["status"] = "running"
        state["message"] = ""
        state["last_attempt"] = epoch(progress.get("started_at"))
        state["completed"] = progress.get("processed") or 0
        state["total"] = progress.get("found") or 0
        state["failed"] = progress.get("failed") or 0
        state["eta_seconds"] = progress.get("eta_seconds")
    return state


async def run_scheduler(jobs):
    setup()
    while True:
        try:
            await run_due(jobs)
        except Exception as error:  # noqa: BLE001 - keep scheduler alive
            event("bitbucket.schedule.error", **error_details(error))
        await asyncio.sleep(60)
