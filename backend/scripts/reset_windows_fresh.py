#!/usr/bin/env python3
"""Safely rebuild a fresh OWL database on a Windows checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MIGRATION_DIRECTORIES = (
    "bookmark_manager/migrations",
    "bitbucket/migrations",
    "semantic_search/migrations",
)


class ResetError(RuntimeError):
    """A safe, actionable fresh-reset failure."""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _dotenv_data_root(project_root: Path) -> str | None:
    dotenv = project_root / ".env"
    if not dotenv.is_file():
        return None

    for raw_line in dotenv.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "OWL_DATA_ROOT":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or None

    return None


def _data_root(project_root: Path) -> Path:
    configured = os.environ.get("OWL_DATA_ROOT") or _dotenv_data_root(project_root) or "var"
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve(strict=False)


def _run_git(project_root: Path, *arguments: str, capture: bool = False) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=project_root,
        check=True,
        shell=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def _restore_official_migrations(project_root: Path) -> None:
    print("Restoring the official migration files from Git...", flush=True)
    _run_git(
        project_root,
        "restore",
        "--source=HEAD",
        "--staged",
        "--worktree",
        "--",
        *MIGRATION_DIRECTORIES,
    )
    untracked = _run_git(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "*/migrations/*.py",
        capture=True,
    )
    extra_files = tuple(line.strip() for line in untracked.splitlines() if line.strip())
    if extra_files:
        formatted = "\n".join(f"  {filename}" for filename in extra_files)
        raise ResetError(
            "Generated migration files still exist:\n"
            f"{formatted}\n"
            "Move those files outside the project and run this script again. "
            "No database files were moved."
        )


def _move_if_present(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    print(f"Preserved: {source} -> {destination}", flush=True)


def _preserve_runtime_data(data_root: Path) -> Path:
    reset_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    recovery = data_root / "recovery" / f"fresh-reset-{reset_id}"
    recovery.mkdir(mode=0o700, parents=True, exist_ok=False)

    database = data_root / "database" / "owl.sqlite3"
    _move_if_present(database, recovery / "owl.sqlite3")
    _move_if_present(database.with_name(f"{database.name}-wal"), recovery / "owl.sqlite3-wal")
    _move_if_present(database.with_name(f"{database.name}-shm"), recovery / "owl.sqlite3-shm")
    return recovery


def _start_owl(project_root: Path) -> int:
    python = project_root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise ResetError(f"OWL's Windows virtual-environment Python was not found: {python}")

    print(
        "Starting OWL. The official migrations will create a fresh database...",
        flush=True,
    )
    try:
        return subprocess.run(
            (str(python), "start.py"),
            cwd=project_root,
            check=False,
            shell=False,
        ).returncode
    except KeyboardInterrupt:
        return 130


def reset_windows_owl() -> int:
    project_root = _project_root()
    if os.name != "nt":
        raise ResetError("This recovery script is intended for the Windows OWL checkout.")
    if shutil.which("git") is None:
        raise ResetError("Git was not found on PATH.")

    _run_git(project_root, "rev-parse", "--show-toplevel", capture=True)

    print(
        "\nThis will create a fresh OWL database.\n"
        "Any current database and repository checkouts will be moved into a "
        "recovery folder.\n"
        "They will not be deleted.\n"
    )
    confirmation = input("Type RESET OWL to continue: ").strip()
    if confirmation.casefold() != "reset owl":
        print("Cancelled. Nothing was changed.")
        return 2

    _restore_official_migrations(project_root)
    recovery = _preserve_runtime_data(_data_root(project_root))
    print(f"Recovery files are stored in: {recovery}\n", flush=True)

    exit_code = _start_owl(project_root)
    if exit_code not in (0, 130):
        print(
            f"OWL did not start successfully. Recovery files remain in: {recovery}",
            file=sys.stderr,
        )
    return exit_code


def main() -> int:
    try:
        return reset_windows_owl()
    except (OSError, ResetError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
