from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "dev.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workspace_dev", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher() -> ModuleType:
    return _load_script()


def test_repository_root_follows_launcher_location(launcher, tmp_path, monkeypatch):
    root = tmp_path / "OWL workspace"
    root.mkdir()
    script = root / "dev.py"
    script.touch()
    monkeypatch.setattr(launcher, "__file__", str(script))

    assert launcher.repository_root() == root


def test_backend_python_prefers_backend_environment(launcher, tmp_path, monkeypatch):
    backend_python = tmp_path / "backend" / ".venv" / "bin" / "python"
    legacy_python = tmp_path / ".venv" / "bin" / "python"
    backend_python.parent.mkdir(parents=True)
    legacy_python.parent.mkdir(parents=True)
    backend_python.touch()
    legacy_python.touch()
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="posix"))

    assert launcher.find_backend_python(tmp_path) == backend_python


def test_service_definitions_start_backend_and_frontend(launcher, tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "start.py").touch()
    (frontend / "package.json").touch()
    (frontend / "node_modules").mkdir()
    python = tmp_path / "python"
    monkeypatch.setattr(launcher, "find_backend_python", Mock(return_value=python))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="/tools/npm"))

    backend_service, frontend_service = launcher.services(
        tmp_path,
        backend_port=8012,
        frontend_port=5185,
    )

    assert backend_service.command == (
        str(python),
        str(backend / "start.py"),
        "127.0.0.1:8012",
    )
    assert backend_service.working_directory == backend
    assert frontend_service.working_directory == frontend
    assert frontend_service.command == (
        "/tools/npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "5185",
        "--strictPort",
    )
    assert frontend_service.environment is not None
    assert frontend_service.environment["OWL_BACKEND_URL"] == "http://127.0.0.1:8012"


def test_available_port_advances_until_a_port_can_bind(launcher, monkeypatch):
    available = Mock(side_effect=lambda _host, port: port == 8002)
    monkeypatch.setattr(launcher, "_port_is_available", available)

    assert launcher.find_available_port("127.0.0.1", 8000) == 8002
    assert available.call_args_list == [
        call("127.0.0.1", 8000),
        call("127.0.0.1", 8001),
        call("127.0.0.1", 8002),
    ]


def test_missing_frontend_packages_fail_before_starting_services(launcher, tmp_path):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "start.py").touch()
    (frontend / "package.json").touch()

    with pytest.raises(launcher.StartupError, match="npm ci"):
        launcher.services(tmp_path)


def test_start_command_launches_both_and_stops_in_reverse_order(launcher, tmp_path, monkeypatch):
    backend = launcher.Service("backend", ("python", "start.py"), tmp_path / "backend")
    frontend_environment = {"OWL_BACKEND_URL": "http://127.0.0.1:8001"}
    frontend = launcher.Service(
        "frontend",
        ("npm", "run", "dev"),
        tmp_path / "frontend",
        environment=frontend_environment,
    )
    backend_process = SimpleNamespace(poll=Mock(return_value=None))
    frontend_process = SimpleNamespace(poll=Mock(return_value=7))
    popen = Mock(side_effect=(backend_process, frontend_process))
    stop = Mock()
    monkeypatch.setattr(launcher, "services", Mock(return_value=(backend, frontend)))
    monkeypatch.setattr(
        launcher,
        "find_available_port",
        Mock(side_effect=(launcher.DEFAULT_BACKEND_PORT, launcher.DEFAULT_FRONTEND_PORT)),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "stop_process", stop)

    assert launcher.run(tmp_path) == 7

    process_options = launcher._process_options()
    assert popen.call_args_list == [
        call(backend.command, cwd=backend.working_directory, **process_options),
        call(
            frontend.command,
            cwd=frontend.working_directory,
            env=frontend_environment,
            **process_options,
        ),
    ]
    assert stop.call_args_list == [call(frontend_process), call(backend_process)]


def test_keyboard_interrupt_stops_both_services(launcher, tmp_path, monkeypatch):
    definitions = (
        launcher.Service("backend", ("python", "start.py"), tmp_path / "backend"),
        launcher.Service("frontend", ("npm", "run", "dev"), tmp_path / "frontend"),
    )
    processes = (
        SimpleNamespace(poll=Mock(return_value=None)),
        SimpleNamespace(poll=Mock(return_value=None)),
    )
    stop = Mock()
    monkeypatch.setattr(launcher, "services", Mock(return_value=definitions))
    monkeypatch.setattr(
        launcher,
        "find_available_port",
        Mock(side_effect=(launcher.DEFAULT_BACKEND_PORT, launcher.DEFAULT_FRONTEND_PORT)),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(side_effect=processes))
    monkeypatch.setattr(launcher.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(launcher, "stop_process", stop)

    assert launcher.run(tmp_path) == 130
    assert stop.call_args_list == [call(processes[1]), call(processes[0])]


def test_busy_ports_are_reported_and_forwarded_to_both_services(
    launcher,
    tmp_path,
    monkeypatch,
    capsys,
):
    backend = launcher.Service("backend", ("python", "start.py"), tmp_path / "backend")
    frontend = launcher.Service("frontend", ("npm", "run", "dev"), tmp_path / "frontend")
    definitions = Mock(return_value=(backend, frontend))
    processes = (
        SimpleNamespace(poll=Mock(return_value=None)),
        SimpleNamespace(poll=Mock(return_value=None)),
    )
    monkeypatch.setattr(launcher, "services", definitions)
    monkeypatch.setattr(
        launcher,
        "find_available_port",
        Mock(side_effect=(8001, 5174)),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(side_effect=processes))
    monkeypatch.setattr(launcher.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(launcher, "stop_process", Mock())

    assert launcher.run(tmp_path) == 130
    definitions.assert_called_once_with(
        tmp_path,
        backend_port=8001,
        frontend_port=5174,
    )
    output = capsys.readouterr().out
    assert "Backend port 8000 is busy; using 8001." in output
    assert "Frontend port 5173 is busy; using 5174." in output
    assert "Open http://127.0.0.1:5174/static/." in output


def test_only_start_command_is_available(launcher):
    assert launcher._parser().parse_args([]).command == "start"
    assert launcher._parser().parse_args(["start"]).command == "start"

    with pytest.raises(SystemExit):
        launcher._parser().parse_args(["restart"])


def test_process_groups_are_isolated_for_coordinated_shutdown(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name=os.name))

    options = launcher._process_options()

    if os.name == "nt":
        assert options == {"creationflags": launcher.subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert options == {"start_new_session": True}
