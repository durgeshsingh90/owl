import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app


class WorkspaceTests(unittest.TestCase):
    def test_revision_tracks_explorer_changes_but_not_opens(self):
        from app.core.database import connection

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/db"}),
            TestClient(app) as client,
        ):

            def revision():
                return client.get("/api/workspace/revision").json()["revision"]

            empty = revision()
            self.assertEqual(client.get("/api/workspace").json()["revision"], empty)
            with connection() as db:
                db.execute(
                    "INSERT INTO tracked_projects(id,project_url,server,project) "
                    "VALUES(1,'https://bb.test/projects/P','https://bb.test','P')"
                )
                db.execute(
                    "INSERT INTO repositories(id,project_id,repo,name) VALUES(1,1,'r','r')"
                )
                db.execute(
                    "INSERT INTO documents(id,repository_id,project,repo,pdf_name,path,url,"
                    "file_size,page_count,pdf_hash,pdf_text,added_at,updated_at,last_scanned) "
                    "VALUES(1,1,'P','r','a.pdf','a.pdf','https://bb.test/a.pdf',1,1,'h','t',"
                    "'2026-01-01','2026-01-01','2026-01-01')"
                )
            added = revision()
            self.assertNotEqual(added, empty)
            self.assertEqual(client.post("/api/document/1/open").status_code, 200)
            self.assertEqual(revision(), added)
            client.patch("/api/document/1/notes", json={"notes": "changed"})
            self.assertNotEqual(revision(), added)
            # The NAAS database has its own revision token.
            self.assertNotEqual(
                client.get("/naas/api/workspace/revision").json()["revision"].split(":")[0],
                added.split(":")[0],
            )

    def test_bookmark_roundtrip_and_conflict(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/db"}),
        ):
            with TestClient(app) as client:
                workspace = client.get("/api/workspace").json()
                self.assertRegex(workspace.pop("revision"), r"^[0-9a-f]{16}:\d+$")
                self.assertEqual(
                    workspace,
                    {
                        "projects": [],
                        "documents": [],
                        "people": [],
                        "nextBefore": None,
                        "backgroundAll": False,
                        "lastCompletedPull": None,
                    },
                )
                payload = client.get("/api/bookmarks/workspace").json()
                payload["bookmarks"] = [
                    {
                        "id": 13,
                        "title": "Real link",
                        "url": "https://example.org",
                        "views": 2,
                    }
                ]
                payload["notes"] = {"13": "My note"}
                self.assertEqual(
                    client.put("/api/bookmarks/workspace", json=payload).status_code,
                    200,
                )
                self.assertEqual(
                    client.put("/api/bookmarks/workspace", json=payload).status_code,
                    409,
                )
            with TestClient(app) as client:
                saved = client.get("/api/bookmarks/workspace").json()
                self.assertEqual(saved["bookmarks"][0]["title"], "Real link")
                self.assertEqual(saved["notes"]["13"], "My note")
