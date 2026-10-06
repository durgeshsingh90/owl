import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.core.config import Settings, parse_target, save_settings
from app.core.database import connection, initialize
from app.pdfs.schedule import INTERVAL, RETRY, run_due, setup, status


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "OWL_DB_PATH": self.temp.name + "/test.db",
                "OWL_CONFIG_DIR": self.temp.name + "/config",
            },
        )
        self.env.start()
        initialize()
        setup()
        self.settings = Settings(
            base_url="https://scm.mastercard.int/stash", username="test", token="test"
        )
        save_settings(self.settings)
        self.now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)  # Friday
        with connection() as db:
            db.execute(
                "INSERT INTO tracked_projects(project_url,server,project) VALUES(?,?,?)",
                (
                    self.settings.base_url + "/projects/SDN",
                    self.settings.base_url,
                    "SDN",
                ),
            )
        self.jobs = FakeJobs()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_clone_url(self):
        result = parse_target(
            "https://scm.mastercard.int/stash/scm/sdn/network-docs-non-prod.git",
            self.settings,
        )
        self.assertEqual(
            result,
            {
                "project": "SDN",
                "url": "https://scm.mastercard.int/stash/projects/SDN",
                "repo": "network-docs-non-prod",
                "path": None,
            },
        )
        for url in [
            "https://evil.test/stash/scm/sdn/repo.git",
            "https://u:p@scm.mastercard.int/stash/scm/sdn/repo.git",
        ]:
            with self.assertRaises(ValueError):
                parse_target(url, self.settings)
        self.assertEqual(
            parse_target(
                self.settings.base_url + "/projects/SDN/repos/example", self.settings
            )["repo"],
            "example",
        )

    async def test_daily_refresh_retries_every_two_hours_until_success(self):
        self.assertEqual((INTERVAL, RETRY), (timedelta(days=1), timedelta(hours=2)))
        client = AsyncMock()
        with patch("app.pdfs.schedule.BitbucketClient", return_value=client):
            await run_due(self.jobs, self.now)
            client.test.assert_not_awaited()
            due = self.now + timedelta(days=1)
            client.test.side_effect = ValueError("offline")
            await run_due(self.jobs, due)
            self.assertEqual(self.jobs.started, 0)
            with connection() as db:
                row = db.execute("SELECT * FROM bitbucket_sync_schedule").fetchone()
            self.assertEqual(
                row["next_attempt"], (due + timedelta(hours=2)).isoformat()
            )
            self.assertEqual(status(self.jobs)["status"], "retrying")
            await run_due(self.jobs, due + timedelta(minutes=119))
            self.assertEqual(client.test.await_count, 1)
            client.test.side_effect = None
            await run_due(self.jobs, due + timedelta(hours=2))
            self.assertEqual(self.jobs.started, 1)
            self.assertEqual(status(self.jobs)["status"], "running")
            self.assertTrue(self.jobs.current["background"])
            await run_due(self.jobs, due + timedelta(hours=3))
            self.assertEqual(self.jobs.started, 1)
            completed = due + timedelta(hours=3)
            self.jobs.finish("succeeded", completed)
            await run_due(self.jobs, completed)
            with connection() as db:
                row = db.execute("SELECT * FROM bitbucket_sync_schedule").fetchone()
            self.assertEqual(row["last_success"], completed.isoformat())
            self.assertEqual(
                row["next_attempt"], (completed + timedelta(days=1)).isoformat()
            )
            state = status(self.jobs)
            self.assertEqual(state["status"], "scheduled")
            self.assertEqual(state["last_success"], completed.timestamp())

    async def test_partial_failure_and_restart_retry_without_overlap(self):
        with connection() as db:
            db.execute(
                "INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES(?,?)",
                (self.settings.base_url, self.now.isoformat()),
            )
        client = AsyncMock()
        with patch("app.pdfs.schedule.BitbucketClient", return_value=client):
            self.jobs.busy = True
            await run_due(self.jobs, self.now)
            client.test.assert_not_awaited()
            self.jobs.busy = False
            await run_due(self.jobs, self.now)
            # A repository whose file list could not be read is a failed refresh.
            self.jobs.current["discovery_failed"] = True
            self.jobs.finish("succeeded_with_errors", self.now)
            # A new scheduler invocation reads the durable failed-job state.
            await run_due(FakeJobs(), self.now)
            with connection() as db:
                row = db.execute("SELECT * FROM bitbucket_sync_schedule").fetchone()
            self.assertIsNone(row["last_success"])
            self.assertEqual(
                row["next_attempt"], (self.now + timedelta(hours=2)).isoformat()
            )

    async def test_pull_with_some_failed_files_is_done_for_the_day(self):
        with connection() as db:
            db.execute(
                "INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES(?,?)",
                (self.settings.base_url, self.now.isoformat()),
            )
        client = AsyncMock()
        with patch("app.pdfs.schedule.BitbucketClient", return_value=client):
            await run_due(self.jobs, self.now)
            # Two unreadable files: they are retried on the next pull, not in two hours.
            self.jobs.current.update(failed=2, repositories_failed=1)
            self.jobs.finish("succeeded_with_errors", self.now)
            await run_due(self.jobs, self.now)
            state = status(self.jobs)
            self.assertEqual(state["status"], "scheduled")
            self.assertEqual(state["next_run"], (self.now + timedelta(days=1)).timestamp())

    async def test_full_manual_pull_counts_as_the_daily_refresh(self):
        with connection() as db:
            project = db.execute("SELECT id FROM tracked_projects").fetchone()[0]
            repo = db.execute(
                "INSERT INTO repositories(project_id,repo,name) VALUES(?,?,?) RETURNING id",
                (project, "docs", "docs"),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES(?,?)",
                (self.settings.base_url, (self.now + timedelta(hours=5)).isoformat()),
            )

        def manual(job_id, completed, statuses, discovery_failed=False):
            with connection() as db:
                db.execute(
                    "INSERT INTO jobs(id,status,progress) VALUES(?,?,?)",
                    (job_id, "succeeded_with_errors", json.dumps({"completed_at": completed.isoformat(), "repository_statuses": statuses, "failed": 2, "discovery_failed": discovery_failed})),
                )

        client = AsyncMock()
        with patch("app.pdfs.schedule.BitbucketClient", return_value=client):
            # A pull of some repositories only, or one that could not list every file, does not count.
            manual("partial", self.now, {})
            await run_due(self.jobs, self.now)
            manual("unread", self.now, {str(repo): {}}, discovery_failed=True)
            await run_due(self.jobs, self.now)
            with connection() as db:
                row = db.execute("SELECT * FROM bitbucket_sync_schedule").fetchone()
            self.assertIsNone(row["last_success"])
            done = self.now + timedelta(minutes=10)
            manual("full", done, {str(repo): {}})
            await run_due(self.jobs, done + timedelta(minutes=1))
            with connection() as db:
                row = db.execute("SELECT * FROM bitbucket_sync_schedule").fetchone()
            self.assertEqual(row["last_success"], done.isoformat())
            self.assertEqual(row["next_attempt"], (done + timedelta(days=1)).isoformat())
            self.assertEqual(status(self.jobs)["status"], "scheduled")
            client.test.assert_not_awaited()

    async def test_manual_pull_after_a_failure_cancels_the_retry(self):
        with connection() as db:
            project = db.execute("SELECT id FROM tracked_projects").fetchone()[0]
            repo = db.execute(
                "INSERT INTO repositories(project_id,repo,name) VALUES(?,?,?) RETURNING id",
                (project, "docs", "docs"),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES(?,?)",
                (self.settings.base_url, self.now.isoformat()),
            )
        client = AsyncMock()
        client.test.side_effect = ValueError("offline")
        with patch("app.pdfs.schedule.BitbucketClient", return_value=client):
            await run_due(self.jobs, self.now)
            self.assertEqual(status(self.jobs)["status"], "retrying")
            done = self.now + timedelta(minutes=30)
            with connection() as db:
                db.execute(
                    "INSERT INTO jobs(id,status,progress) VALUES(?,?,?)",
                    ("manual", "succeeded", json.dumps({"completed_at": done.isoformat(), "repository_statuses": {str(repo): {}}})),
                )
            await run_due(self.jobs, done)
            client.test.side_effect = None
            # The two-hour retry no longer happens; the next refresh is a day later.
            await run_due(self.jobs, self.now + timedelta(hours=2))
            client.test.assert_awaited_once()
            self.assertEqual(self.jobs.started, 0)
            state = status(self.jobs)
            self.assertEqual((state["status"], state["next_run"]), ("scheduled", (done + timedelta(days=1)).timestamp()))


class FakeJobs:
    def __init__(self):
        self.started = 0
        self.busy = False

    def active(self):
        return self.busy

    def start(self, projects):
        self.started += 1
        self.busy = True
        self.current = {"id": "test-job", "status": "running"}
        with connection() as db:
            db.execute(
                "INSERT INTO jobs(id,status,progress) VALUES(?,?,?)",
                ("test-job", "running", json.dumps(self.current)),
            )
        return self.current

    def save(self):
        with connection() as db:
            db.execute(
                "UPDATE jobs SET progress=? WHERE id=?",
                (json.dumps(self.current), self.current["id"]),
            )

    def finish(self, status, now):
        self.busy = False
        self.current.update(status=status, completed_at=now.isoformat())
        with connection() as db:
            db.execute(
                "UPDATE jobs SET status=?,progress=? WHERE id=?",
                (status, json.dumps(self.current), self.current["id"]),
            )
