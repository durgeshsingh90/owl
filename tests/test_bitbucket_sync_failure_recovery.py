from __future__ import annotations

import io
import shutil
import subprocess
from unittest.mock import Mock

import pytest
from django.db import OperationalError

from bitbucket_search.models import (
    BitbucketRepository,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services import git_sync, pdf_catalog, repository_sync


@pytest.mark.parametrize("failure_point", ("progress", "reader_start"))
def test_streaming_git_is_reaped_when_initial_worker_setup_fails(monkeypatch, failure_point):
    process = Mock(stderr=io.StringIO(""), returncode=None)
    process.poll.return_value = None
    reader = Mock()
    failure = OperationalError("synthetic private worker failure")
    progress = Mock()
    if failure_point == "progress":
        progress.side_effect = failure
    else:
        reader.start.side_effect = failure
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(git_sync.threading, "Thread", Mock(return_value=reader))

    with pytest.raises(OperationalError) as captured:
        git_sync._run_streaming(
            ("git", "fetch"),
            phase="fetching",
            progress_start=8,
            progress_end=70,
            status_message="Refreshing repository data in the background…",
            progress_callback=progress,
            failure_code="fetch_failed",
            failure_summary="Git refresh failed.",
        )

    assert captured.value is failure
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()
    if failure_point == "progress":
        reader.join.assert_called_once_with(timeout=2)
    else:
        reader.join.assert_not_called()


@pytest.mark.parametrize("mode", (b"160000", b"040000", b"120000"))
def test_catalogue_does_not_treat_non_file_git_entries_as_missing_pdfs(tmp_path, mode):
    # Gitlinks (uninitialized submodules), trees and symbolic links can have a
    # PDF-looking name without representing a tracked, readable PDF file.
    record = mode + b" " + b"a" * 40 + b" 0\tvendor.pdf"
    assert pdf_catalog._parse_tree_pdfs(tmp_path, iter((record,))) == ()


@pytest.mark.parametrize("mode", (b"100644", b"100755"))
def test_catalogue_still_requires_real_files_for_regular_pdf_blobs(tmp_path, mode):
    record = mode + b" " + b"a" * 40 + b" 0\tGuide.pdf"
    with pytest.raises(git_sync.RepositorySyncError) as captured:
        pdf_catalog._parse_tree_pdfs(tmp_path, iter((record,)))
    assert captured.value.code == "missing_pdf_file"
    (tmp_path / "Guide.pdf").write_bytes(b"%PDF synthetic fixture")
    documents = pdf_catalog._parse_tree_pdfs(tmp_path, iter((record,)))
    assert len(documents) == 1
    assert documents[0].relative_path == "Guide.pdf"


@pytest.mark.django_db
@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_clone_with_pdf_named_gitlink_catalogues_only_real_pdfs(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    source = tmp_path / "source"

    def git(*arguments, cwd=None):
        return subprocess.run(
            ["git", *map(str, arguments)],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "-b", "main", source)
    git("config", "user.name", "Synthetic OWL", cwd=source)
    git("config", "user.email", "owl@example.invalid", cwd=source)
    (source / "Guide.pdf").write_bytes(b"%PDF synthetic fixture")
    git("add", "Guide.pdf", cwd=source)
    git("commit", "-m", "Add synthetic document", cwd=source)
    commit = git("rev-parse", "HEAD", cwd=source)
    git("update-index", "--add", "--cacheinfo", "160000", commit, "vendor.pdf", cwd=source)
    git("commit", "-m", "Add synthetic gitlink", cwd=source)
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic Gitlink",
        canonical_remote_key="example.invalid/team/gitlink-fixture",
        remote_url=source.as_uri(),
    )
    repository.local_path = str(git_sync.managed_repository_path(repository))
    repository.save(update_fields=("local_path", "updated_at"))
    queued = repository_sync.queue_repository_refresh(repository.pk)
    claimed = repository_sync.claim_next_job()
    assert claimed.pk == queued.job.pk

    completed = repository_sync.execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.sync_state == RepositorySyncState.READY
    assert repository.pdf_count == 1
    assert repository.pdf_documents.get().relative_path == "Guide.pdf"
