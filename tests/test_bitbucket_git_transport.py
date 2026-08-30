from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import OperationalError

from bitbucket_search.models import RepositorySyncPhase
from bitbucket_search.services import git_sync

_SYNTHETIC_USERNAME = "synthetic-transport-user"
_SYNTHETIC_PASSWORD = "not-a-real-secret"
_SYNTHETIC_CREDENTIAL_URL = f"https://{_SYNTHETIC_USERNAME}:{_SYNTHETIC_PASSWORD}@private.invalid"


def _repository():
    return SimpleNamespace(
        pk=41,
        display_name="Synthetic",
        remote_url="https://example.invalid/team/synthetic.git",
        default_branch="main",
    )


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("fatal: Could not resolve host: private.invalid", "connection_unreachable"),
        ("ssh: Could not resolve hostname private.invalid", "connection_unreachable"),
        ("Failed to connect to private.invalid port 443", "connection_unreachable"),
        ("Network is unreachable", "connection_unreachable"),
        ("Connection timed out", "connection_timeout"),
        (f"Authentication failed for {_SYNTHETIC_CREDENTIAL_URL}", "connection_auth_failed"),
        ("Permission denied (publickey)", "connection_auth_failed"),
        ("The requested URL returned error: 403", "connection_auth_failed"),
        ("Repository not found", "connection_auth_failed"),
        (
            "SSL certificate problem: unable to get local issuer certificate",
            "connection_tls_failed",
        ),
        ("server certificate verification failed", "connection_tls_failed"),
        ("Host key verification failed", "connection_tls_failed"),
        ("Something unexpected at private.invalid secret", "connection_check_failed"),
    ],
)
def test_connection_failure_classification_uses_fixed_safe_text(stderr, code):
    failure = git_sync._connection_failure(stderr)
    assert failure.code == code
    assert "private.invalid" not in failure.summary
    assert "secret" not in failure.summary
    if code in {"connection_unreachable", "connection_timeout"}:
        assert "if this host requires it" in failure.summary


def test_preflight_uses_real_remote_and_local_git_config_without_showing_metadata(
    monkeypatch, tmp_path
):
    repository = _repository()
    process = Mock(returncode=0)
    process.communicate.return_value = (
        "ref: refs/heads/private-branch\tHEAD\nprivate-hash\tHEAD\n",
        "",
    )
    spawn = Mock(return_value=process)
    monkeypatch.setattr(git_sync.subprocess, "Popen", spawn)
    output = Mock()
    monkeypatch.setattr(git_sync, "emit_git_output", output)
    progress = Mock()

    git_sync._check_connection(repository, progress, repository_path=tmp_path)

    arguments, options = spawn.call_args
    assert arguments[0] == [
        "git",
        "-C",
        str(tmp_path),
        "ls-remote",
        "--symref",
        "--",
        repository.remote_url,
        "HEAD",
    ]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GCM_INTERACTIVE"] == "Never"
    assert options["env"]["SSH_ASKPASS_REQUIRE"] == "never"
    assert "private-branch" not in str(output.call_args_list)
    assert "private-hash" not in str(output.call_args_list)
    assert [call.args[0] for call in output.call_args_list] == [
        "Checking repository connection with Git…",
        "Repository connection verified.",
    ]
    assert all(
        call.args[0] == RepositorySyncPhase.CHECKING_CONNECTION for call in progress.call_args_list
    )
    assert all(call.kwargs["timeout"] <= 1 for call in process.communicate.call_args_list)


def test_preflight_timeout_has_separate_limit_heartbeats_and_reaps_process(monkeypatch, settings):
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 3600
    settings.BITBUCKET_CONNECTION_TIMEOUT_SECONDS = 2
    process = Mock(returncode=None, pid=None)
    process.poll.return_value = None
    process.communicate.side_effect = [subprocess.TimeoutExpired("git", 1), ("", "")]
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        git_sync, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1, 3, 3]))
    )
    progress = Mock()

    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._check_connection(_repository(), progress)

    assert captured.value.code == "connection_timeout"
    assert len(progress.call_args_list) == 2  # start and silent-period heartbeat
    process.kill.assert_called_once_with()
    assert process.communicate.call_args_list[-1].kwargs["timeout"] == 2


