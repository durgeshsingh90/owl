from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pymupdf
import pytest
from django.core.management import get_commands
from django.urls import reverse

from bitbucket import views
from bitbucket.models import (
    Contributor,
    Document,
    DocumentIndexState,
    DocumentKind,
    HTTPSCredential,
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)
from bitbucket.services import remote_sync
from bitbucket.services.api import (
    AddedMetadata,
    BitbucketAPIClient,
    BitbucketAPIError,
    CommitMetadata,
)
from bitbucket.services.catalog import CatalogStats, refresh_catalog
from bitbucket.services.credentials import CredentialError, resolve_credential
from bitbucket.services.repository_urls import (
    RepositoryURLValidationError,
    parse_repository_url,
)
from bitbucket.services.scheduler import queue_due_daily_refreshes

pytestmark = pytest.mark.django_db
TEST_REPOSITORY_HOST = "scm.example.invalid"


@pytest.fixture(autouse=True)
def disable_automatic_pull(settings):
    settings.BITBUCKET_APP_DAILY_REFRESH_ENABLED = False


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


def sample_pdf_bytes(text: str = "Architecture approval evidence") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


def test_app_is_separate_and_integrated_into_homepage(loopback_client):
    assert reverse("bitbucket:index") == "/bitbucket/"
    response = loopback_client.get("/")
    html = response.content.decode()

    assert response.status_code == 200
    frontend = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "home" / "HomeApp.tsx"
    ).read_text()
    assert 'id="home-root"' in html
    assert "workspace.urls.bitbucket" in frontend
    assert "Bitbucket API metadata" in frontend


def test_document_worker_has_an_app_specific_management_command():
    commands = get_commands()

    assert commands["bitbucket_document_worker"] == "bitbucket"
    assert "bitbucket_sync_worker" not in commands


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
            "DJANGO_SECRET_KEY": (
                "synthetic-test-secret-key-only-not-for-real-use-0123456789-abcdefghij"
            ),
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
        "bitbucket_httpscredential",
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
    workspace_response = loopback_client.get(reverse("bitbucket:workspace"))
    payload = workspace_response.json()
    frontend = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "DocumentLibrary.tsx"
    ).read_text()

    assert response.status_code == 200
    assert workspace_response.status_code == 200
    assert 'id="bitbucket-root"' in html
    assert 'data-workspace-url="/bitbucket/workspace/"' in html
    assert "owl-frontend.css" in html
    assert "owl-frontend.js" in html
    assert "bb-workspace" not in html
    assert payload["repositories"][0]["project"] == "adr"
    assert payload["timeline"][0]["documents"][0]["filename"] == "architecture.pdf"
    for label in (
        "PDF name",
        "Date added to repo",
        "Added by",
        "Commit ID",
        "Open count",
        "Show in folder",
    ):
        assert label in frontend


def test_document_desk_uses_react_vite_source_and_no_handwritten_static_assets(settings):
    root = Path(settings.BASE_DIR).parent

    assert (root / "frontend" / "package.json").is_file()
    assert (root / "frontend" / "vite.config.ts").is_file()
    assert (root / "frontend" / "src" / "main.tsx").is_file()
    assert (root / "frontend" / "dist" / "owl-frontend.js").is_file()
    assert not (Path(settings.BASE_DIR) / "static" / "bitbucket" / "app.js").exists()
    assert not (Path(settings.BASE_DIR) / "static" / "bitbucket" / "app.css").exists()


def test_document_desk_has_persistent_system_aware_dark_mode(loopback_client, settings):
    response = loopback_client.get("/bitbucket/")
    html = response.content.decode()
    frontend = Path(settings.BASE_DIR).parent / "frontend" / "src"
    css = (frontend / "styles.css").read_text()
    theme_hook = (frontend / "hooks" / "useTheme.ts").read_text()
    component = (frontend / "components" / "DocumentLibrary.tsx").read_text()

    assert 'name="color-scheme" content="light dark"' in html
    assert ':root[data-theme="dark"]' in css
    assert "prefers-color-scheme: dark" in css
    assert "Dark mode" in component
    assert "bitbucket-document-desk-theme" in theme_hook
    assert "localStorage.setItem" in theme_hook


