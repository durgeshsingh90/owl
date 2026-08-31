#!/usr/bin/env python3
"""Start this OWL checkout with its configured database and background workers."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

BACKUP_STALL_TIMEOUT_SECONDS = 30


def project_root() -> Path:
    return Path(__file__).resolve().parent


def find_python(root: Path) -> Path:
    """Keep the venv path intact: resolving its symlink would bypass the venv."""
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = root / ".venv" / Path(*relative)
    return candidate if candidate.is_file() else Path(sys.executable).absolute()


def backup_database(connection) -> Path:
    """Take a consistent, private SQLite snapshot before changing its schema."""
    from datetime import UTC, datetime

    from django.conf import settings

    if connection.vendor != "sqlite":
        raise RuntimeError("Automatic startup backups require OWL's SQLite database.")
    database = Path(connection.settings_dict["NAME"])
    backup_root = Path(settings.BACKUPS_ROOT)
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    descriptor, filename = tempfile.mkstemp(
        prefix=f"before-start-migration-{stamp}-", suffix=".sqlite3", dir=backup_root
    )
    os.close(descriptor)
    backup = Path(filename)
    last_progress = time.monotonic()

    def progress(status: int, _remaining: int, _total: int) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            if now - last_progress >= BACKUP_STALL_TIMEOUT_SECONDS:
                raise TimeoutError(
                    "Database backup is blocked. Stop other OWL instances and try again. "
                    "No database updates were applied."
                )
        else:
            last_progress = now

    try:
        with (
            closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(backup)) as destination,
        ):
            source.backup(destination, pages=1024, progress=progress, sleep=0.1)
            if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("The database backup did not pass its integrity check.")
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "owl.settings")
    import django
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    django.setup()
    database_existed = Path(connection.settings_dict["NAME"]).is_file()
    call_command("check")
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if args.check:
        connection.close()
        if pending:
            print(
                f"{len(pending)} database updates pending. Start OWL without --check to apply them."
            )
            return 1
        print("OWL is ready to start. No database updates pending.")
        return 0
    if pending:
        if database_existed:
            print("Backing up the existing database before applying updates...", flush=True)
            backup = backup_database(connection)
            print(f"Database backup saved: {backup}", flush=True)
        print(f"Applying {len(pending)} database updates before starting workers...", flush=True)
        call_command("migrate", interactive=False)
    connection.close()
    print("Starting OWL and its background Git/PDF workers. Press Ctrl+C to stop.", flush=True)
    call_command("run_owl", *([args.addrport] if args.addrport else []))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Start OWL using this checkout's Python environment and existing settings."
    )
    parser.add_argument(
        "addrport", nargs="?", help="Optional port or address:port; otherwise use OWL's default."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check setup without applying updates or starting workers.",
    )
    args = parser.parse_args(arguments)
    root = project_root()
    executable = find_python(root)
    try:
        os.chdir(root)
        if executable != Path(sys.executable).absolute():
            command = [str(executable), str(root / "start.py"), *arguments]
            if os.name == "nt":
                # Windows execv joins argv without quoting; preserve spaces and quotes.
                command = [subprocess.list2cmdline([argument]) for argument in command]
            os.execv(str(executable), command)
        return run(args)
    except KeyboardInterrupt:
        print("\nOWL stopped.")
        return 130
    except ImportError as error:
        print(
            f"OWL could not load its dependencies: {error}\n"
            "Install the project's requirements in .venv, then run this script again.",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(f"OWL could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
