from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_bookmark_completion_reload_preserves_open_status_and_notification_panels():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated Bookmark Manager reload tests.")
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "--test", str(project_root / "tests/js/bookmark_completion_reload.test.js")],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
