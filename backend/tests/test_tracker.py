"""Confluence Tracker: metadata-only baselines, then only pages updated since."""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.api import tracker as api
from app.bookmarks import confluence
from app.core.database import connection, initialize
from app.tracker import service

from tests.fake_confluence import FakeConfluence

DAY = 86400


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"OWL_DB_PATH": self.temp.name + "/owl.db"}).start()
        initialize()
        self.now = 1_800_000_000  # 2027-01-15
        patch.object(service.time, "time", side_effect=lambda: self.now).start()
        self.settings = confluence.ConfluenceSettings(base_url="https://wiki.test", token="secret")
        patch.object(confluence, "load", return_value=self.settings).start()
        self.test_connection = patch.object(confluence, "test", new_callable=AsyncMock).start()
        # 100 is the space home; 150 and 160 sit below it, 101 is a second child.
        self.wiki = FakeConfluence(
            {
                "100": {"title": "Home", "ancestors": [], "created": "2026-01-01T00:00:00Z", "updated": "2026-06-01T00:00:00Z", "creator": "Ann", "editor": "Ben"},
                "101": {"title": "Runbooks", "ancestors": ["100"], "created": "2026-02-01T00:00:00Z", "updated": "2026-02-01T00:00:00Z", "creator": "Cat", "editor": "Cat"},
                "150": {"title": "Architecture", "ancestors": ["100"], "created": "2026-03-01T00:00:00Z", "updated": "2026-05-01T00:00:00Z", "creator": "Dan", "editor": "Eve"},
                "160": {"title": "Deep Page", "ancestors": ["100", "150"], "created": "2026-04-01T00:00:00Z", "updated": "2026-04-02T00:00:00Z", "creator": "Eve", "editor": "Eve"},
            }
        )
        patch.object(confluence, "get", side_effect=self.wiki.get).start()
        patch.object(service, "stamp", side_effect=self.stamp).start()

    def stamp(self):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    async def scan(self):
        root = service.claim()
        self.assertIsNotNone(root)
        self.wiki.calls.clear()
        await service.sync(root)
        return api.roots()["roots"][0]

    def edit(self, page_id, editor="Zed"):
        page = self.wiki.pages[page_id]
        page["version"] = page.get("version", 1) + 1
        page["editor"] = editor
        page["updated"] = self.stamp()

    async def test_baseline_stores_who_and_when_without_content(self):
        root_id = await service.add_root("160")  # any page tracks its top-most parent
        state = await self.scan()
        self.assertEqual((state["status"], state["page_count"], state["unread"]), ("completed", 4, 0))
        self.assertTrue(all("body" not in str(params) for _, params in self.wiki.calls))
        pages = {page["page_id"]: page for page in api.pages(root_id)["pages"]}
        deep = pages["160"]
        self.assertEqual(
            (deep["title"], deep["author"], deep["writtenAt"], deep["lastEditor"], deep["confluenceUpdatedAt"]),
            ("Deep Page", "Eve", "2026-04-01T00:00:00Z", "Eve", "2026-04-02T00:00:00Z"),
        )
        self.assertEqual(deep["breadcrumb"], ["Home", "Architecture"])
        self.assertEqual(deep["url"], "https://wiki.test/pages/viewpage.action?pageId=160")
        self.assertEqual(deep["parent_id"], "150")
        self.assertNotIn("contentText", deep)
        self.assertEqual(state["next_run"], self.now + DAY)

    async def test_daily_sync_asks_only_for_updated_pages(self):
        root_id = await service.add_root("100")
        await self.scan()
        self.now += DAY
        self.edit("160")
        self.wiki.pages["170"] = {"title": "New Page", "ancestors": ["100", "150"], "created": self.stamp(), "updated": self.stamp(), "creator": "Fay", "editor": "Fay"}
        state = await self.scan()
        searched = [params for path, params in self.wiki.calls if path == "content/search"]
        self.assertTrue(searched, self.wiki.calls)
        self.assertIn('lastmodified >= "2027-01-14"', searched[0]["cql"])
        self.assertFalse(any(path.endswith("/descendant/page") for path, _ in self.wiki.calls))
        self.assertEqual((state["status"], state["page_count"], state["unread"]), ("completed", 5, 2))
        detail = api.page_details(root_id, "160")
        change = detail["changes"][0]
        self.assertEqual(change["kind"], "updated")
        self.assertEqual(
            {key: change["summary"][key] for key in ("path", "url", "updatedBy", "version")},
            {"path": "Home / Architecture", "url": "https://wiki.test/pages/viewpage.action?pageId=160", "updatedBy": "Zed", "version": {"before": 1, "after": 2}},
        )
        self.assertEqual(api.page_details(root_id, "170")["changes"][0]["kind"], "new")
        # Pages returned again by the overlapping day are not reported twice.
        self.now += DAY
        state = await self.scan()
        self.assertEqual(state["unread"], 2)

    async def test_without_search_the_whole_tree_is_compared(self):
        root_id = await service.add_root("100")
        await self.scan()
        self.wiki.unsupported.add("search")
        self.now += DAY
        self.edit("101")
        del self.wiki.pages["160"]
        state = await self.scan()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(api.page_details(root_id, "101")["changes"][0]["kind"], "updated")
        missing = next(page for page in api.pages(root_id)["pages"] if page["page_id"] == "160")
        self.assertEqual((missing["present"], missing["change_kind"]), (0, "missing"))

    async def test_children_are_walked_when_descendants_cannot_be_listed(self):
        root_id = await service.add_root("100")
        self.wiki.unsupported.add("content/100/descendant/page")
        self.wiki.denied.add("150")  # its own branch is hidden; the rest is still tracked
        state = await self.scan()
        self.assertEqual(state["status"], "completed")
        self.assertEqual({page["page_id"] for page in api.pages(root_id)["pages"]}, {"100", "101", "150"})

    async def test_repeated_failures_wait_two_hours_then_resume_daily(self):
        await service.add_root("100")
        self.test_connection.side_effect = ValueError("connection secret failed")
        for _ in range(3):
            state = await self.scan()
            self.assertEqual(state["status"], "failed")
            self.assertIsNone(state["last_success"])
            self.assertNotIn("secret", state["error"])
            self.assertEqual(state["next_run"], self.now + 7200)
            self.assertEqual(service.schedule_status()["status"], "retrying")
            self.now += 7199
            self.assertIsNone(service.claim())
            self.now += 1
        self.test_connection.side_effect = None
        state = await self.scan()
        self.assertEqual(state["status"], "completed")
        self.now += 7200
        self.assertIsNone(service.claim())
        self.assertEqual(service.schedule_status()["status"], "scheduled")

    async def test_manual_sync_after_a_failure_cancels_the_retry(self):
        root_id = await service.add_root("100")
        self.test_connection.side_effect = ValueError("offline")
        state = await self.scan()
        self.assertEqual(state["next_run"], self.now + 7200)
        self.test_connection.side_effect = None
        self.now += 600
        api.sync_now(root_id)  # Sync now in the tracker
        state = await self.scan()
        self.assertEqual((state["status"], state["next_run"]), ("completed", self.now + DAY))
        self.now += 7200
        self.assertIsNone(service.claim())

    async def test_failed_daily_sync_keeps_pages(self):
        root_id = await service.add_root("100")
        await self.scan()
        self.now += DAY
        self.wiki.unsupported.add("content/100")
        self.wiki.unsupported.add("search")
        state = await self.scan()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["page_count"], 4)
        self.assertTrue(all(page["present"] for page in api.pages(root_id)["pages"]))

    async def test_opens_and_review(self):
        root_id = await service.add_root("100")
        await self.scan()
        api.record_open(root_id, "100")
        self.now += DAY
        self.edit("100")
        self.edit("101")
        state = await self.scan()
        self.assertEqual(state["unread"], 2)
        listing = api.pages(root_id)
        self.assertEqual(next(p for p in listing["pages"] if p["page_id"] == "100")["opens"], 1)
        api.review(root_id, api.ReviewInput(through_id=listing["max_event"]))
        self.assertEqual(api.roots()["roots"][0]["unread"], 0)

    async def test_claim_excludes_live_lease_and_recovers_expired_worker(self):
        await service.add_root("100")
        old = service.claim()
        self.assertIsNone(service.claim())
        self.now += service.LEASE + 1
        new = service.claim()
        self.assertNotEqual(old["owner"], new["owner"])
        service.finish(old, True)
        self.assertEqual(api.roots()["roots"][0]["status"], "discovering")
        service.finish(new, True)
        self.assertIsNone(service.claim())

    async def test_wrong_server_never_fetches_with_new_credentials(self):
        await service.add_root("100")
        with connection() as db:
            db.execute("UPDATE confluence_tracker_roots SET base_url='https://different.test'")
        state = await self.scan()
        self.assertEqual(state["status"], "failed")
        self.test_connection.assert_not_awaited()
        self.assertEqual(self.wiki.calls, [])
        with self.assertRaises(ValueError):
            await service.add_root("https://other.test/pages/100")

    async def test_any_url_tracks_the_top_most_parent(self):
        root = await service.add_root("100")
        for value in (
            "160",
            "https://wiki.test/pages/viewpage.action?pageId=150",
            "https://wiki.test/spaces/ENG/pages/160/Deep+Page",
            "https://wiki.test/display/ENG/Deep+Page",
            "https://wiki.test/pages/viewpage.action?spaceKey=ENG&title=Deep+Page",
            "https://wiki.test/display/ENG",
            "https://wiki.test/spaces/ENG/overview",
            "https://wiki.test/x/oAAAAA",  # short link for page 160
        ):
            self.assertEqual(await service.add_root(value), root, value)
        with connection() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT page_id FROM confluence_tracker_roots")], ["100"])
        with self.assertRaises(ValueError):
            await service.add_root("https://wiki.test/display/ENG/Missing+Page")


