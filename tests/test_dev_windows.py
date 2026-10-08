"""Validate Windows launcher branches without requiring a Windows host."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "owl_dev", Path(__file__).resolve().parents[1] / "dev.py"
)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class WindowsLauncherTests(unittest.TestCase):
    def test_supervisor_outlives_the_terminal(self):
        flags = {"CREATE_NEW_PROCESS_GROUP": 0x200, "CREATE_NO_WINDOW": 0x08000000, "CREATE_BREAKAWAY_FROM_JOB": 0x01000000}
        calls = []

        def popen(command, **options):
            calls.append(options)
            if options["creationflags"] & flags["CREATE_BREAKAWAY_FROM_JOB"] and len(calls) == 1 and refuse:
                raise PermissionError("Access is denied")
            return "process"

        for refuse in (False, True):
            calls.clear()
            with (
                patch.object(dev, "WINDOWS", True),
                patch.multiple(dev.subprocess, create=True, **flags),
                patch.object(dev.subprocess, "Popen", side_effect=popen),
            ):
                self.assertEqual(dev.launch_supervisor(["python", "dev.py", "_serve"], None), "process")
            final = calls[-1]["creationflags"]
            # Its own hidden console, never the terminal's; leaves the terminal's job when allowed.
            self.assertTrue(final & flags["CREATE_NO_WINDOW"] and final & flags["CREATE_NEW_PROCESS_GROUP"])
            self.assertEqual(bool(final & flags["CREATE_BREAKAWAY_FROM_JOB"]), not refuse)
            self.assertEqual(len(calls), 2 if refuse else 1)
        with patch.object(dev, "WINDOWS", False), patch.object(dev.subprocess, "Popen", side_effect=lambda command, **options: options) as started:
            self.assertTrue(dev.launch_supervisor(["python"], None)["start_new_session"])

    def test_windows_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "backend/.venv/Scripts/python.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            with patch.object(dev, "ROOT", root), patch.object(dev, "WINDOWS", True):
                self.assertEqual(dev.python_path(), str(exe))

    def test_identity_requires_path_and_token(self):
        state = {"pid": 123, "token": "unique-test-token"}
        with (
            patch.object(dev, "WINDOWS", True),
            patch.object(dev.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout=f'python "{dev.ROOT / "dev.py"}" _serve --token unique-test-token',
            )
            self.assertTrue(dev.owned(state))
            self.assertEqual(run.call_args.args[0][0], "powershell.exe")
            run.return_value.stdout = "python unrelated.py"
            self.assertFalse(dev.owned(state))
            run.return_value.returncode = 1
            with self.assertRaises(RuntimeError):
                dev.owned(state)

    def test_tree_termination_is_targeted(self):
        with patch.object(dev.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(dev.kill_windows_tree(123))
            self.assertEqual(
                run.call_args.args[0], ["taskkill", "/PID", "123", "/T", "/F"]
            )

    def test_windows_stop_uses_request_file(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dev, "WINDOWS", True),
            patch.object(dev, "RUNTIME", Path(directory)),
            patch.object(dev, "STATE", Path(directory) / "state.json"),
            patch.object(dev, "owned", return_value=True),
            patch.object(dev, "windows_process_running", return_value=False),
        ):
            dev.STATE.write_text('{"pid":123,"token":"abc"}')
            dev.stop()
            self.assertFalse(dev.STATE.exists())
            self.assertFalse(dev.stop_file("abc").exists())

    def test_identity_timeout_is_not_treated_as_stopped(self):
        with (
            patch.object(dev, "WINDOWS", True),
            patch.object(
                dev.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("powershell.exe", 10),
            ),
            self.assertRaises(dev.ProcessIdentityError),
        ):
            dev.owned({"pid": 123, "token": "abc"})

    def test_timeout_can_stop_cooperatively_without_force_kill(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dev, "WINDOWS", True),
            patch.object(dev, "RUNTIME", Path(directory)),
            patch.object(dev, "STATE", Path(directory) / "state.json"),
            patch.object(dev, "owned", side_effect=dev.ProcessIdentityError("timeout")),
            patch.object(dev, "windows_process_running", return_value=False),
            patch.object(dev, "kill_windows_tree") as kill,
        ):
            dev.STATE.write_text('{"pid":123,"token":"abc"}')
            dev.stop()
            kill.assert_not_called()
            self.assertFalse(dev.STATE.exists())

    def test_unconfirmed_stop_preserves_state_and_request(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dev, "WINDOWS", True),
            patch.object(dev, "RUNTIME", Path(directory)),
            patch.object(dev, "STATE", Path(directory) / "state.json"),
            patch.object(dev, "owned", side_effect=dev.ProcessIdentityError("timeout")),
            patch.object(dev, "windows_process_running", return_value=True),
            patch.object(dev.time, "monotonic", side_effect=[0, 16]),
            patch.object(dev, "kill_windows_tree") as kill,
        ):
            dev.STATE.write_text('{"pid":123,"token":"abc"}')
            with self.assertRaises(dev.ProcessIdentityError):
                dev.stop()
            kill.assert_not_called()
            self.assertTrue(dev.STATE.exists())
            self.assertTrue(dev.stop_file("abc").exists())

    def test_surviving_server_keeps_state_after_supervisor_exit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dev, "WINDOWS", True),
            patch.object(dev, "RUNTIME", Path(directory)),
            patch.object(dev, "STATE", Path(directory) / "state.json"),
            patch.object(dev, "owned", return_value=False),
            patch.object(dev.socket, "socket") as socket,
        ):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            dev.STATE.write_text('{"pid":123,"token":"abc","backend_port":8000}')
            with self.assertRaisesRegex(RuntimeError, "Port 8000 is still serving"):
                dev.stop()
            self.assertTrue(dev.STATE.exists())

    def test_free_ports_allow_state_cleanup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dev, "WINDOWS", True),
            patch.object(dev, "RUNTIME", Path(directory)),
            patch.object(dev, "STATE", Path(directory) / "state.json"),
            patch.object(dev, "owned", return_value=False),
            patch.object(dev.socket, "socket") as socket,
        ):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 10061
            dev.STATE.write_text('{"pid":123,"token":"abc","backend_port":8000}')
            dev.stop()
            self.assertFalse(dev.STATE.exists())
