from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from django.core.management import get_commands
from django.test import override_settings
from django.urls import reverse

from bitbucket import views
from bitbucket.models import (
    Contributor,
    Document,
    DocumentKind,
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)
from bitbucket.services import git_sync
from bitbucket.services.catalog import refresh_catalog
from bitbucket.services.repository_urls import (
    RepositoryURLValidationError,
    parse_repository_url,
)
from bitbucket.services.scheduler import queue_due_daily_pulls

pytestmark = pytest.mark.django_db
TEST_REPOSITORY_HOST = "scm.example.invalid"


@pytest.fixture(autouse=True)
def disable_automatic_pull(settings):
    settings.BITBUCKET_APP_DAILY_PULL_ENABLED = False


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    client.defaults["REMOTE_ADDR"] = "127.0.0.1"
    return client


def repository(**values) -> Repository:
    defaults = {
        "url": "https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
        "canonical_url": "https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
        "host": "scm.example.invalid",
        "project": "adr",
        "slug": "engineering-sign-off",
        "state": RepositoryState.READY,
    }
    defaults.update(values)
    return Repository.objects.create(**defaults)


def test_app_is_separate_and_integrated_into_homepage(loopback_client):
    assert reverse("bitbucket:index") == "/bitbucket/"
    response = loopback_client.get("/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'href="/bitbucket/"' in html
    assert "Independent Git document desk" in html
    assert html.count('class="knowledge-app-card ') == 3


def test_document_worker_has_an_app_specific_management_command():
    commands = get_commands()

    assert commands["bitbucket_document_worker"] == "bitbucket"
    assert commands["bitbucket_sync_worker"] == "bitbucket_search"


def test_upgrade_repairs_database_with_removed_draft_migration_history(tmp_path):
    data_root = tmp_path / "legacy-data"
    database = data_root / "database" / "owl.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE django_migrations ("
            "id integer PRIMARY KEY AUTOINCREMENT, app varchar(255) NOT NULL, "
            "name varchar(255) NOT NULL, applied datetime NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE bitbucket_bitbucketrepository "
            "(id integer PRIMARY KEY, display_name varchar(200) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO django_migrations (app, name, applied) "
            "VALUES ('bitbucket', '0001_initial', CURRENT_TIMESTAMP)"
        )
        for number in range(2, 22):
            connection.execute(
                "INSERT INTO django_migrations (app, name, applied) "
                "VALUES ('bitbucket', ?, CURRENT_TIMESTAMP)",
                (f"{number:04d}_removed_draft_migration",),
            )

    environment = os.environ.copy()
    environment.update(
        {
            "OWL_DATA_ROOT": str(data_root),
            "DJANGO_SETTINGS_MODULE": "owl.settings",
            "DJANGO_SECRET_KEY": "migration-test-only-synthetic-key-0123456789-abcdefghij",
        }
    )
    completed = subprocess.run(
        (
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "manage.py"),
            "migrate",
            "bitbucket",
            "--noinput",
        ),
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        applied = connection.execute(
            "SELECT COUNT(*) FROM django_migrations "
            "WHERE app = 'bitbucket' AND name = '0022_ensure_independent_document_schema'"
        ).fetchone()[0]
    assert {
        "bitbucket_repository",
        "bitbucket_document",
        "bitbucket_contributor",
        "bitbucket_syncjob",
    } <= tables
    assert "bitbucket_bitbucketrepository" in tables
    assert applied == 1


