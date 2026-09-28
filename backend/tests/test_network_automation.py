"""Network Automation indexes JSON files with data isolated from PDFs and NAAS."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from app.core.library import library, supported_file
from fastapi.testclient import TestClient
from main import app

REAL_CLIENT = httpx.AsyncClient


class NetworkAutomationTests(unittest.TestCase):
    def test_file_selection(self):
        self.assertFalse(supported_file("devices.json"))
        token = library.set("network")
        try:
            for name in ["devices.json", "sites/Core.JSON", "README.md"]:
                self.assertTrue(supported_file(name))
            for name in ["config.yaml", "guide.pdf", "data.jsonl", "code.py"]:
                self.assertFalse(supported_file(name))
        finally:
            library.reset(token)

    def test_sync_and_isolation(self):
        def upstream(request):
            path = request.url.path
            if "/browse/" in path:
                return httpx.Response(
                    200,
                    json={
                        "children": {
                            "values": [
                                {"type": "FILE", "path": {"name": name}}
                                for name in [
                                    "devices.json",
                                    "README.md",
                                    "config.yaml",
                                    "ignore.pdf",
                                ]
                            ],
                            "isLastPage": True,
                        }
                    },
                )
            if path.endswith("/commits"):
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {
                                "id": "1",
                                "authorTimestamp": 1790000000000,
                                "author": {"displayName": "Tester"},
                            }
                        ],
                        "isLastPage": True,
                    },
                )
            if "/raw/" in path:
                return httpx.Response(200, content=b'{"hostname": "core-sw-01"}\n')
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
            self.assertEqual(
                client.get("/network-automation/bitbucket/workspace/").json()[
                    "settingsSaveUrl"
                ],
                "/network-automation/bitbucket/settings/save/",
            )
            response = client.post(
                "/network-automation/api/imports",
                json={"urls": [settings["base_url"] + "/scm/net/devices.git"]},
            )
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()
            for _ in range(200):
                job = client.get("/network-automation/api/jobs/" + job["id"]).json()
                if job["status"] not in ("running", "queued"):
                    break
                time.sleep(0.01)
            self.assertEqual(job["status"], "succeeded", job)
            workspace = client.get("/network-automation/api/workspace").json()
            self.assertEqual(
                {d["name"] for d in workspace["documents"]},
                {"devices.json", "README.md"},
            )
            self.assertEqual(client.get("/api/workspace").json()["documents"], [])
            self.assertEqual(client.get("/naas/api/workspace").json()["documents"], [])
            doc = next(d for d in workspace["documents"] if d["name"] == "devices.json")
            self.assertIn(
                doc["id"],
                client.post(
                    "/network-automation/api/search/matches",
                    json={"q": "core-sw-01", "fields": ["content"]},
                ).json()["ids"],
            )
            downloaded = client.get(
                f"/network-automation/api/document/{doc['id']}/commits/download?commit_id=1"
            )
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertIn(".json", downloaded.headers["content-disposition"])
            rejected = client.post(
                "/network-automation/api/imports",
                json={
                    "urls": [
                        settings["base_url"]
                        + "/projects/NET/repos/devices/browse/config.yaml"
                    ]
                },
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("JSON", rejected.text)
            self.assertIn("Network Automation", client.get("/network-automation/").text)
