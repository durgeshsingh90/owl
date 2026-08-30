from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocumentAddedEvidence,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services import git_sync, repository_sync
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.repository_sync import (
    claim_next_job,
    execute_claimed_job,
    queue_repository_refresh,
)
from bitbucket_search.services.repository_urls import (
    RepositoryURLValidationError,
    normalize_repository_url,
)

pytestmark = pytest.mark.django_db


def _git(*arguments: object, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *(str(argument) for argument in arguments)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _create_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git("init", "--bare", remote)
    _git("init", "-b", "main", source)
    _git("config", "user.name", "Synthetic OWL", cwd=source)
    _git("config", "user.email", "owl@example.invalid", cwd=source)
    (source / "docs").mkdir()
    (source / "diagrams").mkdir()
    (source / "src").mkdir()
    (source / "docs" / "Architecture.PDF").write_bytes(b"synthetic pdf")
    (source / "diagrams" / "Network.VsDx").write_bytes(b"synthetic vsdx")
    (source / "src" / "large-source.bin").write_bytes(b"not requested")
    (source / "README.md").write_text("not requested", encoding="utf-8")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Initial synthetic documents", cwd=source)
    _git("remote", "add", "origin", remote.as_uri(), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    return remote, source


def _queued_repository(remote: Path) -> tuple[BitbucketRepository, RepositorySyncJob]:
    repository = BitbucketRepository.objects.create(
        display_name="synthetic-documents",
        canonical_remote_key="synthetic.invalid/workspace/synthetic-documents",
        remote_url=remote.as_uri(),
        sync_state=RepositorySyncState.QUEUED,
    )
    repository.local_path = str(managed_repository_path(repository))
    repository.save(update_fields=("local_path", "updated_at"))
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    return repository, job


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_background_clone_then_refresh_materializes_only_pdf_and_vsdx(tmp_path, settings):
    remote, source = _create_remote(tmp_path)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository, first_job = _queued_repository(remote)

    call_command("bitbucket_sync_worker", "--once", verbosity=0)
    completed = RepositorySyncJob.objects.get(pk=first_job.pk)

    repository.refresh_from_db()
    checkout = Path(repository.local_path)
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.sync_state == RepositorySyncState.READY
    assert repository.sync_progress == 100
    assert repository.default_branch == "main"
    assert repository.pdf_count == 1
    assert repository.vsdx_count == 1
    assert (checkout / "docs" / "Architecture.PDF").is_file()
    assert (checkout / "diagrams" / "Network.VsDx").is_file()
    assert not (checkout / "src" / "large-source.bin").exists()
    assert not (checkout / "README.md").exists()
    first_document = repository.pdf_documents.select_related("added_commit").get()
    assert first_document.relative_path == "docs/Architecture.PDF"
    assert first_document.file_size == len(b"synthetic pdf")
    assert first_document.open_count == 0
    assert first_document.index_state == PDFIndexState.PENDING
    assert first_document.extraction_jobs.get().status == PDFExtractionJobStatus.QUEUED
    assert first_document.added_evidence in {
        PDFDocumentAddedEvidence.CONFIRMED,
        PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY,
    }
    assert repository.metadata_indexed_commit == repository.last_synced_commit

    (source / "docs" / "Second.pdf").write_bytes(b"second synthetic pdf")
    (source / "src" / "large-source.bin").write_bytes(b"changed but still excluded")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Add another document", cwd=source)
    _git("push", "origin", "main", cwd=source)

    queued = queue_repository_refresh(repository.pk)
    assert queued.job.operation == RepositorySyncOperation.REFRESH
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.pdf_count == 2
    assert repository.vsdx_count == 1
    assert (checkout / "docs" / "Second.pdf").is_file()
    assert not (checkout / "src" / "large-source.bin").exists()
    assert repository.last_synced_commit == _git("rev-parse", "HEAD", cwd=source)
    assert repository.metadata_indexed_commit == repository.last_synced_commit
    second_document = repository.pdf_documents.select_related("added_commit").get(
        relative_path="docs/Second.pdf"
    )
    assert second_document.added_evidence == PDFDocumentAddedEvidence.CONFIRMED
    assert second_document.added_commit is not None
    assert second_document.added_commit.author_name == "Synthetic OWL"
    assert second_document.index_state == PDFIndexState.PENDING
    assert second_document.extraction_jobs.get().status == PDFExtractionJobStatus.QUEUED
    assert first_document.extraction_jobs.count() == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_dormant_repository_falls_back_to_depth_one_clone(tmp_path, settings, monkeypatch):
    remote, source = _create_remote(tmp_path)
    old_commit_date = "2010-01-02T03:04:05+00:00"
    monkeypatch.setenv("GIT_AUTHOR_DATE", old_commit_date)
    monkeypatch.setenv("GIT_COMMITTER_DATE", old_commit_date)
    _git("commit", "--amend", "--no-edit", "--date", old_commit_date, cwd=source)
    _git("push", "--force", "origin", "main", cwd=source)

    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    settings.BITBUCKET_HISTORY_YEARS = 1
    repository, job = _queued_repository(remote)

    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == job.pk
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.sync_state == RepositorySyncState.READY
    assert repository.pdf_count == 1
    assert repository.vsdx_count == 1
    assert (Path(repository.local_path) / "docs" / "Architecture.PDF").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_refresh_blocks_and_preserves_a_dirty_managed_checkout(tmp_path, settings):
    remote, _source = _create_remote(tmp_path)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository, _job = _queued_repository(remote)
    claimed = claim_next_job()
    assert claimed is not None
    execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    local_change = Path(repository.local_path) / "manual-local-note.txt"
    local_change.write_text("preserve me", encoding="utf-8")

    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.FAILED
    assert repository.sync_state == RepositorySyncState.BLOCKED_DIRTY
    assert repository.last_error_code == "dirty_working_tree"
    assert local_change.read_text(encoding="utf-8") == "preserve me"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_clone_fails_honestly_when_git_lfs_is_unavailable(tmp_path, settings, monkeypatch):
    remote, source = _create_remote(tmp_path)
    pointer = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        b"size 1048576\n"
    )
    (source / "docs" / "Lfs-backed.pdf").write_bytes(pointer)
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Add unresolved LFS document", cwd=source)
    _git("push", "origin", "main", cwd=source)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    real_which = shutil.which
    monkeypatch.setattr(
        git_sync.shutil,
        "which",
        lambda executable: None if executable == "git-lfs" else real_which(executable),
    )
    repository, _job = _queued_repository(remote)

    claimed = claim_next_job()
    assert claimed is not None
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.FAILED
    assert completed.error_code == "git_lfs_unavailable"
    assert repository.sync_state == RepositorySyncState.FAILED
    assert repository.last_error_code == "git_lfs_unavailable"
    assert "Git LFS" in repository.last_error_summary
    assert "select Refresh" in repository.last_error_summary
    assert not Path(repository.local_path).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_failed_lfs_refresh_recovers_after_git_lfs_becomes_available(
    tmp_path,
    settings,
    monkeypatch,
):
    remote, source = _create_remote(tmp_path)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository, _job = _queued_repository(remote)
    claimed = claim_next_job()
    assert claimed is not None
    execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    checkout = Path(repository.local_path)

    pointer = (
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789\n"
        b"size 24\n"
    )
    (source / ".gitattributes").write_text(
        "*.pdf filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    (source / "docs" / "Lfs-backed.pdf").write_bytes(pointer)
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Add an LFS-backed document", cwd=source)
    _git("push", "origin", "main", cwd=source)
    remote_commit = _git("rev-parse", "HEAD", cwd=source)

    real_which = shutil.which
    monkeypatch.setattr(
        git_sync.shutil,
        "which",
        lambda executable: None if executable == "git-lfs" else real_which(executable),
    )
    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    first_attempt = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert first_attempt.status == RepositorySyncJobStatus.FAILED
    assert first_attempt.error_code == "git_lfs_unavailable"
    assert _git("rev-parse", "HEAD", cwd=checkout) == remote_commit
    assert (checkout / "docs" / "Lfs-backed.pdf").read_bytes() == pointer

    monkeypatch.setattr(
        git_sync.shutil,
        "which",
        lambda executable: (
            "/synthetic/bin/git-lfs" if executable == "git-lfs" else real_which(executable)
        ),
    )
    real_run_streaming = git_sync._run_streaming
    lfs_commands = []

    def run_streaming(arguments, **kwargs):
        if tuple(arguments[3:5]) == ("lfs", "pull"):
            lfs_commands.append(tuple(arguments))
            assert f"--include={git_sync._GIT_LFS_DOCUMENT_INCLUDE}" in arguments
            assert "--exclude=" in arguments
            (checkout / "docs" / "Lfs-backed.pdf").write_bytes(b"%PDF-1.7 hydrated document")
            _git(
                "update-index",
                "--assume-unchanged",
                "--",
                "docs/Lfs-backed.pdf",
                cwd=checkout,
            )
            kwargs["progress_callback"](
                kwargs["phase"],
                kwargs["progress_end"],
                kwargs["status_message"],
            )
            return None
        return real_run_streaming(arguments, **kwargs)

    monkeypatch.setattr(git_sync, "_run_streaming", run_streaming)
    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    recovered = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert recovered.status == RepositorySyncJobStatus.SUCCEEDED
    assert repository.sync_state == RepositorySyncState.READY
    assert repository.last_synced_commit == remote_commit
    assert repository.pdf_count == 2
    assert (checkout / "docs" / "Lfs-backed.pdf").read_bytes().startswith(b"%PDF-")
    assert len(lfs_commands) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_refresh_blocks_a_clean_locally_ahead_checkout_without_altering_it(tmp_path, settings):
    remote, _source = _create_remote(tmp_path)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository, _job = _queued_repository(remote)
    claimed = claim_next_job()
    assert claimed is not None
    execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    checkout = Path(repository.local_path)
    remote_commit = repository.last_synced_commit
    _git("config", "user.name", "Synthetic OWL", cwd=checkout)
    _git("config", "user.email", "owl@example.invalid", cwd=checkout)
    _git("commit", "--allow-empty", "-m", "Local-only commit", cwd=checkout)
    local_commit = _git("rev-parse", "HEAD", cwd=checkout)

    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.FAILED
    assert completed.error_code == "local_commits_detected"
    assert repository.sync_state == RepositorySyncState.BLOCKED_DIRTY
    assert repository.last_synced_commit == remote_commit
    assert _git("rev-parse", "HEAD", cwd=checkout) == local_commit


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_refresh_blocks_diverged_history_without_altering_the_checkout(tmp_path, settings):
    remote, source = _create_remote(tmp_path)
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "bitbucket" / "tmp"
    settings.BITBUCKET_GIT_TIMEOUT_SECONDS = 30
    repository, _job = _queued_repository(remote)
    claimed = claim_next_job()
    assert claimed is not None
    execute_claimed_job(claimed.pk)
    repository.refresh_from_db()
    checkout = Path(repository.local_path)
    _git("config", "user.name", "Synthetic OWL", cwd=checkout)
    _git("config", "user.email", "owl@example.invalid", cwd=checkout)
    _git("commit", "--allow-empty", "-m", "Local-only commit", cwd=checkout)
    local_commit = _git("rev-parse", "HEAD", cwd=checkout)
    (source / "docs" / "Remote.pdf").write_bytes(b"remote document")
    _git("add", ".", cwd=source)
    _git("commit", "-m", "Remote-only commit", cwd=source)
    _git("push", "origin", "main", cwd=source)

    queued = queue_repository_refresh(repository.pk)
    claimed = claim_next_job()
    assert claimed is not None and claimed.pk == queued.job.pk
    completed = execute_claimed_job(claimed.pk)

    repository.refresh_from_db()
    assert completed.status == RepositorySyncJobStatus.FAILED
    assert completed.error_code == "history_diverged"
    assert repository.sync_state == RepositorySyncState.BLOCKED_DIRTY
    assert _git("rev-parse", "HEAD", cwd=checkout) == local_commit
    assert not (checkout / "docs" / "Remote.pdf").exists()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_repository_urls_normalize_scheme_variants_to_one_safe_identity():
    ssh = normalize_repository_url("git@bitbucket.org:Example/Architecture.git")
    https = normalize_repository_url("https://bitbucket.org/Example/Architecture.git")

    assert ssh.remote_url == "ssh://git@bitbucket.org/Example/Architecture.git"
    assert https.remote_url == "https://bitbucket.org/Example/Architecture.git"
    assert ssh.canonical_remote_key == https.canonical_remote_key
    assert ssh.display_name == "Architecture"


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "scm.example.invalid"))
def test_repository_urls_preserve_allowed_internal_bitbucket_https_context_path():
    normalized = normalize_repository_url(
        "https://scm.example.invalid/stash/scm/adr/example-repo.git"
    )

    assert normalized.remote_url == "https://scm.example.invalid/stash/scm/adr/example-repo.git"
    assert normalized.canonical_remote_key == "scm.example.invalid/stash/scm/adr/example-repo"
    assert normalized.display_name == "example-repo"
    assert normalized.hostname == "scm.example.invalid"


