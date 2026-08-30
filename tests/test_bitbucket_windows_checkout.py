from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from bitbucket_search.models import BitbucketRepository
from bitbucket_search.services import git_sync
from bitbucket_search.services.filesystem_paths import display_path, filesystem_path


def _windows_error(code=5):
    error = PermissionError(errno.EACCES, "private checkout path")
    error.winerror = code
    return error


def _missing_error():
    error = FileNotFoundError(errno.ENOENT, "private document path")
    error.winerror = 3
    return error


@pytest.fixture
def events(caplog, monkeypatch):
    boundary = logging.getLogger("owl.bitbucket")
    caplog.set_level(logging.DEBUG, logger=boundary.name)
    monkeypatch.setattr(boundary, "propagate", False)
    boundary.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        boundary.removeHandler(caplog.handler)


def _staging(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    staging = settings.BITBUCKET_TEMP_ROOT / ("repository-42-" + "a" * 32)
    staging.mkdir(parents=True)
    return staging


def test_windows_child_git_longpaths_preserves_inherited_config_and_parent_environment(monkeypatch):
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "manager")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.longpaths")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
    parent_environment = os.environ.copy()

    result = git_sync._git_environment()

    assert result["GIT_CONFIG_COUNT"] == "3"
    assert result["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert result["GIT_CONFIG_VALUE_0"] == "manager"
    assert result["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert result["GIT_CONFIG_VALUE_1"] == "false"
    assert result["GIT_CONFIG_KEY_2"] == "core.longpaths"
    assert result["GIT_CONFIG_VALUE_2"] == "true"
    assert os.environ == parent_environment


def test_non_windows_git_config_is_not_changed(monkeypatch):
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    assert "GIT_CONFIG_COUNT" not in git_sync._git_environment()


@pytest.mark.parametrize("count", ["invalid", "-1"])
def test_invalid_inherited_git_config_is_not_silently_discarded(monkeypatch, count):
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._git_environment()
    assert captured.value.code == "invalid_git_environment"


def test_scan_does_not_report_success_after_a_document_disappears(tmp_path, monkeypatch, events):
    document = tmp_path / "private-guide.pdf"
    document.write_bytes(b"synthetic PDF")
    original_stat = Path.stat
    failure = _missing_error()

    def inaccessible(candidate, *args, **kwargs):
        if display_path(candidate) == document:
            raise failure
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync.discover_documents(tmp_path)
    assert captured.value.code == "document_missing"
    assert captured.value.__cause__ is failure
    assert "event=git_document_stat_failed" in events.text
    assert "winerror=3" in events.text
    assert "private-guide.pdf" not in events.text
    assert "private document path" not in events.text
    assert "git_document_scan_completed" not in events.text


@pytest.mark.parametrize("error", [_windows_error(), _missing_error()])
def test_scan_fails_instead_of_silently_ignoring_inaccessible_subdirectories(
    tmp_path, monkeypatch, error
):
    def inaccessible(_path, *, followlinks, onerror):
        assert followlinks is False
        onerror(error)

    monkeypatch.setattr(git_sync.os, "walk", inaccessible)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync.discover_documents(tmp_path)
    assert captured.value.__cause__ is error


def test_unreadable_lfs_pointer_is_not_counted_as_a_downloaded_document(tmp_path, monkeypatch):
    document = tmp_path / "guide.pdf"
    document.write_bytes(b"small file which might be an LFS pointer")
    failure = _windows_error()
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=failure))
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync.discover_documents(tmp_path)
    assert captured.value.code == "document_access_denied"
    assert captured.value.__cause__ is failure


def test_document_scan_preserves_symlink_boundary_and_ignores_non_regular_files(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"outside")
    (checkout / "linked.pdf").symlink_to(outside / "secret.pdf")
    (checkout / "linked-directory").symlink_to(outside, target_is_directory=True)
    (checkout / "real.pdf").write_bytes(b"inside")
    stats = git_sync.discover_documents(checkout)
    assert stats == git_sync.DocumentStats(1, 0, 6)


def test_missing_long_windows_path_is_a_possibility_not_a_definitive_diagnosis():
    long_path = Path("C:/checkout") / ("a" * 100) / ("b" * 100) / ("c" * 100 + ".pdf")
    error = git_sync._document_scan_error(_missing_error(), path=long_path)
    assert error.code == "document_path_unavailable"
    assert "may be a long-path" in error.summary
    assert str(len(str(long_path))) in error.summary
    assert str(long_path) not in error.summary
    short = git_sync._document_scan_error(_missing_error(), path=Path("C:/checkout/guide.pdf"))
    assert short.code == "document_missing"


@pytest.mark.parametrize("code", [5, 32, 33])
def test_windows_publish_retries_temporary_locks_and_keeps_heartbeat(tmp_path, monkeypatch, code):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "repositories" / "checkout"
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    real_replace = os.replace
    calls = []

    def replace(source, destination):
        calls.append((source, destination))
        if len(calls) < 3:
            raise _windows_error(code)
        real_replace(source, destination)

    monkeypatch.setattr(git_sync.os, "replace", replace)
    sleep = Mock()
    monkeypatch.setattr(git_sync.time, "sleep", sleep)
    heartbeat = Mock()

    git_sync._publish_staged_checkout(staging, target, heartbeat_callback=heartbeat)

    assert target.is_dir()
    assert not staging.exists()
    assert len(calls) == 3
    assert sleep.call_count == 2
    assert heartbeat.call_count == 4


