import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.bookmarks import confluence, refresh
from app.core.database import connection, initialize
from fastapi import HTTPException


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"OWL_DB_PATH": self.temp.name + "/owl.db"})
        env.start()
        self.addCleanup(env.stop)
        initialize()
        self.now = 1_800_000_000
        clock = patch.object(refresh.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        settings = confluence.ConfluenceSettings(
            base_url="https://wiki.test", token="test"
        )
        self.load = patch.object(confluence, "load", return_value=settings).start()
        self.test_connection = patch.object(
            confluence, "test", new_callable=AsyncMock
        ).start()
        self.metadata = patch.object(
            confluence, "metadata", new_callable=AsyncMock
        ).start()
        self.addCleanup(patch.stopall)
        self.page = {
            "id": 1,
            "url": "https://wiki.test/pages/123/Old",
            "title": "Old",
            "notes": "mine",
            "favorite": True,
            "views": 4,
        }
        self.data = {
            "sourceType": "confluence",
            "confluenceBaseUrl": "https://wiki.test",
            "title": "Updated",
            "page_id": "123",
            "contentText": "New text",
            "space": "Team",
            "breadcrumb": ["Parent"],
        }
        self.metadata.return_value = self.data
        self.write([self.page, {"id": 2, "url": "https://other.test/", "title": "Web"}])

    def write(self, pages):
        with connection() as db:
            db.execute(
                "UPDATE bookmark_workspace SET payload=?,revision=revision+1 WHERE id=1",
                (
                    json.dumps(
                        {"bookmarks": pages, "groups": [], "notes": {"1": "local note"}}
                    ),
                ),
            )

    def workspace(self):
        with connection() as db:
            return json.loads(
                db.execute(
                    "SELECT payload FROM bookmark_workspace WHERE id=1"
                ).fetchone()[0]
            )

    async def test_success_weekly_and_persistent_across_initialization(self):
        await refresh.run_due()
        self.assertEqual(self.metadata.await_count, 1)
        state = refresh.status()
        self.assertEqual(state["next_run"], self.now + refresh.WEEK)
        self.assertEqual(state["last_success"], self.now)
        item = self.workspace()["bookmarks"][0]
        self.assertEqual(item["title"], "Updated")
        self.assertEqual(
            (item["notes"], item["favorite"], item["views"]), ("mine", True, 4)
        )
        self.assertEqual(self.workspace()["notes"], {"1": "local note"})
        initialize(recover_jobs=True)
        self.now += refresh.WEEK - 1
        await refresh.run_due()
        self.assertEqual(self.metadata.await_count, 1)
        self.now += 1
        await refresh.run_due()
        self.assertEqual(self.metadata.await_count, 2)

    async def test_connection_failure_retries_every_two_hours_until_success(self):
        self.test_connection.side_effect = ValueError("secret upstream text")
        original = self.workspace()
        for _ in range(3):
            await refresh.run_due()
            self.assertEqual(refresh.status()["status"], "retrying")
            self.assertEqual(refresh.status()["next_run"], self.now + refresh.RETRY)
            self.assertIsNone(refresh.status()["last_success"])
            self.assertNotIn("secret", refresh.status()["message"])
            self.now += refresh.RETRY - 1
            count = self.test_connection.await_count
            await refresh.run_due()
            self.assertEqual(self.test_connection.await_count, count)
            self.now += 1
        self.assertEqual(self.workspace(), original)
        self.metadata.assert_not_awaited()
        self.test_connection.side_effect = None
        await refresh.run_due()
        self.assertEqual(refresh.status()["next_run"], self.now + refresh.WEEK)

    async def test_partial_failure_is_not_success_and_recovers(self):
        self.write(
            [
                self.page,
                {**self.page, "id": 3, "url": "https://wiki.test/pages/456/Other"},
            ]
        )
        self.metadata.side_effect = [self.data, ValueError("page unavailable")]
        await refresh.run_due()
        state = refresh.status()
        self.assertEqual(
            (state["completed"], state["total"], state["failed"]), (2, 2, 1)
        )
        self.assertEqual(state["next_run"], self.now + refresh.RETRY)
        self.assertNotIn("last_update_all", self.workspace())
        self.metadata.side_effect = None
        self.now += refresh.RETRY
        await refresh.run_due()
        self.assertEqual(refresh.status()["status"], "scheduled")

    async def test_concurrent_user_edits_are_preserved_and_deletions_not_restored(self):
        async def edited(_):
            self.write(
                [{**self.page, "notes": "new note", "favorite": False, "views": 10}]
            )
            return self.data

        self.metadata.side_effect = edited
        await refresh.run_due()
        item = self.workspace()["bookmarks"][0]
        self.assertEqual(
            (item["notes"], item["favorite"], item["views"]), ("new note", False, 10)
        )

        async def deleted(_):
            self.write([])
            return self.data

        self.metadata.side_effect = deleted
        self.now += refresh.WEEK
        await refresh.run_due()
        self.assertEqual(self.workspace()["bookmarks"], [])

    async def test_downloaded_pages_updated_without_creating_bookmarks(self):
        with connection() as db:
            db.execute(
                "INSERT INTO bookmark_downloaded_pages(folder_key,page_id,title,url,content) VALUES('folder','123','Old','https://wiki.test/pages/viewpage.action?pageId=123','old')"
            )
        await refresh.run_due()
        with connection() as db:
            row = db.execute("SELECT * FROM bookmark_downloaded_pages").fetchone()
        self.assertEqual(row["content"], "New text")
        self.assertEqual(json.loads(row["folder_path"]), ["Team", "Parent"])
        self.assertEqual(len(self.workspace()["bookmarks"]), 2)
        self.assertEqual(refresh.status()["total"], 2)

    async def test_claim_excludes_overlap_and_expired_claim_recovers(self):
        owner = refresh.claim()
        self.assertIsNotNone(owner)
        self.assertIsNone(refresh.claim())
        await refresh.run_due()
        self.test_connection.assert_not_awaited()
        self.now += refresh.LEASE + 1
        await refresh.run_due()
        self.assertEqual(refresh.status()["status"], "scheduled")
        refresh.finish(owner, success=False)
        self.assertEqual(refresh.status()["status"], "scheduled")

    async def test_shutdown_cancellation_keeps_retry_durable(self):
        self.test_connection.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await refresh.run_due()
        self.assertEqual(refresh.status()["next_run"], self.now + refresh.RETRY)
        self.assertEqual(refresh.status()["status"], "retrying")

    async def test_connection_lost_mid_run_stops_requests_until_retry(self):
        self.write(
            [
                self.page,
                {**self.page, "id": 3, "url": "https://wiki.test/pages/456/Other"},
            ]
        )
        self.metadata.side_effect = HTTPException(502, "Connection failed")
        await refresh.run_due()
        self.assertEqual(self.metadata.await_count, 1)
        self.assertEqual(refresh.status()["next_run"], self.now + refresh.RETRY)
        self.assertEqual(refresh.status()["failed"], 1)
        self.assertNotIn("last_update_all", self.workspace())