def test_sample_server_url_extracts_project_and_repository():
    parsed = parse_repository_url(
        "https://scm.example.invalid/stash/scm/adr/engineering-availability-sign-off.git"
    )

    assert parsed.project == "adr"
    assert parsed.slug == "engineering-availability-sign-off"
    assert parsed.url.endswith("/adr/engineering-availability-sign-off.git")
    assert parsed.origin == "https://scm.example.invalid"
    assert parsed.base_url == "https://scm.example.invalid/stash"
    assert parsed.api_repository_url.endswith(
        "/stash/rest/api/latest/projects/adr/repos/engineering-availability-sign-off"
    )
    assert parsed.browse_url("docs/design.pdf").endswith(
        "/stash/projects/adr/repos/engineering-availability-sign-off/browse/docs/design.pdf"
    )


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


def test_settings_saves_encrypted_origin_token_and_queues_api_fetch(loopback_client, monkeypatch):
    wake = Mock(return_value=True)
    monkeypatch.setattr(views, "wake_sync_worker", wake)
    token = "not-a-real-token"

    response = loopback_client.post(
        reverse("bitbucket:settings_save"),
        {
            "repository_url": (
                "https://private.example.invalid/stash/scm/adr/engineering-sign-off.git"
            ),
            "username": "api-reader",
            "access_token": token,
        },
    )

    assert response.status_code == 202
    saved = Repository.objects.get()
    assert saved.host == "private.example.invalid"
    assert saved.project == "adr"
    assert saved.slug == "engineering-sign-off"
    credential = HTTPSCredential.objects.get()
    assert credential.origin == "https://private.example.invalid"
    assert credential.username == "api-reader"
    assert token not in credential.token_ciphertext
    resolved = resolve_credential(saved)
    assert resolved.username == "api-reader"
    assert resolved.token == token
    job = SyncJob.objects.get(repository=saved)
    assert job.operation == SyncOperation.INITIAL
    wake.assert_called_once_with()


def test_api_client_uses_bearer_auth_on_the_exact_data_center_endpoint():
    item = repository()
    observed: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"slug": "engineering-sign-off"})

    with BitbucketAPIClient(
        item,
        "synthetic-never-valid-http-token",
        transport=httpx.MockTransport(respond),
    ) as client:
        client.test_connection()

    assert len(observed) == 1
    request = observed[0]
    assert str(request.url) == (
        "https://scm.example.invalid/stash/rest/api/latest/projects/adr/repos/engineering-sign-off"
    )
    assert request.headers["Authorization"] == "Bearer synthetic-never-valid-http-token"


def test_api_client_uses_username_and_token_basic_auth_when_username_is_configured():
    item = repository()
    observed: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"slug": "engineering-sign-off"})

    with BitbucketAPIClient(
        item,
        "synthetic-never-valid-http-token",
        username="api-reader",
        transport=httpx.MockTransport(respond),
    ) as client:
        client.test_connection()

    scheme, encoded = observed[0].headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == ("api-reader:synthetic-never-valid-http-token")


def test_api_client_retries_rate_limit_and_extracts_downloaded_pdf():
    item = repository()
    content = sample_pdf_bytes("Searchable architecture design")
    waits: list[float] = []
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.url.path.endswith("/raw/docs/architecture.pdf")
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(200, content=content, headers={"Content-Type": "application/pdf"})

    with BitbucketAPIClient(
        item,
        "synthetic-never-valid-http-token",
        transport=httpx.MockTransport(respond),
        sleep=waits.append,
    ) as client:
        downloaded = client.download_pdf("docs/architecture.pdf")

    assert downloaded == content
    assert attempts == 2
    assert waits == [0.25]