def test_persistent_windows_publish_failure_is_classified_and_bounded(
    tmp_path, monkeypatch, events
):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "checkout"
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    failure = _windows_error()
    replace = Mock(side_effect=failure)
    monkeypatch.setattr(git_sync.os, "replace", replace)
    monkeypatch.setattr(git_sync.time, "sleep", Mock())

    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._publish_staged_checkout(staging, target, heartbeat_callback=Mock())

    assert captured.value.code == "checkout_publish_failed"
    assert captured.value.__cause__ is failure
    assert replace.call_count == len(git_sync._WINDOWS_PUBLISH_RETRY_DELAYS) + 1
    assert staging.is_dir()
    assert not target.exists()
    assert "event=git_checkout_publish_failed" in events.text
    assert "winerror=5" in events.text
    assert "private checkout path" not in events.text


@pytest.mark.parametrize(
    "failure", [OSError(errno.EXDEV, "private"), OSError(errno.ENOSPC, "private")]
)
def test_non_transient_publish_failure_is_not_retried(tmp_path, monkeypatch, failure):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)
    replace = Mock(side_effect=failure)
    monkeypatch.setattr(git_sync.os, "replace", replace)
    sleep = Mock()
    monkeypatch.setattr(git_sync.time, "sleep", sleep)
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._publish_staged_checkout(staging, tmp_path / "target", heartbeat_callback=Mock())
    assert captured.value.code == "checkout_publish_failed"
    replace.assert_called_once()
    sleep.assert_not_called()


def test_publish_does_not_overwrite_a_destination_that_appears_during_retry(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)

    def collision(_source, _target):
        target.mkdir()
        (target / "preserve.txt").write_text("preserve")
        raise _windows_error()

    replace = Mock(side_effect=collision)
    monkeypatch.setattr(git_sync.os, "replace", replace)
    monkeypatch.setattr(git_sync.time, "sleep", Mock())
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._publish_staged_checkout(staging, target, heartbeat_callback=Mock())
    assert captured.value.code == "destination_exists"
    assert (target / "preserve.txt").read_text() == "preserve"
    replace.assert_called_once()


def test_cleanup_clears_windows_readonly_bit_only_inside_private_staging(
    tmp_path, settings, monkeypatch
):
    staging = _staging(tmp_path, settings)
    git_object = staging / "object.pack"
    git_object.write_bytes(b"synthetic Git pack")
    git_object.chmod(stat.S_IREAD)
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)

    def readonly_tree(_path, *, onexc):
        onexc(os.unlink, str(filesystem_path(git_object)), _windows_error())
        staging.rmdir()

    monkeypatch.setattr(git_sync.shutil, "rmtree", readonly_tree)
    git_sync._remove_staging_directory(staging)
    assert not staging.exists()


def test_cleanup_does_not_chmod_or_remove_an_outside_callback_path(tmp_path, settings, monkeypatch):
    staging = _staging(tmp_path, settings)
    outside = tmp_path / "preserve.pack"
    outside.write_bytes(b"preserve")
    outside.chmod(stat.S_IREAD)
    original_mode = outside.stat().st_mode
    monkeypatch.setattr(git_sync, "_IS_WINDOWS", True)

    def unsafe_callback(_path, *, onexc):
        onexc(os.unlink, str(filesystem_path(outside)), _windows_error())

    monkeypatch.setattr(git_sync.shutil, "rmtree", unsafe_callback)
    git_sync._remove_staging_directory(staging)
    assert outside.read_bytes() == b"preserve"
    assert outside.stat().st_mode == original_mode


@pytest.mark.parametrize("kind", ["outside", "wrong-name", "symlink"])
def test_cleanup_refuses_anything_other_than_owned_staging(tmp_path, settings, monkeypatch, kind):
    staging = _staging(tmp_path, settings)
    if kind == "outside":
        candidate = tmp_path / staging.name
        candidate.mkdir()
    elif kind == "wrong-name":
        candidate = staging.parent / "repository-manual-data"
        candidate.mkdir()
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        candidate = staging.parent / ("repository-42-" + "b" * 32)
        candidate.symlink_to(outside, target_is_directory=True)
    remove = Mock()
    monkeypatch.setattr(git_sync.shutil, "rmtree", remove)
    git_sync._remove_staging_directory(candidate)
    remove.assert_not_called()
    assert candidate.exists()


@pytest.mark.django_db
def test_failed_cleanup_never_masks_primary_clone_failure(tmp_path, settings, monkeypatch, events):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic",
        canonical_remote_key="example.invalid/team/synthetic",
        remote_url="https://example.invalid/team/synthetic.git",
    )
    primary = git_sync.RepositorySyncError("checkout_failed", "Synthetic primary failure.")
    monkeypatch.setattr(git_sync, "_run_streaming", Mock(side_effect=primary))
    monkeypatch.setattr(git_sync.shutil, "rmtree", Mock(side_effect=_windows_error()))
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        git_sync._clone(repository, Mock())
    assert captured.value is primary
    assert "git_staging_cleanup_failed" in events.text
    assert "winerror=5" in events.text


@pytest.mark.django_db
def test_conservative_clone_uses_fresh_staging_when_failed_clone_cannot_be_cleaned(
    tmp_path, settings, monkeypatch
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "tmp"
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic",
        canonical_remote_key="example.invalid/team/synthetic",
        remote_url="https://example.invalid/team/synthetic.git",
    )
    attempted = []

    def clone(arguments, **_kwargs):
        directory = Path(arguments[-1])
        assert not directory.exists()
        directory.mkdir()
        attempted.append(directory)
        raise git_sync.RepositorySyncError("clone_failed", "Synthetic clone failure.")

    monkeypatch.setattr(git_sync, "_run_streaming", clone)
    monkeypatch.setattr(git_sync.shutil, "rmtree", Mock(side_effect=_windows_error()))
    with pytest.raises(git_sync.RepositorySyncError):
        git_sync._clone(repository, Mock())
    assert len(attempted) == 2
    assert attempted[0] != attempted[1]
    assert all(path.is_dir() for path in attempted)
