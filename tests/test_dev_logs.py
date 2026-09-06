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
