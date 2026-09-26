"""NAAS uses the complete explorer API with isolated data and text indexing."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from app.core.library import extract_text, library, supported_file
from app.pdfs.client import BitbucketError
from fastapi.testclient import TestClient
from main import app

REAL_CLIENT = httpx.AsyncClient


class NaasTests(unittest.TestCase):
    def test_file_selection_and_text(self):
        self.assertTrue(supported_file("guide.pdf"))
        self.assertFalse(supported_file("config.yml"))
        token = library.set("naas")
        try:
            for name in [
                "config.yaml",
                "config.YML",
                "docs/README.md",
                "README",
                "Readme.rst",
            ]:
                self.assertTrue(supported_file(name))
            for name in ["guide.pdf", "code.py", "readme.png", "notreadme.md"]:
                self.assertFalse(supported_file(name))
            self.assertEqual(extract_text(b"key: value\n"), (1, "key: value\n"))
            self.assertEqual(extract_text("hello".encode("utf-16")), (1, "hello"))
            with self.assertRaises(BitbucketError):
                extract_text(b"\x00\xff")
        finally:
            library.reset(token)

    def test_sync_search_history_notes_and_isolation(self):
        version = 1
        calls = []
        empty = False

        def upstream(request):
            path = request.url.path
            calls.append(path)
            if "/browse/" in path and empty:
                return httpx.Response(
                    200, json={"children": {"values": [], "isLastPage": True}}
                )
            if "/browse/" in path:
                return httpx.Response(
                    200,
                    json={
                        "children": {
                            "values": [
                                {"type": "FILE", "path": {"name": name}}
                                for name in [
                                    "config.yaml",
                                    "README.md",
                                    "ignore.pdf",
                                    "code.py",
                                ]
                            ],
                            "isLastPage": True,
                        }
                    },
                )
            if path.endswith("/compare/changes"):
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {"type": "MODIFY", "path": {"toString": "config.yaml"}},
                            {"type": "DELETE", "path": {"toString": "README.md"}},
                            {"type": "ADD", "path": {"toString": "new.yml"}},
                        ],
                        "isLastPage": True,
                    },
                )
            if path.endswith("/commits"):
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {
                                "id": str(version),
                                "authorTimestamp": 1790000000000,
                                "author": {"displayName": "Tester"},
                            }
                        ],
                        "isLastPage": True,
                    },
                )
            if "/raw/" in path:
                return httpx.Response(
                    200, content=f"feature: version{version}\n".encode()
                )
            return httpx.Response(200, json={"values": [], "isLastPage": True})

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(
                os.environ,
                {
                    "OWL_DB_PATH": folder + "/owl.db",
                    "OWL_CONFIG_DIR": folder + "/config",
                    "OWL_LOG_DIR": folder + "/logs",
                },
            ),
            patch(
                "app.pdfs.client.httpx.AsyncClient",
                side_effect=lambda **kw: REAL_CLIENT(
                    transport=httpx.MockTransport(upstream), **kw
                ),
            ),
            TestClient(app) as client,
        ):
            settings = {
                "base_url": "https://bitbucket.example.test",
                "username": "test",
                "token": "secret",
            }
            self.assertEqual(
                client.post("/api/settings", json=settings).status_code, 200
            )
            # NAAS can use the existing connection, while any saved override is independent.
            self.assertEqual(
                client.get("/naas/bitbucket/workspace/").json()["credentials"][0][
                    "username"
                ],
                "test",
            )
            self.assertEqual(
                client.get("/naas/bitbucket/workspace/").json()["settingsSaveUrl"],
                "/naas/bitbucket/settings/save/",
            )

            def finish(response):
                self.assertEqual(response.status_code, 202, response.text)
                job = response.json()
                for _ in range(200):
                    job = client.get("/naas/api/jobs/" + job["id"]).json()
                    if job["status"] not in ("running", "queued"):
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "succeeded", job)
                return job

            finish(
                client.post(
                    "/naas/api/imports",
                    json={"urls": [settings["base_url"] + "/scm/demo/config.git"]},
                )
            )
            workspace = client.get("/naas/api/workspace").json()
            self.assertEqual(
                {d["name"] for d in workspace["documents"]},
                {"config.yaml", "README.md"},
            )
            self.assertEqual(client.get("/api/workspace").json()["documents"], [])
            self.assertEqual(client.get("/api/jobs/latest").json(), {"job": None})
            doc = next(d for d in workspace["documents"] if d["name"] == "config.yaml")
            identifier = doc["id"]
            base = "/naas/api/document/" + str(identifier)
            self.assertEqual(
                client.patch(
                    base + "/notes", json={"notes": "My configuration"}
                ).status_code,
                200,
            )
            self.assertEqual(client.post(base + "/open").json()["open_count"], 1)
            self.assertIn(
                identifier,
                client.post(
                    "/naas/api/search/matches",
                    json={"q": "version1", "fields": ["content"]},
                ).json()["ids"],
            )
            downloaded = client.get(base + "/commits/download?commit_id=1")
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertIn(".yaml", downloaded.headers["content-disposition"])
            self.assertIn("text/plain", downloaded.headers["content-type"])
            calls.clear()
            finish(client.post("/naas/api/crawl", json={}))
            self.assertFalse(any("/raw/" in path for path in calls))
            version = 2
            calls.clear()
            finish(client.post("/naas/api/crawl", json={}))
            self.assertFalse(any(path.endswith("/repos") for path in calls), calls)
            workspace = client.get("/naas/api/workspace").json()
            self.assertEqual(
                {d["name"] for d in workspace["documents"]}, {"config.yaml", "new.yml"}
            )
            record = client.get(base).json()
            self.assertEqual(record["notes"], "My configuration")
            self.assertEqual(record["open_count"], 1)
            self.assertIn("version2", record["pdf_text"])
            self.assertEqual(
                client.post(
                    "/naas/api/imports",
                    json={"urls": [settings["base_url"] + "/projects/OTHER"]},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/naas/api/settings", json={**settings, "username": "naas-user"}
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get("/bitbucket/workspace/").json()["credentials"][0][
                    "username"
                ],
                "test",
            )
            empty = True
            calls.clear()
            finish(
                client.post(
                    "/naas/api/crawl/hard-retry", json={"confirmation": "HARD RETRY"}
                )
            )
            empty_workspace = client.get("/naas/api/workspace").json()
            self.assertEqual(empty_workspace["documents"], [])
            self.assertEqual(len(empty_workspace["projects"][0]["repos"]), 1)
            self.assertFalse(any(path.endswith("/repos") for path in calls))
            self.assertIn("NAAS Update", client.get("/naas/").text)
            self.assertIn("Bitbucket PDF Explorer", client.get("/bitbucket/").text)
