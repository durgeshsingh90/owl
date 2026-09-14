import unittest
from app.pdfs.pull_requests import trace
from app.pdfs.client import BitbucketError


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_deduplication_and_activity(self):
        calls = []

        class Client:
            base = "https://example.test/stash"

            def repo_path(self, project, repo):
                return f"/projects/{project}/repos/{repo}"

            async def pages(self, path):
                calls.append(path)
                if "/commits/" in path:
                    if "/bad/" in path:
                        raise BitbucketError("Forbidden")
                    yield {"id": 683}
                    yield {"id": 683}
                else:
                    yield {
                        "action": "APPROVED",
                        "createdDate": 100,
                        "user": {"displayName": "Alice"},
                    }
                    yield {
                        "action": "MERGED",
                        "createdDate": 200,
                        "user": {"displayName": "Bob"},
                    }
                    yield {
                        "action": "REVIEWED",
                        "createdDate": 150,
                        "user": {"displayName": "Carol"},
                    }

            async def request(self, path):
                calls.append(path)
                return {
                    "id": 683,
                    "title": "Test PR",
                    "state": "MERGED",
                    "reviewers": [
                        {"user": {"displayName": "Alice"}, "approved": True},
                        {"user": {"displayName": "Carol"}, "status": "NEEDS_WORK"},
                        {"user": {"displayName": "Pending"}, "approved": False},
                    ],
                }

        result = await trace(
            Client(), "ADR", "repo", [{"id": "one"}, {"id": "two"}, {"id": "bad"}]
        )
        self.assertEqual(result["commits"]["one"], ["ADR/repo/683"])
        self.assertEqual(result["commits"]["two"], ["ADR/repo/683"])
        self.assertIsNone(result["commits"]["bad"])
        self.assertEqual(len(result["pull_requests"]), 1)
        pr = result["pull_requests"][0]
        self.assertEqual(pr["merged"], 200)
        self.assertEqual(
            [r["status"] for r in pr["reviewers"]],
            ["APPROVED", "NEEDS_WORK", "UNAPPROVED"],
        )
        self.assertEqual([a["timestamp"] for a in pr["activities"]], [100, 150, 200])
        self.assertEqual(calls.count("/projects/ADR/repos/repo/pull-requests/683"), 1)
        self.assertTrue(pr["url"].endswith("/683/overview"))
