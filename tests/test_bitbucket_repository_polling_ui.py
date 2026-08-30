from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "script",
    [
        "bitbucket_repository_polling.test.js",
        "theme_bootstrap.test.js",
        "bitbucket_repository_selection.test.js",
    ],
)
def test_background_completion_polling_and_early_theme_bootstrap(script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated repository polling tests.")
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "--test", str(project_root / "tests/js" / script)],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_early_theme_bootstrap_has_the_response_csp_nonce(client):
    response = client.get("/pdfs/", HTTP_HOST="127.0.0.1")
    html = response.content.decode()
    bootstrap = re.search(r'<script nonce="([^"]+)" data-theme-bootstrap>', html)
    assert bootstrap is not None
    assert f"'nonce-{bootstrap.group(1)}'" in response.headers["Content-Security-Policy"]
    assert html.index(bootstrap.group(0)) < html.index('<a class="skip-link"')
