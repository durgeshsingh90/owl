from __future__ import annotations

import os
import runpy
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import django
import pytest
from django.core import management
from django.db.migrations import executor as migration_executor

import start


def test_importing_launcher_has_no_django_or_process_side_effects(monkeypatch):
    script = Path(start.__file__).resolve()
    setup = Mock(side_effect=AssertionError("import must not initialize Django"))
    reexecute = Mock(side_effect=AssertionError("import must not replace this process"))
    command = Mock(side_effect=AssertionError("import must not run management commands"))
    monkeypatch.setattr(django, "setup", setup)
    monkeypatch.setattr(os, "execv", reexecute)
    monkeypatch.setattr(management, "call_command", command)
    original_cwd = Path.cwd()
    original_settings = os.environ.get("DJANGO_SETTINGS_MODULE")

    imported = runpy.run_path(str(script), run_name="synthetic_start_import")

    assert callable(imported["main"])
    assert Path.cwd() == original_cwd
    assert os.environ.get("DJANGO_SETTINGS_MODULE") == original_settings
    setup.assert_not_called()
    reexecute.assert_not_called()
    command.assert_not_called()


def test_project_root_follows_script_location_not_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "relocated OWL workspace"
    root.mkdir()
    script = root / "start.py"
    script.touch()
    elsewhere = tmp_path / "unrelated directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(start, "__file__", str(script))

    assert start.project_root() == root


@pytest.mark.parametrize(
    ("platform", "relative_interpreter"),
    [("posix", ".venv/bin/python"), ("nt", ".venv/Scripts/python.exe")],
)
def test_find_python_uses_platform_virtual_environment(
    tmp_path, monkeypatch, platform, relative_interpreter
):
    root = tmp_path / "OWL with spaces"
    interpreter = root / relative_interpreter
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    # Replacing only start's os reference avoids changing pathlib's platform.
    monkeypatch.setattr(start, "os", SimpleNamespace(name=platform))

    assert start.find_python(root) == interpreter


def test_find_python_keeps_virtual_environment_symlink_path(tmp_path, monkeypatch):
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(start, "os", SimpleNamespace(name="posix"))

    selected = start.find_python(tmp_path)

    assert selected == interpreter
    assert selected != interpreter.resolve()


