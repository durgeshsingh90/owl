from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views as bitbucket_views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositoryOperationLogChannel,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import repository_sync
from bitbucket_search.services.operation_logs import append_operation_log_entry
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_sync import repository_status_snapshot

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_repository_workspace_has_add_control_list_filter_and_background_copy(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert '<summary class="bb-add-repository" aria-label="Add a new repository">' in html
    assert html.count('aria-label="Add a new repository"') == 2
    assert html.count("data-selected-refresh disabled") == 2
    assert html.count("data-selected-stop-indexing disabled") == 2
    assert "data-selected-unlock" not in html
    assert html.count("data-selected-remove disabled") == 2
    assert html.count('data-delete-locked="true"') == 2
    assert html.count("data-selected-delete-icon") == 2
    assert html.count("🗑️") == 0
    assert html.count("/static/bitbucket_search/icons/stop.png") == 2
    assert html.count("/static/bitbucket_search/icons/delete.png") == 2
    repository_template = (
        Path(__file__).parents[1] / "templates" / "bitbucket_search" / "_repository_list.html"
    ).read_text(encoding="utf-8")
    assert "bitbucket_search/icons/indexing.gif" in repository_template
    assert html.count("data-repository-delete-status") == 1
    assert 'id="bb-repository-selection-form"' in html
    assert 'action="/pdfs/repositories/add/"' in html
    assert 'name="repository_url"' in html
    assert "Paste one repository URL per line." in html
    assert "Add &amp; clone in background" in html
    assert "Allowed host:" in html
    assert "bitbucket.org" in html
    assert 'placeholder="Search repositories…" disabled' in html
    assert "Filter the repositories managed by this OWL workspace." not in html
    assert 'aria-describedby="bb-repository-search-note"' not in html
    assert 'data-repository-status-url="/pdfs/repositories/status/"' in html
    assert 'data-daily-refresh-enabled="true"' in html
    assert 'data-catalog-publication-signature="' in html
    assert "data-bitbucket-schedule-tick-form" in html
    assert 'action="/pdfs/repositories/schedule/tick/"' in html
    assert 'target="owl-bitbucket-schedule-tick"' in html
    assert "bitbucket_search/bitbucket_search.css?v=blocking-repository-actions-v1" in html
    assert "bitbucket_search/bitbucket_search.js?v=blocking-repository-actions-v1" in html
    assert "data-repository-operation-overlay" in html
    assert "bitbucket_search/icons/work-in-progress.gif" in html
    assert "bitbucket_search/icons/connection-connected.png" in html
    assert "bitbucket_search/icons/connection-disconnected.png" in html
    assert "bitbucket_search/icons/no-connection.gif" in html
    assert html.index('class="bb-topbar__actions"') < html.index(
        "data-repository-connection-result"
    )
    assert 'class="bb-connection-test" type="button"' in html
    assert "data-repository-connection-message" in html
    assert 'name="confirmed"' not in html


@pytest.mark.parametrize("view_name", ["bitbucket_search:index", "bitbucket_search:repositories"])
def test_workspace_omits_redundant_search_navigation_and_keeps_repository_controls(
    loopback_client, view_name
):
    response = loopback_client.get(reverse(view_name))
    html = response.content.decode()
    topbar = re.search(r'<header class="bb-topbar">(?P<body>.*?)</header>', html, re.DOTALL)
    rail = re.search(
        r'<aside class="bb-repository-rail"[^>]*>(?P<body>.*?)</aside>',
        html,
        re.DOTALL,
    )
    mobile_navigation = re.search(
        r'<nav aria-label="Bitbucket Search mobile functions">(?P<body>.*?)</nav>',
        html,
        re.DOTALL,
    )

    assert response.status_code == 200
    assert topbar is not None
    assert rail is not None
    assert mobile_navigation is not None
    assert "Sync status" not in topbar.group("body")
    assert "bb-sync-status" not in topbar.group("body")
    assert "data-sync-summary" not in topbar.group("body")
    assert "bb-rail-links" not in html
    assert "Search PDFs" not in rail.group("body")
    assert "Index &amp; refresh status" not in rail.group("body")
    assert 'id="bb-repositories-heading">Repositories</h2>' in rail.group("body")
    assert 'aria-label="Add a new repository"' in rail.group("body")
    assert "data-selected-refresh" in rail.group("body")
    assert "data-selected-exclude" in rail.group("body")
    assert "data-selected-stop-indexing" in rail.group("body")
    assert "data-selected-remove" in rail.group("body")
    assert "data-selected-unlock" not in html
    toolbars = re.findall(
        r'<div class="bb-repository-selection-toolbar"[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    assert len(toolbars) == 2
    for toolbar in toolbars:
        assert len(re.findall(r"<button\b", toolbar)) == 6
        assert "data-selected-select-all" in toolbar
        assert 'data-delete-locked="true"' in toolbar
        assert "data-selected-delete-icon" in toolbar
        assert "🔒" in toolbar
    assert "data-repository-filter" in rail.group("body")
    for total in ("repositories", "pdfs", "vsdx", "bytes"):
        assert f"data-total-{total}" in rail.group("body")
    assert "Search PDFs" not in mobile_navigation.group("body")
    assert 'href="/pdfs/repositories/"' in mobile_navigation.group("body")
    assert ">Repositories</a>" in mobile_navigation.group("body")
    assert 'href="/pdfs/status/">Repository logs</a>' in mobile_navigation.group("body")
    assert 'action="/pdfs/" data-pdf-search-form' in html
    assert "data-pdf-search-input" in html
    assert "data-pdf-search-input disabled" not in html

    static_root = Path(__file__).parents[1] / "static" / "bitbucket_search"
    assert "bb-sync-status" not in (static_root / "bitbucket_search.css").read_text(
        encoding="utf-8"
    )
    assert ".bb-rail-links" not in (static_root / "bitbucket_search.css").read_text(
        encoding="utf-8"
    )
    assert "data-sync-summary" not in (static_root / "bitbucket_search.js").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("view_name", ["bitbucket_search:index", "bitbucket_search:repositories"])
def test_bitbucket_topbar_omits_settings_icon_and_preserves_other_controls(
    loopback_client, view_name
):
    BitbucketRepository.objects.create(
        display_name="design-notes",
        canonical_remote_key="bitbucket.org/workspace/design-notes",
        remote_url="ssh://git@bitbucket.org/workspace/design-notes.git",
        sync_state=RepositorySyncState.READY,
    )
    response = loopback_client.get(reverse(view_name))
    html = response.content.decode()
    # The status and notification dialogs each contain a nested header; match the
    # complete top bar up to the workspace stage rather than its first header end.
    topbar = re.search(
        r'<header class="bb-topbar">(?P<body>.*?)</header>\s*<section class="bb-stage"',
        html,
        re.DOTALL,
    )

    assert response.status_code == 200
    assert topbar is not None
    topbar_html = topbar.group("body")
    controls = re.findall(r"<(?:a|button|summary)\b[^>]*>", topbar_html, re.DOTALL)
    assert "bb-icon-button--repositories" not in topbar_html
    assert not any(
        re.search(r'(?:aria-label|title)="[^"]*(?:settings|manage repositories)', control, re.I)
        for control in controls
    )
    assert "data-settings-open" not in topbar_html
    assert "data-repository-status-toggle" not in topbar_html
    assert 'aria-label="Open repository logs"' not in topbar_html
    assert 'aria-label="Copy all repository URLs"' not in topbar_html
    assert 'aria-label="Copy all repository URLs"' in html
    assert "data-notification-toggle" in topbar_html
    assert 'aria-label="Open notifications"' in topbar_html
    assert '<summary class="bb-icon-button" aria-label="Applications">' in topbar_html
    assert f'href="{reverse("core:system_status")}" aria-label="System status"' in topbar_html
    assert 'data-theme-toggle aria-label="Switch to light mode"' in topbar_html
    assert "data-repositories-refresh-all" in topbar_html
    assert "data-refresh-all-button" in topbar_html
    assert f'action="{reverse("bitbucket_search:repositories_refresh_all")}"' in topbar_html
    assert 'aria-label="Refresh all repositories"' in topbar_html
    assert "data-refresh-all-icon" in topbar_html
    assert "data-refresh-all-spinner hidden" in topbar_html


def test_copy_all_repository_urls_uses_sanitized_one_per_line_source(loopback_client):
    BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="https://bitbucket.org/workspace/architecture.git",
    )
    BitbucketRepository.objects.create(
        display_name="Security",
        canonical_remote_key="bitbucket.org/workspace/security",
        remote_url="ssh://git@bitbucket.org/workspace/security.git",
    )

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="bb-repository-copy-urls"' in html
    assert response.context["repository_copy_urls"] == (
        "https://bitbucket.org/workspace/architecture.git",
        "ssh://git@bitbucket.org/workspace/security.git",
    )
    assert "data-copy-repository-urls" in html


def test_desktop_topbar_spans_results_and_people_starts_below_it(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    stylesheet = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")

    topbar_start = html.index('<header class="bb-topbar">')
    topbar_end = html.index("</header>", topbar_start)
    stage_start = html.index('<section class="bb-stage"')
    people_start = html.index('<aside class="bb-people-rail"')

    assert response.status_code == 200
    assert topbar_start < topbar_end < stage_start < people_start
    assert "grid-template-rows: auto minmax(0, 1fr);" in stylesheet
    assert "grid-column: 2 / -1;" in stylesheet
    assert "top: 81px;" in stylesheet
    assert "height: calc(100vh - 81px);" in stylesheet


def _workspace_refresh_form(html: str) -> str:
    match = re.search(
        r'<form\s+class="bb-refresh-all[^\"]*".*?</form>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def _repository_cards(html: str, repository_id: int) -> tuple[str, ...]:
    return tuple(
        re.findall(
            rf'<li class="bb-repository-card" data-repository-id="{repository_id}".*?</li>',
            html,
            flags=re.DOTALL,
        )
    )


def _assert_refresh_work_summary(form: str, summary: dict) -> None:
    assert summary["active"] is True
    for hook, key in (("label", "label"), ("detail", "detail")):
        content = re.search(rf"data-refresh-all-{hook}>\s*(.*?)\s*</", form, re.DOTALL)
        assert content is not None
        assert content.group(1) == escape(summary[key])
    assert 'tabindex="0"' in form
    assert 'aria-describedby="bb-refresh-all-description"' in form
    assert 'id="bb-refresh-all-description"' in form


def test_refresh_all_controls_are_accessible_and_truthful_when_unavailable(loopback_client):
    empty_response = loopback_client.get(reverse("bitbucket_search:index"))
    empty_html = empty_response.content.decode()
    desktop_empty = _workspace_refresh_form(empty_html)

    assert empty_response.status_code == 200
    assert empty_html.count('action="/pdfs/repositories/refresh/"') == 1
    assert "bb-refresh-all--disabled" in desktop_empty
    assert "Refresh all repositories unavailable: no repositories are connected" in desktop_empty
    assert "disabled" in desktop_empty
    assert "No repositories connected" in empty_html

    disabled_repository = BitbucketRepository.objects.create(
        display_name="archived",
        canonical_remote_key="bitbucket.org/workspace/archived",
        remote_url="ssh://git@bitbucket.org/workspace/archived.git",
        sync_state=RepositorySyncState.READY,
        enabled=False,
    )
    disabled_response = loopback_client.get(reverse("bitbucket_search:index"))
    disabled_html = disabled_response.content.decode()

    assert disabled_response.context["enabled_repository_count"] == 0
    assert disabled_html.count("No repositories included in refresh") == 1
    assert "no repositories are included" in _workspace_refresh_form(disabled_html)

    disabled_repository.sync_state = RepositorySyncState.QUEUED
    disabled_repository.save(update_fields={"sync_state"})
    RepositorySyncJob.objects.create(
        repository=disabled_repository,
        operation=RepositorySyncOperation.REFRESH,
    )
    BitbucketRepository.objects.create(
        display_name="enabled-idle",
        canonical_remote_key="bitbucket.org/workspace/enabled-idle",
        remote_url="ssh://git@bitbucket.org/workspace/enabled-idle.git",
        sync_state=RepositorySyncState.READY,
    )

    mixed_response = loopback_client.get(reverse("bitbucket_search:index"))
    mixed_html = mixed_response.content.decode()

    assert mixed_response.context["active_repository_count"] == 1
    assert mixed_response.context["active_enabled_repository_count"] == 0
    assert 'data-active-repository-count="1"' in mixed_html
    assert "bb-refresh-all--active" in _workspace_refresh_form(mixed_html)
    assert 'disabled aria-busy="true"' in _workspace_refresh_form(mixed_html)


def test_refresh_all_controls_enable_only_when_all_repositories_are_idle(loopback_client):
    idle = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
        sync_state=RepositorySyncState.READY,
    )
    idle_response = loopback_client.get(reverse("bitbucket_search:index"))
    idle_html = idle_response.content.decode()

    assert idle_response.context["enabled_repository_count"] == 1
    assert idle_response.context["active_repository_count"] == 0
    assert 'data-enabled-repository-count="1"' in idle_html
    assert 'aria-label="Refresh all repositories"' in idle_html
    assert "disabled" not in _workspace_refresh_form(idle_html)

    active = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    RepositorySyncJob.objects.create(
        repository=active,
        operation=RepositorySyncOperation.REFRESH,
    )
    partial_response = loopback_client.get(reverse("bitbucket_search:index"))
    partial_html = partial_response.content.decode()
    partial_desktop = _workspace_refresh_form(partial_html)

    assert partial_response.context["enabled_repository_count"] == 2
    assert partial_response.context["active_repository_count"] == 1
    assert "bb-refresh-all--active" in partial_desktop
    _assert_refresh_work_summary(
        partial_desktop, partial_response.context["repository_work_summary"]
    )
    assert "Git sync queued" in partial_desktop
    assert "networking" in partial_desktop
    assert "Refresh remaining repositories" not in partial_html
    assert 'disabled aria-busy="true"' in partial_desktop

    idle.sync_state = RepositorySyncState.QUEUED
    idle.save(update_fields={"sync_state"})
    RepositorySyncJob.objects.create(
        repository=idle,
        operation=RepositorySyncOperation.REFRESH,
    )
    active_response = loopback_client.get(reverse("bitbucket_search:index"))
    active_html = active_response.content.decode()
    active_desktop = _workspace_refresh_form(active_html)

    assert active_response.context["active_repository_count"] == 2
    assert "bb-refresh-all--active" in active_desktop
    _assert_refresh_work_summary(active_desktop, active_response.context["repository_work_summary"])
    assert "architecture" in active_desktop
    assert "networking" in active_desktop
    assert 'disabled aria-busy="true"' in active_desktop


@pytest.mark.parametrize(
    ("sync_state", "job_status"),
    [
        (RepositorySyncState.QUEUED, None),
        (RepositorySyncState.CLONING, None),
        (RepositorySyncState.FETCHING, None),
        (RepositorySyncState.UPDATING, None),
        (RepositorySyncState.READY, RepositorySyncJobStatus.QUEUED),
        (RepositorySyncState.READY, RepositorySyncJobStatus.RUNNING),
    ],
)
def test_refresh_all_controls_stay_disabled_through_add_and_refresh_phases(
    loopback_client,
    sync_state,
    job_status,
):
    repository = BitbucketRepository.objects.create(
        display_name="active",
        canonical_remote_key="bitbucket.org/workspace/active",
        remote_url="ssh://git@bitbucket.org/workspace/active.git",
        sync_state=sync_state,
    )
    if job_status:
        RepositorySyncJob.objects.create(
            repository=repository,
            operation=RepositorySyncOperation.CLONE,
            status=job_status,
            started_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
            heartbeat_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
        )

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    status = loopback_client.get(reverse("bitbucket_search:repository_status"))

    assert response.context["active_repository_count"] == 1
    assert status.json()["repositories"][0]["active"] is True
    form = _workspace_refresh_form(html)
    assert 'disabled aria-busy="true"' in form
    assert 'data-active-repository-count="1"' in form
    _assert_refresh_work_summary(form, response.context["repository_work_summary"])
    assert "data-refresh-all-icon hidden" not in form
    assert "data-refresh-all-spinner hidden" in form
    assert "data-refresh-all-visual" in form
    assert "data-refresh-all-running-visual" in form
    assert "data-overall-progress" in form


@pytest.mark.parametrize(
    ("sync_state", "job_status"),
    [
        (RepositorySyncState.READY, RepositorySyncJobStatus.SUCCEEDED),
        (RepositorySyncState.FAILED, RepositorySyncJobStatus.FAILED),
        (RepositorySyncState.INTERRUPTED, RepositorySyncJobStatus.INTERRUPTED),
    ],
)
def test_refresh_all_controls_reenable_after_jobs_finish(loopback_client, sync_state, job_status):
    repository = BitbucketRepository.objects.create(
        display_name="finished",
        canonical_remote_key="bitbucket.org/workspace/finished",
        remote_url="ssh://git@bitbucket.org/workspace/finished.git",
        sync_state=RepositorySyncState.FETCHING,
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )
    busy_response = loopback_client.get(reverse("bitbucket_search:index"))
    assert 'disabled aria-busy="true"' in _workspace_refresh_form(busy_response.content.decode())

    repository.sync_state = sync_state
    repository.save(update_fields={"sync_state"})
    job.status = job_status
    job.completed_at = timezone.now()
    job.save(update_fields={"status", "completed_at"})

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    status = loopback_client.get(reverse("bitbucket_search:repository_status"))

    assert response.context["active_repository_count"] == 0
    assert status.json()["repositories"][0]["active"] is False
    form = _workspace_refresh_form(html)
    assert "disabled" not in form
    assert 'data-active-repository-count="0"' in form
    assert "Repository work in progress" not in form
    assert "data-refresh-all-icon hidden" not in form
    assert "data-refresh-all-spinner hidden" in form


def _pdf_worker(repository, index, status):
    document = PDFDocument.objects.create(
        repository=repository,
        filename=f"Guide {index}.pdf",
        relative_path=f"docs/Guide {index}.pdf",
        git_blob_id=f"{index:040x}",
        file_size=100,
    )
    return PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id=document.git_blob_id,
        target_source_commit="b" * 40,
        target_relative_path=document.relative_path,
        target_file_size=document.file_size,
        target_extractor_version=PDF_EXTRACTOR_VERSION,
        status=status,
        started_at=timezone.now() - timedelta(minutes=2)
        if status == PDFExtractionJobStatus.RUNNING
        else None,
        heartbeat_at=timezone.now() if status == PDFExtractionJobStatus.RUNNING else None,
    )


@override_settings(PDF_MAX_EXTRACTION_WORKERS=6)
def test_repository_log_endpoint_includes_parallel_pdf_worker_activity(loopback_client):
    repository = BitbucketRepository.objects.create(
        display_name="worker-logs",
        canonical_remote_key="bitbucket.org/workspace/worker-logs",
        remote_url="ssh://git@bitbucket.org/workspace/worker-logs.git",
        sync_state=RepositorySyncState.READY,
    )
    running = _pdf_worker(repository, 1, PDFExtractionJobStatus.RUNNING)
    failed = _pdf_worker(repository, 2, PDFExtractionJobStatus.FAILED)
    failed.phase = PDFExtractionJobPhase.COMPLETED
    failed.progress = 73
    failed.error_code = "synthetic_failure"
    failed.error_summary = "The PDF parser stopped safely."
    failed.completed_at = timezone.now()
    failed.save(update_fields={"phase", "progress", "error_code", "error_summary", "completed_at"})
    append_operation_log_entry(
        repository_id=repository.pk,
        extraction_job_id=running.pk,
        channel=RepositoryOperationLogChannel.INDEXING,
        event="indexing_phase_changed",
        message="Extracting searchable PDF text.",
        phase=PDFExtractionJobPhase.EXTRACTING,
        progress=42,
        worker_pid=running.worker_pid,
    )

    response = loopback_client.get(
        reverse("bitbucket_search:repository_logs", args=(repository.pk,))
    )
    indexing = response.json()["indexing"]

    assert response.status_code == 200
    assert indexing["workerLimit"] == 6
    assert indexing["active"] is True
    assert indexing["counts"][PDFExtractionJobStatus.RUNNING] == 1
    assert indexing["counts"][PDFExtractionJobStatus.FAILED] == 1
    assert "Extracting searchable PDF text." in "\n".join(indexing["lines"])
    assert indexing["countsKind"] == "attempts"
    assert "entries" not in indexing
    assert "no-store" in response.headers["Cache-Control"]


def test_pdf_log_overview_attention_is_scoped_to_the_selected_repository(loopback_client):
    clean_repository = BitbucketRepository.objects.create(
        display_name="clean-selected-repository",
        canonical_remote_key="bitbucket.org/workspace/clean-selected-repository",
        remote_url="ssh://git@bitbucket.org/workspace/clean-selected-repository.git",
        sync_state=RepositorySyncState.READY,
    )
    failed_repository = BitbucketRepository.objects.create(
        display_name="failed-other-repository",
        canonical_remote_key="bitbucket.org/workspace/failed-other-repository",
        remote_url="ssh://git@bitbucket.org/workspace/failed-other-repository.git",
        sync_state=RepositorySyncState.READY,
    )
    _pdf_worker(clean_repository, 1, PDFExtractionJobStatus.SUCCEEDED)
    failed_job = _pdf_worker(failed_repository, 2, PDFExtractionJobStatus.FAILED)
    PDFDocument.objects.filter(pk=failed_job.document_id).update(index_state=PDFIndexState.FAILED)

    def pdf_card_classes(repository):
        html = loopback_client.get(
            reverse("bitbucket_search:index_status"), {"repository": repository.pk}
        ).content.decode()
        card = re.search(
            r'<article class="([^"]*status-card[^"]*)">\s*<div class="status-card__heading">'
            r"<span[^>]*>PDF</span><div><h2>Selected repository PDF indexing</h2>",
            html,
        )
        assert card is not None
        return card.group(1)

    assert "status-card--attention" not in pdf_card_classes(clean_repository)
    assert "status-card--attention" in pdf_card_classes(failed_repository)


@override_settings(PDF_MAX_EXTRACTION_WORKERS=4)
def test_pdf_log_overview_scopes_activity_against_the_global_worker_pool(loopback_client):
    selected_repository = BitbucketRepository.objects.create(
        display_name="selected-idle-repository",
        canonical_remote_key="bitbucket.org/workspace/selected-idle-repository",
        remote_url="ssh://git@bitbucket.org/workspace/selected-idle-repository.git",
        sync_state=RepositorySyncState.READY,
    )
    busy_repository = BitbucketRepository.objects.create(
        display_name="other-busy-repository",
        canonical_remote_key="bitbucket.org/workspace/other-busy-repository",
        remote_url="ssh://git@bitbucket.org/workspace/other-busy-repository.git",
        sync_state=RepositorySyncState.READY,
    )
    _pdf_worker(busy_repository, 1, PDFExtractionJobStatus.RUNNING)

    selected_html = loopback_client.get(
        reverse("bitbucket_search:index_status"), {"repository": selected_repository.pk}
    ).content.decode()
    busy_html = loopback_client.get(
        reverse("bitbucket_search:index_status"), {"repository": busy_repository.pk}
    ).content.decode()

    assert "Selected repository PDF indexing" in selected_html
    assert "0 active for this repository · global pool limit 4" in selected_html
    assert "1 active for this repository · global pool limit 4" in busy_html


def test_full_repository_logs_page_paginates_git_and_pdf_history(loopback_client, monkeypatch):
    monkeypatch.setattr(bitbucket_views, "_INDEX_LOG_PAGE_SIZE", 2)
    monkeypatch.setattr(bitbucket_views, "_INDEX_EVENT_PAGE_SIZE", 2)
    repository = BitbucketRepository.objects.create(
        display_name="five-thousand-pdfs",
        canonical_remote_key="bitbucket.org/workspace/five-thousand-pdfs",
        remote_url="ssh://git@bitbucket.org/workspace/five-thousand-pdfs.git",
        sync_state=RepositorySyncState.READY,
    )
    sync_job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.SUCCEEDED,
        phase=RepositorySyncPhase.COMPLETED,
        progress=100,
        status_message="Repository clone and catalogue publication completed.",
        completed_at=timezone.now(),
    )
    for message, channel, phase in (
        ("Receiving objects: 100%", RepositoryOperationLogChannel.GIT, "clone"),
        (
            "Published 5,000 PDF catalogue rows.",
            RepositoryOperationLogChannel.CATALOGUE,
            "catalogue",
        ),
    ):
        append_operation_log_entry(
            repository_id=repository.pk,
            sync_job_id=sync_job.pk,
            channel=channel,
            event="git_output"
            if channel == RepositoryOperationLogChannel.GIT
            else "catalogue_output",
            message=message,
            phase=phase,
        )
    extraction_jobs = [
        _pdf_worker(repository, index, PDFExtractionJobStatus.SUCCEEDED) for index in range(3)
    ]
    event_entries = []
    for index, extraction_job in enumerate(extraction_jobs):
        event_entries.append(
            append_operation_log_entry(
                repository_id=repository.pk,
                sync_job_id=sync_job.pk,
                extraction_job_id=extraction_job.pk,
                channel=RepositoryOperationLogChannel.INDEXING,
                event="indexing_completed",
                message=f"PDF indexing completed for durable attempt {index}.",
                phase=PDFExtractionJobPhase.COMPLETED,
                progress=100,
            )
        )

    first = loopback_client.get(
        reverse("bitbucket_search:index_status"),
        {"repository": repository.pk},
    )
    first_html = first.content.decode()

    assert first.status_code == 200
    assert "Repository logs" in first_html
    assert "Receiving objects: 100%" in first_html
    assert "Published 5,000 PDF catalogue rows." in first_html
    assert "PDF indexing log snapshot" in first_html
    assert "Refresh snapshot" in first_html
    assert "2 transitions shown" in first_html
    assert "Stable cursor · up to 2 transitions" in first_html
    assert "Stable cursor · up to 2 attempts" in first_html
    assert "docs/Guide 2.pdf" in first_html
    assert "docs/Guide 0.pdf" not in first_html
    assert f"index_job_before={extraction_jobs[-2].pk}" in first_html

    newer_job = _pdf_worker(repository, 3, PDFExtractionJobStatus.SUCCEEDED)
    append_operation_log_entry(
        repository_id=repository.pk,
        sync_job_id=sync_job.pk,
        extraction_job_id=extraction_jobs[2].pk,
        channel=RepositoryOperationLogChannel.INDEXING,
        event="indexing_completed",
        message="A newer transition arrived after the first snapshot.",
        phase=PDFExtractionJobPhase.COMPLETED,
        progress=100,
    )

    second = loopback_client.get(
        reverse("bitbucket_search:index_status"),
        {
            "repository": repository.pk,
            "index_job_before": extraction_jobs[-2].pk,
            "index_event_before": event_entries[-2].pk,
        },
    )
    second_html = second.content.decode()

    assert second.status_code == 200
    assert "docs/Guide 0.pdf" in second_html
    assert newer_job.target_relative_path not in second_html
    assert "PDF indexing completed for durable attempt 0." in second_html
    assert "A newer transition arrived after the first snapshot." not in second_html
    assert (
        f"index_event_before={event_entries[-2].pk}&amp;index_job_before={extraction_jobs[-2].pk}"
    ) in second_html
    assert (
        f"index_job_after={extraction_jobs[0].pk}&amp;index_event_before={event_entries[-2].pk}"
    ) in second_html