def test_workspace_has_requested_columns_actions_and_independent_design(loopback_client):
    item = repository(pdf_count=1)
    Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/architecture.pdf",
        filename="architecture.pdf",
    )
    response = loopback_client.get("/bitbucket/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'class="bb-workspace"' in html
    assert "Your repositories" in html
    assert "PDF name" in html
    assert "Date added to repo" in html
    assert "Added by" in html
    assert "Commit ID" in html
    assert "Opens" in html
    assert "Show in folder" in html
    assert "Open selected" in html
    assert "Copy all paths" in html
    assert "People" in html
    assert "Authenticate your firewall" in html


def test_sample_server_url_extracts_project_and_repository():
    parsed = parse_repository_url(
        "https://scm.example.invalid/stash/scm/adr/engineering-availability-sign-off.git"
    )

    assert parsed.project == "adr"
    assert parsed.slug == "engineering-availability-sign-off"
    assert parsed.url.endswith("/adr/engineering-availability-sign-off.git")
    assert parsed.authentication_url == "https://scm.example.invalid/"


@pytest.mark.parametrize(
    "value",
    (
        "http://scm.example.invalid/scm/adr/repo.git",
        "ssh://git@scm.example.invalid/adr/repo.git",
        "git@scm.example.invalid:adr/repo.git",
        f"https://embedded-userinfo@{TEST_REPOSITORY_HOST}/scm/adr/repo.git",
        f"https://{TEST_REPOSITORY_HOST}/scm/adr/repo.git?unsupported={1}",
    ),
)
def test_only_credential_free_https_urls_are_accepted(value):
    with pytest.raises(RepositoryURLValidationError):
        parse_repository_url(value)


@override_settings(BITBUCKET_ALLOWED_HOSTS=())
def test_add_does_not_use_bitbucket_search_host_allow_list(loopback_client, monkeypatch):
    wake = Mock(return_value=True)
    monkeypatch.setattr(views, "wake_sync_worker", wake)

    response = loopback_client.post(
        "/bitbucket/repositories/add/",
        {
            "repository_url": (
                "https://private.example.invalid/stash/scm/adr/engineering-sign-off.git"
            )
        },
    )

    assert response.status_code == 202
    saved = Repository.objects.get()
    assert saved.host == "private.example.invalid"
    assert saved.project == "adr"
    assert saved.slug == "engineering-sign-off"
    job = SyncJob.objects.get(repository=saved)
    assert job.operation == SyncOperation.CLONE
    wake.assert_called_once_with()


def test_connection_preflight_is_exact_https_git_ls_remote(monkeypatch):
    item = repository()
    runner = Mock(return_value="ref: refs/heads/main\tHEAD\n")
    monkeypatch.setattr(git_sync, "_run", runner)

    git_sync.test_connection(item)

    assert runner.call_args.args[0] == (
        "git",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "ls-remote",
        "--symref",
        "--",
        item.url,
        "HEAD",
    )


def test_preflight_must_succeed_before_clone(monkeypatch, settings, tmp_path):
    settings.BITBUCKET_APP_REPOSITORIES_ROOT = tmp_path
    item = repository(state=RepositoryState.QUEUED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.CLONE,
        status=SyncJobStatus.RUNNING,
    )
    events: list[str] = []
    monkeypatch.setattr(
        git_sync,
        "test_connection",
        Mock(side_effect=lambda _repository: events.append("preflight") or "tested"),
    )
    monkeypatch.setattr(
        git_sync,
        "_clone",
        Mock(side_effect=lambda *_args: events.append("clone") or "cloned"),
    )
    monkeypatch.setattr(
        git_sync,
        "refresh_catalog",
        Mock(side_effect=lambda *_args: events.append("catalog") or (3, 2)),
    )

    result = git_sync.process_job(job)

    assert events == ["preflight", "clone", "catalog"]
    assert result.status == SyncJobStatus.SUCCEEDED
    item.refresh_from_db()
    assert (item.pdf_count, item.vsdx_count) == (3, 2)
    assert item.last_successful_pull_on == date.today()


def test_failed_preflight_requires_authentication_and_never_clones(monkeypatch, settings, tmp_path):
    settings.BITBUCKET_APP_REPOSITORIES_ROOT = tmp_path
    item = repository(state=RepositoryState.QUEUED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.CLONE,
        status=SyncJobStatus.RUNNING,
    )
    clone = Mock()
    monkeypatch.setattr(
        git_sync,
        "test_connection",
        Mock(
            side_effect=git_sync.GitSyncError(
                "connection failed",
                code="connection_failed",
            )
        ),
    )
    monkeypatch.setattr(git_sync, "_clone", clone)

    result = git_sync.process_job(job)

    assert result.status == SyncJobStatus.AUTH_REQUIRED
    clone.assert_not_called()
    item.refresh_from_db()
    assert item.state == RepositoryState.AUTH_REQUIRED


def test_authentication_popup_job_can_retry_or_cancel(loopback_client, monkeypatch):
    item = repository(state=RepositoryState.AUTH_REQUIRED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.CLONE,
        status=SyncJobStatus.AUTH_REQUIRED,
        error_code="connection_failed",
        error_message="Authentication or firewall access is required.",
    )
    wake = Mock(return_value=True)
    monkeypatch.setattr(views, "wake_sync_worker", wake)

    page = loopback_client.get("/bitbucket/")
    status = loopback_client.get(reverse("bitbucket:sync_status"), {"job": str(job.pk)})
    retry = loopback_client.post(reverse("bitbucket:sync_retry", args=(job.pk,)))

    assert str(job.pk) in page.content.decode()
    payload = status.json()["jobs"][0]
    assert payload["status"] == SyncJobStatus.AUTH_REQUIRED
    assert payload["authenticationUrl"] == "https://scm.example.invalid/"
    assert payload["retryUrl"].endswith(f"/sync/{job.pk}/retry/")
    assert payload["cancelUrl"].endswith(f"/sync/{job.pk}/cancel/")
    assert retry.status_code == 202
    job.refresh_from_db()
    assert job.status == SyncJobStatus.QUEUED
    wake.assert_called_once_with()

    job.status = SyncJobStatus.AUTH_REQUIRED
    job.save(update_fields=("status",))
    cancelled = loopback_client.post(reverse("bitbucket:sync_cancel", args=(job.pk,)))
    assert cancelled.status_code == 200
    job.refresh_from_db()
    assert job.status == SyncJobStatus.CANCELLED


def test_daily_pull_is_queued_only_once(settings):
    settings.BITBUCKET_APP_DAILY_PULL_ENABLED = True
    settings.BITBUCKET_APP_DAILY_PULL_LOCAL_HOUR = 0
    observed = datetime(2026, 9, 4, 12, tzinfo=UTC)
    item = repository(last_successful_pull_on=date(2026, 9, 3))

    first = queue_due_daily_pulls(at=observed)
    second = queue_due_daily_pulls(at=observed + timedelta(hours=1))

    assert len(first) == 1
    assert first[0].operation == SyncOperation.PULL
    assert first[0].scheduled_for == date(2026, 9, 4)
    assert second == ()
    assert SyncJob.objects.filter(repository=item).count() == 1


def _git(directory: Path, *arguments: str, env=None) -> None:
    subprocess.run(
        ("git", "-C", str(directory), *arguments),
        check=True,
        capture_output=True,
        env=env,
    )


def test_catalog_uses_real_git_add_history_and_pdf_commit_counts(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-b", "main")
    _git(checkout, "config", "user.name", "Test User")
    _git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "docs").mkdir()
    (checkout / "docs" / "architecture.pdf").write_bytes(b"%PDF-1.4\n")
    first_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Alice Architect",
        "GIT_AUTHOR_EMAIL": "alice@example.invalid",
        "GIT_COMMITTER_NAME": "Alice Architect",
        "GIT_COMMITTER_EMAIL": "alice@example.invalid",
        "GIT_AUTHOR_DATE": "2026-08-30T09:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-08-30T09:00:00+00:00",
    }
    _git(checkout, "add", "docs/architecture.pdf")
    _git(checkout, "commit", "-m", "Add architecture", env=first_env)
    (checkout / "docs" / "architecture.pdf").write_bytes(b"%PDF-1.4\nupdated\n")
    (checkout / "docs" / "process.vsdx").write_bytes(b"vsdx")
    second_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Bob Builder",
        "GIT_AUTHOR_EMAIL": "bob@example.invalid",
        "GIT_COMMITTER_NAME": "Bob Builder",
        "GIT_COMMITTER_EMAIL": "bob@example.invalid",
        "GIT_AUTHOR_DATE": "2026-09-02T11:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-02T11:00:00+00:00",
    }
    _git(checkout, "add", "docs/architecture.pdf", "docs/process.vsdx")
    _git(checkout, "commit", "-m", "Update PDF and add VSDX", env=second_env)
    item = repository()

    counts = refresh_catalog(item, checkout)

    assert counts == (1, 1)
    document = Document.objects.get(kind=DocumentKind.PDF)
    assert document.relative_path == "docs/architecture.pdf"
    assert document.added_by == "Alice Architect"
    assert document.added_at.date() == date(2026, 8, 30)
    contributors = {person.name: person.pdf_commit_count for person in Contributor.objects.all()}
    assert contributors == {"Alice Architect": 1, "Bob Builder": 1}


