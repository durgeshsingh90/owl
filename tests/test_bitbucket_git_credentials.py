from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bitbucket_search.services import git_sync

_USERNAME = "owl+reader@example.com"
_SPECIAL_VALUE = "synthetic:token /?&=+%é"
_REMOTE_URL = "https://Bitbucket.org/workspace/private-repository.git"


def _credential() -> git_sync.GitHTTPSCredential:
    return git_sync.GitHTTPSCredential(username=_USERNAME, token=_SPECIAL_VALUE)


def _authorization_value() -> str:
    encoded = base64.b64encode(f"{_USERNAME}:{_SPECIAL_VALUE}".encode()).decode("ascii")
    return f"Authorization: Basic {encoded}"


def _without_inherited_git_config(monkeypatch) -> None:
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for name in tuple(os.environ):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(name, raising=False)


def test_https_credential_is_host_scoped_and_preserves_inherited_child_config(monkeypatch):
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "manager")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.sslVerify")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")
    parent_environment = os.environ.copy()

    with git_sync._git_https_authorization(
        "https://Bitbucket.org:8443/workspace/repository.git", _credential()
    ):
        child_environment = git_sync._git_environment()

    assert child_environment["GIT_CONFIG_COUNT"] == "5"
    assert child_environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert child_environment["GIT_CONFIG_VALUE_0"] == "manager"
    assert child_environment["GIT_CONFIG_KEY_1"] == "http.sslVerify"
    assert child_environment["GIT_CONFIG_VALUE_1"] == "true"
    assert child_environment["GIT_CONFIG_KEY_2"] == "core.longpaths"
    assert child_environment["GIT_CONFIG_VALUE_2"] == "true"
    assert child_environment["GIT_CONFIG_KEY_3"] == "credential.helper"
    assert child_environment["GIT_CONFIG_VALUE_3"] == ""
    assert child_environment["GIT_CONFIG_KEY_4"] == "http.https://bitbucket.org:8443/.extraHeader"
    assert child_environment["GIT_CONFIG_VALUE_4"] == _authorization_value()
    assert os.environ == parent_environment
    assert _SPECIAL_VALUE not in repr(_credential())

    environment_after_context = git_sync._git_environment()
    assert environment_after_context["GIT_CONFIG_COUNT"] == "3"
    assert not any(value == _authorization_value() for value in environment_after_context.values())


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_real_git_matches_header_only_to_the_https_host(monkeypatch):
    _without_inherited_git_config(monkeypatch)
    with git_sync._git_https_authorization(_REMOTE_URL, _credential()):
        child_environment = git_sync._git_environment()

    matching = subprocess.run(
        ("git", "config", "--get-urlmatch", "http.extraHeader", _REMOTE_URL),
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    unrelated = subprocess.run(
        (
            "git",
            "config",
            "--get-urlmatch",
            "http.extraHeader",
            "https://unrelated.invalid/workspace/repository.git",
        ),
        env=child_environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert matching.returncode == 0
    assert matching.stdout.strip() == _authorization_value()
    assert unrelated.returncode != 0
    assert unrelated.stdout == ""


def test_https_credential_never_enters_command_url_parent_environment_or_output(monkeypatch):
    _without_inherited_git_config(monkeypatch)
    authorization = _authorization_value()
    encoded = authorization.removeprefix("Authorization: Basic ")
    process = Mock(returncode=1)
    process.communicate.return_value = (
        "",
        f"fatal: synthetic server echoed {_USERNAME}, {_SPECIAL_VALUE}, and {authorization}\n",
    )
    spawn = Mock(return_value=process)
    monkeypatch.setattr(git_sync.subprocess, "Popen", spawn)
    output = Mock()
    monkeypatch.setattr(git_sync, "emit_git_output", output)
    parent_environment = os.environ.copy()

    with (
        git_sync._git_https_authorization(_REMOTE_URL, _credential()),
        pytest.raises(git_sync.RepositorySyncError),
    ):
        git_sync._run_capture(
            ("git", "ls-remote", "--", _REMOTE_URL, "HEAD"),
            failure_code="connection_check_failed",
            failure_summary="The connection check failed.",
        )

    arguments, options = spawn.call_args
    rendered_arguments = " ".join(arguments[0])
    assert rendered_arguments.endswith(f"-- {_REMOTE_URL} HEAD")
    assert _USERNAME not in rendered_arguments
    assert _SPECIAL_VALUE not in rendered_arguments
    assert encoded not in rendered_arguments
    assert options["env"]["GIT_CONFIG_VALUE_1"] == authorization
    emitted = str(output.call_args_list)
    assert "[credential removed]" in emitted
    assert _USERNAME not in emitted
    assert _SPECIAL_VALUE not in emitted
    assert encoded not in emitted
    assert authorization not in emitted
    assert os.environ == parent_environment


def test_synchronize_repository_resets_credential_context_after_failure(monkeypatch):
    _without_inherited_git_config(monkeypatch)
    repository = SimpleNamespace(pk=71, remote_url=_REMOTE_URL)
    observed_environment: dict[str, str] = {}
    failure = RuntimeError("synthetic sync failure")

    def fail_sync(_repository, *, operation, progress_callback):
        assert operation == "clone"
        assert progress_callback is not None
        observed_environment.update(git_sync._git_environment())
        raise failure

    monkeypatch.setattr(git_sync, "_synchronize_repository", fail_sync)

    with pytest.raises(RuntimeError) as captured:
        git_sync.synchronize_repository(
            repository,
            operation="clone",
            progress_callback=lambda *_args: None,
            https_credential=_credential(),
        )

    assert captured.value is failure
    assert observed_environment["GIT_CONFIG_VALUE_1"] == _authorization_value()
    assert "GIT_CONFIG_COUNT" not in git_sync._git_environment()


@pytest.mark.parametrize(
    ("remote_url", "credential"),
    [
        ("ssh://git@bitbucket.org/workspace/repository.git", _credential()),
        ("git@bitbucket.org:workspace/repository.git", _credential()),
        (_REMOTE_URL, None),
    ],
)
def test_ssh_and_absent_credentials_leave_git_config_unchanged(monkeypatch, remote_url, credential):
    _without_inherited_git_config(monkeypatch)

    with git_sync._git_https_authorization(remote_url, credential):
        child_environment = git_sync._git_environment()

    assert "GIT_CONFIG_COUNT" not in child_environment
    assert _SPECIAL_VALUE not in str(child_environment)


@pytest.mark.parametrize("count", ["not-a-number", "-1"])
def test_https_credential_rejects_invalid_inherited_git_config_and_resets(monkeypatch, count):
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)

    with (
        git_sync._git_https_authorization(_REMOTE_URL, _credential()),
        pytest.raises(git_sync.RepositorySyncError) as captured,
    ):
        git_sync._git_environment()

    assert captured.value.code == "invalid_git_environment"
    assert git_sync._ACTIVE_GIT_HTTPS_AUTHORIZATION.get() is None


def test_git_lfs_child_receives_the_same_scoped_credential(monkeypatch):
    _without_inherited_git_config(monkeypatch)
    process = Mock(stdout=io.StringIO(""), returncode=0)
    process.poll.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(git_sync.subprocess, "Popen", spawn)
    monkeypatch.setattr(git_sync.shutil, "which", Mock(return_value="git-lfs"))

    with git_sync._git_https_authorization(_REMOTE_URL, _credential()):
        git_sync._hydrate_git_lfs_documents(
            Path("/synthetic/repository"),
            pointer_count=1,
            progress_callback=lambda *_args: None,
        )

    arguments, options = spawn.call_args
    assert arguments[0][:4] == ["git", "-C", "/synthetic/repository", "lfs"]
    assert _SPECIAL_VALUE not in " ".join(arguments[0])
    assert options["env"]["GIT_CONFIG_VALUE_1"] == _authorization_value()
