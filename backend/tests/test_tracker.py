import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.api import tracker as api
from app.bookmarks import confluence
from app.core.database import connection, initialize
from app.tracker import service


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"OWL_DB_PATH": self.temp.name + "/owl.db"}).start()
        initialize()
        self.now = 1_800_000_000
        patch.object(service.time, "time", side_effect=lambda: self.now).start()
        self.settings = confluence.ConfluenceSettings(
            base_url="https://wiki.test", token="secret"
        )
        patch.object(confluence, "load", return_value=self.settings).start()
        self.test_connection = patch.object(
            confluence, "test", new_callable=AsyncMock
        ).start()
        self.ids = ["100", "101"]
        self.order = []
        self.version = 1

        async def discover(*args, **kwargs):
            self.order.append("discovered")
            return list(self.ids)

        async def metadata(url, **kwargs):
            self.assertEqual(self.order[0], "discovered")
            self.assertIs(kwargs["settings"], self.settings)
            page_id = url.split("=")[-1]
            self.order.append(page_id)
            return {
                "page_id": page_id,
                "title": "Page " + page_id,
                "url": url,
                "version": self.version,
                "contentText": f"Text {self.version}",
                "ancestors": [] if page_id == "100" else [{"page_id": "100"}],
                "rawMetadata": {"body": {"storage": {"value": "<p>content</p>"}}},
            }

        # A tiny Confluence: 100 is the space home; 150 and 160 sit below it.
        self.ancestry = {"100": [], "150": ["100"], "160": ["100", "150"]}

        async def get(settings, path, params=None):
            if path.startswith("content/"):
                page = path.split("/")[1]
                return {
                    "id": page,
                    "type": "page",
                    "ancestors": [{"id": item} for item in self.ancestry.get(page, [])],
                }
            if path == "space/ENG":
                return {"key": "ENG", "homepage": {"id": "100"}}
            if path == "content" and params.get("title") == "Deep Page":
                return {"results": [{"id": "160"}]}
            return {"results": []}

        patch.object(confluence, "get", side_effect=get).start()
        patch.object(service, "discover_pages", side_effect=discover).start()
        self.metadata = patch.object(
            confluence, "metadata", side_effect=metadata
        ).start()

    async def scan(self):
        root = service.claim()
        self.assertIsNotNone(root)
        self.order.clear()
        await service.sync(root)
        return api.roots()["roots"][0]

    async def test_daily_success_survives_restart_and_no_early_repeat(self):
        self.assertEqual(
            await service.add_root("100"),
            await service.add_root(
                "https://wiki.test/pages/viewpage.action?pageId=100"
            ),
        )
        state = await self.scan()
        self.assertEqual(self.order, ["discovered", "100", "101"])
        self.assertEqual(
            (state["status"], state["page_count"], state["unread"]), ("completed", 2, 0)
        )
        self.assertEqual(state["next_run"], self.now + 86400)
        initialize(recover_jobs=True)
        self.now += 86399
        self.assertIsNone(service.claim())
        self.now += 1
        state = await self.scan()
        self.assertEqual(state["unread"], 0)
        self.assertIsNone(service.claim())

    async def test_repeated_failures_wait_one_hour_then_resume_daily(self):
        await service.add_root("100")
        self.test_connection.side_effect = ValueError("connection secret failed")
        for _ in range(3):
            state = await self.scan()
            self.assertEqual(state["status"], "failed")
            self.assertIsNone(state["last_success"])
            self.assertNotIn("secret", state["error"])
            self.assertEqual(state["next_run"], self.now + 3600)
            self.assertEqual(service.schedule_status()["status"], "retrying")
            self.now += 3599
            self.assertIsNone(service.claim())
            self.now += 1
        self.test_connection.side_effect = None
        state = await self.scan()
        self.assertEqual(state["status"], "completed")
        self.now += 3600
        self.assertIsNone(service.claim())
        self.assertEqual(service.schedule_status()["status"], "scheduled")

    async def test_changes_prior_text_opens_and_review_cutoff(self):
        root_id = await service.add_root("100")
        await self.scan()
        api.record_open(root_id, "100")
        self.version = 2
        self.ids.append("102")
        self.now += 86400
        state = await self.scan()
        self.assertEqual(state["unread"], 3)
        listing = api.pages(root_id)
        root = next(p for p in listing["pages"] if p["page_id"] == "100")
        self.assertEqual(root["opens"], 1)
        self.assertNotIn("rawMetadata", root)
        self.assertNotIn("contentText", root)
        detail = api.page_details(root_id, "100")
        self.assertEqual(detail["previous_content"], "Text 1")
        self.assertEqual(detail["changes"][0]["kind"], "updated")
        self.assertEqual(api.page_details(root_id, "102")["changes"][0]["kind"], "new")
        self.version = 3
        self.now += 86400
        await self.scan()
        api.review(root_id, api.ReviewInput(through_id=listing["max_event"]))
        self.assertEqual(api.roots()["roots"][0]["unread"], 3)

    async def test_partial_failure_retains_cache_and_does_not_mark_missing(self):
        root_id = await service.add_root("100")
        await self.scan()
        self.ids = ["100"]
        self.metadata.side_effect = ValueError("network failed")
        self.now += 86400
        state = await self.scan()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["page_count"], 2)
        self.assertTrue(all(p["present"] for p in api.pages(root_id)["pages"]))
        self.assertEqual(state["unread"], 0)

    async def test_missing_and_returned_only_after_complete_scan(self):
        root_id = await service.add_root("100")
        await self.scan()
        self.ids = ["100"]
        self.now += 86400
        await self.scan()
        self.assertEqual(
            api.page_details(root_id, "101")["changes"][0]["kind"], "missing"
        )
        self.ids.append("101")
        self.now += 86400
        await self.scan()
        self.assertEqual(
            api.page_details(root_id, "101")["changes"][0]["kind"], "returned"
        )

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
            db.execute(
                "UPDATE confluence_tracker_roots SET base_url='https://different.test'"
            )
        state = await self.scan()
        self.assertEqual(state["status"], "failed")
        self.test_connection.assert_not_awaited()
        self.metadata.assert_not_awaited()
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
            self.assertEqual(
                [row[0] for row in db.execute("SELECT page_id FROM confluence_tracker_roots")],
                ["100"],
            )
        with self.assertRaises(ValueError):
            await service.add_root("https://wiki.test/display/ENG/Missing+Page")


class DiscoveryFallbackTests(unittest.IsolatedAsyncioTestCase):
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