def test_find_python_accepts_legacy_repository_root_virtual_environment(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    backend.mkdir()
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(start, "os", SimpleNamespace(name="posix"))

    assert start.find_python(backend) == interpreter


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_find_python_falls_back_to_current_interpreter(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(start, "os", SimpleNamespace(name=platform))

    assert start.find_python(tmp_path) == Path(sys.executable)


@pytest.fixture
def startup(tmp_path, monkeypatch):
    database = tmp_path / "configured data" / "database.sqlite3"
    database.parent.mkdir()
    database.touch()
    events = []
    connection = SimpleNamespace(
        vendor="sqlite",
        settings_dict={"NAME": str(database)},
        close=Mock(side_effect=lambda: events.append("close")),
    )
    executor = Mock()
    leaves = [("synthetic_app", "0002_next")]
    executor.loader.graph.leaf_nodes.return_value = leaves
    executor.migration_plan.return_value = []
    setup = Mock()
    command = Mock(side_effect=lambda name, *args, **kwargs: events.append(name))
    backup = Mock(side_effect=lambda connection: events.append("backup") or tmp_path / "backup")
    monkeypatch.setattr(django, "setup", setup)
    monkeypatch.setattr(django.db, "connection", connection)
    monkeypatch.setattr(management, "call_command", command)
    monkeypatch.setattr(migration_executor, "MigrationExecutor", Mock(return_value=executor))
    monkeypatch.setattr(start, "backup_database", backup)
    return SimpleNamespace(
        database=database,
        connection=connection,
        executor=executor,
        setup=setup,
        command=command,
        backup=backup,
        events=events,
        leaves=leaves,
    )


def test_run_uses_existing_settings_and_default_server_address(startup, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "synthetic.settings")

    assert start.run(Namespace(check=False, addrport=None)) == 0

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "synthetic.settings"
    startup.setup.assert_called_once_with()
    startup.executor.migration_plan.assert_called_once_with(startup.leaves)
    assert startup.events == ["check", "close", "run_owl"]
    startup.command.assert_any_call("run_owl")
    startup.backup.assert_not_called()


def test_run_sets_default_settings_only_when_missing(startup, monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    assert start.run(Namespace(check=True, addrport=None)) == 0

    assert os.environ["DJANGO_SETTINGS_MODULE"] == "owl.settings"


def test_run_passes_explicit_address_to_supervised_launcher(startup):
    assert start.run(Namespace(check=False, addrport="127.0.0.1:9017")) == 0

    startup.command.assert_any_call("run_owl", "127.0.0.1:9017")


def test_prepare_applies_pending_updates_without_starting_workers(startup, capsys):
    startup.executor.migration_plan.return_value = [object()]

    assert start.run(Namespace(check=False, prepare=True, addrport=None)) == 0

    assert startup.events == ["check", "backup", "migrate", "close"]
    startup.command.assert_any_call("migrate", interactive=False)
    assert "database is ready" in capsys.readouterr().out


@pytest.mark.parametrize("pending", [False, True])
def test_check_never_migrates_backs_up_or_starts_workers(startup, pending, capsys):
    startup.executor.migration_plan.return_value = [object()] if pending else []

    assert start.run(Namespace(check=True, addrport=None)) == int(pending)

    assert startup.events == ["check", "close"]
    startup.command.assert_called_once_with("check")
    startup.backup.assert_not_called()
    message = capsys.readouterr().out
    assert "pending" in message


def test_pending_existing_database_is_backed_up_before_migrate_and_workers(startup):
    startup.executor.migration_plan.return_value = [object()]

    assert start.run(Namespace(check=False, addrport=None)) == 0

    assert startup.events == ["check", "backup", "migrate", "close", "run_owl"]
    startup.backup.assert_called_once_with(startup.connection)
    startup.command.assert_any_call("migrate", interactive=False)


def test_new_database_migrates_without_attempting_to_back_up_nonexistent_file(startup):
    startup.database.unlink()
    startup.executor.migration_plan.return_value = [object()]

    assert start.run(Namespace(check=False, addrport=None)) == 0

    assert startup.events == ["check", "migrate", "close", "run_owl"]
    startup.backup.assert_not_called()


def test_backup_failure_prevents_migration_and_worker_startup(startup):
    startup.executor.migration_plan.return_value = [object()]
    startup.backup.side_effect = OSError("synthetic backup denied")

    with pytest.raises(OSError, match="synthetic backup denied"):
        start.run(Namespace(check=False, addrport=None))

    startup.command.assert_called_once_with("check")


def test_migration_failure_prevents_worker_startup(startup):
    startup.executor.migration_plan.return_value = [object()]

    def command(name, *args, **kwargs):
        startup.events.append(name)
        if name == "migrate":
            raise RuntimeError("synthetic migration failed")

    startup.command.side_effect = command

    with pytest.raises(RuntimeError, match="synthetic migration failed"):
        start.run(Namespace(check=False, addrport=None))

    assert startup.events == ["check", "backup", "migrate"]


def test_system_check_failure_stops_before_migration_planning(startup):
    startup.command.side_effect = RuntimeError("synthetic configuration invalid")

    with pytest.raises(RuntimeError, match="synthetic configuration invalid"):
        start.run(Namespace(check=False, addrport=None))

    startup.executor.migration_plan.assert_not_called()
    startup.backup.assert_not_called()
    startup.command.assert_called_once_with("check")


@pytest.fixture
def launch_location(tmp_path, monkeypatch):
    root = tmp_path / "moved OWL workspace"
    root.mkdir()
    unrelated = tmp_path / "other working directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(start, "project_root", Mock(return_value=root))
    monkeypatch.setattr(start, "find_python", Mock(return_value=Path(sys.executable).absolute()))
    return root


def test_main_changes_to_its_own_checkout_and_does_not_hardcode_address(
    launch_location, monkeypatch
):
    runner = Mock(return_value=0)
    monkeypatch.setattr(start, "run", runner)

    assert start.main([]) == 0

    assert Path.cwd() == launch_location
    assert runner.call_args.args[0] == Namespace(check=False, prepare=False, addrport=None)


def test_main_passes_check_mode_without_overriding_default_address(launch_location, monkeypatch):
    runner = Mock(return_value=1)
    monkeypatch.setattr(start, "run", runner)

    assert start.main(["--check"]) == 1

    assert runner.call_args.args[0] == Namespace(check=True, prepare=False, addrport=None)


def test_main_passes_prepare_mode_without_starting_server(launch_location, monkeypatch):
    runner = Mock(return_value=0)
    monkeypatch.setattr(start, "run", runner)

    assert start.main(["--prepare"]) == 0

    assert runner.call_args.args[0] == Namespace(check=False, prepare=True, addrport=None)


def test_main_preserves_arguments_and_space_containing_paths_when_selecting_venv(
    launch_location, monkeypatch
):
    class Reexecuted(BaseException):
        pass

    interpreter = launch_location / ".venv" / "bin" / "python"
    monkeypatch.setattr(start, "find_python", Mock(return_value=interpreter))
    reexecute = Mock(side_effect=Reexecuted)
    monkeypatch.setattr(start.os, "execv", reexecute)
    runner = Mock()
    monkeypatch.setattr(start, "run", runner)

    with pytest.raises(Reexecuted):
        start.main(["--check", "9017"])

    assert Path.cwd() == launch_location
    reexecute.assert_called_once_with(
        str(interpreter),
        [str(interpreter), str(launch_location / "start.py"), "--check", "9017"],
    )
    runner.assert_not_called()


def test_windows_reexecution_quotes_space_containing_arguments_but_not_executable(
    launch_location, monkeypatch
):
    class Reexecuted(BaseException):
        pass

    interpreter = launch_location / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(start, "find_python", Mock(return_value=interpreter))
    reexecute = Mock(side_effect=Reexecuted)
    monkeypatch.setattr(start, "os", SimpleNamespace(name="nt", chdir=os.chdir, execv=reexecute))
    runner = Mock()
    monkeypatch.setattr(start, "run", runner)

    with pytest.raises(Reexecuted):
        start.main(["--check", "9017"])

    reexecute.assert_called_once_with(
        str(interpreter),
        [f'"{interpreter}"', f'"{launch_location / "start.py"}"', "--check", "9017"],
    )
    runner.assert_not_called()


def test_help_exits_before_interpreter_selection_cwd_changes_or_django_setup(monkeypatch, capsys):
    original_cwd = Path.cwd()
    root = Mock(side_effect=AssertionError("must not inspect the checkout for --help"))
    runner = Mock(side_effect=AssertionError("must not start Django for --help"))
    monkeypatch.setattr(start, "project_root", root)
    monkeypatch.setattr(start, "run", runner)

    with pytest.raises(SystemExit) as exited:
        start.main(["--help"])

    assert exited.value.code == 0
    assert Path.cwd() == original_cwd
    root.assert_not_called()
    runner.assert_not_called()
    assert "--check" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "exit_code", "message"),
    [
        (ImportError("synthetic missing dependency"), 1, "dependencies"),
        (OSError("synthetic backup denied"), 1, "synthetic backup denied"),
        (RuntimeError("synthetic migration failed"), 1, "synthetic migration failed"),
        (KeyboardInterrupt(), 130, "OWL stopped"),
    ],
)
def test_main_reports_failures_with_nonzero_exit_status(
    launch_location, monkeypatch, capsys, error, exit_code, message
):
    monkeypatch.setattr(start, "run", Mock(side_effect=error))

    assert start.main([]) == exit_code

    captured = capsys.readouterr()
    assert message in captured.out + captured.err


def test_sqlite_backup_is_complete_private_and_uses_configured_location(tmp_path, settings):
    database = tmp_path / "relocated data" / "source with spaces.sqlite3"
    database.parent.mkdir()
    settings.BACKUPS_ROOT = tmp_path / "configured backups"
    with sqlite3.connect(database) as source:
        source.execute("CREATE TABLE synthetic_rows (name TEXT NOT NULL)")
        source.execute("INSERT INTO synthetic_rows VALUES (?)", ("retained row",))
    connection = SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(database)})

    backup = start.backup_database(connection)

    assert backup.parent == settings.BACKUPS_ROOT
    assert backup != database
    with sqlite3.connect(backup) as snapshot:
        assert snapshot.execute("SELECT name FROM synthetic_rows").fetchall() == [("retained row",)]
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
    with sqlite3.connect(database) as source:
        assert source.execute("SELECT name FROM synthetic_rows").fetchall() == [("retained row",)]
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600