@pytest.mark.parametrize("view_name", ["bitbucket_search:index", "bitbucket_search:repositories"])
@pytest.mark.parametrize(("queued", "running"), [(3, 0), (2, 1), (0, 3)])
def test_git_ready_repository_shows_pdf_worker_phase_in_sidebar_and_refresh_tooltip(
    loopback_client, view_name, queued, running
):
    repository = BitbucketRepository.objects.create(
        display_name="design-notes",
        canonical_remote_key="bitbucket.org/workspace/design-notes",
        remote_url="ssh://git@bitbucket.org/workspace/design-notes.git",
        sync_state=RepositorySyncState.READY,
    )
    for index in range(queued + running):
        _pdf_worker(
            repository,
            index,
            PDFExtractionJobStatus.QUEUED if index < queued else PDFExtractionJobStatus.RUNNING,
        )
    idle = BitbucketRepository.objects.create(
        display_name="idle-notes",
        canonical_remote_key="bitbucket.org/workspace/idle-notes",
        remote_url="ssh://git@bitbucket.org/workspace/idle-notes.git",
        sync_state=RepositorySyncState.READY,
    )
    response = loopback_client.get(reverse(view_name))
    html = response.content.decode()
    payload = loopback_client.get(reverse("bitbucket_search:repository_status")).json()
    status = next(item for item in payload["repositories"] if item["id"] == repository.pk)
    activity = status["activity"]
    assert response.status_code == 200
    assert status["state"] == "ready"
    assert status["active"] is False
    assert status["hasActiveWork"] is True
    assert activity["active"] is True
    assert activity["phase"] == ("extracting" if running else "pdf_queued")
    assert activity["queuedPdfs"] == queued
    assert activity["runningPdfs"] == running
    assert response.context["active_repository_count"] == 0, "The Git job has already finished"
    cards = _repository_cards(html, repository.pk)
    assert len(cards) == 2, "Desktop and mobile show the same current worker evidence"
    for card in cards:
        icon = re.search(r"<span[^>]+data-repository-state-icon[^>]*>", card)
        assert icon is not None
        assert "bb-repository-state--working" in icon.group()
        assert "bb-repository-state--ready" not in icon.group()
        assert escape(activity["label"]) in icon.group()
        label = re.search(
            r"<small[^>]+data-repository-work-label[^>]*>(.*?)</small>", card, re.DOTALL
        )
        assert label is not None
        assert " hidden" not in label.group()
        assert label.group(1).strip() == escape(activity["detail"] or activity["label"])
        assert "data-repository-run-timer" not in card
        assert "data-repository-success-ticks" in card
    for card in _repository_cards(html, idle.pk):
        assert "bb-repository-state--ready" in card
        assert "bb-repository-state--working" not in card
        assert re.search(r"data-repository-work-label[^>]*\bhidden", card)
    form = _workspace_refresh_form(html)
    _assert_refresh_work_summary(form, payload["work"])

    # These are separate truthful snapshots, so their observation clocks may differ.
    # Compare the durable activity contract without pretending they were sampled together.
    def stable_work(work):
        return {
            **work,
            "activities": [
                {
                    key: value
                    for key, value in activity.items()
                    if key not in {"startedAt", "observedAt"}
                }
                for activity in work["activities"]
            ],
        }

    assert stable_work(response.context["repository_work_summary"]) == stable_work(payload["work"])
    assert repository.display_name in payload["work"]["detail"]
    assert idle.display_name not in payload["work"]["detail"]
    assert 'disabled aria-busy="true"' in form
    assert "data-refresh-all-icon hidden" not in form
    assert "data-refresh-all-spinner hidden" in form
    assert "data-refresh-all-visual" in form
    assert "data-refresh-all-running-visual" in form
    assert "data-overall-progress" in form


