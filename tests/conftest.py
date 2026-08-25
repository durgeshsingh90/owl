from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connections

from bookmark_manager.services.secret_store import reset_secret_store_cache


@pytest.fixture(autouse=True)
def isolated_secret_store():
    reset_secret_store_cache()
    yield
    reset_secret_store_cache()


def pytest_sessionfinish(session, exitstatus):
    connections.close_all()
    root = Path(settings.TEST_OWL_DATA_ROOT).resolve()
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    if root.parent == temporary_parent and root.name.startswith("owl-tests-"):
        shutil.rmtree(root, ignore_errors=True)
