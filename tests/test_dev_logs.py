"""Portable log following: append, partial lines, rotation and truncation."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "owl_dev", Path(__file__).resolve().parents[1] / "dev.py"
)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class LogTests(unittest.TestCase):
    def test_follow_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            tail = dev.LogTail(path, 2)
            self.assertEqual(tail.read(), [])
            path.write_bytes(b"old\nfirst\nsecond\n")
            self.assertEqual(tail.read(), ["first", "second"])
            with path.open("ab") as output:
                output.write(b"par")
            self.assertEqual(tail.read(), [])
            with path.open("ab") as output:
                output.write(b"tial\n")
            self.assertEqual(tail.read(), ["partial"])
            path.rename(path.with_suffix(".1"))
            path.write_bytes(b"rotated\n")
            self.assertEqual(tail.read(), ["rotated"])
            path.write_bytes(b"new\n")
            self.assertEqual(tail.read(), ["new"])

    def test_skip_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            path.write_bytes(b"old\n")
            tail = dev.LogTail(path, 0)
            self.assertEqual(tail.read(), [])
            with path.open("ab") as output:
                output.write(b"live\n")
            self.assertEqual(tail.read(), ["live"])

    def test_error_filter_and_tracebacks(self):
        log_filter = dev.LogFilter()
        self.assertFalse(
            log_filter.accepts('{"level":"INFO","event":"crawl.pdf_found"}')
        )
        self.assertTrue(
            log_filter.accepts('{"level":"ERROR","event":"crawl.folder_failed"}')
        )
        self.assertFalse(log_filter.accepts("INFO: GET /api/jobs/123 200 OK"))
        self.assertTrue(log_filter.accepts("ERROR: Exception in ASGI application"))
        self.assertTrue(log_filter.accepts("Traceback (most recent call last):"))
        self.assertTrue(log_filter.accepts('  File "main.py", line 1'))
        self.assertTrue(log_filter.accepts("RuntimeError: failed"))
        self.assertFalse(log_filter.accepts("INFO: request completed"))
        self.assertFalse(log_filter.accepts("WARNING: something"))
        self.assertTrue(log_filter.accepts("CRITICAL: stopped"))
        self.assertTrue(dev.LogFilter("all").accepts("INFO: normal"))
