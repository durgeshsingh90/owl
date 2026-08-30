from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, GitCommit, GitCommitFolder, PDFDocument
from bitbucket_search.services import git_activity
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.pdf_catalog import (
    build_repository_pdf_catalog,
    publish_repository_pdf_catalog,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(shutil.which("git") is None, reason="Git is required"),
]


def _git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout.strip()


@pytest.fixture
def local_repository(settings, tmp_path):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "spool"
    repository = BitbucketRepository.objects.create(
        display_name="Synthetic Activity",
        canonical_remote_key="example.invalid/team/activity",
        remote_url="https://example.invalid/team/activity.git",
    )
    checkout = managed_repository_path(repository)
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-b", "main")
    _git(checkout, "config", "user.name", "Synthetic Committer")
    _git(checkout, "config", "user.email", "synthetic@example.invalid")
    return repository, checkout


def _commit(checkout: Path, message: str = "Synthetic change") -> str:
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "--allow-empty", "-m", message)
    return _git(checkout, "rev-parse", "HEAD")


def _publish(repository: BitbucketRepository, head: str):
    catalog = build_repository_pdf_catalog(
        repository,
        result_commit=head,
        progress_callback=lambda *_args: None,
    )
    publish_repository_pdf_catalog(
        repository, catalog, result_commit=head, observed_at=timezone.now()
    )
    repository.refresh_from_db()
    return catalog


def _folders(repository: BitbucketRepository) -> dict[str, set[str]]:
    return {
        commit.commit_hash: set(commit.folders.values_list("folder_path", flat=True))
        for commit in repository.git_commits.filter(in_activity_history=True)
    }


def test_catalog_indexes_all_commits_and_direct_folders_not_only_current_pdf_evidence(
    local_repository,
):
    repository, checkout = local_repository
    (checkout / "docs").mkdir()
    (checkout / "docs" / "guide.pdf").write_bytes(b"synthetic pdf")
    (checkout / "README.md").write_text("one", encoding="utf-8")
    first = _commit(checkout)
    (checkout / "source").mkdir()
    (checkout / "source" / "one.py").write_text("one", encoding="utf-8")
    (checkout / "source" / "two.py").write_text("two", encoding="utf-8")
    second = _commit(checkout)
    (checkout / "source" / "one.py").write_text("changed", encoding="utf-8")
    third = _commit(checkout)
    fourth = _commit(checkout, "Empty commit still counts")

    _publish(repository, fourth)

    assert _folders(repository) == {
        first: {"", "docs"},
        second: {"source"},
        third: {"source"},
        fourth: set(),
    }
    assert repository.activity_indexed_commit == fourth
    assert repository.activity_indexed_at is not None
    assert repository.pdf_documents.get().last_commit.commit_hash == first


def test_renames_and_deletions_count_old_and_new_parent_folders(local_repository):
    repository, checkout = local_repository
    (checkout / "old").mkdir()
    (checkout / "new").mkdir()
    (checkout / "old" / "diagram.vsdx").write_bytes(b"diagram")
    first = _commit(checkout)
    _git(checkout, "mv", "old/diagram.vsdx", "new/diagram.vsdx")
    second = _commit(checkout)
    _git(checkout, "rm", "new/diagram.vsdx")
    third = _commit(checkout)

    _publish(repository, third)

    assert _folders(repository) == {first: {"old"}, second: {"old", "new"}, third: {"new"}}


def test_merge_counts_once_and_first_parent_diff_does_not_omit_side_branch_commits(
    local_repository,
):
    repository, checkout = local_repository
    (checkout / "README.md").write_text("base", encoding="utf-8")
    first = _commit(checkout)
    _git(checkout, "checkout", "-b", "feature")
    (checkout / "feature").mkdir()
    (checkout / "feature" / "design.md").write_text("feature", encoding="utf-8")
    feature = _commit(checkout)
    _git(checkout, "checkout", "main")
    (checkout / "main").mkdir()
    (checkout / "main" / "service.py").write_text("main", encoding="utf-8")
    main = _commit(checkout)
    _git(checkout, "merge", "--no-ff", "feature", "-m", "Merge feature")
    merge = _git(checkout, "rev-parse", "HEAD")

    _publish(repository, merge)

    assert _folders(repository) == {
        first: {""},
        feature: {"feature"},
        main: {"main"},
        merge: {"feature"},
    }


