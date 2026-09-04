#!/usr/bin/env python3
"""Start OWL's Django backend and Vite frontend together."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

STOP_TIMEOUT_SECONDS = 10
HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
PORT_SEARCH_LIMIT = 100


class StartupError(RuntimeError):
    """A local service cannot be started from this checkout."""


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]
    working_directory: Path
    environment: dict[str, str] | None = None


def repository_root() -> Path:
    return Path(__file__).resolve().parent


def find_backend_python(root: Path) -> Path:
    """Prefer the backend venv while accepting OWL's former root venv."""
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidates = (
        root / "backend" / ".venv" / Path(*relative),
        root / ".venv" / Path(*relative),
    )
    return next(
        (candidate for candidate in candidates if candidate.is_file()),
        Path(sys.executable).absolute(),
    )


def _port_is_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError:
        return False
    return True


def find_available_port(host: str, preferred_port: int) -> int:
    """Return the preferred local port, or the next bindable port."""
    last_port = min(65_535, preferred_port + PORT_SEARCH_LIMIT)
    for port in range(preferred_port, last_port + 1):
        if _port_is_available(host, port):
            return port
    raise StartupError(
        f"No available port was found from {preferred_port} through {last_port}."
    )


def services(
    root: Path,
    *,
    backend_port: int = DEFAULT_BACKEND_PORT,
    frontend_port: int = DEFAULT_FRONTEND_PORT,
) -> tuple[Service, Service]:
    backend = root / "backend"
    frontend = root / "frontend"
    backend_launcher = backend / "start.py"
    frontend_manifest = frontend / "package.json"

    if not backend_launcher.is_file():
        raise StartupError(f"Django launcher was not found: {backend_launcher}")
    if not frontend_manifest.is_file():
        raise StartupError(
            f"Frontend package manifest was not found: {frontend_manifest}"
        )
    if not (frontend / "node_modules").is_dir():
        raise StartupError(
            "Frontend packages are not installed. Run `cd frontend && npm ci`, "
            "then start OWL again."
        )

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise StartupError(
            "npm was not found on PATH. Install Node.js and npm, then try again."
        )

    frontend_environment = os.environ.copy()
    frontend_environment["OWL_BACKEND_URL"] = f"http://{HOST}:{backend_port}"

    return (
        Service(
            name="backend",
            command=(
                str(find_backend_python(root)),
                str(backend_launcher),
                f"{HOST}:{backend_port}",
            ),
            working_directory=backend,
        ),
        Service(
            name="frontend",
            command=(
                npm,
                "run",
                "dev",
                "--",
                "--host",
                HOST,
                "--port",
                str(frontend_port),
                "--strictPort",
            ),
            working_directory=frontend,
            environment=frontend_environment,
        ),
    )


def prepare_backend(root: Path) -> None:
    """Apply Django database updates before either development service starts."""
    backend = root / "backend"
    backend_launcher = backend / "start.py"
    print("Checking OWL database updates...", flush=True)
    try:
        completed = subprocess.run(
            (str(find_backend_python(root)), str(backend_launcher), "--prepare"),
            cwd=backend,
            check=False,
        )
    except OSError as error:
        raise StartupError(
            f"Django database preparation could not start: {error}"
        ) from error
    if completed.returncode:
        raise StartupError(
            "Django checks or database updates failed. Review the error above; "
            "the frontend and backend were not started."
        )


def _process_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _signal_process_group(
    process: subprocess.Popen[bytes], requested_signal: int
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        if requested_signal == signal.SIGINT:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif requested_signal == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
        return
    os.killpg(process.pid, requested_signal)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        _signal_process_group(process, signal.SIGINT)
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
        return
    except OSError:
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        _signal_process_group(process, signal.SIGTERM)
        process.wait(timeout=5)
        return
    except OSError:
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(root: Path) -> int:
    processes: list[tuple[Service, subprocess.Popen[bytes]]] = []
    try:
        backend_port = find_available_port(HOST, DEFAULT_BACKEND_PORT)
        frontend_port = find_available_port(HOST, DEFAULT_FRONTEND_PORT)
        if backend_port != DEFAULT_BACKEND_PORT:
            print(
                f"Backend port {DEFAULT_BACKEND_PORT} is busy; using {backend_port}.",
                flush=True,
            )
        if frontend_port != DEFAULT_FRONTEND_PORT:
            print(
                f"Frontend port {DEFAULT_FRONTEND_PORT} is busy; using {frontend_port}.",
                flush=True,
            )
        service_definitions = services(
            root,
            backend_port=backend_port,
            frontend_port=frontend_port,
        )
        prepare_backend(root)
        for service in service_definitions:
            print(f"Starting OWL {service.name}...", flush=True)
            process_options = _process_options()
            if service.environment is not None:
                process_options["env"] = service.environment
            process = subprocess.Popen(
                service.command,
                cwd=service.working_directory,
                **process_options,
            )
            processes.append((service, process))

        print(
            f"OWL is starting. Open http://{HOST}:{frontend_port}/static/. "
            "Press Ctrl+C to stop both frontend and backend.",
            flush=True,
        )
        while True:
            for service, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(
                        f"OWL {service.name} stopped with exit code {return_code}.",
                        file=sys.stderr,
                    )
                    return return_code or 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping OWL frontend and backend...", flush=True)
        return 130
    except OSError as error:
        raise StartupError(f"An OWL service could not be started: {error}") from error
    finally:
        for _service, process in reversed(processes):
            stop_process(process)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start OWL's backend and frontend together."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=("start",),
        help="start both Django and Vite",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    _parser().parse_args(arguments)
    try:
        return run(repository_root())
    except StartupError as error:
        print(f"OWL could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