def test_sqlite_backup_captures_committed_wal_data(tmp_path, settings):
    database = tmp_path / "source.sqlite3"
    settings.BACKUPS_ROOT = tmp_path / "backups"
    connection = SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(database)})
    source = sqlite3.connect(database)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE synthetic_rows (value INTEGER)")
        source.execute("INSERT INTO synthetic_rows VALUES (17)")
        source.commit()
        assert Path(f"{database}-wal").is_file()

        backup = start.backup_database(connection)

        with sqlite3.connect(backup) as snapshot:
            assert snapshot.execute("SELECT value FROM synthetic_rows").fetchall() == [(17,)]
    finally:
        source.close()


def test_backup_filenames_are_unique_for_back_to_back_starts(tmp_path, settings):
    database = tmp_path / "source.sqlite3"
    settings.BACKUPS_ROOT = tmp_path / "backups"
    with sqlite3.connect(database) as source:
        source.execute("CREATE TABLE synthetic_rows (value INTEGER)")
    connection = SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(database)})

    first = start.backup_database(connection)
    second = start.backup_database(connection)

    assert first != second
    assert first.is_file() and second.is_file()


def test_backup_failure_cleans_partial_snapshot_and_does_not_create_source(tmp_path, settings):
    database = tmp_path / "missing.sqlite3"
    settings.BACKUPS_ROOT = tmp_path / "backups"
    connection = SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(database)})

    with pytest.raises(sqlite3.OperationalError):
        start.backup_database(connection)

    assert not database.exists()
    assert not list(settings.BACKUPS_ROOT.iterdir())