@pytest.mark.parametrize(
    "job_status",
    [
        PDFExtractionJobStatus.SUCCEEDED,
        PDFExtractionJobStatus.FAILED,
        PDFExtractionJobStatus.INTERRUPTED,
        PDFExtractionJobStatus.CANCELLED,
    ],
)
def test_terminal_pdf_jobs_do_not_leave_global_refresh_or_sidebar_busy(loopback_client, job_status):
    repository = BitbucketRepository.objects.create(
        display_name="finished-pdf-work",
        canonical_remote_key="bitbucket.org/workspace/finished-pdf-work",
        remote_url="ssh://git@bitbucket.org/workspace/finished-pdf-work.git",
        sync_state=RepositorySyncState.READY,
    )
    job = _pdf_worker(repository, 1, PDFExtractionJobStatus.RUNNING)
    active_response = loopback_client.get(reverse("bitbucket_search:index"))
    assert 'disabled aria-busy="true"' in _workspace_refresh_form(active_response.content.decode())
    job.status = job_status
    job.completed_at = timezone.now()
    job.save(update_fields={"status", "completed_at"})
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    payload = loopback_client.get(reverse("bitbucket_search:repository_status")).json()
    assert payload["work"]["active"] is False
    assert payload["repositories"][0]["hasActiveWork"] is False
    assert payload["repositories"][0]["activity"]["active"] is False
    assert payload["extraction"]["pendingDocuments"] == 1, "Pending text is not a running job"
    form = _workspace_refresh_form(html)
    assert "disabled" not in form
    assert 'aria-busy="true"' not in form
    assert "data-refresh-all-icon hidden" not in form
    assert "data-refresh-all-spinner hidden" in form
    for card in _repository_cards(html, repository.pk):
        assert "bb-repository-state--working" not in card
        assert re.search(r"data-repository-work-label[^>]*\bhidden", card)


