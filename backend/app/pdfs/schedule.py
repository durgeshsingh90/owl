"""Durable background Bitbucket sync: once a day, retry failures every two hours until one succeeds."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.core.config import load_settings
from app.core.database import connection
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient


INTERVAL = timedelta(days=1)
RETRY = timedelta(hours=2)


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


def completed(job_status, progress):
    """A pull that finished and read every repository's file list counts as done.

    Individual files that failed (an unreadable PDF or a non-UTF-8 YAML file) do not
    make it a failure: they are kept in failed_documents and retried on every pull, so
    retrying the whole refresh every two hours would only repeat them.
    """
    return job_status in {"succeeded", "succeeded_with_errors"} and not progress.get(
        "discovery_failed"
    )


def manual_success(db, last_success):
    """When a manual pull of every tracked repository last finished.

    Returns its completion time if it is newer than the last recorded success.
    """
    row = db.execute(
        "SELECT status,progress FROM jobs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    progress = json.loads(row["progress"])
    if progress.get("background") or not completed(row["status"], progress):
        return None
    if not progress.get("completed_at"):
        return None
    covered = {str(key) for key in progress.get("repository_statuses", {})}
    tracked = {
        str(item[0])
        for item in db.execute(
            "SELECT r.id FROM repositories r JOIN tracked_projects p ON p.id=r.project_id"
        )
    }
    if not tracked or not tracked <= covered:
        return None
    finished = datetime.fromisoformat(progress["completed_at"])
    if last_success and finished <= datetime.fromisoformat(last_success):
        return None
    return finished


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
        if not row["job_id"]:
            manual = manual_success(db, row["last_success"])
            if manual:
                # A full manual pull counts as today's refresh.
                db.execute(
                    "UPDATE bitbucket_sync_schedule SET next_attempt=?,last_success=?,error='' WHERE server=?",
                    ((manual + INTERVAL).isoformat(), manual.isoformat(), server),
                )
                return
        if row["job_id"]:
            job = db.execute(
                "SELECT status,progress FROM jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
            if job and job["status"] in {"queued", "running", "paused"}:
                return
            progress = json.loads(job["progress"]) if job else {}
            success = job is not None and completed(job["status"], progress)
            finished = (
                datetime.fromisoformat(progress["completed_at"])
                if progress.get("completed_at")
                else now
            )
            next_attempt = finished + (INTERVAL if success else RETRY)
            db.execute(
                "UPDATE bitbucket_sync_schedule SET job_id=NULL,next_attempt=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,error=? WHERE server=?",
                (
                    next_attempt.isoformat(),
                    bool(success),
                    finished.isoformat(),
                    ""
                    if success
                    else "Sync stopped before reading every repository. Retrying in two hours.",
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
                    "Connection or sync failed. Retrying in two hours.",
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
    if job and job["status"] not in {"queued", "running", "paused"}:
        # A pull that just ended, before the scheduler's next check records it: show
        # its outcome now rather than the schedule from before it ran.
        progress = json.loads(job["progress"])
        finished = progress.get("completed_at")
        if finished:
            if completed(job["status"], progress):
                state.update(status="scheduled", message="", last_success=epoch(finished),
                             next_run=epoch(finished) + INTERVAL.total_seconds())
            else:
                state.update(status="retrying", message="The pull stopped before reading every repository. Retrying in two hours.",
                             next_run=epoch(finished) + RETRY.total_seconds())
            state["last_attempt"] = epoch(progress.get("started_at"))
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
        # What it is doing now: listing a repository's files (found grows, nothing is
        # processed yet) reads very differently from updating them.
        statuses = progress.get("repository_statuses") or {}
        working = next(
            (repo for repo in statuses.values() if repo.get("status") in {"scanning", "processing", "retrying"}),
            None,
        )
        state["repositories"] = progress.get("repositories") or len(statuses)
        state["repositories_done"] = progress.get("repositories_done") or 0
        if working:
            name = f"{working.get('project', '')}/{working['repo']}".strip("/")
            state["phase"] = {"scanning": "finding", "processing": "updating", "retrying": "retrying"}[working["status"]]
            state["current"] = name
            state["current_found"] = working.get("found") or 0
            state["current_processed"] = working.get("processed") or 0
            state["current_eta_seconds"] = working.get("eta_seconds")
    return state


def start_now(jobs):
    """Start a background Git pull of every tracked repository right away, as the
    schedule would; returns "started", "running" or "not set up"."""
    try:
        settings = load_settings()
    except ValueError:
        return "not set up"
    setup()
    with connection() as db:
        projects = [
            row[0]
            for row in db.execute("SELECT id FROM tracked_projects WHERE server=?", (settings.base_url,))
        ]
    if not projects:
        return "not set up"
    if jobs.active():
        return "running"
    job = jobs.start(projects)
    job["background"] = True
    jobs.save()
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        # The schedule follows this pull like one it started: done, or retried in two hours.
        db.execute(
            "INSERT INTO bitbucket_sync_schedule(server,next_attempt,job_id) VALUES(?,?,?) "
            "ON CONFLICT(server) DO UPDATE SET job_id=excluded.job_id,error=''",
            (settings.base_url, now, job["id"]),
        )
    return "started"


async def run_scheduler(jobs):
    setup()
    while True:
        try:
            await run_due(jobs)
        except Exception as error:  # noqa: BLE001 - keep scheduler alive
            event("bitbucket.schedule.error", **error_details(error))
        await asyncio.sleep(60)
