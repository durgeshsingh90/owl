"""Isolated settings used only by OWL's synthetic automated tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_OWL_DATA_ROOT = Path(tempfile.mkdtemp(prefix="owl-tests-")).resolve()

# Set these before importing the normal settings so no test can initialize the real
# credential store, inherit a real integration target, or write runtime data to the repo.
os.environ["OWL_DATA_ROOT"] = str(TEST_OWL_DATA_ROOT)
os.environ["OWL_ENV_FILE"] = str(TEST_OWL_DATA_ROOT / "no-environment-file")
os.environ["CONFLUENCE_SECRET_BACKEND"] = "memory"
os.environ["OWL_ALLOW_IN_MEMORY_SECRET_STORE"] = "true"
os.environ["OWL_ALLOW_LIVE_EXTERNAL_TESTS"] = "false"
os.environ["OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS"] = "true"
os.environ["CONFLUENCE_ACTION_COOLDOWN_SECONDS"] = "0"
os.environ["OWL_ALLOW_NON_LOOPBACK"] = "false"
os.environ["DJANGO_DEBUG"] = "false"
os.environ["DJANGO_SECRET_KEY"] = (
    "synthetic-test-secret-key-only-not-for-real-use-0123456789-abcdefghij"
)
os.environ["CONFLUENCE_BASE_URL"] = ""
os.environ["CONFLUENCE_PAT"] = ""

from owl.settings import *  # noqa: E402,F403

OWL_SYNTHETIC_GIT_LOG_HOSTS = ("private.invalid",)
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
