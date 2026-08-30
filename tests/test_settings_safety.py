import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from django.conf import settings


def _isolated_subprocess_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BITBUCKET_ALLOWED_HOSTS": "",
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


def test_daily_repository_refresh_hour_above_twenty_three_is_rejected(tmp_path):
    environment = _isolated_subprocess_environment(tmp_path)
    environment["BITBUCKET_DAILY_REFRESH_LOCAL_HOUR"] = "24"

    result = subprocess.run(
        [sys.executable, "-c", "import owl.settings"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BITBUCKET_DAILY_REFRESH_LOCAL_HOUR must be between 0 and 23" in result.stderr


def test_bitbucket_inventory_and_search_page_sizes_are_capped_at_two_hundred(tmp_path):
    environment = _isolated_subprocess_environment(tmp_path)
    environment["BITBUCKET_PDF_PAGE_SIZE"] = "500"
    environment["BITBUCKET_SEARCH_PAGE_SIZE"] = "500"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from owl import settings; "
                "print(settings.BITBUCKET_PDF_PAGE_SIZE, settings.BITBUCKET_SEARCH_PAGE_SIZE)"
            ),
        ],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "200 200"


@pytest.mark.parametrize(
    ("configured_hosts", "expected_hosts"),
    [
        (None, ("bitbucket.org", "github.com", "scm.mastercard.int")),
        ("git.example.invalid", ("git.example.invalid",)),
        ("", ()),
    ],
    ids=("default-hosts", "explicit-override", "explicitly-disabled"),
)
def test_bitbucket_allowed_hosts_defaults_and_environment_override(
    tmp_path, configured_hosts, expected_hosts
):
    environment = _isolated_subprocess_environment(tmp_path)
    environment["OWL_DATA_ROOT"] = str(tmp_path / "runtime")
    if configured_hosts is None:
        environment.pop("BITBUCKET_ALLOWED_HOSTS", None)
    else:
        environment["BITBUCKET_ALLOWED_HOSTS"] = configured_hosts

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from owl import settings; print(repr(settings.BITBUCKET_ALLOWED_HOSTS))",
        ],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(expected_hosts)


def test_test_settings_ignore_an_environment_file_with_external_values(tmp_path):
    marker = "synthetic-env-pat-never-valid-7b18e3f2"
    environment_file = tmp_path / "private.env"
    environment_file.write_text(
        "CONFLUENCE_BASE_URL=https://confluence.example.invalid\n"
        f"CONFLUENCE_PAT={marker}\n"
        "BITBUCKET_ALLOWED_HOSTS=bitbucket.example.invalid\n",
        encoding="utf-8",
    )
    environment = _isolated_subprocess_environment(tmp_path)
    environment["OWL_ENV_FILE"] = str(environment_file)
    environment["CONFLUENCE_BASE_URL"] = "https://confluence.example.invalid"
    environment["CONFLUENCE_PAT"] = marker
    environment["BITBUCKET_ALLOWED_HOSTS"] = "bitbucket.example.invalid"

    code = """
import shutil
from owl import settings_test
try:
    assert settings_test.CONFLUENCE_BASE_URL == ""
    assert settings_test.CONFLUENCE_PAT == ""
    assert settings_test.BITBUCKET_ALLOWED_HOSTS == ()
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
