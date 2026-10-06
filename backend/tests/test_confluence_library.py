import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.bookmarks import confluence
from app.core.database import connection
from app.tracker import service
from fastapi.testclient import TestClient
from main import app

from tests.fake_confluence import FakeConfluence

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=30)).isoformat()
OLDER = (NOW - timedelta(days=400)).isoformat()
ANCIENT = (NOW - timedelta(days=1200)).isoformat()
HOME = {"page_id": "100", "title": "Engineering Home"}
ARCH = {"page_id": "200", "title": "Architecture"}
DESIGN = {"page_id": "202", "title": "Design Reviews"}
# page_id: (title, ancestors, created, updated, creator, editor, unused)
PAGES = {
    "100": ("Engineering Home", [], OLDER, RECENT, "Ann", "Ben", "welcome"),
    "200": ("Architecture", [HOME], OLDER, OLDER, "Ann", "Ann", "architecture overview"),
    "201": ("AWS for IDE", [HOME, ARCH], OLDER, RECENT, "Cat", "Dan", "how to use aws for ide toolkits"),
    "202": ("Design Reviews", [HOME, ARCH], OLDER, OLDER, "Cat", "Cat", "design review process"),
    "203": ("Review 2026", [HOME, ARCH, DESIGN], RECENT, RECENT, "Dan", "Dan", "ide setup and aws notes"),
    "204": ("Legacy Diagram", [HOME, ARCH], ANCIENT, ANCIENT, "Old", "Old", "aws legacy"),
    "300": ("Runbooks", [HOME], OLDER, RECENT, "Ben", "Ben", "operations runbooks"),
    "301": ("Restart Service", [HOME, {"page_id": "300", "title": "Runbooks"}], RECENT, RECENT, "Ben", "Eve", "restart the service"),
    "400": ("Loose Page", [HOME], RECENT, RECENT, "Eve", "Eve", "a loose page"),
}


class ConfluenceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"OWL_DB_PATH": self.temp.name + "/owl.db"}).start()
        settings = confluence.ConfluenceSettings(base_url="https://wiki.test", token="secret")
        patch.object(confluence, "load", return_value=settings).start()
        patch.object(confluence, "test", new_callable=AsyncMock).start()

        self.wiki = FakeConfluence(
            {
                page_id: {
                    "title": title,
                    "ancestors": [item["page_id"] for item in ancestors],
                    "created": created,
                    "updated": updated,
                    "creator": creator,
                    "editor": editor,
                    "version": 3,
                    "message": "Edited " + title,
                }
                for page_id, (title, ancestors, created, updated, creator, editor, _) in PAGES.items()
            },
            page_size=4,
        )
        patch.object(confluence, "get", side_effect=self.wiki.get).start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def sync(self, url="https://wiki.test/pages/viewpage.action?pageId=203"):
        import asyncio

        job = self.client.post("/api/confluence-library/imports", json={"urls": [url]})
        self.assertEqual(job.status_code, 202, job.text)
        self.assertEqual(job.json()["status"], "queued")
        # Only changes since tracking started are kept; start before every sample page.
        with connection() as db:
            db.execute("UPDATE confluence_tracker_roots SET created_at=?", ((NOW - timedelta(days=1500)).isoformat(),))
        asyncio.run(service.sync(service.claim()))
        return job.json()["id"]

    def test_workspace_maps_tree_to_projects_sections_and_two_year_list(self):
        job = self.sync()
        state = self.client.get(f"/api/confluence-library/jobs/{job}").json()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual((state["repositories"], state["repositories_done"], state["failed"]), (9, 9, 0))
        data = self.client.get("/api/confluence-library/workspace").json()
        # The deep page 203 was pasted; its top-most parent became the project.
        self.assertEqual([p["name"] for p in data["projects"]], ["Engineering Home"])
        repos = {repo["name"]: repo["pdfCount"] for repo in data["projects"][0]["repos"]}
        self.assertEqual(repos, {"Architecture": 4, "Engineering Home": 2, "Runbooks": 2})
        docs = {doc["name"]: doc for doc in data["documents"]}
        self.assertNotIn("Legacy Diagram", docs, "older than two years")
        self.assertEqual(len(docs), 8)
        review = docs["Review 2026"]
        self.assertEqual((review["repo"], review["path"]), ("Architecture", "Architecture / Design Reviews"))
        self.assertEqual((review["createdBy"], review["editedBy"], review["commitCount"]), ("Dan", "Dan", 3))
        aws = docs["AWS for IDE"]
        self.assertEqual((aws["committedAt"], aws["commitAuthor"]), (RECENT, "Dan"), "latest of created/updated")
        self.assertEqual(aws["commitMessage"], "Edited AWS for IDE")
        self.assertEqual(docs["Loose Page"]["repo"], "Engineering Home")
        self.assertEqual(docs["Design Reviews"]["ancestorIds"], ["200"])
        self.assertTrue(docs["Design Reviews"]["hasChildren"])
        # The tree keeps every page, including those outside the two-year list.
        self.assertEqual(len(data["tree"]), 9)
        self.assertIn("Old", {p["name"] for p in self.client.get("/api/confluence-library/workspace?days=0").json()["people"]})
        self.assertNotIn("Old", {p["name"] for p in data["people"]})
        paged = self.client.get("/api/confluence-library/workspace?limit=3&summaries=false").json()
        self.assertEqual(len(paged["documents"]), 3)
        rest = self.client.get(f"/api/confluence-library/workspace?limit=10&before={paged['nextBefore']}&summaries=false").json()
        self.assertEqual(len(paged["documents"]) + len(rest["documents"]), 8)

    def test_search_notes_opens_details_and_revision(self):
        self.sync()
        docs = {doc["name"]: doc for doc in self.client.get("/api/confluence-library/workspace").json()["documents"]}
        found = self.client.post("/api/confluence-library/search/matches", json={"q": "aws for ide"}).json()
        self.assertEqual(found["ids"][0], docs["AWS for IDE"]["id"])
        self.assertEqual(found["tiers"][str(docs["AWS for IDE"]["id"])], 3)
        # Only metadata is tracked: titles, paths and notes are searchable, content is not.
        self.assertNotIn(docs["Review 2026"]["id"], found["ids"])
        revision = self.client.get("/api/confluence-library/workspace/revision").json()["revision"]
        page = docs["Restart Service"]["id"]
        opened = self.client.post(f"/api/confluence-library/document/{page}/open").json()
        self.assertEqual(opened["open_count"], 1)
        self.assertEqual(self.client.get("/api/confluence-library/workspace/revision").json()["revision"], revision)
        self.client.patch(f"/api/confluence-library/document/{page}/notes", json={"notes": "kubernetes restart"})
        self.assertNotEqual(self.client.get("/api/confluence-library/workspace/revision").json()["revision"], revision)
        by_notes = self.client.post("/api/confluence-library/search/matches", json={"q": "kubernetes", "fields": ["notes"]}).json()
        self.assertEqual(by_notes["ids"], [page])
        details = self.client.get(f"/api/confluence-library/document/{page}").json()
        self.assertEqual((details["created_by"], details["updated_by"], details["notes"]), ("Ben", "Eve", "kubernetes restart"))
        self.assertEqual(details["pdf_text"], "")
        self.assertEqual(details["path"], "Runbooks")
        self.assertEqual(self.client.get("/api/confluence-library/document/99999").status_code, 404)

    def test_jobs_failures_and_deleting_a_tree(self):
        self.sync()
        project = self.client.get("/api/confluence-library/workspace").json()["projects"][0]
        section = project["repos"][0]["id"]
        job = self.client.post("/api/confluence-library/crawl", json={"repository_ids": [section]}).json()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.client.get("/api/confluence-library/jobs/latest").json()["job"]["id"], job["id"])
        self.assertEqual(self.client.post(f"/api/confluence-library/jobs/{job['id']}/cancel").status_code, 409)
        self.assertEqual(self.client.post("/api/confluence-library/failed/retry").status_code, 400)
        self.assertEqual(self.client.get("/api/confluence-library/activity").json()["total"], 2)
        self.assertEqual(
            self.client.post("/api/confluence-library/crawl/hard-retry", json={"confirmation": "no"}).status_code, 400
        )
        preview = self.client.post("/api/confluence-library/projects/delete-preview", json={"project_id": int(project["id"])}).json()
        self.assertEqual(preview["documents"], 9)
        with connection() as db:
            db.execute("UPDATE confluence_tracker_roots SET status='completed'")
        self.assertEqual(
            self.client.post("/api/confluence-library/projects/delete", json={"project_id": int(project["id"])}).status_code, 400
        )
        deleted = self.client.post(
            "/api/confluence-library/projects/delete", json={"project_id": int(project["id"]), "confirmation": "delete all"}
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        data = self.client.get("/api/confluence-library/workspace").json()
        self.assertEqual((data["projects"], data["documents"], data["tree"]), ([], [], []))
        self.assertEqual(self.client.post("/api/confluence-library/search/matches", json={"q": "aws"}).json()["ids"], [])