def test_empty_pdf_timeline_keeps_search_available_and_explains_the_zero_state(
    loopback_client,
):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Showing <strong>0 PDFs</strong>" in html
    assert "No PDFs are in the local inventory yet" in html
    assert 'placeholder="Search extracted text, filenames or repo paths…"' in html
    assert "data-pdf-search-form" in html
    assert "data-pdf-search-input" in html
    assert "bb-search-submit" not in html
    assert 'name="page_size" value="100"' in html
    assert 'data-pdf-timeline data-next-page-url=""' in html
    assert "data-load-older-container" not in html
    assert "data-pdf-row" not in html


def test_responsive_pdf_rows_override_the_generic_block_row_rule():
    css_path = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.css"
    css = css_path.read_text(encoding="utf-8")
    responsive_start = css.index("@media (max-width: 940px)")
    responsive_end = css.index("@media (max-width: 620px)", responsive_start)
    responsive_css = css[responsive_start:responsive_end]

    assert "@media (max-width: 1280px)" in css[:responsive_start]
    assert ".bb-refresh-all--desktop {\n        display: none;" not in responsive_css
    assert ".bb-repository-selection-toolbar" in css
    assert ".bb-refresh-all__meta {\n    display: grid;" in css
    assert ".bb-overall-progress" in css
    assert ".bb-results-table tr," in responsive_css
    assert ".bb-results-table .bb-document-row {\n        display: grid;" in responsive_css
    assert ".bb-document-row td.bb-document-cell--actions {\n    overflow: visible;" in css
    assert ".bb-document-actions button[data-tooltip]:hover::after," in css
    assert ".bb-document-actions button[data-tooltip]:focus-visible::after" in css

    compact_start = responsive_end
    compact_end = css.index("@media (max-width: 390px)", compact_start)
    compact_css = css[compact_start:compact_end]
    mobile_css = css[compact_end : css.index("@media (prefers-reduced-motion", compact_end)]
    assert ".bb-results-table .bb-document-row {\n        grid-template-areas:" in compact_css
    assert ".bb-results-table .bb-document-row {\n        grid-template-areas:" in mobile_css


def test_background_work_styles_show_an_animated_overall_status_and_progress():
    css = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")
    working = re.search(
        r"\.bb-repository-state--working,\s*\.bb-repository-state--unknown\s*\{([^}]+)\}",
        css,
    )
    assert working is not None
    assert "color: var(--bb-amber);" in working.group(1)
    assert re.search(r"\.bb-repository-state--working::after\s*\{[^}]*animation:", css)
    assert re.search(
        r"\.bb-repository-state--unknown \.bb-state-icon--attention\s*\{\s*display: block;",
        css,
    )
    assert ".bb-refresh-all__visual" in css
    assert ".bb-refresh-all--active .bb-refresh-all__visual" in css
    assert ".bb-overall-progress progress" in css
    assert ".bb-repository-work-label[hidden] {\n    display: none;" in css


def test_pdf_inventory_does_not_auto_append_more_pages_on_scroll():
    javascript_path = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.js"
    javascript = javascript_path.read_text(encoding="utf-8")

    assert "appendTimelineGroups" not in javascript
    assert "IntersectionObserver" not in javascript
    assert "data-load-older" not in javascript


