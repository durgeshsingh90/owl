from __future__ import annotations

from unittest.mock import Mock

import pytest

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFIndexState,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bookmark_manager.models import Bookmark, ConfluencePageNode
from semantic_search.management.commands import semantic_index_worker
from semantic_search.models import (
    SemanticIndex,
    SemanticIndexJob,
    SemanticIndexJobStatus,
    SemanticSourceType,
)
from semantic_search.services import jobs
from semantic_search.services.sources import current_source_content_hash

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def semantic_queue_settings(settings):
    settings.SEMANTIC_SEARCH_ENABLED = True
    settings.SEMANTIC_MODEL_VERSION = "queue-performance-model-v1"
    settings.SEMANTIC_CHUNKER_VERSION = "queue-performance-chunker-v1"
    settings.SEMANTIC_SWEEP_BATCH_SIZE = 500
    settings.SEMANTIC_FAILED_RETRY_SECONDS = 3_600
    settings.SEMANTIC_JOB_LEASE_SECONDS = 900
    settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES = 2
    settings.SEMANTIC_MAX_WORKERS = 4


def _bookmark(number: int) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=f"queue-performance-{number}",
        title=f"Bookmark {number}",
        url=f"https://confluence.example.test/pages/queue-performance-{number}",
    )
    return Bookmark.objects.create(
        page_id=node.page_id,
        tree_node=node,
        title=node.title,
        url=node.url,
        page_text=f"Stored semantic body {number}",
    )


def _repository(number: int, *, enabled: bool = True) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=f"Semantic repository {number}",
        canonical_remote_key=f"bitbucket.example.invalid/team/semantic-{number}",
        remote_url=f"ssh://git@bitbucket.example.invalid/team/semantic-{number}.git",
        enabled=enabled,
    )


def _pdf_revision(number: int, *, repository: BitbucketRepository | None = None):
    revision = PDFTextRevision.objects.create(
        content_sha256=f"{number:064x}",
        extractor_version="queue-performance-extractor-v1",
        source_byte_size=100,
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=20,
    )
    if repository is not None:
        PDFDocument.objects.create(
            repository=repository,
            filename=f"document-{number}.pdf",
            relative_path=f"docs/document-{number}.pdf",
            git_blob_id=f"{number:040x}",
            indexed_revision=revision,
            indexed_git_blob_id=f"{number:040x}",
            index_state=PDFIndexState.READY,
            extracted_character_count=20,
        )
    return revision


def _current_index(*, source_type: SemanticSourceType, source_id: int) -> SemanticIndex:
    source_values = (
        {"bookmark_id": source_id}
        if source_type == SemanticSourceType.BOOKMARK
        else {"pdf_revision_id": source_id}
    )
    return SemanticIndex.objects.create(
        source_type=source_type,
        **source_values,
        content_hash=current_source_content_hash(source_type, source_id),
        model_version="queue-performance-model-v1",
        chunker_version="queue-performance-chunker-v1",
        dimensions=3,
    )


def test_deep_sweep_bulk_prefilters_current_sources_without_per_source_queries(
    django_assert_max_num_queries,
):
    bookmarks = [_bookmark(number) for number in range(1, 25)]
    for bookmark in bookmarks:
        _current_index(source_type=SemanticSourceType.BOOKMARK, source_id=bookmark.pk)

    with django_assert_max_num_queries(30):
        queued = jobs.sweep_semantic_index_queue()

    assert queued == ()
    assert not SemanticIndexJob.objects.exists()


def test_deep_sweep_repairs_bookmark_changes_that_bypass_model_signals():
    bookmark = _bookmark(101)
    SemanticIndexJob.objects.all().delete()
    _current_index(source_type=SemanticSourceType.BOOKMARK, source_id=bookmark.pk)

    Bookmark.objects.filter(pk=bookmark.pk).update(
        page_text="Changed through a bulk update without a post-save signal."
    )

    queued = jobs.sweep_semantic_index_queue()

    assert len(queued) == 1
    repair = SemanticIndexJob.objects.get(pk=queued[0])
    assert repair.source_type == SemanticSourceType.BOOKMARK
    assert repair.bookmark_id == bookmark.pk
    assert repair.target_content_hash == current_source_content_hash(
        SemanticSourceType.BOOKMARK,
        bookmark.pk,
    )


def test_deep_sweep_interleaves_bookmark_and_pdf_backfill(settings):
    settings.SEMANTIC_SWEEP_BATCH_SIZE = 4
    repository = _repository(1)
    for number in range(1, 4):
        _pdf_revision(number, repository=repository)
        _bookmark(number)

    queued = jobs.sweep_semantic_index_queue()

    assert len(queued) == 4
    assert list(
        SemanticIndexJob.objects.filter(pk__in=queued)
        .order_by("pk")
        .values_list("source_type", flat=True)
    ) == [
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
    ]


