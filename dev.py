#!/usr/bin/env python3
"""Manage OWL's static frontend and FastAPI backend on macOS/Linux."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.owl-dev'
STATE = RUNTIME / 'state.json'
HOST = '127.0.0.1'


def read_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def owned(state):
    """Check identity, not just a potentially recycled PID."""
    if not state:
        return False
    result = subprocess.run(
        ['ps', '-p', str(state['pid']), '-o', 'command='],
        capture_output=True, text=True,
    )
    return str(ROOT / 'dev.py') in result.stdout and state['token'] in result.stdout


def python_path():
    for path in (ROOT / 'backend/.venv/bin/python', ROOT / '.venv/bin/python'):
        if path.is_file():
            return str(path)
    return sys.executable


def stop():
    state = read_state()
    if not owned(state):
        STATE.unlink(missing_ok=True)
        print('OWL is stopped.')
        return
    # The supervisor and its children share a dedicated process group.
    os.killpg(state['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            os.killpg(state['pid'], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(state['pid'], signal.SIGKILL)
    STATE.unlink(missing_ok=True)
    print('Stopped OWL frontend and backend.')


def available(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            raise RuntimeError(f'Port {port} is in use. Stop its server or choose another port.')


def show(state):
    print(f"Frontend: http://{HOST}:{state['frontend_port']}/home/")
    print(f"API docs: http://{HOST}:{state['backend_port']}/docs")
    print(f'Logs: {RUNTIME}')


def start(args):
    state = read_state()
    if owned(state):
        print('OWL is already running.')
        show(state)
        return
    if not (ROOT / 'backend/main.py').is_file() or not (ROOT / 'frontend/index.html').is_file():
        raise RuntimeError('Expected backend/main.py and frontend/index.html.')
    python = python_path()
    check = subprocess.run([python, '-c', 'import fastapi, uvicorn'], capture_output=True)
    if check.returncode:
        raise RuntimeError(
            'Install backend dependencies first:\n'
            '  python3 -m venv backend/.venv\n'
            '  backend/.venv/bin/python -m pip install -r backend/requirements.txt'
        )
    if args.frontend_port == args.backend_port:
        raise RuntimeError('Frontend and backend ports must be different.')
    for port in (args.frontend_port, args.backend_port):
        available(port)
    token = uuid.uuid4().hex
    with (RUNTIME / 'supervisor.log').open('ab') as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / 'dev.py'), '_serve', '--token', token,
             '--frontend-port', str(args.frontend_port), '--backend-port', str(args.backend_port)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    state = dict(pid=process.pid, token=token, frontend_port=args.frontend_port,
                 backend_port=args.backend_port)
    STATE.write_text(json.dumps(state))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'OWL exited during startup. See logs in {RUNTIME}.')
            try:
                for port, path in ((args.frontend_port, '/home/'), (args.backend_port, '/openapi.json')):
                    with urlopen(f'http://{HOST}:{port}{path}', timeout=0.5) as response:
                        if response.status != 200:
                            raise OSError('Service not ready')
                print('Started OWL frontend and FastAPI backend.')
                show(state)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f'Startup timed out. See logs in {RUNTIME}.')
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
            ('backend', [python_path(), '-m', 'uvicorn', 'main:app', '--host', HOST,
                         '--port', str(args.backend_port), '--reload', '--reload-dir', str(ROOT / 'backend')], ROOT / 'backend'),
            ('frontend', [sys.executable, '-u', '-m', 'http.server', str(args.frontend_port),
                          '--bind', HOST, '--directory', str(ROOT / 'frontend')], ROOT),
        ]
        for name, command, cwd in commands:
            log = (RUNTIME / f'{name}.log').open('ab')
            logs.append(log)
            children.append(subprocess.Popen(command, cwd=cwd, stdout=log, stderr=log,
                                             stdin=subprocess.DEVNULL))
        while running and all(child.poll() is None for child in children):
            time.sleep(0.2)
    finally:
        # Also reaches the Uvicorn reload child, including when one service crashes.
        os.killpg(os.getpgrp(), signal.SIGTERM)
        deadline = time.monotonic() + 8
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgrp(), signal.SIGKILL)
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='start', choices=['start', 'stop', 'restart', 'status', '_serve'])
    parser.add_argument('--frontend-port', type=int, default=8771)
    parser.add_argument('--backend-port', type=int, default=8000)
    parser.add_argument('--token', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not all(1 <= port <= 65535 for port in (args.frontend_port, args.backend_port)):
        parser.error('Ports must be between 1 and 65535.')
    if args.command == '_serve':
        if not args.token:
            parser.error('Internal command requires a token.')
        serve(args)
        return
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == 'status':
            state = read_state()
            print('OWL is running.' if owned(state) else 'OWL is stopped.')
            if owned(state):
                show(state)
        elif args.command == 'stop':
            stop()
        else:
            if args.command == 'restart':
                stop()
            start(args)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError) as error:
        print(f'OWL: {error}', file=sys.stderr)
        sys.exit(1)
