"""Compare: the backend aligns, highlights, filters and pages; the UI only renders."""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app.compare import diff
from fastapi.testclient import TestClient
from main import app

ORIGINAL = "name: owl\nversion: 1\n  port: 8000\nkeep\nremoved line\nsame"
MODIFIED = "name: owl\nversion: 2\n  port: 8000  \nkeep\nsame\nadded line"


class DiffTests(unittest.TestCase):
    def test_rows_are_aligned_with_source_line_numbers(self):
        result = diff.compare(ORIGINAL, MODIFIED)
        kinds = [row["kind"] for row in result["rows"]]
        self.assertEqual(
            kinds, ["same", "changed", "same", "same", "removed", "same", "added"]
        )
        removed = result["rows"][4]
        self.assertEqual((removed["originalLine"], removed["modifiedLine"]), (5, None))
        added = result["rows"][6]
        self.assertEqual((added["originalLine"], added["modifiedLine"]), (None, 6))
        self.assertEqual(
            result["summary"] | {"original": None, "modified": None},
            {
                "rows": 7,
                "similarities": 4,
                "differences": 3,
                "changed": 1,
                "removed": 1,
                "added": 1,
                "identical": False,
                "original": None,
                "modified": None,
            },
        )
        self.assertEqual(result["hunks"], [1, 4, 6])

    def test_changed_words_are_highlighted(self):
        row = diff.compare(ORIGINAL, MODIFIED)["rows"][1]
        self.assertEqual(
            row["originalSegments"],
            [{"text": "version: ", "changed": False}, {"text": "1", "changed": True}],
        )
        self.assertEqual(row["modifiedSegments"][-1], {"text": "2", "changed": True})

    def test_trailing_spaces_matter_only_when_asked(self):
        strict = diff.compare(ORIGINAL, MODIFIED, ignore_whitespace=False)
        self.assertEqual(strict["rows"][2]["kind"], "changed")
        self.assertEqual(
            strict["rows"][2]["modifiedSegments"][-1], {"text": "  ", "changed": True}
        )

    def test_filtered_views_and_pages_come_from_the_cache(self):
        result = diff.compare(ORIGINAL, MODIFIED, view="differences")
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["position"] for row in result["rows"]], [0, 1, 2])
        # Separate blocks stay separate even when the filter makes them adjacent.
        self.assertEqual(result["hunks"], [0, 1, 2])
        same = diff.page(result["key"], "similarities", offset=2, limit=1)
        self.assertEqual((same["total"], same["offset"]), (4, 2))
        self.assertEqual(same["rows"][0]["original"], "keep")
        self.assertIsNone(diff.page("missing", "all"))

    def test_large_input_is_paged(self):
        original = "\n".join(f"line {i}" for i in range(5000))
        modified = original.replace("line 4321", "line 4321 changed")
        result = diff.compare(original, modified, view="all", offset=4000, limit=500)
        self.assertEqual((result["total"], result["offset"], len(result["rows"])), (5000, 4000, 500))
        self.assertEqual(result["hunks"], [4321])
        with self.assertRaises(ValueError):
            diff.compare("x" * (diff.MAX_TEXT_CHARS + 1), "")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "OWL_DB_PATH": self.temp.name + "/owl.db",
                "OWL_CONFIG_DIR": self.temp.name + "/config",
                "OWL_LOG_DIR": self.temp.name + "/logs",
            },
        )
        self.env.start()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def test_analyze_then_page_by_key(self):
        response = self.client.post(
            "/api/compare/analyze",
            json={"originalText": ORIGINAL, "modifiedText": MODIFIED, "view": "differences"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()["key"]
        page = self.client.get(f"/api/compare/results/{key}?view=all&offset=5&limit=2").json()
        self.assertEqual([row["kind"] for row in page["rows"]], ["same", "added"])
        self.assertEqual(self.client.get("/api/compare/results/unknown").status_code, 404)
        self.assertEqual(
            self.client.post("/api/compare/analyze", json={"view": "bogus"}).status_code, 422
        )

    def test_share_links_expire_after_a_day(self):
        payload = {"originalTitle": "A", "originalText": ORIGINAL, "modifiedText": MODIFIED, "view": "differences"}
        created = self.client.post("/api/compare/share", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        token = created.json()["token"]
        shared = self.client.get(f"/api/compare/share/{token}").json()
        self.assertEqual(shared["payload"]["originalText"], ORIGINAL)
        self.assertEqual(shared["payload"]["view"], "differences")
        self.assertEqual(self.client.get("/api/compare/share/nope").status_code, 404)
        with patch("app.api.compare.time.time", return_value=time.time() + 86401):
            self.assertEqual(self.client.get(f"/api/compare/share/{token}").status_code, 410)


if __name__ == "__main__":
    unittest.main()