def test_api_client_rejects_redirects_without_forwarding_the_token():
    item = repository()
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://foreign.example.invalid/"})

    with (
        BitbucketAPIClient(
            item,
            "synthetic-never-valid-http-token",
            transport=httpx.MockTransport(redirect),
        ) as client,
        pytest.raises(BitbucketAPIError) as caught,
    ):
        client.test_connection()

    assert caught.value.code == "unsafe_redirect"
    assert len(requests) == 1


def test_api_client_lists_only_document_types_and_reads_oldest_path_commit():
    item = repository()
    first_timestamp = int(datetime(2026, 8, 30, 9, tzinfo=UTC).timestamp() * 1000)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        "docs/architecture.pdf",
                        "docs/process.VSDX",
                        "README.md",
                    ],
                    "isLastPage": True,
                },
            )
        assert request.url.path.endswith("/commits")
        assert request.url.params["path"] == "docs/architecture.pdf"
        if "start" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "id": "b" * 40,
                            "authorTimestamp": first_timestamp + 86_400_000,
                            "author": {"name": "Later Editor"},
                        }
                    ],
                    "isLastPage": False,
                    "nextPageStart": 41,
                },
            )
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "id": "a" * 40,
                        "authorTimestamp": first_timestamp,
                        "author": {
                            "displayName": "Alice Architect",
                            "emailAddress": "alice@example.invalid",
                        },
                    }
                ],
                "isLastPage": True,
            },
        )

    with BitbucketAPIClient(
        item,
        "synthetic-never-valid-http-token",
        transport=httpx.MockTransport(respond),
    ) as client:
        pdfs, vsdx_count = client.document_paths()
        added = client.added_metadata(pdfs[0])

    assert pdfs == ("docs/architecture.pdf",)
    assert vsdx_count == 1
    assert added == AddedMetadata(
        commit_id="a" * 40,
        author="Alice Architect",
        email="alice@example.invalid",
        authored_at=datetime(2026, 8, 30, 9, tzinfo=UTC),
    )


def test_spawned_document_worker_inherits_server_console(monkeypatch, settings):
    process = SimpleNamespace(pid=4321, poll=lambda: None)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(remote_sync, "_worker_process", None)
    monkeypatch.setattr(remote_sync.subprocess, "Popen", spawn)

    assert remote_sync.wake_sync_worker() is True

    kwargs = spawn.call_args.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs


def test_token_check_must_succeed_before_api_catalogue(monkeypatch):
    item = repository(state=RepositoryState.QUEUED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.INITIAL,
        status=SyncJobStatus.RUNNING,
    )
    events: list[str] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def test_connection(self):
            events.append("token-check")

    monkeypatch.setattr(
        remote_sync,
        "resolve_credential",
        Mock(return_value=SimpleNamespace(username="", token="not-a-real-token")),
    )
    monkeypatch.setattr(remote_sync, "BitbucketAPIClient", Mock(return_value=FakeClient()))
    monkeypatch.setattr(
        remote_sync,
        "refresh_catalog",
        Mock(
            side_effect=lambda *_args, **_kwargs: (
                events.append("catalog") or CatalogStats(3, 3, 0, 2, 3, 0)
            )
        ),
    )

    result = remote_sync.process_job(job)

    assert events == ["token-check", "catalog"]
    assert result.status == SyncJobStatus.SUCCEEDED
    item.refresh_from_db()
    assert (item.pdf_count, item.vsdx_count) == (3, 2)
    assert item.last_successful_refresh_on == date.today()


def test_missing_token_requires_authentication_and_never_calls_api(monkeypatch):
    item = repository(state=RepositoryState.QUEUED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.INITIAL,
        status=SyncJobStatus.RUNNING,
    )
    api_client = Mock()
    monkeypatch.setattr(
        remote_sync,
        "resolve_credential",
        Mock(side_effect=CredentialError("Configure an HTTP access token.")),
    )
    monkeypatch.setattr(remote_sync, "BitbucketAPIClient", api_client)

    result = remote_sync.process_job(job)

    assert result.status == SyncJobStatus.AUTH_REQUIRED
    api_client.assert_not_called()
    item.refresh_from_db()
    assert item.state == RepositoryState.AUTH_REQUIRED