def test_unchanged_head_reuses_history_and_missing_coverage_backfills(
    local_repository, monkeypatch
):
    repository, checkout = local_repository
    (checkout / "README.md").write_text("base", encoding="utf-8")
    head = _commit(checkout)
    _publish(repository, head)
    original_ids = list(repository.git_commits.values_list("pk", flat=True))
    original = git_activity._run_binary_spooled

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("Unchanged indexed HEAD should reuse activity")

    monkeypatch.setattr(git_activity, "_run_binary_spooled", unexpected_read)
    assert _publish(repository, head).activity is None
    assert list(repository.git_commits.values_list("pk", flat=True)) == original_ids
    monkeypatch.setattr(git_activity, "_run_binary_spooled", original)
    repository.activity_indexed_commit = ""
    repository.activity_indexed_at = None
    repository.save(update_fields=("activity_indexed_commit", "activity_indexed_at"))
    assert _publish(repository, head).activity is not None
    assert list(repository.git_commits.values_list("pk", flat=True)) == original_ids
    assert GitCommitFolder.objects.count() == 1


def test_rewritten_history_retires_activity_but_preserves_pdf_commit_references(local_repository):
    repository, checkout = local_repository
    base = _commit(checkout)
    (checkout / "docs").mkdir()
    (checkout / "docs" / "guide.pdf").write_bytes(b"synthetic pdf")
    abandoned = _commit(checkout)
    _publish(repository, abandoned)
    document = repository.pdf_documents.get()
    original_commit_id = document.last_commit_id
    _git(checkout, "checkout", "--detach", base)
    replacement = _commit(checkout, "Replacement branch")

    _publish(repository, replacement)

    document.refresh_from_db()
    assert document.last_commit_id == original_commit_id
    assert GitCommit.objects.get(pk=original_commit_id).in_activity_history is False
    assert set(_folders(repository)) == {base, replacement}
    assert GitCommitFolder.objects.count() == 0


def test_shallow_boundaries_omit_unverifiable_folder_diff_and_deepening_rebuilds(
    settings, tmp_path, local_repository
):
    source_repository, source = local_repository
    (source / "older").mkdir()
    (source / "older" / "notes.md").write_text("base", encoding="utf-8")
    first = _commit(source)
    (source / "newer").mkdir()
    (source / "newer" / "notes.md").write_text("newer", encoding="utf-8")
    head = _commit(source)
    repository = BitbucketRepository.objects.create(
        display_name="Shallow", canonical_remote_key="example.invalid/team/shallow"
    )
    checkout = managed_repository_path(repository)
    _git(source, "clone", "--depth=1", source.as_uri(), str(checkout))
    _publish(repository, head)
    assert _folders(repository) == {head: set()}
    assert repository.git_commits.get(commit_hash=head).is_shallow_boundary is True
    _git(checkout, "fetch", "--unshallow", "origin")

    assert _publish(repository, head).activity is not None

    assert _folders(repository) == {first: {"older"}, head: {"newer"}}
    assert repository.git_commits.filter(is_shallow_boundary=True).count() == 0


def test_activity_publication_rolls_back_together_with_catalog_on_failure(
    local_repository, monkeypatch
):
    repository, checkout = local_repository
    (checkout / "guide.pdf").write_bytes(b"first PDF")
    first = _commit(checkout)
    _publish(repository, first)
    previous_indexed_at = repository.activity_indexed_at
    (checkout / "guide.pdf").write_bytes(b"changed PDF")
    second = _commit(checkout)
    catalog = build_repository_pdf_catalog(
        repository, result_commit=second, progress_callback=lambda *_args: None
    )

    def fail_documents(*_args, **_kwargs):
        raise RuntimeError("Synthetic publication failure")

    monkeypatch.setattr(PDFDocument.objects, "bulk_update", fail_documents)
    with pytest.raises(RuntimeError, match="Synthetic publication failure"):
        publish_repository_pdf_catalog(
            repository, catalog, result_commit=second, observed_at=timezone.now()
        )

    repository.refresh_from_db()
    assert repository.activity_indexed_commit == first
    assert repository.activity_indexed_at == previous_indexed_at
    assert set(_folders(repository)) == {first}
    assert not repository.git_commits.filter(commit_hash=second).exists()
    assert repository.pdf_documents.get().last_commit.commit_hash == first


def _record(*changes: bytes) -> bytes:
    return b"\0".join(
        (
            b"",
            b"OWL-GIT-ACTIVITY-COMMIT",
            b"a" * 40,
            b"2026-08-30T10:00:00+01:00",
            b"Synthetic Author",
            b"2026-08-30T11:00:00+01:00",
            b"Synthetic Committer",
            b"",
            *changes,
            b"",
        )
    )


def test_marker_looking_filename_does_not_break_framing():
    records = tuple(
        git_activity._parse_activity(
            io.BytesIO(_record(b"A", b"OWL-GIT-ACTIVITY-COMMIT", b"M", b"docs/guide.md")),
            shallow_boundaries=frozenset(),
            heartbeat_callback=lambda: None,
        )
    )
    assert records[0].folders == ("", "docs")