def test_preflight_heartbeat_failure_cleans_up_and_preserves_primary_error(monkeypatch):
    process = Mock(returncode=None, pid=None)
    process.poll.return_value = None
    process.communicate.side_effect = [subprocess.TimeoutExpired("git", 1), ("", "")]
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    failure = OperationalError("synthetic heartbeat failure")
    progress = Mock(side_effect=[None, failure])
    with pytest.raises(OperationalError) as captured:
        git_sync._check_connection(_repository(), progress)
    assert captured.value is failure
    process.kill.assert_called_once_with()
    assert process.communicate.call_count == 2


def test_preflight_spawn_failure_stops_with_actionable_code(monkeypatch):
    monkeypatch.setattr(
        git_sync.subprocess, "Popen", Mock(side_effect=FileNotFoundError("private"))
    )
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._check_connection(_repository(), Mock())
    assert captured.value.code == "connection_check_failed"
    assert "private" not in captured.value.summary


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup contract")
@pytest.mark.parametrize("isolated", [True, False])
def test_abort_only_kills_transport_process_group_when_owned(monkeypatch, isolated):
    process = Mock(returncode=None, pid=987654)
    process.poll.return_value = None
    process.communicate.return_value = ("private metadata", "synthetic diagnostic")
    kill_group = Mock()
    monkeypatch.setattr(git_sync.os, "killpg", kill_group)
    assert git_sync._abort_capture(process, isolate_process=isolated) == "synthetic diagnostic"
    assert kill_group.call_count == (1 if isolated else 0)
    process.kill.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=2)


def test_abort_cleanup_is_bounded_if_an_escaped_helper_keeps_pipes_open():
    process = Mock(returncode=-9, pid=None)
    process.poll.return_value = -9
    process.communicate.side_effect = subprocess.TimeoutExpired("git", 2)
    assert git_sync._abort_capture(process, isolate_process=True) == ""
    process.communicate.assert_called_once_with(timeout=2)
    process.wait.assert_called_once_with(timeout=2)
    process.kill.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX isolated helper process-group cleanup")
def test_abort_reaps_helper_pipes_even_after_git_group_leader_has_exited():
    # Disposable local processes only: the short-lived parent models Git while
    # its child models an SSH/credential helper holding the output pipe open.
    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.wait(timeout=3)
        assert process.returncode == 0
        started = time.monotonic()
        assert git_sync._abort_capture(process, isolate_process=True) == ""
        assert time.monotonic() - started < 1.5
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, git_sync.signal.SIGKILL)
        process.stdout.close()
        process.stderr.close()


def test_failed_preflight_prevents_clone_and_staging_creation(tmp_path, settings, monkeypatch):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "staging"
    check = Mock(side_effect=git_sync.RepositorySyncError("connection_unreachable", "Unavailable."))
    monkeypatch.setattr(git_sync, "_check_connection", check)
    clone = Mock()
    monkeypatch.setattr(git_sync, "_run_streaming", clone)
    scan = Mock()
    monkeypatch.setattr(git_sync, "discover_documents", scan)
    with pytest.raises(git_sync.RepositorySyncError):
        git_sync._clone(_repository(), Mock())
    check.assert_called_once()
    clone.assert_not_called()
    scan.assert_not_called()
    assert not settings.BITBUCKET_TEMP_ROOT.exists()


