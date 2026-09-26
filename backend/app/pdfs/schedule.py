"""Durable background Bitbucket sync: three weekdays, retry failures in two hours."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.core.config import load_settings
from app.core.database import connection
from app.core.logging import error_details, event
from app.pdfs.client import BitbucketClient


def after_weekdays(value, count=3):
    while count:
        value += timedelta(days=1)
        if value.weekday() < 5:
            count -= 1
    return value


def setup():
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS bitbucket_sync_schedule (
            server TEXT PRIMARY KEY, next_attempt TEXT NOT NULL,
            job_id TEXT, last_success TEXT, error TEXT NOT NULL DEFAULT ''
        )""")


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
                (server, after_weekdays(baseline).isoformat()),
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
                after_weekdays(completed) if success else completed + timedelta(hours=2)
            )
            db.execute(
                "UPDATE bitbucket_sync_schedule SET job_id=NULL,next_attempt=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,error=? WHERE server=?",
                (
                    next_attempt.isoformat(),
                    bool(success),
                    completed.isoformat(),
                    ""
                    if success
                    else "Sync did not complete successfully. Retrying in two hours.",
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
                    (now + timedelta(hours=2)).isoformat(),
                    "Connection or sync failed. Retrying in two hours.",
                    server,
                ),
            )
    finally:
        await client.close()


async def run_scheduler(jobs):
    setup()
    while True:
        try:
            await run_due(jobs)
        except Exception as error:  # noqa: BLE001 - keep scheduler alive
            event("bitbucket.schedule.error", **error_details(error))
        await asyncio.sleep(60)
