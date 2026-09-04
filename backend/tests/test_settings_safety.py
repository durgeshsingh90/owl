import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from django.conf import settings


def _isolated_subprocess_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CONFLUENCE_BASE_URL": "",
            "CONFLUENCE_PAT": "",
            "DJANGO_SECRET_KEY": (
                "synthetic-subprocess-secret-key-not-for-real-use-0123456789-abcdef"
            ),
            "OWL_ENV_FILE": str(tmp_path / "no-environment-file"),
            "PYTHONPATH": str(settings.BASE_DIR),
        }
    )
    return environment


def test_trackable_repository_data_root_is_rejected_before_any_write(tmp_path):
    unsafe_root = Path(settings.BASE_DIR) / f"private-runtime-{uuid.uuid4().hex}"
    environment = _isolated_subprocess_environment(tmp_path)
    environment["OWL_DATA_ROOT"] = str(unsafe_root)

    try:
        result = subprocess.run(
            [sys.executable, "-c", "import owl.settings"],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert not unsafe_root.exists()
        assert "ignored directories" in result.stderr
    finally:
        shutil.rmtree(unsafe_root, ignore_errors=True)


def test_test_settings_ignore_an_environment_file_with_external_values(tmp_path):
    marker = "synthetic-env-pat-never-valid-7b18e3f2"
    environment_file = tmp_path / "private.env"
    environment_file.write_text(
        f"CONFLUENCE_BASE_URL=https://confluence.example.invalid\nCONFLUENCE_PAT={marker}\n",
        encoding="utf-8",
    )
    environment = _isolated_subprocess_environment(tmp_path)
    environment["OWL_ENV_FILE"] = str(environment_file)
    environment["CONFLUENCE_BASE_URL"] = "https://confluence.example.invalid"
    environment["CONFLUENCE_PAT"] = marker

    code = """
import shutil
from owl import settings_test
try:
    assert settings_test.CONFLUENCE_BASE_URL == ""
    assert settings_test.CONFLUENCE_PAT == ""
    print("isolated")
finally:
    shutil.rmtree(settings_test.TEST_OWL_DATA_ROOT)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "isolated"
    assert marker not in result.stdout
    assert marker not in result.stderr