def test_git_byte_names_and_literal_escape_names_remain_distinct_and_sql_safe(local_repository):
    repository, _checkout = local_repository
    output = _record(
        b"A",
        b"bad\xff/file.md",
        b"A",
        b"bad\\xff/file.md",
        b"A",
        b"bad\x80/file.md",
        b"A",
        b"bad\xc2\x80/file.md",
        b"A",
        b"normal/caf\xc3\xa9.md",
        b"A",
        b"control\t/name",
        b"A",
        b"control\\x09/name",
        b"A",
        b"root\xff.md",
    )
    records = tuple(
        git_activity._parse_activity(
            io.BytesIO(output),
            shallow_boundaries=frozenset(),
            heartbeat_callback=lambda: None,
        )
    )
    assert set(records[0].folders) == {
        "",
        r"bad\xff",
        r"bad\\xff",
        r"bad\x80",
        r"bad\u0080",
        "normal",
        r"control\x09",
        r"control\\x09",
    }
    git_activity.publish_repository_git_activity(
        repository,
        git_activity.GitActivityBuild("a" * 40, records),
        result_commit="a" * 40,
        observed_at=timezone.now(),
    )
    assert set(repository.git_commits.get().folders.values_list("folder_path", flat=True)) == set(
        records[0].folders
    )


@pytest.mark.parametrize("output", (b"", _record() + _record()))
def test_missing_or_duplicate_head_fails_before_publication_and_logs_error(
    local_repository, monkeypatch, caplog, output
):
    repository, checkout = local_repository
    monkeypatch.setattr(
        git_activity,
        "_run_binary_spooled",
        lambda *_args, **_kwargs: nullcontext(io.BytesIO(output)),
    )
    with (
        caplog.at_level(logging.ERROR, logger="owl.bitbucket.activity"),
        pytest.raises(RepositorySyncError),
    ):
        git_activity.build_repository_git_activity(
            repository,
            repository_path=checkout,
            result_commit="a" * 40,
            shallow_boundaries=frozenset(),
            heartbeat_callback=lambda: None,
        )
    assert "git_activity_build_failed" in caplog.text
    repository.refresh_from_db()
    assert repository.activity_indexed_at is None
    assert not repository.git_commits.exists()


def test_activity_requires_a_valid_commit_identity(local_repository):
    repository, checkout = local_repository
    with pytest.raises(RepositorySyncError):
        git_activity.build_repository_git_activity(
            repository,
            repository_path=checkout,
            result_commit="--all",
            shallow_boundaries=frozenset(),
            heartbeat_callback=lambda: None,
        )


@pytest.mark.parametrize(
    "data",
    (
        b"not-a-record\0",
        b"\0OWL-GIT-ACTIVITY-COMMIT\0short\0",
        _record(b"R100", b"old", b"new"),
        _record(b"A", b"../outside"),
        _record(b"A", b"/absolute"),
        _record(b"A", (b"a" * 2049) + b"/file"),
        _record(b"A", b""),
    ),
)
def test_malformed_activity_does_not_produce_partial_statistics(data):
    with pytest.raises(RepositorySyncError):
        tuple(
            git_activity._parse_activity(
                io.BytesIO(data), shallow_boundaries=frozenset(), heartbeat_callback=lambda: None
            )
        )


@pytest.mark.django_db(transaction=True)
def test_activity_migration_preserves_pdf_fts_data_and_triggers_in_both_directions():
    before = [("bitbucket_search", "0007_pdflocalpolicy")]
    after = [("bitbucket_search", "0008_git_activity_history")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(before)
        apps = executor.loader.project_state(before).apps
        repository = apps.get_model("bitbucket_search", "BitbucketRepository").objects.create(
            display_name="Before migration", canonical_remote_key="example.invalid/fts"
        )
        apps.get_model("bitbucket_search", "PDFDocument").objects.create(
            repository_id=repository.pk,
            filename="one.pdf",
            relative_path="docs/one.pdf",
            timeline_at=timezone.now(),
        )
        MigrationExecutor(connection).migrate(after)
        repository = BitbucketRepository.objects.get(pk=repository.pk)
        repository.display_name = "After migration"
        repository.save(update_fields=("display_name",))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT repository_name FROM bitbucket_search_pdf_metadata_fts WHERE filename = 'one.pdf'"
            )
            assert cursor.fetchone() == ("After migration",)
        MigrationExecutor(connection).migrate(before)
        old_repository = apps.get_model("bitbucket_search", "BitbucketRepository").objects.get(
            pk=repository.pk
        )
        old_repository.display_name = "Reversed migration"
        old_repository.save(update_fields=("display_name",))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT repository_name FROM bitbucket_search_pdf_metadata_fts WHERE filename = 'one.pdf'"
            )
            assert cursor.fetchone() == ("Reversed migration",)
    finally:
        MigrationExecutor(connection).migrate(after)
