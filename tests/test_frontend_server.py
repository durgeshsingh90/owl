"""Which paths the frontend server forwards to the backend."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from frontend_server import forwarded  # noqa: E402


class Forwarding(unittest.TestCase):
    def test_every_library_reaches_its_api(self):
        for path in ("/api/health", "/naas/api/workspace", "/network-automation/api/search/matches",
                     "/naas/bitbucket/workspace/", "/network-automation/bitbucket/settings/test/", "/bookmarks/settings/x"):
            self.assertTrue(forwarded(path), path)

    def test_static_files_stay_static(self):
        for path in ("/naas/", "/naas/script.js", "/network-automation/index.html", "/home/", "/json-visualizer/app.js"):
            self.assertFalse(forwarded(path), path)


if __name__ == "__main__":
    unittest.main()
