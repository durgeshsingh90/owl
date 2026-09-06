import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf
from app.core.database import connection
from fastapi.testclient import TestClient
from main import app

REAL_CLIENT = httpx.AsyncClient


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "OWL_DB_PATH": self.temp.name + "/test.db",
                "OWL_LOG_DIR": self.temp.name + "/logs",
                "OWL_CONFIG_DIR": self.temp.name + "/config",
            },
        )
        self.env.start()
        self.version = 1
        self.fail = False
        self.calls = []
        self.patch = patch(
            "app.pdfs.client.httpx.AsyncClient",
            side_effect=lambda **kw: REAL_CLIENT(
                transport=httpx.MockTransport(self.upstream), **kw
            ),
        )
        self.patch.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.config = {
            "base_url": "https://bitbucket.example.test/stash",
            "username": "tester",
            "token": "test-secret",
        }
        self.assertEqual(
            self.client.post("/api/settings", json=self.config).status_code, 200
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_connection_diagnostics(self):
        import socket

        from app.core.config import Settings
        from app.pdfs.client import BitbucketClient

        normalized = Settings(
            **{**self.config, "base_url": self.config["base_url"] + "/rest/api/1.0"}
        )
        self.assertEqual(normalized.base_url, self.config["base_url"])

        def dns_failure(request):
            try:
                raise socket.gaierror(11001, "secret-do-not-log")
            except socket.gaierror as cause:
                raise httpx.ConnectError(
                    "test-secret secret-do-not-log", request=request
                ) from cause

        async def fail_without_delays():
            client = BitbucketClient(normalized)
            await client.close()
            client.client = REAL_CLIENT(transport=httpx.MockTransport(dns_failure))
            try:
                await client.request("/projects")
            finally:
                await client.close()

        from unittest.mock import AsyncMock

        with (
            patch("app.pdfs.client.asyncio.sleep", new_callable=AsyncMock),
            self.assertRaisesRegex(RuntimeError, "hostname could not be resolved"),
        ):
            asyncio.run(fail_without_delays())
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["x-request-id"])
        logs = Path(self.temp.name, "logs/backend.log").read_text()
        self.assertIn("bitbucket.transport_failed", logs)
        self.assertIn("11001", logs)
        self.assertIn(response.headers["x-request-id"], logs)
        self.assertNotIn("test-secret", logs)
        self.assertNotIn("secret-do-not-log", logs)

    def upstream(self, r):
        self.calls.append(str(r.url))
        path = r.url.path
        start = int(r.url.params.get("start", 0))
        if path.endswith("/projects"):
            return httpx.Response(200, json={"values": []})
        if path.endswith("/repos"):
            return httpx.Response(
                200,
                json={
                    "values": [{"slug": "one" if start == 0 else "two"}],
                    "isLastPage": start != 0,
                    "nextPageStart": 7,
                },
            )
        if "/browse/" in path:
            nested = path.split("/browse/")[1]
            item = {
                "type": "FILE",
                "path": {"name": "second.pdf" if start else "first.pdf"},
            }
            if not nested and not start:
                item = {"type": "DIRECTORY", "path": {"name": "nested"}}
            return httpx.Response(
                200,
                json={
                    "children": {
                        "values": [item],
                        "isLastPage": bool(nested or start),
                        "nextPageStart": 11,
                    }
                },
            )
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "id": str(self.version),
                            "authorTimestamp": 1720000000000,
                            "author": {"displayName": "Test Writer"},
                        }
                    ]
                },
            )
        if "/raw/" in path:
            if self.fail:
                return httpx.Response(403)
            with pymupdf.open() as doc:
                doc.new_page().insert_text(
                    (30, 30), "azure aws" if self.version == 1 else "kafka changed"
                )
                return httpx.Response(200, content=doc.tobytes())
        return httpx.Response(404)

    def crawl(self):
        project = self.client.post(
            "/api/project",
            json={"project_url": self.config["base_url"] + "/projects/DEMO"},
        ).json()
        response = self.client.post("/api/crawl", json={"project_ids": [project["id"]]})
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        for _ in range(300):
            job = self.client.get("/api/jobs/" + job["id"]).json()
            if job["status"] not in ("running", "queued"):
                return job
            time.sleep(0.01)
        self.fail("Timed out")

    def test_incremental_crawl_fts_and_deletion(self):
        job = self.crawl()
        self.assertEqual((job["status"], job["new"]), ("succeeded", 4), job)
        self.assertTrue(any("start=7" in url for url in self.calls))
        self.assertTrue(any("start=11" in url for url in self.calls))
        rows = self.client.get("/api/search", params={"q": "+azure +aws"}).json()
        self.assertEqual(len(rows), 4)
        identity = rows[0]["id"]
        self.client.patch(f"/api/document/{identity}/notes", json={"notes": "Keep me"})
        self.client.post(f"/api/document/{identity}/open")
        before = sum("/raw/" in url for url in self.calls)
        self.assertEqual(self.crawl()["unchanged"], 4)
        self.assertEqual(before, sum("/raw/" in url for url in self.calls))
        self.version = 2
        self.assertEqual(self.crawl()["updated"], 4)
        self.assertEqual(
            self.client.get("/api/search", params={"q": "azure"}).json(), []
        )
        self.assertEqual(
            len(self.client.get("/api/search", params={"q": "kafka"}).json()), 4
        )
        detail = self.client.get(f"/api/document/{identity}").json()
        self.assertEqual((detail["notes"], detail["open_count"]), ("Keep me", 1))
        repo = rows[0]["repository_id"]
        self.assertEqual(
            self.client.request(
                "DELETE",
                f"/api/repositories/{repo}",
                json={"confirmation": "DELETE ALL"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.request(
                "DELETE",
                f"/api/repositories/{repo}",
                json={"confirmation": "delete all"},
            ).status_code,
            200,
        )
        self.assertEqual(
            len(self.client.get("/api/search", params={"q": "kafka"}).json()), 2
        )
        with connection() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.execute(
                "INSERT INTO documents_fts(documents_fts) VALUES('integrity-check')"
            )

    def test_settings_validation_and_security(self):
        self.assertNotIn("test-secret", self.client.get("/api/settings").text)
        self.assertNotIn(
            b"test-secret", Path(self.temp.name + "/config/settings.enc").read_bytes()
        )
        self.assertEqual(self.client.post("/api/connection/test").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/project", json={"project_url": "https://evil.test/projects/X"}
            ).status_code,
            400,
        )
        response = self.client.post(
            "/api/settings", json={**self.config, "max_workers": 0}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("test-secret", response.text)
        self.assertEqual(
            self.client.post(
                "/api/settings",
                json=self.config,
                headers={"Origin": "https://evil.test"},
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/document/999").status_code, 404)
        for q in ['"', "+", "OR", "a:b", "(", '"azure aws"']:
            self.assertEqual(
                self.client.get("/api/search", params={"q": q}).status_code, 200
            )
        self.assertTrue(self.client.get("/bitbucket/workspace/").json()["ok"])
        self.assertEqual(
            self.client.post(
                "/bitbucket/settings/test/",
                data={
                    "base_url": self.config["base_url"],
                    "username": "tester",
                    "verify_ssl": "on",
                },
            ).status_code,
            200,
        )

    def test_settings_always_disable_ssl_verification(self):
        form = {
            "base_url": self.config["base_url"] + "/rest/api/1.0",
            "username": "tester",
            "access_token": "test-secret",
        }
        from app.core.config import Settings

        self.assertFalse(Settings(**self.config, verify_ssl=True).verify_ssl)
        self.assertFalse(Settings(**self.config).verify_ssl)
        for verify in (False, True):
            if verify:
                form["verify_ssl"] = "on"
            else:
                form.pop("verify_ssl", None)
            self.assertEqual(
                self.client.post("/bitbucket/settings/save/", data=form).status_code,
                200,
            )
            self.assertEqual(
                self.client.get("/bitbucket/workspace/").json()["credentials"][0][
                    "verifySsl"
                ],
                False,
            )
            with patch("app.api.compat.test_value") as test:
                test.return_value = {"ok": True}
                retry = {k: v for k, v in form.items() if k != "access_token"}
                self.assertEqual(
                    self.client.post(
                        "/bitbucket/settings/test/", data=retry
                    ).status_code,
                    200,
                )
                self.assertFalse(test.call_args.args[0].verify_ssl)

    def test_failure_and_retry(self):
        self.fail = True
        job = self.crawl()
        self.assertEqual((job["status"], job["failed"]), ("succeeded_with_errors", 4))
        self.assertEqual(len(self.client.get("/api/failed").json()), 4)
        self.fail = False
        self.assertEqual(self.crawl()["new"], 4)
        self.assertEqual(self.client.get("/api/failed").json(), [])

    def test_cancel_and_single_job(self):
        async def delayed(*args, **kwargs):
            await asyncio.sleep(30)
            yield {}

        with patch("app.pdfs.client.BitbucketClient.pages", delayed):
            self.client.post(
                "/api/project",
                json={"project_url": self.config["base_url"] + "/projects/DEMO"},
            )
            job = self.client.post("/api/crawl", json={}).json()
            self.assertEqual(self.client.post("/api/crawl", json={}).status_code, 409)
            self.assertEqual(
                self.client.post("/api/jobs/" + job["id"] + "/cancel").json()["status"],
                "cancelled",
            )
