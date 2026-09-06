import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
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

    def test_project_crawl_visits_every_repo_across_pages_despite_failure(self):
        original = self.upstream
        browsed = set()
        slugs = [f"repo-{number}" for number in range(7)]

        def paginated(request):
            path = request.url.path
            if path.endswith("/repos"):
                start = int(request.url.params.get("start", 0))
                return httpx.Response(
                    200,
                    json={
                        "values": [{"slug": slug} for slug in slugs[start : start + 3]],
                        "isLastPage": start + 3 >= len(slugs),
                        "nextPageStart": start + 3,
                    },
                )
            if "/browse/" in path:
                slug = path.split("/repos/")[1].split("/")[0]
                browsed.add(slug)
                if slug == "repo-0":
                    return httpx.Response(403)
                if slug == "repo-1":
                    return httpx.Response(
                        200,
                        json={
                            "children": {
                                "values": [],
                                "isLastPage": True,
                            }
                        },
                    )
            return original(request)

        self.upstream = paginated
        job = self.crawl()
        self.assertEqual(browsed, set(slugs))
        self.assertEqual(job["repositories"], 7)
        self.assertEqual(job["repositories_done"], 7)
        self.assertEqual(job["repositories_succeeded"], 6)
        self.assertEqual(job["repositories_failed"], 1)
        self.assertEqual(job["status"], "succeeded_with_errors")
        self.assertEqual((job["found"], job["processed"], job["new"]), (10, 10, 10))
        statuses = {r["repo"]: r["status"] for r in job["repository_statuses"].values()}
        self.assertEqual(
            statuses,
            {slug: "failed" if slug == "repo-0" else "succeeded" for slug in slugs},
        )
        with connection() as db:
            scanned = {
                r["repo"]
                for r in db.execute(
                    "SELECT repo FROM repositories WHERE last_scanned IS NOT NULL"
                )
            }
            indexed = {
                r["repo"] for r in db.execute("SELECT DISTINCT repo FROM documents")
            }
        self.assertEqual(scanned, set(slugs[1:]))
        self.assertEqual(indexed, set(slugs[2:]))

    def test_hard_retry_clears_index_and_downloads_unchanged_pdfs(self):
        self.crawl()
        with connection() as db:
            db.execute("UPDATE documents SET notes='old note', open_count=9")
            db.execute(
                "INSERT INTO failed_documents(repository_id,path,error,last_attempt) SELECT id,'stale.pdf','old failure','now' FROM repositories"
            )
            db.execute(
                "INSERT INTO tracked_projects(project_url,server,project) VALUES('https://other.test/projects/OTHER','https://other.test','OTHER')"
            )
        preview = self.client.get("/api/crawl/hard-retry/preview").json()
        self.assertEqual(
            (
                preview["documents"],
                preview["failed_documents"],
                preview["repositories"],
            ),
            (4, 2, 2),
        )
        self.assertEqual([p["project"] for p in preview["projects"]], ["DEMO"])
        denied = self.client.post("/api/crawl/hard-retry", json={"confirmation": ""})
        self.assertEqual(denied.status_code, 400)
        with connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 4
            )
        original = self.upstream
        checked_empty = []
        downloads = []

        def inspect(request):
            if request.url.path.endswith("/repos") and not checked_empty:
                with connection() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
                    )
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM failed_documents").fetchone()[
                            0
                        ],
                        0,
                    )
                checked_empty.append(True)
            if "/raw/" in request.url.path:
                downloads.append(request.url.path)
            return original(request)

        self.upstream = inspect
        response = self.client.post(
            "/api/crawl/hard-retry", json={"confirmation": "HARD RETRY"}
        )
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        for _ in range(300):
            job = self.client.get(f"/api/jobs/{job['id']}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertTrue(job["hard_retry"])
        self.assertTrue(checked_empty)
        self.assertEqual(len(downloads), 4)
        self.assertEqual(
            (job["new"], job["unchanged"], job["repositories_succeeded"]), (4, 0, 2)
        )
        with closing(sqlite3.connect(job["backup_path"])) as backup:
            self.assertEqual(
                backup.execute(
                    "SELECT COUNT(*) FROM documents WHERE notes='old note'"
                ).fetchone()[0],
                4,
            )
        with connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM documents WHERE notes='' AND open_count=0"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM tracked_projects").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM bookmark_workspace").fetchone()[0], 1
            )
        self.assertEqual(
            len(self.client.get("/api/search", params={"q": "azure"}).json()), 4
        )

    def test_alphabetical_sequential_import_and_hard_retry(self):
        original = self.upstream
        from app.pdfs.crawler import crawl_repository

        active = 0
        maximum = 0
        order = []
        browsed = []

        def unordered(request):
            if request.url.path.endswith("/repos"):
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {"slug": slug} for slug in ["zebra", "Beta", "alpha"]
                        ],
                        "isLastPage": True,
                    },
                )
            if "/browse/" in request.url.path:
                slug = request.url.path.split("/repos/")[1].split("/")[0]
                if not browsed or browsed[-1] != slug:
                    browsed.append(slug)
            return original(request)

        async def tracked(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            order.append(args[2])
            try:
                await asyncio.sleep(0.01)
                return await crawl_repository(*args)
            finally:
                active -= 1

        self.upstream = unordered
        with patch("app.pdfs.jobs.crawl_repository", tracked):
            for endpoint, body in [
                (
                    "/api/imports",
                    {"urls": [self.config["base_url"] + "/projects/DEMO"]},
                ),
                ("/api/crawl/hard-retry", {"confirmation": "HARD RETRY"}),
            ]:
                order.clear()
                browsed.clear()
                response = self.client.post(endpoint, json=body)
                self.assertEqual(response.status_code, 202, response.text)
                job = response.json()
                for _ in range(300):
                    job = self.client.get(f"/api/jobs/{job['id']}").json()
                    if job["status"] not in ("queued", "running"):
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "succeeded", job)
                self.assertEqual(order, ["alpha", "Beta", "zebra"])
                self.assertEqual(browsed, order)
                self.assertEqual(maximum, 1)

    def test_unreadable_folder_does_not_discard_or_skip_accessible_pdfs(self):
        original = self.upstream

        def folder_error(request):
            path = request.url.path
            if path.endswith("/browse/"):
                return httpx.Response(
                    200,
                    json={
                        "children": {
                            "values": [
                                {"type": "FILE", "path": {"name": "root.pdf"}},
                                {"type": "DIRECTORY", "path": {"name": "nested"}},
                                {"type": "DIRECTORY", "path": {"name": "broken"}},
                            ],
                            "isLastPage": True,
                        }
                    },
                )
            if path.endswith("/browse/broken"):
                return httpx.Response(404)
            return original(request)

        self.upstream = folder_error
        job = self.crawl()
        self.assertEqual(job["status"], "succeeded_with_errors", job)
        self.assertEqual((job["found"], job["processed"], job["new"]), (4, 4, 4))
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["repositories_failed"], 2)
        self.assertEqual(len(job["folder_failures"]), 2)
        self.assertTrue(
            all(
                f["path"] == "broken" and "404" in f["error"]
                for f in job["folder_failures"]
            )
        )
        with connection() as db:
            paths = {row["path"] for row in db.execute("SELECT path FROM documents")}
        self.assertEqual(paths, {"root.pdf", "nested/first.pdf"})

    def test_one_failed_pdf_saves_identifiers_and_others_continue(self):
        original = self.upstream

        def one_failure(request):
            if "/repos/one/raw/nested/first.pdf" in request.url.path:
                return httpx.Response(404)
            return original(request)

        self.upstream = one_failure
        job = self.crawl()
        self.assertEqual((job["new"], job["failed"], job["processed"]), (3, 1, 4))
        failures = self.client.get("/api/failed").json()
        self.assertEqual(len(failures), 1)
        failure = failures[0]
        self.assertEqual(failure["pdf_name"], "first.pdf")
        self.assertEqual(
            failure["url"],
            self.config["base_url"]
            + "/projects/DEMO/repos/one/browse/nested/first.pdf",
        )
        self.assertEqual(failure["attempts"], 2)
        with connection() as db:
            saved = db.execute("SELECT pdf_name,url FROM failed_documents").fetchone()
            self.assertEqual(
                dict(saved), {key: failure[key] for key in ("pdf_name", "url")}
            )
            # Simulate an older failure without these identifiers.
            db.execute("UPDATE failed_documents SET pdf_name='',url=''")
        from app.core.database import initialize

        initialize()
        restored = self.client.get("/api/failed").json()[0]
        self.assertEqual(restored["url"], failure["url"])
        self.assertEqual(restored["pdf_name"], "first.pdf")

    def test_bulk_delete_is_atomic_and_cascades_selected_repo_data(self):
        self.crawl()
        with connection() as db:
            ids = [
                r["id"] for r in db.execute("SELECT id FROM repositories ORDER BY id")
            ]
            db.execute(
                "INSERT INTO failed_documents(repository_id,path,error,last_attempt) SELECT id,'bad.pdf','failed','now' FROM repositories"
            )
        preview = self.client.post(
            "/api/repositories/delete-preview", json={"repository_ids": ids}
        ).json()
        self.assertEqual((preview["documents"], preview["failed_documents"]), (4, 2))
        self.assertEqual(
            self.client.post(
                "/api/repositories/delete",
                json={"repository_ids": ids, "confirmation": "wrong"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/repositories/delete",
                json={"repository_ids": [ids[0], 9999], "confirmation": "delete all"},
            ).status_code,
            404,
        )
        with connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 4
            )
        self.assertEqual(
            self.client.post(
                "/api/repositories/delete",
                json={"repository_ids": [ids[0]], "confirmation": "delete all"},
            ).status_code,
            200,
        )
        self.assertEqual(
            len(self.client.get("/api/search", params={"q": "azure"}).json()), 2
        )
        self.assertEqual(len(self.client.get("/api/failed").json()), 1)
        self.assertEqual(
            self.client.post(
                "/api/repositories/delete",
                json={"repository_ids": [ids[1]], "confirmation": "delete all"},
            ).json()["deleted"],
            1,
        )
        with connection() as db:
            for table in ("repositories", "documents", "failed_documents"):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM tracked_projects").fetchone()[0], 1
            )
        self.assertEqual(
            self.client.get("/api/search", params={"q": "azure"}).json(), []
        )

    def test_import_pdf_and_project(self):
        base = self.config["base_url"] + "/projects/TEST"
        for url, expected in [(base + "/repos/one/browse/manual.pdf", 1), (base, 5)]:
            response = self.client.post("/api/imports", json={"urls": [url]})
            self.assertEqual(response.status_code, 202, response.text)
            identifier = response.json()["id"]
            for _ in range(200):
                job = self.client.get(f"/api/jobs/{identifier}").json()
                if job["status"] not in ("running", "queued"):
                    break
                time.sleep(0.01)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(
                len(self.client.get("/api/search", params={"q": "azure"}).json()),
                expected,
            )
        response = self.client.post(
            "/api/imports", json={"urls": ["https://wrong.test/projects/TEST"]}
        )
        self.assertEqual(response.status_code, 400)

    def test_temporary_pdf_cleanup(self):
        from app.pdfs.crawler import extract

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("tempfile.tempdir", directory),
        ):
            with self.assertRaises(RuntimeError):
                extract(b"not a pdf")
            self.assertEqual(list(Path(directory).iterdir()), [])
            with pymupdf.open() as document:
                document.new_page().insert_text((30, 30), "cleanup test")
                self.assertEqual(extract(document.tobytes())[0], 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_incremental_crawl_fts_and_deletion(self):
        job = self.crawl()
        self.assertEqual((job["status"], job["new"]), ("succeeded", 4), job)
        self.assertTrue(job["bitbucket_connected"])
        self.assertEqual(job["repositories_succeeded"], 2)
        self.assertEqual(job["repositories"], 2)
        self.assertEqual(
            {r["status"] for r in job["repository_statuses"].values()}, {"succeeded"}
        )
        self.assertEqual(
            {r["repo"] for r in job["repository_statuses"].values()}, {"one", "two"}
        )
        self.assertTrue(job["discovery_complete"])
        self.assertEqual((job["processed"], job["found"]), (4, 4))
        workspace = self.client.get("/api/workspace").json()
        saved = workspace["documents"][0]
        detail = self.client.get(f"/api/document/{saved['id']}").json()
        self.assertEqual(saved["pageCount"], detail["page_count"])
        self.assertEqual(saved["fileSize"], detail["file_size"])
        self.assertEqual(saved["commitId"], detail["commit_id"])
        self.assertEqual(saved["project"], detail["project"])
        self.assertIn("azure", detail["pdf_text"])
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
        self.client.post("/api/settings", json={**self.config, "max_workers": 10})
        self.assertEqual(self.client.get("/api/settings").json()["max_workers"], 1)
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

    def test_automatic_retry_recovers_counts_once(self):
        original = self.upstream
        downloads = {}

        def transient(request):
            if "/raw/" in request.url.path:
                key = request.url.path
                downloads[key] = downloads.get(key, 0) + 1
                if downloads[key] == 1:
                    return httpx.Response(403)
            return original(request)

        self.upstream = transient
        job = self.crawl()
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual((job["found"], job["processed"], job["new"]), (4, 4, 4))
        self.assertEqual(
            (job["retry_total"], job["retry_recovered"], job["failed"]), (4, 4, 0)
        )
        self.assertEqual(
            (job["repositories_succeeded"], job["repositories_failed"]), (2, 0)
        )
        self.assertEqual(
            {r["status"] for r in job["repository_statuses"].values()}, {"succeeded"}
        )
        self.assertTrue(all(count == 2 for count in downloads.values()))
        self.assertEqual(self.client.get("/api/failed").json(), [])

    def test_failure_and_retry(self):
        self.fail = True
        job = self.crawl()
        self.assertEqual((job["status"], job["failed"]), ("succeeded_with_errors", 4))
        self.assertEqual(
            {r["status"] for r in job["repository_statuses"].values()}, {"failed"}
        )
        failures = self.client.get("/api/failed").json()
        self.assertEqual(len(failures), 4)
        self.assertTrue(all(row["attempts"] == 2 for row in failures))
        self.assertEqual(failures[0]["error"], "Bitbucket returned HTTP 403.")
        self.assertTrue(failures[0]["repo"])
        self.assertTrue(failures[0]["project"])
        for succeeds in (False, True):
            self.fail = not succeeds
            before = len(self.calls)
            response = self.client.post("/api/failed/retry")
            self.assertEqual(response.status_code, 202, response.text)
            identifier = response.json()["id"]
            for _ in range(200):
                job = self.client.get(f"/api/jobs/{identifier}").json()
                if job["status"] not in ("queued", "running"):
                    break
                time.sleep(0.01)
            self.assertEqual(job["processed"], 4)
            self.assertFalse(any("/browse/" in url for url in self.calls[before:]))
            if not succeeds:
                self.assertTrue(
                    all(
                        row["attempts"] == 3
                        for row in self.client.get("/api/failed").json()
                    )
                )
            else:
                self.assertEqual(job["new"], 4)
                self.assertEqual(self.client.get("/api/failed").json(), [])
        self.assertEqual(self.client.post("/api/failed/retry").status_code, 400)

    def test_repository_queue_and_active_status_survive_polling(self):
        async def delayed(*args, **kwargs):
            await asyncio.sleep(30)
            yield "example.pdf"

        self.client.post("/api/settings", json={**self.config, "max_workers": 1})
        self.client.post(
            "/api/project",
            json={"project_url": self.config["base_url"] + "/projects/DEMO"},
        )
        with patch("app.pdfs.jobs.discover_pdfs", delayed):
            job = self.client.post("/api/crawl", json={}).json()
            for _ in range(100):
                current = self.client.get(f"/api/jobs/{job['id']}").json()
                statuses = {
                    r["status"] for r in current["repository_statuses"].values()
                }
                if "scanning" in statuses:
                    break
                time.sleep(0.01)
            self.assertEqual(statuses, {"scanning", "queued"})
            latest = self.client.get("/api/jobs/latest").json()["job"]
            self.assertEqual(
                latest["repository_statuses"], current["repository_statuses"]
            )
            stopped = self.client.post(f"/api/jobs/{job['id']}/cancel").json()
            self.assertEqual(
                {r["status"] for r in stopped["repository_statuses"].values()},
                {"cancelled"},
            )

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
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            recovered = self.client.get("/api/jobs/latest").json()["job"]
            self.assertEqual(recovered["id"], job["id"])
            self.assertIn(recovered["status"], ("queued", "running"))
            self.assertEqual(
                self.client.post(
                    "/api/crawl/hard-retry", json={"confirmation": "HARD RETRY"}
                ).status_code,
                409,
            )
            self.assertEqual(self.client.post("/api/crawl", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/failed/retry").status_code, 409)
            self.assertEqual(
                self.client.post("/api/jobs/" + job["id"] + "/cancel").json()["status"],
                "cancelled",
            )