def test_pdf_timeline_renders_grouped_metadata_actions_and_page_navigation(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        sync_state=RepositorySyncState.READY,
        last_sync_successful_at=timezone.now(),
    )
    network_plan = PDFDocument.objects.create(
        repository=repository,
        filename="Network <Plan>.pdf",
        relative_path="docs/network/Network <Plan>.pdf",
        open_count=4,
        index_state=PDFIndexState.READY,
    )
    archived_plan = PDFDocument.objects.create(
        repository=repository,
        filename="Archive.pdf",
        relative_path="archive/Archive.pdf",
        open_count=0,
        index_state=PDFIndexState.FAILED,
    )
    context = bitbucket_views._index_context()
    next_page_url = f"{reverse('bitbucket_search:document_page')}?page=2"
    context.update(
        {
            "pdf_document_count": 2,
            "next_pdf_page_url": next_page_url,
            "pdf_timeline_groups": (
                {
                    "key": "today-2026-08-29",
                    "label": "Today",
                    "detail": "29 Aug 2026",
                    "rows": (
                        {
                            "document": network_plan,
                            "full_path": "/managed/networking/docs/Network <Plan>.pdf",
                            "display_path": "networking/docs/Network <Plan>.pdf",
                            "path_copy_available": True,
                            "project_label": "Architecture & Design",
                            "added_by_label": "A. Author",
                            "added_date_label": "Today",
                            "added_date_source_label": "Git addition",
                            "added_date_detail": "10:24 AM",
                            "history_label": "Git addition",
                        },
                    ),
                },
                {
                    "key": "year-2025",
                    "label": "2025",
                    "detail": "Older PDFs",
                    "rows": (
                        {
                            "document": archived_plan,
                            "full_path": "/managed/networking/archive/Archive.pdf",
                            "display_path": "networking/archive/Archive.pdf",
                            "path_copy_available": True,
                            "project_label": "",
                            "added_by_label": "",
                            "added_date_label": "14 Dec 2025",
                            "added_date_source_label": "First seen by OWL",
                            "added_date_detail": "",
                            "history_label": "Discovered by OWL",
                        },
                    ),
                },
            ),
        }
    )
    monkeypatch.setattr(bitbucket_views, "_index_context", lambda **_kwargs: context)

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "data-pdf-visible-start>1</b>–<b data-pdf-visible-end>2</b> of 2 PDFs" in html
    assert "1 indexed · 0 pending" in html
    assert 'data-timeline-group-key="today-2026-08-29"' in html
    assert 'data-timeline-group-key="year-2025"' in html
    assert html.index("<strong>Today</strong>") < html.index("<strong>2025</strong>")
    assert 'scope="rowgroup"' in html
    assert "Network &lt;Plan&gt;.pdf" in html
    assert "/managed/networking/docs/Network &lt;Plan&gt;.pdf" in html
    assert "Architecture &amp; Design" in html
    assert "A. Author" in html
    assert "Added by (Git author)" in html
    assert 'id="bb-pdf-column-status">Status</th>' in html
    assert html.count('aria-label="Git clone or pull complete"') == 2
    assert 'aria-label="PDF text indexed"' in html
    assert 'aria-label="PDF text indexing failed"' in html
    assert "Git addition" in html
    assert "First seen by OWL" in html
    assert "Discovered by OWL" not in html
    assert f'id="pdf-document-{network_plan.pk}"' in html
    network_row_start = html.index(f'data-document-id="{network_plan.pk}"')
    network_row = html[network_row_start : html.index("</tr>", network_row_start)]
    assert (
        f'action="{reverse("bitbucket_search:document_open", args=(network_plan.pk,))}"'
        in network_row
    )
    assert (
        f'action="{reverse("bitbucket_search:document_reveal", args=(network_plan.pk,))}"'
        in network_row
    )
    assert network_row.count('name="csrfmiddlewaretoken"') == 2
    assert network_row.count('name="return_page" value="1"') == 2
    assert 'type="submit" disabled' not in network_row
    assert "bb-index-health" not in network_row
    archived_row_start = html.index(f'data-document-id="{archived_plan.pk}"')
    archived_row = html[archived_row_start : html.index("</tr>", archived_row_start)]
    assert archived_row.count("Unavailable") == 2
    assert archived_row.count('method="post"') == 2
    assert "bb-index-health" not in archived_row
    assert html.count('data-tooltip="Open file"') == 2
    assert html.count('data-tooltip="Open folder"') == 2
    assert 'aria-label="Open file: Archive.pdf"' in archived_row
    assert 'aria-label="Open folder containing Archive.pdf"' in archived_row
    assert f'data-next-page-url="{next_page_url}"' in html
    assert "data-load-older" not in html
    assert "100 PDFs per page" in html
    assert "Page <strong data-pdf-current-page>1</strong> of 1" in html
    assert f'href="{next_page_url}" rel="next" data-pdf-next-page' in html