def test_timeline_has_every_requested_non_overlapping_bucket():
    today = date(2026, 9, 17)
    examples = {
        date(2026, 9, 17): "Today",
        date(2026, 9, 16): "Yesterday",
        date(2026, 9, 15): "Day before yesterday",
        date(2026, 9, 14): "This week",
        date(2026, 9, 5): "This month",
        date(2026, 8, 5): "Last month",
        date(2026, 7, 5): "Last 3 months",
        date(2026, 4, 5): "Last 6 months",
        date(2026, 1, 5): "This year",
        date(2025, 5, 5): "Last year",
        date(2024, 5, 5): "Last 2 years",
        date(2023, 5, 5): "Last 3 years",
    }

    assert {views.timeline_label(value, today=today) for value in examples} == set(
        examples.values()
    )


def test_pdf_page_size_is_exactly_500(loopback_client):
    item = repository(pdf_count=501)
    Document.objects.bulk_create(
        [
            Document(
                repository=item,
                kind=DocumentKind.PDF,
                relative_path=f"docs/{index:03d}.pdf",
                filename=f"{index:03d}.pdf",
            )
            for index in range(501)
        ]
    )

    response = loopback_client.get("/bitbucket/")

    assert response.context["page"].paginator.per_page == 500
    assert len(response.context["page"].object_list) == 500
    assert response.context["page"].paginator.num_pages == 2


