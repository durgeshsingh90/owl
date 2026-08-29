from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views as bitbucket_views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
    RepositorySyncTrigger,
)
from bitbucket_search.services import repository_sync
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
    assert ">Add Repository<" in html
    assert 'action="/pdfs/repositories/add/"' in html
    assert "Add &amp; clone in background" in html
    assert "Allowed host:" in html
    assert "bitbucket.org" in html
    assert 'placeholder="Search repositories…" disabled' in html
    assert 'data-repository-status-url="/pdfs/repositories/status/"' in html
    assert 'data-daily-refresh-enabled="true"' in html
    assert 'data-catalog-publication-signature="' in html
    assert "bitbucket_search/bitbucket_search.js?v=people-groups-v1" in html


def _workspace_refresh_form(html: str, *, mobile: bool = False) -> str:
    class_name = "bb-mobile-refresh-all" if mobile else "bb-refresh-all"
    match = re.search(
        rf'<form\s+class="{class_name}[^\"]*".*?</form>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_refresh_all_controls_are_accessible_and_truthful_when_unavailable(loopback_client):
    empty_response = loopback_client.get(reverse("bitbucket_search:index"))
    empty_html = empty_response.content.decode()
    desktop_empty = _workspace_refresh_form(empty_html)
    mobile_empty = _workspace_refresh_form(empty_html, mobile=True)

    assert empty_response.status_code == 200
    assert empty_html.count('action="/pdfs/repositories/refresh/"') == 2
    assert "bb-refresh-all--disabled" in desktop_empty
    assert "bb-mobile-refresh-all--disabled" in mobile_empty
    assert "Refresh all repositories unavailable: no repositories are connected" in desktop_empty
    assert "Refresh all repositories unavailable: no repositories are connected" in mobile_empty
    assert "disabled" in desktop_empty
    assert "disabled" in mobile_empty
    assert empty_html.count("No repositories connected yet") >= 1

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
    assert disabled_html.count("No enabled repositories to queue") == 2
    assert "no repositories are enabled" in _workspace_refresh_form(disabled_html)
    assert "no repositories are enabled" in _workspace_refresh_form(
        disabled_html,
        mobile=True,
    )

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
    assert 'data-active-repository-count="0"' in mixed_html
    assert "bb-refresh-all--active" not in _workspace_refresh_form(mixed_html)


def test_refresh_all_controls_queue_idle_repositories_and_report_active_work(loopback_client):
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
    assert "Queue a background refresh for all 1 enabled repository" in idle_html
    assert "disabled" not in _workspace_refresh_form(idle_html)
    assert "disabled" not in _workspace_refresh_form(idle_html, mobile=True)

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
    partial_mobile = _workspace_refresh_form(partial_html, mobile=True)

    assert partial_response.context["enabled_repository_count"] == 2
    assert partial_response.context["active_repository_count"] == 1
    assert "bb-refresh-all--partial" in partial_desktop
    assert "bb-mobile-refresh-all--partial" in partial_mobile
    assert partial_html.count("Refresh remaining repositories") == 2
    assert "1 already queued or syncing" in partial_desktop
    assert "disabled" not in partial_desktop
    assert "disabled" not in partial_mobile

    idle.sync_state = RepositorySyncState.QUEUED
    idle.save(update_fields={"sync_state"})
    RepositorySyncJob.objects.create(
        repository=idle,
        operation=RepositorySyncOperation.REFRESH,
    )
    active_response = loopback_client.get(reverse("bitbucket_search:index"))
    active_html = active_response.content.decode()
    active_desktop = _workspace_refresh_form(active_html)
    active_mobile = _workspace_refresh_form(active_html, mobile=True)

    assert active_response.context["active_repository_count"] == 2
    assert "bb-refresh-all--active" in active_desktop
    assert "bb-mobile-refresh-all--active" in active_mobile
    assert active_html.count("All repositories queued or syncing") == 2
    assert 'disabled aria-busy="true"' in active_desktop
    assert 'disabled aria-busy="true"' in active_mobile


def test_empty_pdf_timeline_keeps_search_available_and_explains_the_zero_state(
    loopback_client,
):
    response = loopback_client.get(reverse("bitbucket_search:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Showing <strong>0 PDFs</strong>" in html
    assert "No PDFs are in the local inventory yet" in html
    assert 'placeholder="Type a phrase, then press Enter…"' in html
    assert "data-pdf-search-form" in html
    assert "data-pdf-search-input" in html
    assert 'data-pdf-timeline data-next-page-url=""' in html
    assert 'class="bb-load-older" hidden data-load-older-container' in html
    assert "data-pdf-row" not in html


def test_responsive_pdf_rows_override_the_generic_block_row_rule():
    css_path = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.css"
    css = css_path.read_text(encoding="utf-8")
    responsive_start = css.index("@media (max-width: 940px)")
    responsive_end = css.index("@media (max-width: 620px)", responsive_start)
    responsive_css = css[responsive_start:responsive_end]

    assert ".bb-mobile-refresh-all {\n    display: none;" in css[:responsive_start]
    assert "@media (min-width: 1281px) and (max-width: 1700px)" in css[:responsive_start]
    assert ".bb-refresh-all--desktop {\n        display: none;" in responsive_css
    assert ".bb-mobile-refresh-all {\n        display: block;" in responsive_css
    assert ".bb-results-table tr," in responsive_css
    assert ".bb-results-table .bb-document-row {\n        display: grid;" in responsive_css

    compact_start = responsive_end
    compact_end = css.index("@media (max-width: 390px)", compact_start)
    compact_css = css[compact_start:compact_end]
    mobile_css = css[compact_end : css.index("@media (prefers-reduced-motion", compact_end)]
    assert ".bb-results-table .bb-document-row {\n        grid-template-areas:" in compact_css
    assert ".bb-results-table .bb-document-row {\n        grid-template-areas:" in mobile_css


def test_load_older_failure_releases_the_html_fallback_link():
    javascript_path = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.js"
    javascript = javascript_path.read_text(encoding="utf-8")

    assert "loadingOlder || htmlFallbackRequired || !candidate" in javascript
    assert "htmlFallbackRequired = true;" in javascript
    assert "event.metaKey ||" in javascript
    assert "event.ctrlKey ||" in javascript
    assert "event.shiftKey ||" in javascript
    assert "event.altKey" in javascript


def test_pdf_timeline_renders_grouped_metadata_actions_and_load_older_fallback(
    loopback_client,
    monkeypatch,
):
    repository = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        sync_state=RepositorySyncState.READY,
    )
    network_plan = PDFDocument.objects.create(
        repository=repository,
        filename="Network <Plan>.pdf",
        relative_path="docs/network/Network <Plan>.pdf",
        open_count=4,
    )
    archived_plan = PDFDocument.objects.create(
        repository=repository,
        filename="Archive.pdf",
        relative_path="archive/Archive.pdf",
        open_count=0,
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
                            "project_label": "Architecture & Design",
                            "added_by_label": "A. Author",
                            "added_date_label": "Today",
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
                            "project_label": "",
                            "added_by_label": "",
                            "added_date_label": "14 Dec 2025",
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
    assert "0 indexed · 2 pending" in html
    assert 'data-timeline-group-key="today-2026-08-29"' in html
    assert 'data-timeline-group-key="year-2025"' in html
    assert html.index("<strong>Today</strong>") < html.index("<strong>2025</strong>")
    assert 'scope="rowgroup"' in html
    assert "Network &lt;Plan&gt;.pdf" in html
    assert "/managed/networking/docs/Network &lt;Plan&gt;.pdf" in html
    assert "Architecture &amp; Design" in html
    assert "A. Author" in html
    assert "Added by (Git author)" in html
    assert "Git addition" in html
    assert "Discovered by OWL" in html
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
    archived_row_start = html.index(f'data-document-id="{archived_plan.pk}"')
    archived_row = html[archived_row_start : html.index("</tr>", archived_row_start)]
    assert archived_row.count("Unavailable") == 2
    assert archived_row.count('method="post"') == 2
    assert html.count("Open PDF") == 2
    assert html.count('<span class="visually-hidden">Open folder</span>') == 2
    assert 'aria-label="Open folder containing Archive.pdf"' in archived_row
    assert f'data-next-page-url="{next_page_url}"' in html
    assert f'href="{next_page_url}" data-load-older' in html
    assert "Load older PDFs" in html
    assert "All PDFs loaded." in html
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
    assert 'data-timeline-group-key="today"' in first_html
    assert f'href="{next_page_url}" data-load-older' in first_html

    second_page = loopback_client.get(
        next_page_url,
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    payload = second_page.json()

    assert second_page.status_code == 200
    assert payload["nextPageUrl"] == ""
    assert f'data-document-id="{older_document.pk}"' in payload["html"]
    assert 'data-timeline-group-key="today"' in payload["html"]
    assert payload["html"].count('name="return_page" value="2"') == 2

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
    assert 'class="bb-mobile-add-repository" open' in html


@override_settings(BITBUCKET_ALLOWED_HOSTS=("bitbucket.org",))
def test_add_repository_returns_immediately_and_deduplicates_active_job(
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

    assert first.status_code == 202
    assert first.json()["state"] == "queued"
    assert second.status_code == 202
    assert second.json()["state"] == "already_running"
    assert BitbucketRepository.objects.count() == 1
    assert RepositorySyncJob.objects.count() == 1
    repository = BitbucketRepository.objects.get()
    assert repository.sync_state == RepositorySyncState.QUEUED
    assert repository.remote_url == "ssh://git@bitbucket.org/workspace/architecture.git"
    assert launched.call_count == 2
    launched.assert_called_with()


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
    assert "repository.automatic || {}" in javascript
    assert "bb-repository-card__automatic--${automatic.state" in javascript


def test_ready_repository_renders_green_tick_counts_and_refresh_action(loopback_client):
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

    assert response.status_code == 200
    assert 'data-repository-state="ready"' in html
    assert "bb-repository-state--ready" in html
    assert "12 PDF · 3 VSDX" in html
    assert f'action="/pdfs/repositories/{repository.pk}/refresh/"' in html
    assert "Daily refresh due" in html
    assert "data-repository-automatic" in html
    assert "No PDF catalogue published yet" in html
    assert "<dd data-total-repositories>1</dd>" in html


def test_failed_daily_refresh_shows_retry_time_and_retained_pdf_catalogue(loopback_client):
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

    assert response.status_code == 200
    assert 'data-repository-state="failed"' in html
    assert "bb-repository-state--failed" in html
    assert "bb-repository-card__automatic--retry_wait" in html
    assert "Retry scheduled" in html
    assert "Automatic retry 1 of 3" in html
    assert "Catalogue retained from" in html


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
    assert response.status_code == 202
    assert response.json()["state"] == "already_running"
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
    already_queued = BitbucketRepository.objects.create(
        display_name="networking",
        canonical_remote_key="bitbucket.org/workspace/networking",
        remote_url="ssh://git@bitbucket.org/workspace/networking.git",
        sync_state=RepositorySyncState.QUEUED,
    )
    RepositorySyncJob.objects.create(
        repository=already_queued,
        operation=RepositorySyncOperation.CLONE,
    )
    already_running = BitbucketRepository.objects.create(
        display_name="payments",
        canonical_remote_key="bitbucket.org/workspace/payments",
        remote_url="ssh://git@bitbucket.org/workspace/payments.git",
        sync_state=RepositorySyncState.FETCHING,
    )
    RepositorySyncJob.objects.create(
        repository=already_running,
        operation=RepositorySyncOperation.REFRESH,
        status=RepositorySyncJobStatus.RUNNING,
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
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

    assert response.status_code == 202
    assert response.json() == {
        "state": "queued",
        "detail": (
            "Queued 2 repositories for background Git refresh. 1 already queued; 1 already running."
        ),
        "eligible": 4,
        "queued": 2,
        "alreadyActive": 2,
        "alreadyQueued": 1,
        "alreadyRunning": 1,
        "workersStarted": 1,
    }
    assert launched.call_count == 1
    assert RepositorySyncJob.objects.filter(repository=ready).count() == 1
    assert RepositorySyncJob.objects.filter(repository=second_ready).count() == 1
    assert RepositorySyncJob.objects.filter(repository=already_queued).count() == 1
    assert RepositorySyncJob.objects.filter(repository=already_running).count() == 1
    assert not RepositorySyncJob.objects.filter(repository=disabled).exists()

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.20")
    assert remote_client.post(path).status_code == 403


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
        "detail": "No enabled repositories are available to refresh.",
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
