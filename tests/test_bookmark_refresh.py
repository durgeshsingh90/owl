from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkRefreshRun,
    BookmarkRefreshStatus,
)
from bookmark_manager.services.bookmark_domain import ConfluencePageSnapshot, upsert_bookmark
from bookmark_manager.services.bookmark_refresh import (
    RefreshFetchResult,
    create_or_get_refresh_run,
    execute_refresh_run,
)
from bookmark_manager.services.configuration import ActiveConfluenceProfile
from bookmark_manager.services.confluence_validation import CanonicalOrigin

pytestmark = pytest.mark.django_db(transaction=True)

SYNTHETIC_ORIGIN = "https://confluence.example.invalid/wiki"
SYNTHETIC_PAT = "synthetic-refresh-test-pat-never-valid-001"


def _profile() -> ActiveConfluenceProfile:
    return ActiveConfluenceProfile(
        origin=CanonicalOrigin(
            scheme="https",
            host="confluence.example.invalid",
            port=443,
            context_path="/wiki",
            is_test_target=True,
        ),
        token=SYNTHETIC_PAT,
        auth_mode="bearer",
        source="environment",
    )


def _snapshot(
    page_id: str,
    title: str,
    *,
    version: int = 1,
    page_text: str = "old searchable text",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ConfluencePageSnapshot:
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=title,
        url=f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/{page_id}/{title.casefold().replace(' ', '-')}",
        space_name="Engineering",
        space_key="ENG",
        version=version,
        created_at=created_at or datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
        updated_at=updated_at or datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        created_by_name="Original Writer",
        modified_by_name="Original Editor",
        author_name="Original Writer",
        page_text=page_text,
        ancestors=(),
    )


def _bookmark(page_id: str, title: str) -> Bookmark:
    return upsert_bookmark(_snapshot(page_id, title)).bookmark


def test_only_one_global_refresh_run_can_be_active():
    first, created = create_or_get_refresh_run()
    second, second_created = create_or_get_refresh_run()

    assert created is True
    assert second_created is False
    assert second.pk == first.pk
    with pytest.raises(IntegrityError):
        BookmarkRefreshRun.objects.create(status=BookmarkRefreshStatus.RUNNING)


def test_background_refresh_fetches_in_parallel_and_serially_updates_search_metadata():
    bookmarks = [_bookmark(str(920000 + index), f"Old title {index}") for index in range(3)]
    bookmarks[0].favorite = True
    bookmarks[0].notes = "Keep my local note"
    bookmarks[0].save(update_fields=("favorite", "notes"))
    run = BookmarkRefreshRun.objects.create()
    barrier = threading.Barrier(3)
    state_lock = threading.Lock()
    active_fetches = 0
    peak_fetches = 0
    written_at = datetime(2024, 3, 4, 8, 15, tzinfo=UTC)
    updated_at = datetime(2026, 8, 28, 7, 45, tzinfo=UTC)

    def fetcher(_profile, bookmark_id: int, page_id: str) -> RefreshFetchResult:
        nonlocal active_fetches, peak_fetches
        with state_lock:
            active_fetches += 1
            peak_fetches = max(peak_fetches, active_fetches)
        barrier.wait(timeout=2)
        time.sleep(0.01)
        with state_lock:
            active_fetches -= 1
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            snapshot=_snapshot(
                page_id,
                f"Renamed page {page_id}",
                version=2,
                page_text=f"latest indexed text for {page_id}",
                created_at=written_at,
                updated_at=updated_at,
            ),
        )

    before = timezone.now()
    completed = execute_refresh_run(
        run.pk,
        profile=_profile(),
        fetcher=fetcher,
        max_workers=3,
    )
    after = timezone.now()

    assert completed is not None
    assert completed.status == BookmarkRefreshStatus.SUCCEEDED
    assert (
        completed.total_bookmarks,
        completed.processed_bookmarks,
        completed.succeeded_bookmarks,
        completed.failed_bookmarks,
    ) == (3, 3, 3, 0)
    assert peak_fetches == 3
    for bookmark in Bookmark.objects.order_by("id"):
        assert bookmark.title == f"Renamed page {bookmark.page_id}"
        assert bookmark.page_text == f"latest indexed text for {bookmark.page_id}"
        assert bookmark.created_at == written_at
        assert bookmark.updated_at == updated_at
        assert bookmark.version == 2
        assert before <= bookmark.last_refresh_attempt_at <= after
        assert before <= bookmark.last_refreshed_at <= after
    bookmarks[0].refresh_from_db()
    assert bookmarks[0].favorite is True
    assert bookmarks[0].notes == "Keep my local note"