@override_settings(BITBUCKET_PDF_PAGE_SIZE=1)
def test_pdf_timeline_fragment_preserves_boundary_group_key(loopback_client):
    repository = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
        sync_state=RepositorySyncState.READY,
    )
    timeline_at = timezone.now()
    older_document = PDFDocument.objects.create(
        repository=repository,
        filename="Older.pdf",
        relative_path="docs/Older.pdf",
        timeline_at=timeline_at,
    )
    newer_document = PDFDocument.objects.create(
        repository=repository,
        filename="Newer.pdf",
        relative_path="docs/Newer.pdf",
        timeline_at=timeline_at,
    )

    first_page = loopback_client.get(reverse("bitbucket_search:index"))
    first_html = first_page.content.decode()
    next_page_url = f"{reverse('bitbucket_search:document_page')}?page=2"

    assert first_page.status_code == 200
    assert f'data-document-id="{newer_document.pk}"' in first_html
    assert 'data-timeline-group-key="repo-date-unavailable"' not in first_html
    assert "Git date unavailable" in first_html
    assert f'href="{next_page_url}" rel="next" data-pdf-next-page' in first_html

    second_page = loopback_client.get(
        next_page_url,
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    payload = second_page.json()

    assert second_page.status_code == 200
    assert payload["nextPageUrl"] == ""
    assert f'data-document-id="{older_document.pk}"' in payload["html"]
    assert 'data-timeline-group-key="repo-date-unavailable"' not in payload["html"]
    assert "Git date unavailable" in payload["html"]
    assert payload["html"].count('name="return_page" value="2"') == 2
    assert 'data-tooltip="Open file"' in payload["html"]
    assert 'data-tooltip="Open folder"' in payload["html"]

    fallback_page = loopback_client.get(next_page_url)
    fallback_html = fallback_page.content.decode()
    previous_page_url = f"{reverse('bitbucket_search:document_page')}?page=1"

    assert fallback_page.status_code == 200
    assert "data-pdf-visible-start>2</b>–<b data-pdf-visible-end>2</b> of 2 PDFs" in fallback_html
    assert "Page <strong data-pdf-current-page>2</strong> of 2" in fallback_html
    assert f'href="{previous_page_url}" rel="prev"' in fallback_html
    assert f'id="pdf-document-{older_document.pk}"' in fallback_html
    assert fallback_html.count('name="return_page" value="2"') == 2


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_repository_destination_expands_desktop_and_mobile_add_panels(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:repositories"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="bb-add-repository" open' in html
    assert '<details class="bb-mobile-repositories" open>' in html
    assert re.search(
        r'bb-repository-tools--mobile">\s*<details class="bb-add-repository-panel" open>',
        html,
    )


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_add_repository_returns_immediately_and_deduplicates_active_job_and_page_tick(
    loopback_client,
    monkeypatch,
):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    url = "git@bitbucket.org:workspace/architecture.git"

    first = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": url},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    second = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": "https://bitbucket.org/workspace/architecture.git"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    immediate_tick = loopback_client.post(
        reverse("bitbucket_search:repository_schedule_tick"),
    )

    assert first.status_code == 202
    assert first.json()["state"] == "queued"
    assert second.status_code == 202
    assert second.json()["state"] == "already_running"
    assert immediate_tick.status_code == 200
    assert immediate_tick.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    assert BitbucketRepository.objects.count() == 1
    assert RepositorySyncJob.objects.count() == 1
    repository = BitbucketRepository.objects.get()
    assert repository.sync_state == RepositorySyncState.QUEUED
    assert repository.remote_url == "ssh://git@bitbucket.org/workspace/architecture.git"
    launched.assert_called_once_with()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_add_repository_accepts_one_repository_url_per_line(loopback_client, monkeypatch):
    monkeypatch.setattr(
        "bitbucket_search.views.resident_repository_workers_active",
        Mock(return_value=True),
    )

    response = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "repository_url": (
                "git@bitbucket.org:workspace/architecture.git\n\n"
                "https://bitbucket.org/workspace/security.git\n"
                "https://bitbucket.org/workspace/architecture.git"
            )
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    assert response.json()["submitted"] == 2
    assert response.json()["added"] == 2
    assert response.json()["queued"] == 2
    assert BitbucketRepository.objects.count() == 2
    assert RepositorySyncJob.objects.count() == 2
    assert set(BitbucketRepository.objects.values_list("display_name", flat=True)) == {
        "architecture",
        "security",
    }


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_add_repository_rejects_invalid_batch_before_queuing_any_repository(loopback_client):
    response = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {
            "repository_url": (
                "git@bitbucket.org:workspace/architecture.git\n"
                "https://example.invalid/workspace/not-allowed.git"
            )
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "host_not_allowed"
    assert response.json()["detail"].startswith("Line 2:")
    assert not BitbucketRepository.objects.exists()
    assert not RepositorySyncJob.objects.exists()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org", "scm.example.invalid"))
def test_add_internal_bitbucket_repository_preserves_context_path_and_queues_clone(
    loopback_client,
    monkeypatch,
):
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    url = "https://scm.example.invalid/stash/scm/adr/example-repo.git"

    response = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": url},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    repository = BitbucketRepository.objects.get()
    assert repository.remote_url == url
    assert repository.canonical_remote_key == "scm.example.invalid/stash/scm/adr/example-repo"
    assert repository.display_name == "example-repo"
    assert repository.sync_state == RepositorySyncState.QUEUED
    job = RepositorySyncJob.objects.get()
    assert job.repository_id == repository.pk
    assert job.operation == RepositorySyncOperation.CLONE
    assert job.status == RepositorySyncJobStatus.QUEUED
    launched.assert_called_once_with()


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_manual_repository_wakeup_is_left_to_the_run_owl_resident_pool(
    loopback_client,
    monkeypatch,
):
    launched = Mock()
    monkeypatch.setattr(
        "bitbucket_search.views.resident_repository_workers_active",
        Mock(return_value=True),
    )
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)

    response = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": "git@bitbucket.org:workspace/resident.git"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    immediate_tick = loopback_client.post(
        reverse("bitbucket_search:repository_schedule_tick"),
    )

    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    assert immediate_tick.status_code == 200
    assert immediate_tick.json()["state"] == "waiting"
    assert RepositorySyncJob.objects.filter(status=RepositorySyncJobStatus.QUEUED).count() == 1
    launched.assert_not_called()


def test_queued_repository_job_does_not_expire_while_waiting_for_a_worker():
    repository = BitbucketRepository.objects.create(
        display_name="queued-documents",
        canonical_remote_key="bitbucket.org/workspace/queued-documents",
        remote_url="ssh://git@bitbucket.org/workspace/queued-documents.git",
        sync_state=RepositorySyncState.QUEUED,
        status_message="Waiting for the background worker…",
    )
    job = RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    RepositorySyncJob.objects.filter(pk=job.pk).update(
        requested_at=timezone.now() - timedelta(days=1)
    )

    repository_status_snapshot()

    job.refresh_from_db()
    repository.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.QUEUED
    assert job.completed_at is None
    assert repository.sync_state == RepositorySyncState.QUEUED


def test_fresh_heartbeat_wins_stale_candidate_race_without_overwriting_repository(
    monkeypatch,
):
    observed_at = timezone.now()
    stale_heartbeat = observed_at - repository_sync.STALE_JOB_AFTER - timedelta(seconds=1)
    stale_repository = BitbucketRepository.objects.create(
        display_name="stopped-worker",
        canonical_remote_key="bitbucket.org/workspace/stopped-worker",
        remote_url="ssh://git@bitbucket.org/workspace/stopped-worker.git",
        sync_state=RepositorySyncState.CLONING,
        sync_progress=20,
        status_message="Old clone heartbeat",
    )
    healthy_repository = BitbucketRepository.objects.create(
        display_name="healthy-worker",
        canonical_remote_key="bitbucket.org/workspace/healthy-worker",
        remote_url="ssh://git@bitbucket.org/workspace/healthy-worker.git",
        sync_state=RepositorySyncState.FETCHING,
        sync_progress=70,
        status_message="Refresh is still running",
    )
    stale_job = RepositorySyncJob.objects.create(
        repository=stale_repository,
        operation=RepositorySyncOperation.CLONE,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=stale_heartbeat,
        heartbeat_at=stale_heartbeat,
    )
    healthy_job = RepositorySyncJob.objects.create(
        repository=healthy_repository,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=stale_heartbeat,
        heartbeat_at=stale_heartbeat,
    )

    def candidates_then_worker_heartbeats(_cutoff):
        RepositorySyncJob.objects.filter(pk=healthy_job.pk).update(heartbeat_at=observed_at)
        return stale_job.pk, healthy_job.pk

    monkeypatch.setattr(
        repository_sync,
        "_stale_running_job_ids",
        candidates_then_worker_heartbeats,
    )

    repository_sync._interrupt_stale_jobs(at=observed_at)

    stale_job.refresh_from_db()
    healthy_job.refresh_from_db()
    stale_repository.refresh_from_db()
    healthy_repository.refresh_from_db()
    assert stale_job.status == RepositorySyncJobStatus.INTERRUPTED
    assert stale_repository.sync_state == RepositorySyncState.INTERRUPTED
    assert healthy_job.status == RepositorySyncJobStatus.RUNNING
    assert healthy_job.heartbeat_at == observed_at
    assert healthy_repository.sync_state == RepositorySyncState.FETCHING
    assert healthy_repository.sync_progress == 70
    assert healthy_repository.status_message == "Refresh is still running"


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_credential_bearing_url_is_rejected_without_redisplay_or_persistence(loopback_client):
    secret = "not-a-real-secret"
    response = loopback_client.post(
        reverse("bitbucket_search:repository_add"),
        {"repository_url": f"https://user:{secret}@bitbucket.org/workspace/repository.git"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json()["code"] == "credential_bearing_repository_url"
    assert secret not in response.content.decode()
    assert not BitbucketRepository.objects.exists()


def test_repository_status_is_compact_and_never_returns_the_remote_url(loopback_client):
    repository = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
        local_path="/private/synthetic/path",
        sync_state=RepositorySyncState.READY,
        sync_progress=100,
        status_message="Repository is ready.",
        pdf_count=8,
        vsdx_count=2,
        document_bytes=4096,
    )

    response = loopback_client.get(reverse("bitbucket_search:repository_status"))
    payload = response.json()

    assert response.status_code == 200
    assert payload["repositories"][0]["id"] == repository.pk
    assert payload["repositories"][0]["state"] == "ready"
    assert payload["repositories"][0]["automatic"]["state"] == "due"
    assert payload["repositories"][0]["automatic"]["maxRetries"] == 3
    assert payload["automation"]["enabled"] is True
    assert payload["automation"]["state"] == "due"
    assert len(payload["catalog"]["publicationSignature"]) == 64
    assert payload["totals"] == {
        "repositories": 1,
        "pdfs": 8,
        "vsdx": 2,
        "documents": 10,
        "bytes": 4096,
        "bytesLabel": "4.0 KB",
    }
    serialized = response.content.decode()
    assert "ssh://" not in serialized
    assert "/private/synthetic/path" not in serialized


def test_repository_poller_tracks_daily_idle_and_catalog_publication_contract():
    javascript_path = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.js"
    javascript = javascript_path.read_text(encoding="utf-8")

    assert "const activePollDelay = 1500;" in javascript
    assert "const idlePollDelay = 30000;" in javascript
    assert "workspace.dataset.dailyRefreshEnabled" in javascript
    assert "workspace.dataset.catalogPublicationSignature" in javascript
    assert "catalogPublicationChanged" in javascript
    assert "`${repository.name}: ${visibleLabel}`" in javascript
    assert "`${repository.pdfCount} PDF · ${repository.vsdxCount} VSDX`" in javascript
    assert (
        "Boolean(repository.activity?.active || repository.hasActiveWork || repository.active)"
        in javascript
    )
    assert "card.dataset.repositoryActiveWork = String(working);" in javascript
    assert (
        "card.dataset.repositoryRemovalPending = String(Boolean(repository.hasRemovalPending));"
        in javascript
    )
    assert "updateSelectedRepositoryActions" in javascript
    assert "card.dataset.repositorySearchValue || card.textContent" in javascript
    assert "repository.automatic || {}" not in javascript
    assert "data-repository-catalog-status" not in javascript
    assert 'changed.closest("[data-people-filter-form]")' in javascript
    assert "form.requestSubmit();" in javascript
    assert 'panel.querySelector("[data-people-filter-search]")' in javascript
    assert 'search?.addEventListener("input", applyPeopleSearch);' in javascript
    assert 'search.value = "";' in javascript
    assert "data-people-search-toggle" not in javascript
    assert "closePeopleSearch" not in javascript
    assert '.normalize("NFKD")' in javascript
    assert "Apply people" not in javascript


def test_ready_repository_renders_green_tick_counts_and_shared_action_selection(loopback_client):
    repository = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        local_path="/private/synthetic/path",
        sync_state=RepositorySyncState.READY,
        sync_progress=100,
        status_message="Repository is ready.",
        pdf_count=12,
        vsdx_count=3,
    )

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    cards = _repository_cards(html, repository.pk)

    assert response.status_code == 200
    assert len(cards) == 2
    for card in cards:
        assert 'data-repository-state="ready"' in card
        assert "bb-repository-state--ready" in card
        assert 'role="img" aria-label="networking: Ready"' in card
        assert '<strong data-repository-name title="networking">networking</strong>' in card
        assert "<small data-repository-documents>12 PDF · 3 VSDX</small>" in card
        assert 'name="repository_ids"' in card
        assert f'value="{repository.pk}" form="bb-repository-selection-form"' in card
        assert 'aria-label="Select networking"' in card
        assert "data-repository-select" in card
        assert "data-repository-refresh-form" not in card
        assert "data-repository-menu" not in card
        assert "data-repository-run-timer" not in card
        assert "data-repository-success-ticks" in card
        assert "Repository is ready." not in card
        assert "Daily refresh due" not in card
        assert "data-repository-state-label" not in card
        assert "data-repository-message" not in card
        assert "data-repository-automatic" not in card
        assert "data-repository-catalog-status" not in card
        assert "No PDF catalogue published yet" not in card
    assert "<dd data-total-repositories>1</dd>" in html


def test_failed_daily_refresh_uses_compact_failure_card_and_keeps_status_detail(
    loopback_client,
):
    failed_at = timezone.now() - timedelta(hours=1)
    repository = BitbucketRepository.objects.create(
        display_name="retrying",
        canonical_remote_key="bitbucket.org/workspace/retrying",
        remote_url="ssh://git@bitbucket.org/workspace/retrying.git",
        sync_state=RepositorySyncState.FAILED,
        status_message="The remote was temporarily unavailable.",
        pdf_count=5,
        last_sync_completed_at=failed_at,
        last_sync_successful_at=failed_at - timedelta(days=1),
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
        trigger=RepositorySyncTrigger.DAILY,
        scheduled_day=timezone.localdate(),
        status=RepositorySyncJobStatus.FAILED,
        completed_at=failed_at,
        error_code="temporary_git_failure",
        error_summary="The remote was temporarily unavailable.",
    )

    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()
    cards = _repository_cards(html, repository.pk)

    assert response.status_code == 200
    assert len(cards) == 2
    for card in cards:
        assert 'data-repository-state="failed"' in card
        assert "bb-repository-state--git-failed" in card
        assert 'role="img" aria-label="retrying: Git connection or pull failed"' in card
        assert '<strong data-repository-name title="retrying">retrying</strong>' in card
        assert "<small data-repository-documents>5 PDF · 0 VSDX</small>" in card
        assert 'name="repository_ids"' in card
        assert 'aria-label="Select retrying"' in card
        assert "data-repository-refresh-form" not in card
        assert "The remote was temporarily unavailable." not in card
        assert "Retry scheduled" not in card
        assert "Automatic retry 1 of 3" not in card
        assert "Catalogue retained from" not in card
        assert "data-repository-automatic" not in card
        assert "data-repository-catalog-status" not in card

    status_response = loopback_client.get(reverse("bitbucket_search:index_status"))
    status_html = status_response.content.decode()

    assert status_response.status_code == 200
    assert "The remote was temporarily unavailable." in status_html
    assert "Retained from" in status_html


def test_repository_schedule_tick_is_post_only_csrf_protected_and_loopback_only(
    loopback_client,
):
    path = reverse("bitbucket_search:repository_schedule_tick")

    assert loopback_client.get(path).status_code == 405

    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert csrf_client.post(path).status_code == 403

    page = csrf_client.get(reverse("bitbucket_search:index"))
    token = page.cookies["csrftoken"].value
    accepted = csrf_client.post(
        path,
        {"csrfmiddlewaretoken": token},
        HTTP_ORIGIN="http://127.0.0.1",
    )
    assert accepted.status_code == 200
    assert accepted.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.20")
    assert remote_client.post(path).status_code == 403


def test_repository_schedule_tick_uses_hidden_form_navigation_for_opaque_origins():
    javascript_path = Path(__file__).parents[1] / "static" / "owl" / "owl.js"
    javascript = javascript_path.read_text(encoding="utf-8")

    assert '"[data-bitbucket-schedule-tick-form]"' in javascript
    assert "bitbucketScheduleTickForm.requestSubmit();" in javascript
    assert "bitbucketScheduleTickForm.submit();" in javascript
    assert "center.dataset.bitbucketScheduleTickUrl" not in javascript


def test_repository_schedule_tick_returns_busy_when_daily_queue_hits_sqlite_lock(
    loopback_client,
    monkeypatch,
):
    queue = Mock(side_effect=OperationalError("database is locked"))
    wake = Mock()
    monkeypatch.setattr("bitbucket_search.views.queue_due_daily_repository_refreshes", queue)
    monkeypatch.setattr("bitbucket_search.views._wake_queued_repository_workers", wake)

    response = loopback_client.post(reverse("bitbucket_search:repository_schedule_tick"))

    assert response.status_code == 202
    assert response.json() == {
        "state": "busy",
        "queued": 0,
        "workersStarted": 0,
    }
    queue.assert_called_once_with()
    wake.assert_not_called()


def test_repository_schedule_tick_returns_busy_when_wakeup_reservation_hits_sqlite_lock(
    loopback_client,
    monkeypatch,
):
    queued = (Mock(),)
    queue = Mock(return_value=queued)
    wake = Mock(side_effect=OperationalError("database is locked"))
    monkeypatch.setattr("bitbucket_search.views.queue_due_daily_repository_refreshes", queue)
    monkeypatch.setattr("bitbucket_search.views._wake_queued_repository_workers", wake)

    response = loopback_client.post(reverse("bitbucket_search:repository_schedule_tick"))

    assert response.status_code == 202
    assert response.json() == {
        "state": "busy",
        "queued": 1,
        "workersStarted": 0,
    }
    queue.assert_called_once_with()
    wake.assert_called_once_with()


def test_repository_schedule_tick_queues_due_work_wakes_worker_and_is_idempotent(
    loopback_client,
    monkeypatch,
    settings,
):
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = True
    settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR = 11
    settings.BITBUCKET_MAX_REPO_WORKERS = 2
    observed_at = datetime(2026, 8, 30, 11, tzinfo=UTC)
    repository = BitbucketRepository.objects.create(
        display_name="daily-tick",
        canonical_remote_key="bitbucket.org/workspace/daily-tick",
        remote_url="ssh://git@bitbucket.org/workspace/daily-tick.git",
    )
    launched = Mock()
    monkeypatch.setattr(repository_sync.timezone, "now", Mock(return_value=observed_at))
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repository_schedule_tick")

    with timezone.override("UTC"):
        response = loopback_client.post(path)
        repeated = loopback_client.post(path)

    assert response.status_code == 202
    assert response.json() == {
        "state": "queued",
        "queued": 1,
        "workersStarted": 1,
    }
    assert repeated.status_code == 200
    assert repeated.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    assert launched.call_count == 1
    job = RepositorySyncJob.objects.get(repository=repository)
    repository.refresh_from_db()
    assert job.operation == RepositorySyncOperation.CLONE
    assert job.trigger == RepositorySyncTrigger.DAILY
    assert job.scheduled_day == observed_at.date()
    assert job.automatic_retry_number == 0
    assert job.status == RepositorySyncJobStatus.QUEUED
    assert repository.sync_state == RepositorySyncState.QUEUED


def test_repository_schedule_tick_reports_preexisting_worker_wakeup_once(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="preexisting-queue",
        canonical_remote_key="bitbucket.org/workspace/preexisting-queue",
        remote_url="ssh://git@bitbucket.org/workspace/preexisting-queue.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repository_schedule_tick")

    response = loopback_client.post(path)
    repeated = loopback_client.post(path)

    assert response.status_code == 202
    assert response.json() == {
        "state": "worker_started",
        "queued": 0,
        "workersStarted": 1,
    }
    assert repeated.status_code == 200
    assert repeated.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    launched.assert_called_once_with()


def test_repository_schedule_tick_retries_a_stale_unclaimed_worker_wakeup(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="stale-worker-wakeup",
        canonical_remote_key="bitbucket.org/workspace/stale-worker-wakeup",
        remote_url="ssh://git@bitbucket.org/workspace/stale-worker-wakeup.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    observed_at = datetime(2026, 8, 30, 11, tzinfo=UTC)
    clock = Mock(return_value=observed_at)
    launched = Mock()
    monkeypatch.setattr(repository_sync.timezone, "now", clock)
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repository_schedule_tick")

    with timezone.override("UTC"):
        first = loopback_client.post(path)
        clock.return_value = observed_at + timedelta(seconds=29)
        still_reserved = loopback_client.post(path)
        clock.return_value = observed_at + timedelta(seconds=31)
        retried = loopback_client.post(path)

    assert first.json()["state"] == "worker_started"
    assert still_reserved.json()["state"] == "waiting"
    assert retried.json()["state"] == "worker_started"
    assert launched.call_count == 2


def test_repository_schedule_tick_leaves_work_for_the_run_owl_resident_pool(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="resident-pool",
        canonical_remote_key="bitbucket.org/workspace/resident-pool",
        remote_url="ssh://git@bitbucket.org/workspace/resident-pool.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.CLONE,
    )
    launched = Mock()
    monkeypatch.setattr(
        "bitbucket_search.views.resident_repository_workers_active",
        Mock(return_value=True),
    )
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)

    response = loopback_client.post(reverse("bitbucket_search:repository_schedule_tick"))

    assert response.status_code == 200
    assert response.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    launched.assert_not_called()


def test_repository_schedule_tick_keeps_durable_job_when_worker_wakeup_fails(
    loopback_client,
    monkeypatch,
    settings,
):
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = True
    settings.BITBUCKET_DAILY_REFRESH_LOCAL_HOUR = 11
    observed_at = datetime(2026, 8, 30, 11, tzinfo=UTC)
    repository = BitbucketRepository.objects.create(
        display_name="durable-daily-tick",
        canonical_remote_key="bitbucket.org/workspace/durable-daily-tick",
        remote_url="ssh://git@bitbucket.org/workspace/durable-daily-tick.git",
    )
    launched = Mock(side_effect=OSError("synthetic launch failure"))
    monkeypatch.setattr(repository_sync.timezone, "now", Mock(return_value=observed_at))
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repository_schedule_tick")

    with timezone.override("UTC"):
        response = loopback_client.post(path)
        repeated = loopback_client.post(path)

    assert response.status_code == 202
    assert response.json() == {
        "state": "worker_wakeup_failed",
        "queued": 1,
        "workersStarted": 0,
    }
    assert repeated.status_code == 202
    assert repeated.json() == {
        "state": "worker_wakeup_failed",
        "queued": 0,
        "workersStarted": 0,
    }
    assert launched.call_count == 2
    assert RepositorySyncJob.objects.filter(repository=repository).count() == 1
    job = RepositorySyncJob.objects.get(repository=repository)
    repository.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.QUEUED
    assert job.error_code == ""
    assert repository.sync_state == RepositorySyncState.QUEUED


def test_refresh_is_post_only_deduplicated_and_loopback_only(loopback_client, monkeypatch):
    repository = BitbucketRepository.objects.create(
        display_name="security",
        canonical_remote_key="bitbucket.org/workspace/security",
        remote_url="ssh://git@bitbucket.org/workspace/security.git",
        local_path="/private/synthetic/path",
        sync_state=RepositorySyncState.READY,
    )
    RepositorySyncJob.objects.create(
        repository=repository,
        operation=RepositorySyncOperation.REFRESH,
    )
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repository_refresh", args=(repository.pk,))

    assert loopback_client.get(path).status_code == 405
    response = loopback_client.post(path, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    immediate_tick = loopback_client.post(
        reverse("bitbucket_search:repository_schedule_tick"),
    )
    assert response.status_code == 202
    assert response.json()["state"] == "already_running"
    assert immediate_tick.status_code == 200
    assert immediate_tick.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    launched.assert_called_once_with()

    RepositorySyncJob.objects.update(
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )
    launched.reset_mock()

    running_response = loopback_client.post(path, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    assert running_response.status_code == 202
    assert running_response.json()["state"] == "already_running"
    launched.assert_not_called()

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.20")
    assert remote_client.post(path).status_code == 403


def test_refresh_all_queues_every_enabled_repository_and_starts_bounded_workers(
    loopback_client,
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 2
    ready = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
        sync_state=RepositorySyncState.READY,
    )
    second_ready = BitbucketRepository.objects.create(
        display_name="standards",
        canonical_remote_key="bitbucket.org/workspace/standards",
        remote_url="ssh://git@bitbucket.org/workspace/standards.git",
        sync_state=RepositorySyncState.READY,
    )
    third_ready = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        sync_state=RepositorySyncState.READY,
    )
    fourth_ready = BitbucketRepository.objects.create(
        display_name="payments",
        canonical_remote_key="bitbucket.org/workspace/payments",
        remote_url="ssh://git@bitbucket.org/workspace/payments.git",
        sync_state=RepositorySyncState.READY,
    )
    disabled = BitbucketRepository.objects.create(
        display_name="archived",
        canonical_remote_key="bitbucket.org/workspace/archived",
        remote_url="ssh://git@bitbucket.org/workspace/archived.git",
        sync_state=RepositorySyncState.READY,
        enabled=False,
    )
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    path = reverse("bitbucket_search:repositories_refresh_all")

    assert loopback_client.get(path).status_code == 405
    response = loopback_client.post(path, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    immediate_tick = loopback_client.post(
        reverse("bitbucket_search:repository_schedule_tick"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "state": "queued",
        "detail": "Queued 4 repositories for background Git refresh.",
        "eligible": 4,
        "queued": 4,
        "alreadyActive": 0,
        "alreadyQueued": 0,
        "alreadyRunning": 0,
        "workersStarted": 2,
    }
    assert immediate_tick.status_code == 200
    assert immediate_tick.json() == {
        "state": "waiting",
        "queued": 0,
        "workersStarted": 0,
    }
    assert launched.call_count == 2
    assert RepositorySyncJob.objects.filter(repository=ready).count() == 1
    assert RepositorySyncJob.objects.filter(repository=second_ready).count() == 1
    assert RepositorySyncJob.objects.filter(repository=third_ready).count() == 1
    assert RepositorySyncJob.objects.filter(repository=fourth_ready).count() == 1
    assert not RepositorySyncJob.objects.filter(repository=disabled).exists()

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.20")
    assert remote_client.post(path).status_code == 403


@pytest.mark.parametrize(
    ("sync_state", "job_status", "enabled"),
    [
        (RepositorySyncState.QUEUED, None, True),
        (RepositorySyncState.CLONING, None, True),
        (RepositorySyncState.FETCHING, None, True),
        (RepositorySyncState.UPDATING, None, True),
        (RepositorySyncState.READY, RepositorySyncJobStatus.QUEUED, True),
        (RepositorySyncState.READY, RepositorySyncJobStatus.RUNNING, True),
        (RepositorySyncState.CLONING, RepositorySyncJobStatus.RUNNING, False),
        (RepositorySyncState.READY, RepositorySyncJobStatus.QUEUED, False),
    ],
)
def test_refresh_all_rejects_mixed_busy_repositories_without_queueing_or_waking_workers(
    loopback_client,
    monkeypatch,
    sync_state,
    job_status,
    enabled,
):
    idle = BitbucketRepository.objects.create(
        display_name="idle",
        canonical_remote_key="bitbucket.org/workspace/idle",
        remote_url="ssh://git@bitbucket.org/workspace/idle.git",
        sync_state=RepositorySyncState.READY,
    )
    busy = BitbucketRepository.objects.create(
        display_name="busy",
        canonical_remote_key="bitbucket.org/workspace/busy",
        remote_url="ssh://git@bitbucket.org/workspace/busy.git",
        sync_state=sync_state,
        enabled=enabled,
    )
    if job_status:
        RepositorySyncJob.objects.create(
            repository=busy,
            operation=RepositorySyncOperation.CLONE,
            status=job_status,
            started_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
            heartbeat_at=timezone.now() if job_status == RepositorySyncJobStatus.RUNNING else None,
        )
    original_job_count = RepositorySyncJob.objects.count()
    wake_workers = Mock()
    monkeypatch.setattr("bitbucket_search.views._wake_queued_repository_workers", wake_workers)
    path = reverse("bitbucket_search:repositories_refresh_all")

    async_response = loopback_client.post(path, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    form_response = loopback_client.post(path)

    assert async_response.status_code == 409
    assert async_response.json() == {
        "state": "busy",
        "detail": "Wait for repository additions and refreshes to finish before refreshing all.",
        "queued": 0,
        "workersStarted": 0,
    }
    assert form_response.status_code == 302
    assert form_response.url == reverse("bitbucket_search:index")
    assert [str(message) for message in form_response.wsgi_request._messages] == [
        "Wait for repository additions and refreshes to finish before refreshing all."
    ]
    assert RepositorySyncJob.objects.count() == original_job_count
    assert not RepositorySyncJob.objects.filter(repository=idle).exists()
    idle.refresh_from_db()
    assert idle.sync_state == RepositorySyncState.READY
    wake_workers.assert_not_called()


def test_refresh_all_keeps_durable_jobs_queued_when_no_helper_can_start(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="security",
        canonical_remote_key="bitbucket.org/workspace/security",
        remote_url="ssh://git@bitbucket.org/workspace/security.git",
        sync_state=RepositorySyncState.READY,
    )
    monkeypatch.setattr(
        "bitbucket_search.views.launch_sync_worker",
        Mock(side_effect=OSError("synthetic launch failure")),
    )

    response = loopback_client.post(
        reverse("bitbucket_search:repositories_refresh_all"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    assert response.json()["state"] == "queued_worker_wakeup_failed"
    assert response.json()["workersStarted"] == 0
    job = RepositorySyncJob.objects.get(repository=repository)
    repository.refresh_from_db()
    assert job.status == RepositorySyncJobStatus.QUEUED
    assert job.error_code == ""
    assert repository.sync_state == RepositorySyncState.QUEUED


def test_refresh_all_reports_partial_helper_launch_without_losing_jobs(
    loopback_client,
    monkeypatch,
    settings,
):
    settings.BITBUCKET_MAX_REPO_WORKERS = 3
    repositories = [
        BitbucketRepository.objects.create(
            display_name=f"repository-{number}",
            canonical_remote_key=f"bitbucket.org/workspace/repository-{number}",
            remote_url=f"ssh://git@bitbucket.org/workspace/repository-{number}.git",
            sync_state=RepositorySyncState.READY,
        )
        for number in range(3)
    ]
    launched = Mock(side_effect=[Mock(), OSError("synthetic launch failure")])
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)

    response = loopback_client.post(
        reverse("bitbucket_search:repositories_refresh_all"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == "queued_worker_wakeup_failed"
    assert payload["queued"] == 3
    assert payload["workersStarted"] == 1
    assert launched.call_count == 2
    assert (
        RepositorySyncJob.objects.filter(
            repository__in=repositories,
            status=RepositorySyncJobStatus.QUEUED,
        ).count()
        == 3
    )


def test_refresh_all_reports_when_no_enabled_repositories_exist(loopback_client):
    BitbucketRepository.objects.create(
        display_name="archived",
        canonical_remote_key="bitbucket.org/workspace/archived",
        remote_url="ssh://git@bitbucket.org/workspace/archived.git",
        sync_state=RepositorySyncState.READY,
        enabled=False,
    )

    response = loopback_client.post(
        reverse("bitbucket_search:repositories_refresh_all"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "empty",
        "detail": "No repositories are included in refresh.",
        "eligible": 0,
        "queued": 0,
        "alreadyActive": 0,
        "alreadyQueued": 0,
        "alreadyRunning": 0,
        "workersStarted": 0,
    }


def test_refresh_all_requires_csrf_even_for_loopback_requests():
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )

    response = csrf_client.post(reverse("bitbucket_search:repositories_refresh_all"))

    assert response.status_code == 403


def test_refresh_all_accepts_a_valid_same_origin_csrf_form(monkeypatch):
    repository = BitbucketRepository.objects.create(
        display_name="same-origin",
        canonical_remote_key="bitbucket.org/workspace/same-origin",
        remote_url="ssh://git@bitbucket.org/workspace/same-origin.git",
        sync_state=RepositorySyncState.READY,
    )
    launched = Mock()
    monkeypatch.setattr("bitbucket_search.views.launch_sync_worker", launched)
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = csrf_client.get(reverse("bitbucket_search:index"))
    token = page.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("bitbucket_search:repositories_refresh_all"),
        {"csrfmiddlewaretoken": token},
        HTTP_ORIGIN="http://127.0.0.1",
    )

    assert response.status_code == 302
    assert response.url == reverse("bitbucket_search:index")
    assert RepositorySyncJob.objects.filter(
        repository=repository,
        status=RepositorySyncJobStatus.QUEUED,
    ).exists()
    launched.assert_called_once_with()
