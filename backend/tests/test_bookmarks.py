import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from main import app

REAL_CLIENT = httpx.AsyncClient


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "OWL_DB_PATH": self.temp.name + "/test.db",
                "OWL_CONFIG_DIR": self.temp.name,
                "OWL_LOG_DIR": self.temp.name + "/logs",
            },
        )
        self.env.start()
        self.calls = []
        self.code = 200
        self.mock = patch(
            "app.bookmarks.confluence.httpx.AsyncClient",
            side_effect=lambda **kw: REAL_CLIENT(
                transport=httpx.MockTransport(self.upstream), **kw
            ),
        )
        self.mock.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.settings = {
            "base_url": "https://wiki.example.test/wiki",
            "personal_access_token": "private-test-pat",
            "verify_ssl": "on",
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.mock.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_folder_download_keeps_pages_out_of_bookmarks_and_searches_text(self):
        self.client.post("/bookmarks/settings/save/", data=self.settings)
        from unittest.mock import AsyncMock

        from app.core.database import connection

        page = {
            "id": "123",
            "title": "Guide",
            "body": {"view": {"value": "<p>azure deployment</p>"}},
        }
        with (
            patch(
                "app.bookmarks.confluence.resolved_content",
                new=AsyncMock(return_value=page),
            ),
            patch(
                "app.bookmarks.confluence.get",
                new=AsyncMock(
                    side_effect=[
                        {
                            "results": [{"id": "123"}],
                            "_links": {"next": "?start=1&limit=100"},
                        },
                        {"results": [{"id": "456"}], "_links": {}},
                    ]
                ),
            ) as listing,
        ):
            response = self.client.post(
                "/api/bookmarks/downloads",
                json={"folder_key": "test-space", "space_key": "CLOUD"},
            )
        self.assertEqual(response.status_code, 202)
        status = self.client.get("/api/bookmarks/downloads").json()[0]
        self.assertEqual((status["status"], status["count"]), ("completed", 2))
        self.assertEqual(listing.call_args_list[1].args[2]["start"], "1")
        matches = self.client.get(
            "/api/bookmarks/downloaded-search",
            params={"q": "azure", "fields": "content"},
        ).json()
        self.assertEqual(matches["total"], 2)
        self.assertEqual(
            self.client.get("/api/bookmarks/downloaded-search").json()["total"], 0
        )
        self.assertEqual(
            self.client.get(
                "/api/bookmarks/downloaded-search", params={"include_all": "true"}
            ).json()["total"],
            2,
        )
        self.assertEqual(
            self.client.get(
                "/api/bookmarks/downloaded-search",
                params={"include_all": "true", "q": "no-match"},
            ).json()["total"],
            0,
        )
        self.assertEqual(
            self.client.get(
                "/api/bookmarks/downloaded-search",
                params={"q": "azure", "fields": "title"},
            ).json()["total"],
            0,
        )
        with connection() as db:
            self.assertEqual(
                json.loads(
                    db.execute("SELECT payload FROM bookmark_workspace").fetchone()[0]
                )["bookmarks"],
                [],
            )
        with patch(
            "app.bookmarks.confluence.get",
            new=AsyncMock(side_effect=ValueError("failure")),
        ):
            self.client.post(
                "/api/bookmarks/downloads",
                json={"folder_key": "test-space", "space_key": "CLOUD"},
            )
        self.assertEqual(
            self.client.get("/api/bookmarks/downloads").json()[0]["status"], "failed"
        )
        self.assertEqual(
            self.client.get(
                "/api/bookmarks/downloaded-search", params={"q": "azure"}
            ).json()["total"],
            2,
        )

    def test_folder_title_resolves_parent_and_downloads_descendants(self):
        from unittest.mock import AsyncMock

        self.client.post("/bookmarks/settings/save/", data=self.settings)
        page = {
            "title": "Downloaded",
            "body": {"view": {"value": "<p>search text</p>"}},
        }
        with (
            patch(
                "app.bookmarks.confluence.resolved_content",
                new=AsyncMock(return_value=page),
            ),
            patch(
                "app.bookmarks.confluence.get",
                new=AsyncMock(
                    side_effect=[
                        {"results": [{"id": "123"}]},
                        {"results": [{"id": "456"}], "_links": {}},
                    ]
                ),
            ) as get,
        ):
            self.client.post(
                "/api/bookmarks/downloads",
                json={
                    "folder_key": "guides",
                    "space_key": "CLOUD",
                    "root_title": "Guides",
                },
            )
        self.assertEqual(get.call_args_list[0].args[2]["title"], "Guides")
        self.assertEqual(get.call_args_list[1].args[1], "content/123/descendant/page")
        self.assertEqual(
            self.client.get("/api/bookmarks/downloads").json()[0]["count"], 2
        )

    def test_folder_stars_persist_in_workspace(self):
        data = self.client.get("/api/bookmarks/workspace").json()
        data["starred_folders"] = ['folder-["Engineering","Guides"]']
        response = self.client.put("/api/bookmarks/workspace", json=data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/bookmarks/workspace").json()["starred_folders"],
            data["starred_folders"],
        )

    def upstream(self, request):
        self.calls.append(request)
        if self.code != 200:
            return httpx.Response(self.code, json={})
        if request.url.path.endswith("/user/current"):
            return httpx.Response(200, json={"name": "alice", "displayName": "Alice"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, json={"results": [{"id": "123"}]})
        return httpx.Response(
            200,
            json={
                "id": "123",
                "type": "page",
                "status": "current",
                "title": "AKS Guide",
                "space": {"key": "CLOUD", "name": "Cloud Engineering"},
                "history": {
                    "createdBy": {"displayName": "Alice"},
                    "createdDate": "2025-01-01T00:00:00Z",
                },
                "version": {
                    "number": 17,
                    "by": {"displayName": "Bob"},
                    "when": "2026-09-01T00:00:00Z",
                },
                "ancestors": [
                    {"id": "1", "title": "Home"},
                    {"id": "2", "title": "Platforms"},
                    {"id": "3", "title": "Azure"},
                ],
                "body": {
                    "view": {
                        "value": "<h1>AKS Guide</h1><p>AWS &amp; Azure</p><table><tr><td>Region</td><td>Ireland</td></tr></table><script>secretScript()</script>"
                    }
                },
            },
        )

    def configure(self):
        response = self.client.post("/bookmarks/settings/save/", data=self.settings)
        self.assertEqual(response.status_code, 200, response.text)

    def test_settings_encrypted_and_reused(self):
        self.configure()
        response = self.client.get("/bookmarks/settings/workspace/")
        self.assertNotIn("private-test-pat", response.text)
        from pathlib import Path

        self.assertNotIn(
            b"private-test-pat", Path(self.temp.name, "confluence.enc").read_bytes()
        )
        settings = {**self.settings, "personal_access_token": ""}
        self.assertEqual(
            self.client.post("/bookmarks/settings/test/", data=settings).status_code,
            200,
        )
        settings["base_url"] = "https://other.example.test"
        self.assertEqual(
            self.client.post("/bookmarks/settings/save/", data=settings).status_code,
            400,
        )

    def test_page_variants_text_and_breadcrumb(self):
        self.configure()
        for suffix in (
            "/pages/viewpage.action?pageId=123",
            "/spaces/CLOUD/pages/123/AKS",
            "/display/CLOUD/AKS+Guide",
            "/x/ewAAAA",
        ):
            response = self.client.post(
                "/api/bookmarks/resolve",
                json={"url": self.settings["base_url"] + suffix},
            )
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["breadcrumb"], ["Home", "Platforms", "Azure"])
            self.assertEqual(data["page_id"], "123")
            self.assertEqual(data["version"], 17)
            self.assertEqual(data["author"], "Alice")
            self.assertIn("AWS & Azure", data["contentText"])
            self.assertNotIn("secretScript", data["contentText"])
        self.assertTrue(
            all(
                r.headers["authorization"] == "Bearer private-test-pat"
                for r in self.calls
            )
        )

    def test_screenshot_confluence_urls_resolve_and_persist(self):
        # IDs and URL shapes from the supplied screenshots. All HTTP is mocked;
        # the placeholder origin deliberately avoids the internal company server.
        self.configure()
        examples = [
            (
                "817229951",
                "/spaces/SIB/pages/817229951/Akamai+Edge+Routing+RNTZ+Ingress+Architecture",
            ),
            ("2135277690", "/spaces/BEP/pages/2135277690/AWS+Cloud+Services"),
            ("1207786608", "/spaces/SIB/pages/1207786608/AWS+in+Mastercard+-+DRAFT"),
            (
                "382810275",
                "/spaces/DIGITALSD/pages/382810275/IDTS+Infrastructure+Design+Training+Series#IDTS:Infrastructure",
            ),
            ("2135277690", "/pages/viewpage.action?pageId=2135277690"),
        ]
        original = self.upstream

        def upstream(request):
            response = original(request)
            if "/rest/api/content/" in request.url.path:
                data = response.json()
                data["id"] = request.url.path.rsplit("/", 1)[1]
                return httpx.Response(200, json=data)
            return response

        self.upstream = upstream
        for index, (page_id, suffix) in enumerate(examples, 1):
            with self.subTest(url=suffix):
                url = self.settings["base_url"] + suffix
                result = self.client.post("/api/bookmarks/resolve", json={"url": url})
                self.assertEqual(result.status_code, 200, result.text)
                bookmark = result.json()
                self.assertEqual(bookmark["page_id"], page_id)
                self.assertEqual(bookmark["url"], url)
                self.assertIn("AWS & Azure", bookmark["contentText"])
                self.assertEqual(bookmark["breadcrumb"], ["Home", "Platforms", "Azure"])
                self.assertEqual(
                    self.calls[-1].url.path, f"/wiki/rest/api/content/{page_id}"
                )
                bookmark["id"] = index
                workspace = self.client.get("/api/bookmarks/workspace").json()
                workspace["bookmarks"].append(bookmark)
                workspace["notes"][str(index)] = "Sample notes: AWS and Azure"
                saved = self.client.put("/api/bookmarks/workspace", json=workspace)
                self.assertEqual(saved.status_code, 200, saved.text)
                loaded = self.client.get("/api/bookmarks/workspace").json()
                self.assertEqual(loaded["bookmarks"][-1]["url"], url)
                self.assertEqual(
                    loaded["notes"][str(index)], "Sample notes: AWS and Azure"
                )

    def test_url_html_fallback_and_redirect(self):
        self.configure()
        original = self.upstream

        def upstream(request):
            if request.url.path.endswith("/friendly"):
                return httpx.Response(302, headers={"Location": "/wiki/readable-page"})
            if request.url.path.endswith("/readable-page"):
                return httpx.Response(
                    200, text='<meta name="ajs-page-id" content="123">'
                )
            return original(request)

        self.upstream = upstream
        response = self.client.post(
            "/api/bookmarks/resolve",
            json={"url": self.settings["base_url"] + "/friendly"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["page_id"], "123")

    def test_canonical_link_and_varied_id_formats(self):
        self.configure()
        original = self.upstream

        def upstream(request):
            if request.url.path.endswith("/friendly"):
                return httpx.Response(
                    200,
                    text='<link rel="canonical" href="/wiki/pages/viewpage.action?pageId=123">',
                )
            return original(request)

        self.upstream = upstream
        for suffix in (
            "/friendly",
            "/read#pageId=123",
            "/read?content_id=123",
            "/read?ref=pageId%3D123",
        ):
            response = self.client.post(
                "/api/bookmarks/resolve",
                json={"url": self.settings["base_url"] + suffix},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["page_id"], "123")

    def test_long_numeric_candidate_is_validated(self):
        self.configure()
        original = self.upstream

        def upstream(request):
            if "/rest/api/content/" not in request.url.path:
                return httpx.Response(404)
            candidate = request.url.path.rsplit("/", 1)[-1]
            if candidate != "987654321":
                return httpx.Response(404)
            data = original(request).json()
            data["id"] = candidate
            return httpx.Response(200, json=data)

        self.upstream = upstream
        response = self.client.post(
            "/api/bookmarks/resolve",
            json={
                "url": self.settings["base_url"]
                + "/custom/11111111/link?value=987654321"
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["page_id"], "987654321")

    def test_external_redirect_never_receives_pat(self):
        self.configure()
        seen = []

        def upstream(request):
            seen.append(str(request.url))
            return httpx.Response(
                302, headers={"Location": "https://elsewhere.example.org/login"}
            )

        self.upstream = upstream
        response = self.client.post(
            "/api/bookmarks/resolve",
            json={"url": self.settings["base_url"] + "/friendly"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("private-test-pat", response.text)

    def test_web_domains_never_receive_pat(self):
        self.configure()
        count = len(self.calls)
        for url in (
            "https://example.org/docs",
            "https://wiki.example.test.evil.org/wiki/pages/123",
            "https://wiki.example.test/elsewhere",
        ):
            response = self.client.post("/api/bookmarks/resolve", json={"url": url})
            self.assertEqual(response.json()["sourceType"], "web")
        self.assertEqual(len(self.calls), count)
        for url in ("javascript:alert(1)", "https://user:password@example.org"):
            self.assertEqual(
                self.client.post(
                    "/api/bookmarks/resolve", json={"url": url}
                ).status_code,
                400,
            )

    def test_failed_page_fetch_is_explicit(self):
        self.configure()
        self.code = 404
        response = self.client.post(
            "/api/bookmarks/resolve",
            json={"url": self.settings["base_url"] + "/pages/123"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("/rest/api/content/123", response.text)
        self.assertNotIn("private-test-pat", response.text)

    def test_page_details_and_opens_survive_workspace_reload(self):
        self.configure()
        data = self.client.post(
            "/api/bookmarks/resolve",
            json={"url": self.settings["base_url"] + "/pages/123"},
        ).json()
        state = self.client.get("/api/bookmarks/workspace").json()
        state["bookmarks"] = [
            {**data, "id": 1, "views": 2, "lastViewed": 1780000000000}
        ]
        self.assertEqual(
            self.client.put("/api/bookmarks/workspace", json=state).status_code, 200
        )
        saved = self.client.get("/api/bookmarks/workspace").json()["bookmarks"][0]
        self.assertEqual(saved["views"], 2)
        self.assertEqual(saved["contentText"], data["contentText"])
        self.assertEqual(saved["ancestors"], data["ancestors"])
        self.assertNotIn("private-test-pat", json.dumps(saved))