def test_failed_page_keeps_last_good_text_and_records_refresh_diagnostics():
    bookmark = _bookmark("930001", "Last good title")
    run = BookmarkRefreshRun.objects.create()

    def fetcher(_profile, bookmark_id: int, page_id: str) -> RefreshFetchResult:
        return RefreshFetchResult(
            bookmark_id=bookmark_id,
            page_id=page_id,
            error_code="not_found",
            error_message="The Confluence page was not found.",
            availability_status=BookmarkAvailability.NOT_FOUND,
        )

    completed = execute_refresh_run(run.pk, profile=_profile(), fetcher=fetcher)

    assert completed is not None
    assert completed.status == BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS
    assert (completed.succeeded_bookmarks, completed.failed_bookmarks) == (0, 1)
    bookmark.refresh_from_db()
    assert bookmark.title == "Last good title"
    assert bookmark.page_text == "old searchable text"
    assert bookmark.availability_status == BookmarkAvailability.NOT_FOUND
    assert bookmark.last_error_code == "not_found"
    assert bookmark.last_refresh_attempt_at is not None
    assert bookmark.last_refreshed_at is None


def test_refresh_start_returns_immediately_and_reuses_an_active_run(
    client,
    monkeypatch,
):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    launched: list[int] = []
    monkeypatch.setattr(
        "bookmark_manager.views.get_configuration_summary",
        lambda: SimpleNamespace(complete=True),
    )
    monkeypatch.setattr(
        "bookmark_manager.views.launch_refresh_worker",
        lambda run_id: launched.append(run_id),
    )

    first = client.post(
        reverse("bookmark_manager:refresh_start"),
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    second = client.post(
        reverse("bookmark_manager:refresh_start"),
        HTTP_ACCEPT="application/json",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert first.status_code == 202
    assert first.json()["state"] == "queued"
    assert first.json()["refresh"]["active"] is True
    assert second.status_code == 202
    assert second.json()["state"] == "already_running"
    assert launched == [first.json()["refresh"]["run_id"]]


def test_refresh_status_is_compact_and_includes_last_completed_timestamp(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    completed_at = timezone.now() - timedelta(minutes=3)
    BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED,
        total_bookmarks=4,
        processed_bookmarks=4,
        succeeded_bookmarks=4,
        started_at=completed_at - timedelta(seconds=2),
        heartbeat_at=completed_at,
        completed_at=completed_at,
    )

    response = client.get(reverse("bookmark_manager:refresh_status"))
    payload = response.json()["refresh"]

    assert response.status_code == 200
    assert payload["active"] is False
    assert payload["progress"] == 100
    assert payload["succeeded"] == 4
    assert payload["last_completed_at"] == completed_at.isoformat()


def test_bookmark_page_renders_global_refresh_control_and_timestamp(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    completed_at = timezone.now() - timedelta(hours=1)
    BookmarkRefreshRun.objects.create(
        status=BookmarkRefreshStatus.SUCCEEDED_WITH_ERRORS,
        total_bookmarks=3,
        processed_bookmarks=3,
        succeeded_bookmarks=2,
        failed_bookmarks=1,
        started_at=completed_at - timedelta(seconds=5),
        heartbeat_at=completed_at,
        completed_at=completed_at,
        last_error_message="1 bookmark could not be refreshed.",
    )

    response = client.get(reverse("bookmark_manager:index"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'aria-label="Refresh all Confluence bookmarks"' in html
    assert f'data-last-completed-at="{completed_at.isoformat()}"' in html
    assert "Last completed" in html
    assert reverse("bookmark_manager:refresh_status") in html
