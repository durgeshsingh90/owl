"""Home: storage per app and refreshing every app at once."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app


class HomeTests(unittest.TestCase):
    def test_storage_by_app_and_refresh_all(self):
        from app.core.database import connection

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/owl.db", "OWL_CONFIG_DIR": folder + "/config"}),
            TestClient(app) as client,
        ):
            with connection() as db:
                db.execute("INSERT INTO tracked_projects(id,project_url,server,project) VALUES(1,'u','s','P')")
                db.execute("INSERT INTO repositories(id,project_id,repo,name) VALUES(1,1,'r','r')")
                db.execute(
                    "INSERT INTO documents(repository_id,project,repo,pdf_name,path,url,file_size,page_count,"
                    "pdf_hash,pdf_text,added_at,updated_at,last_scanned) VALUES(1,'P','r','a.pdf','a.pdf','u',1,1,'h',?,'x','x','x')",
                    ("pdf text " * 50000,),
                )
                db.execute("INSERT INTO compare_shares(token,payload,created_at,expires_at) VALUES('t',?,0,0)", ("x" * 1000,))
                db.execute("INSERT INTO bitbucket_sync_schedule(server,next_attempt) VALUES('s','2099-01-01T00:00:00+00:00')")
                db.execute("UPDATE bookmark_refresh_schedule SET next_run=9999999999")
            storage = client.get("/api/home/storage?fresh=true").json()
            sizes = {item["app"]: item["bytes"] for item in storage["apps"]}
            self.assertGreater(sizes["bitbucket"], 400000)
            self.assertGreater(sizes["bitbucket"], sizes["compare"])
            self.assertLessEqual(sum(sizes.values()), storage["total_bytes"])
            self.assertEqual({file["name"] for file in storage["files"]} & {"owl.db"}, {"owl.db"})
            result = client.post("/api/home/refresh-all").json()["started"]
            self.assertTrue(result["bitbucket"])
            self.assertTrue(result["bookmarks"])
            with connection() as db:
                due = db.execute("SELECT next_attempt FROM bitbucket_sync_schedule").fetchone()[0]
                self.assertLess(due, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 5)))
                self.assertEqual(db.execute("SELECT next_run FROM bookmark_refresh_schedule").fetchone()[0], 0)


class OpenCountTests(unittest.TestCase):
    def test_each_app_counts_its_visits(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/owl.db", "OWL_CONFIG_DIR": folder + "/config"}),
            TestClient(app) as client,
        ):
            for _ in range(3):
                client.post("/api/home/opened", json={"app": "naas"})
            self.assertEqual(client.post("/api/home/opened", json={"app": "compare"}).json()["count"], 1)
            self.assertEqual(client.post("/api/home/opened", json={"app": "nope"}).status_code, 422)
            opens = client.get("/api/home/opens").json()["opens"]
            self.assertEqual({app: entry["count"] for app, entry in opens.items()}, {"naas": 3, "compare": 1})


if __name__ == "__main__":
    unittest.main()