def test_authentication_job_can_retry_or_cancel(loopback_client, monkeypatch):
    item = repository(state=RepositoryState.AUTH_REQUIRED)
    job = SyncJob.objects.create(
        repository=item,
        operation=SyncOperation.INITIAL,
        status=SyncJobStatus.AUTH_REQUIRED,
        error_code="connection_failed",
        error_message="Authentication or firewall access is required.",
    )
    wake = Mock(return_value=True)
    monkeypatch.setattr(views, "wake_sync_worker", wake)

    page = loopback_client.get(reverse("bitbucket:workspace"))
    status = loopback_client.get(reverse("bitbucket:sync_status"), {"job": str(job.pk)})
    retry = loopback_client.post(reverse("bitbucket:sync_retry", args=(job.pk,)))

    assert page.json()["jobs"][0]["id"] == str(job.pk)
    payload = status.json()["jobs"][0]
    assert payload["status"] == SyncJobStatus.AUTH_REQUIRED
    assert payload["authenticationUrl"] == "https://scm.example.invalid/stash/"
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


def test_daily_api_refresh_is_queued_only_once(settings):
    settings.BITBUCKET_APP_DAILY_REFRESH_ENABLED = True
    settings.BITBUCKET_APP_DAILY_REFRESH_LOCAL_HOUR = 0
    observed = datetime(2026, 9, 4, 12, tzinfo=UTC)
    item = repository(last_successful_refresh_on=date(2026, 9, 3))

    first = queue_due_daily_refreshes(at=observed)
    second = queue_due_daily_refreshes(at=observed + timedelta(hours=1))

    assert len(first) == 1
    assert first[0].operation == SyncOperation.REFRESH
    assert first[0].scheduled_for == date(2026, 9, 4)
    assert second == ()
    assert SyncJob.objects.filter(repository=item).count() == 1


def test_catalog_stores_only_pdf_metadata_and_vsdx_count():
    item = repository()
    pdf_content = sample_pdf_bytes()
    Document.objects.create(
        repository=item,
        kind=DocumentKind.VSDX,
        relative_path="old/local-row.vsdx",
        filename="local-row.vsdx",
    )
    Contributor.objects.create(
        repository=item,
        name="Old contributor",
        identity_key="old@example.invalid",
    )

    class FakeClient:
        def document_paths(self):
            return (("docs/architecture.pdf",), 1)

        def added_metadata(self, path):
            assert path == "docs/architecture.pdf"
            return AddedMetadata(
                commit_id="a" * 40,
                author="Alice Architect",
                email="alice@example.invalid",
                authored_at=datetime(2026, 8, 30, 9, tzinfo=UTC),
            )

        def latest_metadata(self, path):
            assert path == "docs/architecture.pdf"
            return CommitMetadata(
                commit_id="b" * 40,
                message="Refresh architecture diagram",
                author="Bob Builder",
                email="bob@example.invalid",
                authored_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
            )

        def download_pdf(self, path):
            assert path == "docs/architecture.pdf"
            return pdf_content

    counts = refresh_catalog(item, FakeClient())

    assert counts == CatalogStats(1, 1, 0, 1, 1, 0)
    document = Document.objects.get()
    assert document.kind == DocumentKind.PDF
    assert document.relative_path == "docs/architecture.pdf"
    assert document.added_by == "Alice Architect"
    assert document.added_at.date() == date(2026, 8, 30)
    assert document.commit_id == "a" * 40
    assert document.latest_commit_id == "b" * 40
    assert document.latest_commit_message == "Refresh architecture diagram"
    assert document.latest_commit_author == "Bob Builder"
    assert document.page_count == 1
    assert document.file_size > 0
    assert document.content_sha256 == hashlib.sha256(pdf_content).hexdigest()
    assert "Architecture approval evidence" in document.extracted_text
    assert document.index_state == DocumentIndexState.INDEXED
    assert not Contributor.objects.exists()

    document.open_count = 7
    document.save(update_fields=("open_count",))

    class UnchangedClient:
        def document_paths(self):
            return (("docs/architecture.pdf",), 2)

        def added_metadata(self, _path):
            raise AssertionError("Unchanged PDF history should use cached addition metadata")

        def latest_metadata(self, path):
            assert path == "docs/architecture.pdf"
            return CommitMetadata(
                commit_id="b" * 40,
                message="Refresh architecture diagram",
                author="Bob Builder",
                email="bob@example.invalid",
                authored_at=datetime(2026, 9, 4, 9, tzinfo=UTC),
            )

        def download_pdf(self, _path):
            raise AssertionError("Unchanged PDFs must not be downloaded again")

    assert refresh_catalog(item, UnchangedClient()) == CatalogStats(1, 1, 0, 2, 0, 1)
    assert Document.objects.get().open_count == 7


