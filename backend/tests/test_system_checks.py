from pathlib import Path

from django.conf import settings
from django.test import override_settings

from core import checks


def _error_ids():
    return {error.id for error in checks.owl_runtime_checks(None)}


@override_settings(ALLOWED_HOSTS=["127.0.0.1", "owl.example.invalid"])
def test_system_check_rejects_non_loopback_host():
    assert "owl.E001" in _error_ids()


def test_system_check_rejects_trackable_repository_data_root():
    with override_settings(OWL_DATA_ROOT=Path(settings.BASE_DIR) / "private-runtime"):
        assert "owl.E002" in _error_ids()


def test_system_check_rejects_database_outside_data_root(tmp_path, monkeypatch):
    data_root = tmp_path / "data-root"
    monkeypatch.setitem(settings.DATABASES["default"], "NAME", tmp_path / "outside.sqlite3")

    with override_settings(OWL_DATA_ROOT=data_root):
        assert "owl.E003" in _error_ids()


def test_system_check_reports_missing_fts5(monkeypatch):
    class MissingFTSCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query):
            return None

        def fetchone(self):
            return None

    monkeypatch.setattr(checks.connection, "cursor", MissingFTSCursor)

    assert "owl.E004" in _error_ids()
