from __future__ import annotations

import os
import subprocess
import sys

import pytest
from django.conf import settings


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        (
            "PDF_PIPELINE_CONTROLLER_MODE",
            "automatic",
            "PDF_PIPELINE_CONTROLLER_MODE must be one of",
        ),
        (
            "PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION",
            "0",
            "must be greater than zero",
        ),
        (
            "PDF_PIPELINE_MANUAL_FIXED_TARGET",
            "9",
            "cannot exceed PDF_MAX_EXTRACTION_WORKERS",
        ),
        (
            "PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES",
            "0",
            "must be at least 1",
        ),
    ),
)
def test_invalid_controller_configuration_fails_closed_at_startup(
    tmp_path,
    name,
    value,
    message,
):
    environment = os.environ.copy()
    environment.update(
        {
            "BITBUCKET_ALLOWED_HOSTS": "",
            "CONFLUENCE_BASE_URL": "",
            "CONFLUENCE_PAT": "",
            "DJANGO_SECRET_KEY": (
                "synthetic-controller-settings-secret-key-0123456789-abcdefgh"
            ),
            "OWL_DATA_ROOT": str(tmp_path / "runtime"),
            "OWL_ENV_FILE": str(tmp_path / "no-environment-file"),
            "PDF_MAX_EXTRACTION_WORKERS": "8",
            name: value,
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import owl.settings"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert message in result.stderr
