"""Compare: the backend aligns, colours, filters and copies blocks; the UI only renders."""

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
    def test_both_sides_are_aligned_with_fillers(self):
        layout = diff.layout(ORIGINAL, MODIFIED)
        self.assertEqual(layout["kinds"], "scssrsa")
        self.assertEqual(layout["lines"], 7)
        # A line missing on one side is an empty filler there, so rows line up.
        self.assertEqual(layout["original"]["text"].split("\n")[6], "")
        self.assertEqual(layout["modified"]["text"].split("\n")[4], "")
        self.assertEqual(layout["original"]["numbers"], [1, 2, 3, 4, 5, 6, None])
        self.assertEqual(layout["modified"]["numbers"], [1, 2, 3, 4, None, 5, 6])
        self.assertEqual(layout["blocks"], [[1, 2], [4, 5], [6, 7]])
        self.assertEqual(
            {k: layout["summary"][k] for k in ("rows", "differences", "similarities", "changed", "removed", "added")},
            {"rows": 7, "differences": 3, "similarities": 4, "changed": 1, "removed": 1, "added": 1},
        )

    def test_changed_words_are_column_ranges(self):
        layout = diff.layout(ORIGINAL, MODIFIED)
        self.assertEqual(layout["original"]["words"], {1: [[9, 10]]})
        self.assertEqual(layout["modified"]["words"], {1: [[9, 10]]})

    def test_trailing_spaces_matter_only_when_asked(self):
        self.assertEqual(diff.layout(ORIGINAL, MODIFIED)["kinds"][2], "s")
        strict = diff.layout(ORIGINAL, MODIFIED, ignore_whitespace=False)
        self.assertEqual(strict["kinds"][2], "c")
        self.assertEqual(strict["modified"]["words"][2], [[12, 14]])

    def test_filtered_views_come_from_the_cache(self):
        key = diff.layout(ORIGINAL, MODIFIED)["key"]
        differences = diff.layout_by_key(key, "differences")
        self.assertEqual(differences["kinds"], "cra")
        # Separate blocks stay separate even when the filter makes them adjacent.
        self.assertEqual(differences["blocks"], [[0, 1], [1, 2], [2, 3]])
        self.assertEqual(diff.layout_by_key(key, "similarities")["original"]["text"], "name: owl\n  port: 8000\nkeep\nsame")
        self.assertIsNone(diff.layout_by_key("missing"))

    def test_copy_block_to_either_side(self):
        key = diff.layout(ORIGINAL, MODIFIED)["key"]
        right = diff.copy_block(key, 0, "right")
        self.assertEqual(right["modifiedText"].split("\n")[1], "version: 1")
        self.assertEqual(right["originalText"], ORIGINAL)
        self.assertEqual(right["kinds"], "ssssrsa")
        self.assertEqual(right["summary"]["changed"], 0)
        left = diff.copy_block(key, 2, "left")
        self.assertEqual(left["originalText"], ORIGINAL + "\nadded line")
        removed = diff.copy_block(key, 1, "right")
        self.assertNotIn("removed line", MODIFIED)
        self.assertIn("removed line", removed["modifiedText"])
        with self.assertRaises(ValueError):
            diff.copy_block(key, 9, "right")

    def test_large_input_and_limit(self):
        original = "\n".join(f"line {i}" for i in range(50000))
        modified = original.replace("line 43210", "line 43210 changed")
        layout = diff.layout(original, modified)
        self.assertEqual(layout["blocks"], [[43210, 43211]])
        with self.assertRaises(ValueError):
            diff.layout("x" * (diff.MAX_TEXT_CHARS + 1), "")


