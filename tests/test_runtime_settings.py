from pathlib import Path

from django.conf import settings


def test_default_runtime_is_loopback_only():
    # Django's test environment appends its synthetic ``testserver`` host.
    assert set(settings.ALLOWED_HOSTS) == {
        "localhost",
        "127.0.0.1",
        "[::1]",
        "testserver",
    }
    assert settings.OWL_ALLOW_NON_LOOPBACK is False


def test_default_daily_repository_refresh_uses_eleven_in_owl_timezone():
    assert settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR == 11
    assert settings.TIME_ZONE == "Europe/Dublin"


def test_default_bitbucket_inventory_and_search_pages_hold_one_hundred_results():
    assert settings.BITBUCKET_PDF_PAGE_SIZE == 100
    assert settings.BITBUCKET_SEARCH_PAGE_SIZE == 100


def test_test_database_and_runtime_data_are_isolated_outside_repository():
    data_root = Path(settings.OWL_DATA_ROOT).resolve()
    repository_root = Path(settings.BASE_DIR).resolve()
    database_name = str(settings.DATABASES["default"]["NAME"])

    assert data_root != repository_root
    assert not data_root.is_relative_to(repository_root)
    # pytest-django replaces SQLite with a private in-memory database during the
    # test run. If a file-backed test database is selected, it must remain under
    # the isolated temporary data root.
    assert database_name.startswith("file:memorydb_") or Path(
        database_name
    ).resolve().is_relative_to(data_root)


def test_automated_tests_use_only_the_in_memory_secret_backend():
    assert settings.ENV_FILE == settings.TEST_OWL_DATA_ROOT / "no-environment-file"
    assert not settings.ENV_FILE.exists()
    assert settings.CONFLUENCE_SECRET_BACKEND == "memory"
    assert settings.OWL_ALLOW_IN_MEMORY_SECRET_STORE is True
    assert settings.OWL_ALLOW_LIVE_EXTERNAL_TESTS is False
    assert settings.CONFLUENCE_BASE_URL == ""
    assert settings.CONFLUENCE_PAT == ""
    assert settings.BITBUCKET_ALLOWED_HOSTS == ()