def test_conservative_clone_also_rechecks_connection_before_another_download(
    tmp_path, settings, monkeypatch
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "staging"
    unavailable = git_sync.RepositorySyncError("connection_unreachable", "Unavailable.")
    check = Mock(side_effect=[None, unavailable])
    monkeypatch.setattr(git_sync, "_check_connection", check)
    clone = Mock(side_effect=git_sync.RepositorySyncError("clone_failed", "Synthetic failure."))
    monkeypatch.setattr(git_sync, "_run_streaming", clone)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._clone(_repository(), Mock())
    assert captured.value is unavailable
    assert check.call_count == 2
    clone.assert_called_once()


@pytest.mark.parametrize("dirty", [False, True])
def test_refresh_preflight_follows_dirty_check_and_prevents_fetch_and_catalogue(
    tmp_path, monkeypatch, dirty
):
    monkeypatch.setattr(git_sync, "_validated_existing_path", Mock(return_value=tmp_path))
    monkeypatch.setattr(git_sync, "_verify_origin", Mock())
    monkeypatch.setattr(git_sync, "_branch", Mock(return_value="main"))
    monkeypatch.setattr(git_sync, "_git_value", Mock(return_value=" M guide.pdf" if dirty else ""))
    monkeypatch.setattr(git_sync, "_commit", Mock(return_value="a" * 40))
    check = Mock(side_effect=git_sync.RepositorySyncError("connection_auth_failed", "Unavailable."))
    monkeypatch.setattr(git_sync, "_check_connection", check)
    transport = Mock()
    monkeypatch.setattr(git_sync, "_run_streaming", transport)
    scan = Mock()
    monkeypatch.setattr(git_sync, "discover_documents", scan)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._refresh(_repository(), Mock())
    assert captured.value.code == ("dirty_working_tree" if dirty else "connection_auth_failed")
    assert check.call_count == (0 if dirty else 1)
    transport.assert_not_called()
    scan.assert_not_called()


def test_console_lines_keep_chunks_together_crlf_and_omit_entire_oversized_line(monkeypatch):
    output = Mock()
    monkeypatch.setattr(git_sync, "emit_git_output", output)
    lines = git_sync._ConsoleLines("clone")
    lines.feed("Authorization: Bea")
    assert output.call_count == 0
    lines.feed("rer complete-secret\r\nCounting objects: 2")
    assert output.call_count == 1
    lines.feed("0%\r" + "X" * 16385 + "private-token-tail\nSafe next line")
    lines.finish()
    assert [call.args[0] for call in output.call_args_list] == [
        "Authorization: Bearer complete-secret",
        "Counting objects: 20%",
        "[Git output line omitted: too long]",
        "Safe next line",
    ]


def test_streaming_merges_output_and_emits_on_orchestration_thread(monkeypatch):
    process = Mock(
        stdout=io.StringIO(
            "Counting objects: 50%\nUpdating files: 100%\nfatal: synthetic failure\n"
        ),
        returncode=1,
    )
    process.poll.return_value = 1
    spawn = Mock(return_value=process)
    monkeypatch.setattr(git_sync.subprocess, "Popen", spawn)
    emitted = []
    thread_id = threading.get_ident()

    def output(text, **kwargs):
        emitted.append((text, kwargs, threading.get_ident()))

    monkeypatch.setattr(git_sync, "emit_git_output", output)
    with pytest.raises(git_sync.RepositorySyncError):
        git_sync._run_streaming(
            ("git", "clone", "synthetic"),
            phase="cloning",
            progress_start=5,
            progress_end=70,
            status_message="Synthetic",
            progress_callback=Mock(),
            failure_code="clone_failed",
            failure_summary="Synthetic failure.",
        )
    assert spawn.call_args.kwargs["stdout"] == subprocess.PIPE
    assert spawn.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert len(emitted) == 3
    assert all(observed_id == thread_id for _text, _kwargs, observed_id in emitted)
    assert emitted[-1][1]["level"] == "error"
    assert process.stdout.closed


def test_capture_never_exposes_successful_metadata_stdout_but_forwards_stderr(monkeypatch):
    process = Mock(returncode=0)
    process.communicate.return_value = (
        "private tracked document\0private metadata",
        "warning: synthetic warning\n",
    )
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    output = Mock()
    monkeypatch.setattr(git_sync, "emit_git_output", output)
    result = git_sync._run_capture(
        ("git", "ls-files"), failure_code="failed", failure_summary="Failed."
    )
    assert result == "private tracked document\0private metadata"
    output.assert_called_once_with(
        "warning: synthetic warning", operation="ls-files", level="warning"
    )


def test_inherited_wire_trace_is_disabled_without_mutating_parent_environment(monkeypatch):
    for variable in (
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE_PACKET",
        "GIT_TRACE2_EVENT",
        "GIT_CURL_VERBOSE",
    ):
        monkeypatch.setenv(variable, "1")
    original = os.environ.copy()
    environment = git_sync._git_environment()
    assert not any(key.startswith("GIT_TRACE") or key == "GIT_CURL_VERBOSE" for key in environment)
    assert os.environ == original


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_real_local_git_preflight_accepts_option_boundary_and_does_not_modify_checkout(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    repository = _repository()
    repository.remote_url = source.as_uri()
    before = sorted(path.relative_to(source) for path in source.rglob("*"))
    output = Mock()
    monkeypatch.setattr(git_sync, "emit_git_output", output)
    git_sync._check_connection(repository, Mock())
    after = sorted(path.relative_to(source) for path in source.rglob("*"))
    assert before == after
    assert "Repository connection verified." in str(output.call_args_list)
