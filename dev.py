#!/usr/bin/env python3
"""Manage OWL's static frontend and FastAPI backend on Windows, macOS and Linux."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".owl-dev"
STATE = RUNTIME / "state.json"
HOST = "127.0.0.1"
WINDOWS = os.name == "nt"


@contextmanager
def control_lock():
    with (RUNTIME / "control.lock").open("a+b") as lock:
        if WINDOWS:
            import msvcrt

            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if WINDOWS:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def stop_file(token):
    return RUNTIME / ("stop-" + token)


def kill_windows_tree(pid):
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
        timeout=15,
    )
    return result.returncode == 0


def read_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def owned(state):
    """Check identity, not just a potentially recycled PID."""
    if not state:
        return False
    if WINDOWS:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$ErrorActionPreference='Stop'; (Get-CimInstance Win32_Process -Filter 'ProcessId = {int(state['pid'])}').CommandLine",
        ]
    else:
        command = ["ps", "-p", str(state["pid"]), "-o", "command="]
    result = subprocess.run(
        command, capture_output=True, check=False, text=True, timeout=10
    )
    if WINDOWS and result.returncode:
        raise RuntimeError("Unable to verify OWL process identity using PowerShell.")
    return (
        str(ROOT / "dev.py").casefold() in result.stdout.casefold()
        and state["token"] in result.stdout
    )


def python_path():
    suffix = "Scripts/python.exe" if WINDOWS else "bin/python"
    for path in (ROOT / "backend/.venv" / suffix, ROOT / ".venv" / suffix):
        if path.is_file():
            return str(path)
    return sys.executable


def stop():
    state = read_state()
    if not owned(state):
        STATE.unlink(missing_ok=True)
        print("OWL is stopped.")
        return
    if WINDOWS:
        request = stop_file(state["token"])
        request.touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and owned(state):
            time.sleep(0.2)
        if owned(state) and not kill_windows_tree(state["pid"]):
            raise RuntimeError(
                "Could not stop the OWL process tree. State retained for retry."
            )
        request.unlink(missing_ok=True)
        STATE.unlink(missing_ok=True)
        print("Stopped OWL frontend and backend.")
        return
    # The supervisor and its children share a dedicated process group.
    os.killpg(state["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            os.killpg(state["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(state["pid"], signal.SIGKILL)
    STATE.unlink(missing_ok=True)
    print("Stopped OWL frontend and backend.")


def available(port):
    with socket.socket() as sock:
        option = socket.SO_EXCLUSIVEADDRUSE if WINDOWS else socket.SO_REUSEADDR
        sock.setsockopt(socket.SOL_SOCKET, option, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            raise RuntimeError(
                f"Port {port} is in use. Stop its server or choose another port."
            )


def show(state):
    print(f"Frontend: http://{HOST}:{state['frontend_port']}/home/")
    print(f"API docs: http://{HOST}:{state['backend_port']}/docs")
    print(f"Logs: {RUNTIME}")


def start(args):
    state = read_state()
    if owned(state):
        print("OWL is already running.")
        show(state)
        return
    if (
        not (ROOT / "backend/main.py").is_file()
        or not (ROOT / "frontend/index.html").is_file()
    ):
        raise RuntimeError("Expected backend/main.py and frontend/index.html.")
    python = python_path()
    check = subprocess.run(
        [python, "-c", "import fastapi, uvicorn, httpx, pymupdf, cryptography"],
        capture_output=True,
        check=False,
    )
    if check.returncode:
        venv_python = (
            "backend\\.venv\\Scripts\\python.exe"
            if WINDOWS
            else "backend/.venv/bin/python"
        )
        raise RuntimeError(
            "Install backend dependencies first:\n"
            f"  {'python' if WINDOWS else 'python3'} -m venv backend/.venv\n"
            f"  {venv_python} -m pip install -r backend/requirements.txt"
        )
    if args.frontend_port == args.backend_port:
        raise RuntimeError("Frontend and backend ports must be different.")
    for port in (args.frontend_port, args.backend_port):
        available(port)
    token = uuid.uuid4().hex
    with (RUNTIME / "supervisor.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "dev.py"),
                "_serve",
                "--token",
                token,
                "--frontend-port",
                str(args.frontend_port),
                "--backend-port",
                str(args.backend_port),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **(
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if WINDOWS
                else {"start_new_session": True}
            ),
        )
    state = {
        "pid": process.pid,
        "token": token,
        "frontend_port": args.frontend_port,
        "backend_port": args.backend_port,
    }
    STATE.write_text(json.dumps(state))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"OWL exited during startup. See logs in {RUNTIME}.")
            try:
                for port, path in (
                    (args.frontend_port, "/home/"),
                    (args.backend_port, "/openapi.json"),
                ):
                    with urlopen(
                        f"http://{HOST}:{port}{path}", timeout=0.5
                    ) as response:
                        if response.status != 200:
                            raise OSError("Service not ready")
                print("Started OWL frontend and FastAPI backend.")
                show(state)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"Startup timed out. See logs in {RUNTIME}.")
    except BaseException:
        stop()
        raise


def serve(args):
    running = True
    children = []
    logs = []

    def shutdown(_signal, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        commands = [
            (
                "backend",
                [
                    python_path(),
                    "-u",
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    HOST,
                    "--port",
                    str(args.backend_port),
                    "--reload",
                    "--reload-dir",
                    str(ROOT / "backend"),
                ],
                ROOT / "backend",
            ),
            (
                "frontend",
                [
                    sys.executable,
                    "-u",
                    str(ROOT / "scripts/frontend_server.py"),
                    "--port",
                    str(args.frontend_port),
                    "--backend-port",
                    str(args.backend_port),
                    "--directory",
                    str(ROOT / "frontend"),
                ],
                ROOT,
            ),
        ]
        for name, command, cwd in commands:
            log = (RUNTIME / f"{name}.log").open("ab")
            logs.append(log)
            children.append(
                subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    **(
                        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                        if WINDOWS
                        else {}
                    ),
                )
            )
        while (
            running
            and not stop_file(args.token).exists()
            and all(child.poll() is None for child in children)
        ):
            time.sleep(0.2)
    finally:
        # Also reaches the Uvicorn reload child, including when one service crashes.
        if WINDOWS:
            for child in children:
                if child.poll() is None:
                    try:
                        child.send_signal(signal.CTRL_BREAK_EVENT)
                    except OSError:
                        kill_windows_tree(child.pid)
        else:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        deadline = time.monotonic() + 8
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if WINDOWS:
                    if not kill_windows_tree(child.pid):
                        raise RuntimeError("Could not stop an OWL child process.")
                else:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
        for log in logs:
            log.close()


class LogTail:
    """Reopen each poll so Windows log rotation is never blocked by our handle."""

    def __init__(self, path, lines=20):
        self.path = path
        self.lines = lines
        self.identity = None
        self.offset = 0
        self.pending = b""

    def read(self):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is None:
                    content = b"".join(deque(stream, maxlen=self.lines))
                    self.offset = stream.tell()
                    self.identity = identity
                else:
                    if identity != self.identity or stat.st_size < self.offset:
                        self.offset = 0
                        self.pending = b""
                    self.identity = identity
                    stream.seek(self.offset)
                    content = stream.read(65536)
                    self.offset = stream.tell()
        except FileNotFoundError:
            return []
        parts = (self.pending + content).split(b"\n")
        self.pending = parts.pop()
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in parts]


def follow_logs(args):
    directory = Path(os.environ.get("OWL_LOG_DIR", ROOT / "backend/data/logs"))
    if not directory.is_absolute():
        directory = ROOT / "backend" / directory
    sources = {
        "backend": [
            ("backend", RUNTIME / "backend.log"),
            ("crawler", directory / "backend.log"),
        ],
        "frontend": [("frontend", RUNTIME / "frontend.log")],
        "supervisor": [("supervisor", RUNTIME / "supervisor.log")],
    }
    selected = [
        item
        for name, items in sources.items()
        if args.service in ("all", name)
        for item in items
    ]
    tails = [(name, LogTail(path, args.lines)) for name, path in selected]
    print("OWL logs — Ctrl+C stops following; the app keeps running.", flush=True)
    for name, path in selected:
        print(f"[{name}] {path}", flush=True)
    try:
        while True:
            for name, tail in tails:
                for line in tail.read():
                    print(f"[{name}] {line}", flush=True)
            if args.no_follow:
                return
            time.sleep(0.25)
    except KeyboardInterrupt:
        print(
            "\nStopped following logs. OWL is still running if it was started.",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=["start", "stop", "restart", "status", "logs", "_serve"],
    )
    parser.add_argument("--frontend-port", type=int, default=8771)
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--service",
        choices=["all", "backend", "frontend", "supervisor"],
        default="all",
        help="Log source (default: all)",
    )
    parser.add_argument(
        "--lines", type=int, default=20, help="Recent lines per log before following"
    )
    parser.add_argument(
        "--no-follow", action="store_true", help="Print recent logs and exit"
    )
    args = parser.parse_args()
    if args.lines < 0:
        parser.error("--lines must be zero or greater.")
    if args.command == "logs":
        follow_logs(args)
        return
    if not all(1 <= port <= 65535 for port in (args.frontend_port, args.backend_port)):
        parser.error("Ports must be between 1 and 65535.")
    if args.command == "_serve":
        if not args.token:
            parser.error("Internal command requires a token.")
        serve(args)
        return
    RUNTIME.mkdir(exist_ok=True)
    with control_lock():
        if args.command == "status":
            state = read_state()
            print("OWL is running." if owned(state) else "OWL is stopped.")
            if owned(state):
                show(state)
        elif args.command == "stop":
            stop()
        else:
            if args.command == "restart":
                stop()
            start(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as error:
        print(f"OWL: {error}", file=sys.stderr)
        sys.exit(1)