@pytest.mark.parametrize(
    "hostname",
    (
        "unapproved.example.invalid",
        "scm.example.invalid.evil.invalid",
        "other.scm.example.invalid",
        "scm-example.invalid",
    ),
)
@override_settings(BITBUCKET_ALLOWED_HOSTS=("scm.example.invalid",))
def test_internal_bitbucket_allowlist_requires_the_exact_hostname(hostname):
    with pytest.raises(RepositoryURLValidationError) as captured:
        normalize_repository_url(f"https://{hostname}/stash/scm/adr/example-repo.git")

    assert captured.value.code == "host_not_allowed"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("http://bitbucket.org/workspace/repository.git", "unsupported_repository_protocol"),
        ("https://evil.invalid/workspace/repository.git", "host_not_allowed"),
        (
            "https://" + "user:not-a-real-secret" + "@bitbucket.org/workspace/repository.git",
            "credential_bearing_repository_url",
        ),
        ("ssh://root@bitbucket.org/workspace/repository.git", "unsupported_ssh_user"),
        ("https://bitbucket.org/../repository.git", "invalid_repository_path"),
    ],
)
@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_repository_urls_reject_unsafe_inputs_without_echoing_them(value, code):
    with pytest.raises(RepositoryURLValidationError) as captured:
        normalize_repository_url(value)

    assert captured.value.code == code
    assert "not-a-real-secret" not in str(captured.value)