class BlankLinesAndPairingTests(unittest.TestCase):
    def test_empty_lines_are_ignored_by_default(self):
        original = "name: owl\n\nversion: 1\nport: 8000\n\n\nkeep"
        modified = "name: owl\nversion: 2\n\nport: 8000\nkeep"
        strict = diff.layout(original, modified)
        relaxed = diff.layout(original, modified, ignore_blank=True)
        self.assertGreater(strict["summary"]["differences"], 1)
        self.assertEqual(relaxed["summary"]["differences"], 1)
        line = relaxed["original"]["text"].split("\n").index("version: 1")
        self.assertEqual(relaxed["modified"]["text"].split("\n")[line], "version: 2")
        self.assertEqual(relaxed["kinds"][line], "c")
        # Every line of both texts is still shown, in order.
        for side, text in (("original", original), ("modified", modified)):
            numbers = [n for n in relaxed[side]["numbers"] if n]
            self.assertEqual(numbers, list(range(1, text.count("\n") + 2)))

    def test_changed_line_pairs_with_its_most_similar_line(self):
        original = "start\nlegacy export line here\nend"
        modified = "start\nbrand new line entirely\nlegacy export line HERE\nend"
        layout = diff.layout(original, modified)
        self.assertEqual(layout["kinds"], "sacs")
        self.assertEqual(layout["original"]["words"], {2: [[19, 23]]})


class FormatTests(unittest.TestCase):
    def test_json_jsonl_and_json5(self):
        from app.compare.formatting import prettify

        self.assertEqual(prettify('{"a":1,"b":[1,2]}'), ('{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}', "json"))
        text, kind = prettify('{"a":1}\n{"a":2}\n')
        self.assertEqual((kind, text.count('"a"')), ("jsonl", 2))
        json5 = """// settings
        {unquoted: 'single', hex: 0x1F, trailing: [1, 2,], nested: {ok: true,}, inf: +Infinity, /* note */ text: "a\\"b"}"""
        text, kind = prettify(json5)
        self.assertEqual(kind, "json5")
        self.assertIn('"unquoted": "single"', text)
        self.assertIn('"hex": 31', text)
        self.assertIn('"inf": Infinity', text)
        self.assertIn('"text": "a\\"b"', text)
        with self.assertRaises(ValueError):
            prettify("key: value")


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

    def test_layout_view_and_copy(self):
        response = self.client.post(
            "/api/compare/layout",
            json={"originalText": ORIGINAL, "modifiedText": MODIFIED},
        )
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()["key"]
        view = self.client.get(f"/api/compare/layout/{key}?view=differences").json()
        self.assertEqual(view["kinds"], "cra")
        copied = self.client.post(
            "/api/compare/copy", json={"key": key, "block": 2, "direction": "left"}
        ).json()
        self.assertTrue(copied["originalText"].endswith("added line"))
        self.assertEqual(self.client.get("/api/compare/layout/unknown").status_code, 404)
        self.assertEqual(
            self.client.post("/api/compare/copy", json={"key": key, "block": 9, "direction": "left"}).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/compare/layout", json={"view": "bogus"}).status_code, 422
        )

    def test_recent_comparisons_keep_the_latest_20_and_their_links_work(self):
        tokens = []
        for index in range(22):
            body = {"originalTitle": f"left {index}", "originalText": f"a {index}", "modifiedText": "b"}
            tokens.append(self.client.post("/api/compare/history", json=body).json()["token"])
            time.sleep(0.002)
        items = self.client.get("/api/compare/history").json()["items"]
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0]["original_title"], "left 21")
        self.assertNotIn(tokens[0], {item["token"] for item in items}, "the oldest are deleted")
        # Saving again updates the same comparison instead of adding one.
        again = self.client.post("/api/compare/history", json={"token": tokens[5], "originalText": "edited"}).json()
        self.assertEqual(again["token"], tokens[5])
        self.assertEqual(len(self.client.get("/api/compare/history").json()["items"]), 20)
        shared = self.client.get(f"/api/compare/share/{tokens[5]}").json()
        self.assertEqual((shared["payload"]["originalText"], shared["expiresAt"]), ("edited", None))
        self.assertEqual(self.client.get(f"/api/compare/share/{tokens[0]}").status_code, 404)
        formatted = self.client.post("/api/compare/format", json={"text": "{a: 1}"}).json()
        self.assertEqual(formatted, {"text": '{\n  "a": 1\n}', "kind": "json5"})
        self.assertEqual(self.client.post("/api/compare/format", json={"text": "plain"}).status_code, 422)

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