def test_backup_rejects_unsupported_database_without_touching_storage(tmp_path, settings):
    settings.BACKUPS_ROOT = tmp_path / "backups"
    connection = SimpleNamespace(vendor="postgresql")

    with pytest.raises(RuntimeError, match="SQLite"):
        start.backup_database(connection)

    assert not settings.BACKUPS_ROOT.exists()


@pytest.fixture
def simulated_backup(tmp_path, settings, monkeypatch):
    settings.BACKUPS_ROOT = tmp_path / "configured backups"
    settings.BACKUPS_ROOT.mkdir()
    previous = settings.BACKUPS_ROOT / "previous-good-backup.sqlite3"
    previous.touch()
    database = tmp_path / "source.sqlite3"
    database.touch()
    source = Mock()
    destination = Mock()
    destination.execute.return_value.fetchone.return_value = ("ok",)
    connect = Mock(side_effect=[source, destination])
    monkeypatch.setattr(start.sqlite3, "connect", connect)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(start, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return SimpleNamespace(
        connection=SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(database)}),
        database=database,
        root=settings.BACKUPS_ROOT,
        previous=previous,
        source=source,
        destination=destination,
        clock=clock,
    )


@pytest.mark.parametrize("status", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_locked_backup_times_out_and_removes_only_partial_snapshot(simulated_backup, status):
    def stalled_backup(destination, *, pages, progress, sleep):
        assert pages == 1024
        assert sleep == 0.1
        progress(status, 10, 10)
        simulated_backup.clock.now = start.BACKUP_STALL_TIMEOUT_SECONDS
        progress(status, 10, 10)

    simulated_backup.source.backup.side_effect = stalled_backup

    with pytest.raises(TimeoutError, match="Stop other OWL instances"):
        start.backup_database(simulated_backup.connection)

    assert simulated_backup.database.is_file()
    assert list(simulated_backup.root.iterdir()) == [simulated_backup.previous]
    simulated_backup.source.close.assert_called_once_with()
    simulated_backup.destination.close.assert_called_once_with()


def test_successful_progress_resets_stall_timer_for_long_backups(simulated_backup):
    def progressing_backup(destination, *, pages, progress, sleep):
        for batch in range(4):
            simulated_backup.clock.now += start.BACKUP_STALL_TIMEOUT_SECONDS - 1
            progress(sqlite3.SQLITE_BUSY, 10 - batch, 10)
            progress(sqlite3.SQLITE_OK, 9 - batch, 10)
        progress(sqlite3.SQLITE_DONE, 0, 10)

    simulated_backup.source.backup.side_effect = progressing_backup

    backup = start.backup_database(simulated_backup.connection)

    assert simulated_backup.clock.now > start.BACKUP_STALL_TIMEOUT_SECONDS
    assert backup.is_file()
    assert simulated_backup.previous.is_file()


def test_cancelled_backup_cleans_partial_snapshot_but_preserves_source_and_older_backups(
    simulated_backup,
):
    simulated_backup.source.backup.side_effect = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        start.backup_database(simulated_backup.connection)

    assert simulated_backup.database.is_file()
    assert list(simulated_backup.root.iterdir()) == [simulated_backup.previous]
    simulated_backup.source.close.assert_called_once_with()
    simulated_backup.destination.close.assert_called_once_with()


def test_backup_timeout_is_reported_and_never_starts_migrations_or_workers(
    startup, launch_location, capsys
):
    startup.executor.migration_plan.return_value = [object()]
    startup.backup.side_effect = TimeoutError(
        "Database backup is blocked. Stop other OWL instances."
    )

    assert start.main([]) == 1

    startup.command.assert_called_once_with("check")
    assert "Stop other OWL instances" in capsys.readouterr().err