def test_clicking_pdf_opens_it_and_increments_count(loopback_client, monkeypatch):
    item = repository()
    document = Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/guide.pdf",
        filename="guide.pdf",
        open_count=4,
    )
    opener = Mock()
    monkeypatch.setattr(views, "open_pdf", opener)

    response = loopback_client.post(reverse("bitbucket:document_open", args=(document.pk,)))

    assert response.status_code == 200
    assert response.json()["openCount"] == 5
    document.refresh_from_db()
    assert document.open_count == 5
    opener.assert_called_once_with(document)


def test_bulk_open_and_reveal_are_local_actions(loopback_client, monkeypatch):
    item = repository()
    documents = [
        Document.objects.create(
            repository=item,
            kind=DocumentKind.PDF,
            relative_path=f"docs/{name}.pdf",
            filename=f"{name}.pdf",
        )
        for name in ("one", "two")
    ]
    opener = Mock()
    revealer = Mock(return_value=Path("/local/docs"))
    monkeypatch.setattr(views, "open_pdf", opener)
    monkeypatch.setattr(views, "reveal_pdf", revealer)

    opened = loopback_client.post(
        reverse("bitbucket:documents_open"),
        data=json.dumps({"documentIds": [document.pk for document in documents]}),
        content_type="application/json",
    )
    revealed = loopback_client.post(reverse("bitbucket:document_reveal", args=(documents[0].pk,)))

    assert opened.json()["opened"] == [document.pk for document in documents]
    assert opener.call_args_list == [call(document) for document in documents]
    assert revealed.json() == {"ok": True, "folder": "/local/docs"}
    revealer.assert_called_once_with(documents[0])


def test_app_does_not_import_or_reference_bitbucket_search(settings):
    app_root = Path(settings.BASE_DIR) / "bitbucket"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert "bitbucket_search" not in source