class StartFreshTests(unittest.TestCase):
    def test_trees_tracked_with_content_are_removed_once(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OWL_DB_PATH": folder + "/owl.db"}):
            initialize()
            with connection() as db:
                db.execute("DELETE FROM owl_migrations")
                db.execute("INSERT INTO confluence_tracker_roots(base_url,page_id,url,title,created_at) VALUES('https://wiki.test','1','u','Old','now')")
            initialize()
            with connection() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM confluence_tracker_roots").fetchone())
                db.execute("INSERT INTO confluence_tracker_roots(base_url,page_id,url,title,created_at) VALUES('https://wiki.test','2','u','New','now')")
            initialize()
            with connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM confluence_tracker_roots").fetchone()[0], 1)


class DiscoveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Bookmarks folder downloads still discover page IDs this way."""

    async def test_restricted_branch_is_skipped_during_child_walk(self):
        from app.bookmarks.discovery import discover_pages

        settings = confluence.ConfluenceSettings(base_url="https://wiki.test", token="secret")
        children = {"1": ["2", "3"], "2": ["4"], "4": [], "5": []}

        async def get(settings, path, params=None):
            if path.endswith("/descendant/page"):
                raise confluence.ConfluenceRequestError(500, "broken")
            page = path.split("/")[1]
            if page == "3":  # restricted: listed by its parent, but its children are hidden
                raise confluence.ConfluenceRequestError(403, "denied")
            return {"results": [{"id": child} for child in children.get(page, [])]}

        with patch.object(confluence, "get", side_effect=get):
            self.assertEqual(await discover_pages(settings, ["1"]), ["1", "2", "3", "4"])

        async def failing(settings, path, params=None):
            if path.endswith("/descendant/page"):
                raise confluence.ConfluenceRequestError(500, "broken")
            raise confluence.ConfluenceRequestError(401, "bad token")

        with patch.object(confluence, "get", side_effect=failing):
            with self.assertRaises(confluence.ConfluenceRequestError):
                await discover_pages(settings, ["1"])


if __name__ == "__main__":
    unittest.main()