def test_pdf_crawler_defaults_to_one_worker(settings):
    assert settings.BITBUCKET_APP_MAX_WORKERS == 1


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

    response = loopback_client.get(reverse("bitbucket:workspace"))
    payload = response.json()

    assert payload["pageSize"] == 500
    assert sum(len(group["documents"]) for group in payload["timeline"]) == 500
    assert payload["pagination"]["total"] == 2


def test_workspace_searches_saved_pdf_text_and_returns_a_text_preview(loopback_client):
    item = repository(pdf_count=2, indexed_pdf_count=2)
    matching = Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/security-review.pdf",
        filename="security-review.pdf",
        extracted_text="The approved zero trust recovery plan covers every service.",
        index_state=DocumentIndexState.INDEXED,
    )
    Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/other.pdf",
        filename="other.pdf",
        extracted_text="Unrelated reference material.",
        index_state=DocumentIndexState.INDEXED,
    )

    response = loopback_client.get(reverse("bitbucket:workspace"), {"q": "zero trust"})
    payload = response.json()

    assert response.status_code == 200
    assert payload["search"] == {"query": "zero trust", "active": True, "resultCount": 1}
    documents = [document for group in payload["timeline"] for document in group["documents"]]
    assert [document["id"] for document in documents] == [matching.pk]
    assert "zero trust recovery plan" in documents[0]["textPreview"]


def test_clicking_pdf_records_open_count(loopback_client):
    item = repository()
    document = Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/guide.pdf",
        filename="guide.pdf",
        open_count=4,
    )
    response = loopback_client.post(reverse("bitbucket:document_open", args=(document.pk,)))

    assert response.status_code == 200
    assert response.json()["openCount"] == 5
    document.refresh_from_db()
    assert document.open_count == 5


def test_workspace_returns_direct_file_and_containing_folder_urls(loopback_client):
    item = repository()
    Document.objects.create(
        repository=item,
        kind=DocumentKind.PDF,
        relative_path="docs/architecture/guide.pdf",
        filename="guide.pdf",
    )

    response = loopback_client.get(reverse("bitbucket:workspace"))
    payload = response.json()["timeline"][0]["documents"][0]

    assert payload["browserUrl"].endswith(
        "/stash/projects/adr/repos/engineering-sign-off/browse/docs/architecture/guide.pdf"
    )
    assert payload["folderUrl"].endswith(
        "/stash/projects/adr/repos/engineering-sign-off/browse/docs/architecture"
    )
    assert "token" not in payload["browserUrl"].casefold()


def test_app_does_not_import_or_reference_bitbucket_search(settings):
    app_root = Path(settings.BASE_DIR) / "bitbucket"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert "bitbucket_search" not in source