def test_deep_sweep_prunes_only_ineligible_pdf_semantics():
    enabled_repository = _repository(10)
    disabled_repository = _repository(11, enabled=False)
    eligible_revision = _pdf_revision(10, repository=enabled_repository)
    ineligible_revision = _pdf_revision(11, repository=disabled_repository)
    eligible_index = _current_index(
        source_type=SemanticSourceType.PDF_REVISION,
        source_id=eligible_revision.pk,
    )
    ineligible_index = _current_index(
        source_type=SemanticSourceType.PDF_REVISION,
        source_id=ineligible_revision.pk,
    )
    ineligible_job_id = jobs.queue_semantic_source(
        SemanticSourceType.PDF_REVISION,
        ineligible_revision.pk,
        force=True,
    )

    jobs.sweep_semantic_index_queue()

    assert SemanticIndex.objects.filter(pk=eligible_index.pk).exists()
    assert not SemanticIndex.objects.filter(pk=ineligible_index.pk).exists()
    assert PDFTextRevision.objects.filter(pk=ineligible_revision.pk).exists()
    cancelled_job = SemanticIndexJob.objects.get(pk=ineligible_job_id)
    assert cancelled_job.status == SemanticIndexJobStatus.CANCELLED
    assert cancelled_job.worker_pid is None
    assert cancelled_job.heartbeat_at is None


def test_claim_balances_source_families_and_prioritizes_bookmarks_on_a_tie():
    pdf_jobs = [
        jobs.queue_semantic_source(
            SemanticSourceType.PDF_REVISION,
            _pdf_revision(number).pk,
        )
        for number in range(20, 22)
    ]
    bookmark_jobs = [
        jobs.queue_semantic_source(
            SemanticSourceType.BOOKMARK,
            _bookmark(number).pk,
        )
        for number in range(20, 22)
    ]
    assert all(job_id is not None for job_id in (*pdf_jobs, *bookmark_jobs))

    claimed = [jobs.claim_next_semantic_job() for _index in range(4)]

    assert [job.source_type for job in claimed if job is not None] == [
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
    ]


def test_sequential_single_worker_claims_alternate_after_terminal_jobs(settings):
    settings.SEMANTIC_MAX_WORKERS = 1
    for number in range(30, 32):
        jobs.queue_semantic_source(
            SemanticSourceType.PDF_REVISION,
            _pdf_revision(number).pk,
        )
        jobs.queue_semantic_source(
            SemanticSourceType.BOOKMARK,
            _bookmark(number).pk,
        )

    claimed_sources: list[str] = []
    terminal_statuses = (
        SemanticIndexJobStatus.SUCCEEDED,
        SemanticIndexJobStatus.CANCELLED,
        SemanticIndexJobStatus.SUCCEEDED,
        SemanticIndexJobStatus.CANCELLED,
    )
    for terminal_status in terminal_statuses:
        claimed = jobs.claim_next_semantic_job()
        assert claimed is not None
        claimed_sources.append(claimed.source_type)
        SemanticIndexJob.objects.filter(pk=claimed.pk).update(
            status=terminal_status,
            worker_pid=None,
            heartbeat_at=None,
        )

    assert claimed_sources == [
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
        SemanticSourceType.BOOKMARK,
        SemanticSourceType.PDF_REVISION,
    ]


def test_empty_queue_claim_does_not_reserve_a_sqlite_write(monkeypatch):
    reserve = Mock()
    monkeypatch.setattr(jobs, "_reserve_sqlite_write", reserve)

    assert jobs.claim_next_semantic_job() is None

    reserve.assert_not_called()


def test_worker_uses_configured_idle_backoff(monkeypatch, settings):
    settings.SEMANTIC_WORKER_IDLE_SECONDS = 17
    work_one = Mock(return_value=None)
    sleep = Mock()
    monkeypatch.setattr(semantic_index_worker, "work_one_semantic_job", work_one)
    monkeypatch.setattr(semantic_index_worker.time, "sleep", sleep)
    monkeypatch.setattr(
        semantic_index_worker.time,
        "monotonic",
        Mock(side_effect=(0.0, 0.0, 2.0)),
    )

    semantic_index_worker.Command()._run(
        once=False,
        idle_timeout=1,
        poll_interval=None,
        no_startup_sweep=True,
    )

    assert work_one.call_count == 2
    sleep.assert_called_once_with(17.0)


def test_two_standalone_worker_startups_preserve_a_fresh_running_lease():
    bookmark = _bookmark(90)
    job_id = jobs.queue_semantic_source(SemanticSourceType.BOOKMARK, bookmark.pk)
    claimed = jobs.claim_next_semantic_job()
    assert claimed is not None
    assert claimed.pk == job_id
    original_started_at = claimed.started_at
    original_heartbeat_at = claimed.heartbeat_at
    original_worker_pid = claimed.worker_pid

    command = semantic_index_worker.Command()
    for _startup in range(2):
        command._run(
            once=True,
            idle_timeout=0,
            poll_interval=None,
            no_startup_sweep=False,
        )

    claimed.refresh_from_db()
    assert claimed.status == SemanticIndexJobStatus.RUNNING
    assert claimed.retry_count == 0
    assert claimed.started_at == original_started_at
    assert claimed.heartbeat_at == original_heartbeat_at
    assert claimed.worker_pid == original_worker_pid