@override_settings(BITBUCKET_ALLOWED_HOSTS=())
def test_repository_registration_requires_an_explicit_host_allowlist():
    with pytest.raises(RepositoryURLValidationError) as captured:
        normalize_repository_url("git@bitbucket.org:workspace/repository.git")

    assert captured.value.code == "allowed_hosts_not_configured"


def test_worker_wakeup_capacity_is_reserved_while_cross_process_lock_is_held(
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 1
    repository = BitbucketRepository.objects.create(
        display_name="serialized-wakeup",
        canonical_remote_key="bitbucket.org/workspace/serialized-wakeup",
        remote_url="ssh://git@bitbucket.org/workspace/serialized-wakeup.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    events: list[object] = []

    @contextmanager
    def serialized_wakeup():
        events.append("lock")
        yield
        events.append(
            (
                "unlock",
                RepositorySyncJob.objects.get(pk=job.pk).worker_pid,
            )
        )

    monkeypatch.setattr(repository_sync, "repository_worker_wakeup_lock", serialized_wakeup)

    reservation = repository_sync.reserve_queued_repository_worker_wakeups()

    assert reservation.job_ids == (job.pk,)
    assert events == ["lock", ("unlock", os.getpid())]


def test_worker_once_processes_at_most_one_queued_job(monkeypatch):
    completed = type(
        "CompletedJob",
        (),
        {"pk": 7, "get_status_display": lambda self: "Succeeded"},
    )()
    worker = Mock(return_value=completed)
    monkeypatch.setattr(
        "bitbucket_search.management.commands.bitbucket_sync_worker.work_one_job",
        worker,
    )

    call_command("bitbucket_sync_worker", "--once", verbosity=0)

    worker.assert_called_once_with()


def test_repository_worker_moves_to_pdf_queue_only_after_repository_queue_is_idle(
    monkeypatch,
):
    completed = type(
        "CompletedExtraction",
        (),
        {"pk": 11, "get_status_display": lambda self: "Succeeded"},
    )()
    repository_worker = Mock(return_value=None)
    extraction_worker = Mock(return_value=completed)
    monkeypatch.setattr(
        "bitbucket_search.management.commands.bitbucket_sync_worker.work_one_job",
        repository_worker,
    )
    monkeypatch.setattr(
        "bitbucket_search.management.commands.bitbucket_sync_worker.work_one_extraction_job",
        extraction_worker,
    )

    call_command("bitbucket_sync_worker", "--once", verbosity=0)

    repository_worker.assert_called_once_with()
    extraction_worker.assert_called_once_with()


def test_silent_capture_command_emits_periodic_worker_heartbeats(monkeypatch):
    class SilentProcess:
        def __init__(self):
            self.returncode = None
            self.communicate_count = 0

        def communicate(self, timeout=None):
            self.communicate_count += 1
            if self.communicate_count <= 2:
                raise subprocess.TimeoutExpired(["git", "rev-parse"], timeout)
            self.returncode = 0
            return "synthetic-commit\n", ""

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = SilentProcess()
    monkeypatch.setattr(git_sync.subprocess, "Popen", Mock(return_value=process))
    heartbeat = Mock()

    result = git_sync._run_capture(
        ("git", "rev-parse", "HEAD"),
        failure_code="invalid_commit",
        failure_summary="Synthetic capture failed.",
        heartbeat_callback=heartbeat,
    )

    assert result == "synthetic-commit"
    assert heartbeat.call_count == 2


def test_document_discovery_emits_periodic_worker_heartbeats(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "one.pdf").write_bytes(b"synthetic pdf")
    (tmp_path / "diagram.vsdx").write_bytes(b"synthetic vsdx")
    heartbeat = Mock()
    monkeypatch.setattr(git_sync, "_HEARTBEAT_INTERVAL_SECONDS", 0)

    stats = git_sync.discover_documents(tmp_path, heartbeat_callback=heartbeat)

    assert (stats.pdf_count, stats.vsdx_count) == (1, 1)
    assert heartbeat.call_count >= 2


def test_interrupted_job_cannot_be_reactivated_by_a_stale_worker(monkeypatch):
    repository = BitbucketRepository.objects.create(
        display_name="lease-test",
        canonical_remote_key="bitbucket.org/workspace/lease-test",
        remote_url="ssh://git@bitbucket.org/workspace/lease-test.git",
        sync_state=RepositorySyncState.CLONING,
        sync_progress=25,
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.RUNNING,
        phase=RepositorySyncPhase.CLONING,
        progress=25,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )

    def lose_worker_lease(_repository, *, operation, progress_callback):
        assert operation == RepositorySyncOperation.CLONE
        RepositorySyncJob.objects.filter(pk=job.pk).update(
            status=RepositorySyncJobStatus.INTERRUPTED,
            completed_at=timezone.now(),
        )
        BitbucketRepository.objects.filter(pk=repository.pk).update(
            sync_state=RepositorySyncState.INTERRUPTED,
            status_message="Worker interrupted.",
        )
        progress_callback(RepositorySyncPhase.CLONING, 50, "Stale progress")
        raise AssertionError("A lost worker lease must stop progress publication.")

    monkeypatch.setattr(
        "bitbucket_search.services.repository_sync.synchronize_repository",
        lose_worker_lease,
    )

    completed = execute_claimed_job(job.pk)
    repository.refresh_from_db()

    assert completed.status == RepositorySyncJobStatus.INTERRUPTED
    assert repository.sync_state == RepositorySyncState.INTERRUPTED
    assert repository.sync_progress == 25
    assert repository.status_message == "Worker interrupted."
