import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "reset_windows_fresh.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reset_windows_fresh", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preserve_runtime_data_moves_database_sidecars_and_repositories(tmp_path):
    module = _load_script()
    database = tmp_path / "database" / "owl.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_text("database", encoding="utf-8")
    database.with_name("owl.sqlite3-wal").write_text("wal", encoding="utf-8")
    database.with_name("owl.sqlite3-shm").write_text("shm", encoding="utf-8")
    repository = tmp_path / "media" / "bitbucket" / "repositories" / "sample"
    repository.mkdir(parents=True)
    (repository / "README.md").write_text("repository", encoding="utf-8")

    recovery = module._preserve_runtime_data(tmp_path)

    assert not database.exists()
    assert not (tmp_path / "media" / "bitbucket" / "repositories").exists()
    assert (recovery / "owl.sqlite3").read_text(encoding="utf-8") == "database"
    assert (recovery / "owl.sqlite3-wal").read_text(encoding="utf-8") == "wal"
    assert (recovery / "owl.sqlite3-shm").read_text(encoding="utf-8") == "shm"
    assert (recovery / "repositories" / "sample" / "README.md").read_text(
        encoding="utf-8"
    ) == "repository"


def test_restore_aborts_on_untracked_migration_before_runtime_data_is_moved(monkeypatch, tmp_path):
    module = _load_script()
    calls = []

    def fake_run_git(project_root, *arguments, capture=False):
        calls.append((arguments, capture))
        if "ls-files" in arguments:
            return "bitbucket_search/migrations/9999_generated.py\n"
        return ""

    monkeypatch.setattr(module, "_run_git", fake_run_git)

    with pytest.raises(module.ResetError, match="9999_generated.py"):
        module._restore_official_migrations(tmp_path)

    assert calls[0][0][:4] == (
        "restore",
        "--source=HEAD",
        "--staged",
        "--worktree",
    )
    assert "ls-files" in calls[1][0]


def test_script_avoids_migration_generation_and_destructive_cleanup():
    script = SCRIPT.read_text(encoding="utf-8").casefold()

    assert "makemigrations" not in script
    assert ".unlink(" not in script
    assert "rmtree(" not in script
    assert '"clean"' not in script
