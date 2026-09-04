"""Durable, crash-safe semantic indexing jobs for stored bookmarks."""

from __future__ import annotations

import hashlib
import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter

import numpy as np
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from bookmark_manager.models import Bookmark
from semantic_search.models import (
    SemanticChunk,
    SemanticCorpusState,
    SemanticIndex,
    SemanticIndexJob,
    SemanticIndexJobStatus,
    SemanticSourceType,
)
from semantic_search.services.chunking import normalize_semantic_text
from semantic_search.services.logging_events import get_logger, log_event
from semantic_search.services.provider import (
    EmbeddingProvider,
    EmbeddingProviderError,
    SerializedVector,
    get_embedding_provider,
    serialize_vector,
)
from semantic_search.services.sources import (
    SourceUnavailable,
    current_source_content_hash,
    load_source_snapshot,
)

logger = get_logger("indexing")

_ACTIVE_JOB_STATUSES = (
    SemanticIndexJobStatus.QUEUED,
    SemanticIndexJobStatus.RUNNING,
)


@dataclass(frozen=True, slots=True)
class SemanticQueueSnapshot:
    queued: int
    running: int
    succeeded: int
    failed: int
    indexed_bookmarks: int
    embedded_chunks: int


@dataclass(frozen=True, slots=True)
class _PreparedEmbeddingBatch:
    chunk_vectors: tuple[SerializedVector, ...]
    centroid_vector: SerializedVector
    dimensions: int


def _source_filter(source_type: SemanticSourceType, source_id: int) -> dict[str, int]:
    if source_type != SemanticSourceType.BOOKMARK:
        raise ValueError("Only bookmarks can be semantically indexed.")
    return {"bookmark_id": source_id}


def _source_values(source_type: SemanticSourceType, source_id: int) -> dict[str, object]:
    return {
        "source_type": source_type,
        "bookmark_id": source_id,
    }


def queue_semantic_source(
    source_type: SemanticSourceType | str,
    source_id: int,
    *,
    force: bool = False,
) -> int | None:
    """Queue one current source revision once, returning only a newly created job ID."""

    if not settings.SEMANTIC_SEARCH_ENABLED:
        return None
    selected_type = SemanticSourceType(source_type)
    target_hash = current_source_content_hash(selected_type, source_id)
    source_filter = _source_filter(selected_type, source_id)
    model_version = str(settings.SEMANTIC_MODEL_VERSION)
    chunker_version = str(settings.SEMANTIC_CHUNKER_VERSION)

    current = SemanticIndex.objects.filter(**source_filter).first()
    if (
        not force
        and current is not None
        and current.content_hash == target_hash
        and current.model_version == model_version
        and current.chunker_version == chunker_version
    ):
        return None

    now = timezone.now()
    if not force:
        recent_failure = (
            SemanticIndexJob.objects.filter(
                **source_filter,
                target_content_hash=target_hash,
                target_model_version=model_version,
                target_chunker_version=chunker_version,
                status=SemanticIndexJobStatus.FAILED,
                completed_at__gte=now - timedelta(seconds=settings.SEMANTIC_FAILED_RETRY_SECONDS),
            )
            .order_by("-completed_at", "-id")
            .first()
        )
        if recent_failure is not None:
            return None

    try:
        with transaction.atomic():
            _reserve_sqlite_write()
            existing = SemanticIndexJob.objects.filter(
                **source_filter,
                status__in=(
                    SemanticIndexJobStatus.QUEUED,
                    SemanticIndexJobStatus.RUNNING,
                ),
                target_content_hash=target_hash,
                target_model_version=model_version,
                target_chunker_version=chunker_version,
            ).first()
            if existing is not None:
                return None
            SemanticIndexJob.objects.filter(
                **source_filter,
                status__in=(
                    SemanticIndexJobStatus.QUEUED,
                    SemanticIndexJobStatus.RUNNING,
                ),
            ).update(
                status=SemanticIndexJobStatus.CANCELLED,
                completed_at=now,
                heartbeat_at=None,
                worker_pid=None,
                error_code="source_changed",
                error_summary="A newer stored source revision replaced this embedding request.",
            )
            job = SemanticIndexJob.objects.create(
                **_source_values(selected_type, source_id),
                target_content_hash=target_hash,
                target_model_version=model_version,
                target_chunker_version=chunker_version,
            )
    except IntegrityError:
        return None
    log_event(
        logger,
        logging.DEBUG,
        "semantic_index_queued",
        source_type=selected_type,
        source_id=source_id,
        job_id=job.pk,
    )
    return job.pk


def sweep_semantic_index_queue(
    *,
    interrupt_running: bool = False,
    interrupt_worker_pids: Sequence[int] = (),
) -> tuple[int, ...]:
    """Deep-reconcile stored bookmarks without fetching external content."""

    if not settings.SEMANTIC_SEARCH_ENABLED:
        return ()
    _recover_stale_jobs(
        force=interrupt_running,
        worker_pids=interrupt_worker_pids,
    )
    limit = int(settings.SEMANTIC_SWEEP_BATCH_SIZE)
    queued: list[int] = []
    bookmark_candidates = _bookmark_reconciliation_candidates()
    for source_id in bookmark_candidates:
        try:
            job_id = queue_semantic_source(SemanticSourceType.BOOKMARK, source_id)
        except SourceUnavailable:
            continue
        if job_id is not None:
            queued.append(job_id)
            if len(queued) >= limit:
                break
    log_event(
        logger,
        logging.DEBUG,
        "semantic_reconciliation_completed",
        count=len(bookmark_candidates),
        queued_count=len(queued),
    )
    return tuple(queued)


def _reconciliation_state(source_type: SemanticSourceType):
    source_field = "bookmark_id"
    model_version = str(settings.SEMANTIC_MODEL_VERSION)
    chunker_version = str(settings.SEMANTIC_CHUNKER_VERSION)
    current_indexes = dict(
        SemanticIndex.objects.filter(
            source_type=source_type,
            model_version=model_version,
            chunker_version=chunker_version,
        ).values_list(source_field, "content_hash")
    )
    active_jobs = dict(
        SemanticIndexJob.objects.filter(
            source_type=source_type,
            status__in=_ACTIVE_JOB_STATUSES,
            target_model_version=model_version,
            target_chunker_version=chunker_version,
        ).values_list(source_field, "target_content_hash")
    )
    failed_after = timezone.now() - timedelta(seconds=settings.SEMANTIC_FAILED_RETRY_SECONDS)
    recent_failures = set(
        SemanticIndexJob.objects.filter(
            source_type=source_type,
            status=SemanticIndexJobStatus.FAILED,
            target_model_version=model_version,
            target_chunker_version=chunker_version,
            completed_at__gte=failed_after,
        ).values_list(source_field, "target_content_hash")
    )
    return current_indexes, active_jobs, recent_failures


def _needs_reconciliation(
    source_id: int,
    content_hash: str,
    *,
    current_indexes: dict[int, str],
    active_jobs: dict[int, str],
    recent_failures: set[tuple[int, str]],
) -> bool:
    return not (
        current_indexes.get(source_id) == content_hash
        or active_jobs.get(source_id) == content_hash
        or (source_id, content_hash) in recent_failures
    )


def _bookmark_reconciliation_candidates() -> tuple[int, ...]:
    current_indexes, active_jobs, recent_failures = _reconciliation_state(
        SemanticSourceType.BOOKMARK
    )
    candidates: list[int] = []
    bookmarks = (
        Bookmark.objects.only("pk", "title", "page_text", "notes")
        .prefetch_related("tags")
        .order_by("pk")
    )
    for bookmark in bookmarks.iterator(chunk_size=500):
        content_hash = _bookmark_reconciliation_hash(bookmark)
        if content_hash is not None and _needs_reconciliation(
            bookmark.pk,
            content_hash,
            current_indexes=current_indexes,
            active_jobs=active_jobs,
            recent_failures=recent_failures,
        ):
            candidates.append(bookmark.pk)
    return tuple(candidates)


def _bookmark_reconciliation_hash(bookmark: Bookmark) -> str | None:
    sections: list[str] = []
    title = normalize_semantic_text(bookmark.title)
    page_text = normalize_semantic_text(bookmark.page_text)
    notes = normalize_semantic_text(bookmark.notes)
    tags = sorted(
        {
            normalized
            for tag in bookmark.tags.all()
            if (normalized := normalize_semantic_text(tag.name))
        },
        key=lambda value: (value.casefold(), value),
    )
    if title:
        sections.append(f"Title: {title}")
    if page_text:
        sections.append(f"Page text:\n{page_text}")
    if notes:
        sections.append(f"Notes:\n{notes}")
    if tags:
        sections.append(f"Tags: {', '.join(tags)}")
    semantic_text = normalize_semantic_text("\n\n".join(sections))
    if not semantic_text:
        return None
    return hashlib.sha256(semantic_text.encode("utf-8")).hexdigest()


def _reserve_sqlite_write() -> None:
    from django.db import connection

    if connection.vendor == "sqlite":
        SemanticIndexJob.objects.filter(pk=-1).update(status=F("status"))


def _normalized_worker_pids(worker_pids: Sequence[int]) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(
            worker_pid
            for worker_pid in worker_pids
            if isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and worker_pid > 0
        )
    )


def stale_semantic_worker_pids(*, observed_at=None) -> tuple[int, ...]:
    """Return semantic lease owners whose durable heartbeat expired."""

    now = observed_at or timezone.now()
    stale_before = now - timedelta(seconds=settings.SEMANTIC_JOB_LEASE_SECONDS)
    return tuple(
        dict.fromkeys(
            SemanticIndexJob.objects.filter(
                status=SemanticIndexJobStatus.RUNNING,
                worker_pid__isnull=False,
            )
            .filter(Q(heartbeat_at__isnull=True) | Q(heartbeat_at__lt=stale_before))
            .order_by("worker_pid")
            .values_list("worker_pid", flat=True)
        )
    )


def _recover_stale_jobs(
    *,
    force: bool = False,
    worker_pids: Sequence[int] = (),
) -> tuple[int, ...]:
    now = timezone.now()
    stale_before = now - timedelta(seconds=settings.SEMANTIC_JOB_LEASE_SECONDS)
    exact_worker_pids = _normalized_worker_pids(worker_pids)
    query = Q(status=SemanticIndexJobStatus.RUNNING)
    if not force:
        query &= (
            Q(heartbeat_at__isnull=True)
            | Q(heartbeat_at__lt=stale_before)
            | Q(worker_pid__in=exact_worker_pids)
        )
    recovered_worker_pids: list[int] = []
    with transaction.atomic():
        _reserve_sqlite_write()
        stale_jobs = tuple(SemanticIndexJob.objects.select_for_update().filter(query))
        for job in stale_jobs:
            if job.worker_pid:
                recovered_worker_pids.append(job.worker_pid)
            if job.retry_count < settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES:
                job.status = SemanticIndexJobStatus.QUEUED
                job.retry_count += 1
                job.next_attempt_at = now
                job.started_at = None
                job.heartbeat_at = None
                job.worker_pid = None
                job.error_code = "worker_interrupted"
                job.error_summary = "The semantic worker stopped before publication and will retry."
                job.save(
                    update_fields=(
                        "status",
                        "retry_count",
                        "next_attempt_at",
                        "started_at",
                        "heartbeat_at",
                        "worker_pid",
                        "error_code",
                        "error_summary",
                    )
                )
            else:
                job.status = SemanticIndexJobStatus.FAILED
                job.heartbeat_at = now
                job.completed_at = now
                job.worker_pid = None
                job.error_code = "worker_interrupted"
                job.error_summary = "The semantic worker stopped repeatedly before publication."
                job.save(
                    update_fields=(
                        "status",
                        "heartbeat_at",
                        "completed_at",
                        "worker_pid",
                        "error_code",
                        "error_summary",
                    )
                )
    return tuple(dict.fromkeys(recovered_worker_pids))


def claim_next_semantic_job() -> SemanticIndexJob | None:
    """Claim one due job in a short SQLite write transaction."""

    if not settings.SEMANTIC_SEARCH_ENABLED:
        return None
    now = timezone.now()
    due_jobs = SemanticIndexJob.objects.filter(status=SemanticIndexJobStatus.QUEUED).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )
    # Idle workers must not acquire SQLite's write lock merely to discover that
    # the queue is empty. This path is exercised far more often than a real claim.
    if not due_jobs.exists():
        return None
    if (
        SemanticIndexJob.objects.filter(status=SemanticIndexJobStatus.RUNNING).count()
        >= settings.SEMANTIC_MAX_WORKERS
    ):
        return None
    with transaction.atomic():
        _reserve_sqlite_write()
        if (
            SemanticIndexJob.objects.filter(status=SemanticIndexJobStatus.RUNNING).count()
            >= settings.SEMANTIC_MAX_WORKERS
        ):
            return None
        candidate_id = (
            SemanticIndexJob.objects.filter(status=SemanticIndexJobStatus.QUEUED)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("requested_at", "id")
            .values_list("id", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        claimed = SemanticIndexJob.objects.filter(
            pk=candidate_id,
            status=SemanticIndexJobStatus.QUEUED,
        ).update(
            status=SemanticIndexJobStatus.RUNNING,
            started_at=now,
            heartbeat_at=now,
            next_attempt_at=None,
            worker_pid=os.getpid(),
            error_code="",
            error_summary="",
        )
        if claimed != 1:
            return None
        job = SemanticIndexJob.objects.get(pk=candidate_id)
    log_event(
        logger,
        logging.DEBUG,
        "semantic_index_claimed",
        source_type=job.source_type,
        source_id=job.bookmark_id,
        job_id=job.pk,
        worker_pid=job.worker_pid,
    )
    return job


def _update_heartbeat(job_id: int) -> None:
    SemanticIndexJob.objects.filter(
        pk=job_id,
        status=SemanticIndexJobStatus.RUNNING,
    ).update(heartbeat_at=timezone.now())


def _encode_chunks(snapshot, provider: EmbeddingProvider, *, job_id: int) -> np.ndarray:
    batch_size = int(settings.SEMANTIC_EMBEDDING_BATCH_SIZE)
    encoded: list[np.ndarray] = []
    for start in range(0, len(snapshot.chunks), batch_size):
        batch = snapshot.chunks[start : start + batch_size]
        encoded.append(provider.encode_documents(tuple(chunk.chunk_text for chunk in batch)))
        _update_heartbeat(job_id)
    if not encoded:
        raise EmbeddingProviderError("The stored source did not contain embeddable text.")
    return np.ascontiguousarray(np.vstack(encoded), dtype=np.float32)


def _prepare_embedding_batch(snapshot, vectors: np.ndarray) -> _PreparedEmbeddingBatch:
    """Validate and serialize vectors before entering the SQLite publication write."""

    if vectors.shape[0] != len(snapshot.chunks):
        raise EmbeddingProviderError("The local model returned an incomplete embedding batch.")
    chunk_vectors = tuple(serialize_vector(vector) for vector in vectors)
    dimensions = chunk_vectors[0].dimensions
    if any(item.dimensions != dimensions for item in chunk_vectors):
        raise EmbeddingProviderError("The local model returned inconsistent dimensions.")
    centroid = np.asarray(vectors, dtype=np.float32).mean(axis=0, dtype=np.float32)
    centroid_norm = float(np.linalg.norm(centroid))
    if not math.isfinite(centroid_norm) or centroid_norm <= 0:
        raise EmbeddingProviderError("The local model returned an invalid source centroid.")
    centroid_vector = serialize_vector(centroid / centroid_norm)
    if centroid_vector.dimensions != dimensions:
        raise EmbeddingProviderError("The source centroid has inconsistent dimensions.")
    return _PreparedEmbeddingBatch(
        chunk_vectors=chunk_vectors,
        centroid_vector=centroid_vector,
        dimensions=dimensions,
    )


def bump_semantic_corpus_generation(source_type: str) -> None:
    """Advance the durable cache generation for one semantic source family."""

    with transaction.atomic():
        state, created = SemanticCorpusState.objects.select_for_update().get_or_create(
            source_type=source_type,
            defaults={"generation": 1},
        )
        if not created:
            SemanticCorpusState.objects.filter(pk=state.pk).update(generation=F("generation") + 1)


def _publish_job(
    job_id: int,
    snapshot,
    prepared: _PreparedEmbeddingBatch,
) -> SemanticIndexJob | None:
    now = timezone.now()
    with transaction.atomic():
        _reserve_sqlite_write()
        try:
            job = SemanticIndexJob.objects.select_for_update().get(pk=job_id)
        except SemanticIndexJob.DoesNotExist:
            return None
        if job.status != SemanticIndexJobStatus.RUNNING:
            return job
        source_id = job.bookmark_id
        try:
            current_hash = current_source_content_hash(job.source_type, source_id)
        except SourceUnavailable:
            job.status = SemanticIndexJobStatus.CANCELLED
            job.completed_at = now
            job.heartbeat_at = None
            job.worker_pid = None
            job.error_code = "source_unavailable"
            job.error_summary = "The stored source was removed before embedding publication."
            job.save(
                update_fields=(
                    "status",
                    "completed_at",
                    "heartbeat_at",
                    "worker_pid",
                    "error_code",
                    "error_summary",
                )
            )
            return job
        if current_hash != job.target_content_hash or current_hash != snapshot.content_hash:
            job.status = SemanticIndexJobStatus.CANCELLED
            job.completed_at = now
            job.heartbeat_at = None
            job.worker_pid = None
            job.error_code = "source_changed"
            job.error_summary = "A newer stored source revision replaced this embedding request."
            job.save(
                update_fields=(
                    "status",
                    "completed_at",
                    "heartbeat_at",
                    "worker_pid",
                    "error_code",
                    "error_summary",
                )
            )
            transaction.on_commit(
                lambda: queue_semantic_source(job.source_type, source_id, force=True)
            )
            return job
        source_filter = _source_filter(SemanticSourceType(job.source_type), source_id)
        index, _created = SemanticIndex.objects.select_for_update().get_or_create(
            **source_filter,
            defaults={
                "source_type": job.source_type,
                "content_hash": snapshot.content_hash,
                "model_version": job.target_model_version,
                "chunker_version": job.target_chunker_version,
                "dimensions": prepared.dimensions,
                "centroid_vector": prepared.centroid_vector.value,
                "chunk_count": 0,
                "character_count": 0,
                "published_at": now,
            },
        )
        index.source_type = job.source_type
        index.content_hash = snapshot.content_hash
        index.model_version = job.target_model_version
        index.chunker_version = job.target_chunker_version
        index.dimensions = prepared.dimensions
        index.centroid_vector = prepared.centroid_vector.value
        index.chunk_count = len(snapshot.chunks)
        index.character_count = snapshot.character_count
        index.published_at = now
        index.save()
        index.chunks.all().delete()
        SemanticChunk.objects.bulk_create(
            [
                SemanticChunk(
                    index=index,
                    ordinal=chunk.ordinal,
                    page_number=chunk.page_number,
                    chunk_text=chunk.chunk_text,
                    text_hash=chunk.text_hash,
                    vector=vector.value,
                    character_count=chunk.character_count,
                )
                for chunk, vector in zip(snapshot.chunks, prepared.chunk_vectors, strict=True)
            ],
            batch_size=250,
        )
        job.status = SemanticIndexJobStatus.SUCCEEDED
        job.heartbeat_at = now
        job.completed_at = now
        job.worker_pid = None
        job.error_code = ""
        job.error_summary = ""
        job.save(
            update_fields=(
                "status",
                "heartbeat_at",
                "completed_at",
                "worker_pid",
                "error_code",
                "error_summary",
            )
        )
        bump_semantic_corpus_generation(job.source_type)
    return job


def _fail_or_retry_job(job_id: int, *, error_code: str, error_summary: str) -> None:
    now = timezone.now()
    with transaction.atomic():
        _reserve_sqlite_write()
        try:
            job = SemanticIndexJob.objects.select_for_update().get(pk=job_id)
        except SemanticIndexJob.DoesNotExist:
            return
        if job.status != SemanticIndexJobStatus.RUNNING:
            return
        if job.retry_count < settings.SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES:
            job.status = SemanticIndexJobStatus.QUEUED
            job.retry_count += 1
            job.next_attempt_at = now + timedelta(seconds=settings.SEMANTIC_JOB_RETRY_SECONDS)
            job.started_at = None
            job.heartbeat_at = None
            job.worker_pid = None
            job.error_code = error_code
            job.error_summary = error_summary
            job.save(
                update_fields=(
                    "status",
                    "retry_count",
                    "next_attempt_at",
                    "started_at",
                    "heartbeat_at",
                    "worker_pid",
                    "error_code",
                    "error_summary",
                )
            )
        else:
            job.status = SemanticIndexJobStatus.FAILED
            job.heartbeat_at = now
            job.completed_at = now
            job.worker_pid = None
            job.error_code = error_code
            job.error_summary = error_summary
            job.save(
                update_fields=(
                    "status",
                    "heartbeat_at",
                    "completed_at",
                    "worker_pid",
                    "error_code",
                    "error_summary",
                )
            )


def _cancel_unavailable_job(job_id: int) -> None:
    """Release a claimed lease when its stored source disappeared or became empty."""

    SemanticIndexJob.objects.filter(
        pk=job_id,
        status=SemanticIndexJobStatus.RUNNING,
    ).update(
        status=SemanticIndexJobStatus.CANCELLED,
        completed_at=timezone.now(),
        heartbeat_at=None,
        worker_pid=None,
        error_code="source_unavailable",
        error_summary="The stored source was removed before embedding completed.",
    )


def execute_semantic_job(
    job_id: int,
    *,
    provider: EmbeddingProvider | None = None,
) -> SemanticIndexJob | None:
    """Embed outside SQLite writes, then atomically replace the published chunks."""

    started = perf_counter()
    try:
        job = SemanticIndexJob.objects.get(pk=job_id)
    except SemanticIndexJob.DoesNotExist:
        return None
    if job.status != SemanticIndexJobStatus.RUNNING:
        return job
    source_id = job.bookmark_id
    try:
        snapshot = load_source_snapshot(job.source_type, source_id)
        if snapshot.content_hash != job.target_content_hash:
            with transaction.atomic():
                SemanticIndexJob.objects.filter(
                    pk=job.pk, status=SemanticIndexJobStatus.RUNNING
                ).update(
                    status=SemanticIndexJobStatus.CANCELLED,
                    completed_at=timezone.now(),
                    heartbeat_at=None,
                    worker_pid=None,
                    error_code="source_changed",
                    error_summary="A newer stored source revision replaced this embedding request.",
                )
            queue_semantic_source(job.source_type, source_id, force=True)
        else:
            vectors = _encode_chunks(
                snapshot,
                provider or get_embedding_provider(),
                job_id=job.pk,
            )
            prepared = _prepare_embedding_batch(snapshot, vectors)
            _publish_job(job.pk, snapshot, prepared)
    except SourceUnavailable:
        _cancel_unavailable_job(job.pk)
    except EmbeddingProviderError as exc:
        log_event(
            logger,
            logging.ERROR,
            "semantic_index_provider_failed",
            error=exc,
            source_type=job.source_type,
            source_id=source_id,
            job_id=job.pk,
        )
        _fail_or_retry_job(
            job.pk,
            error_code="embedding_provider_unavailable",
            error_summary="The local embedding model is unavailable; exact search remains active.",
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "semantic_index_failed",
            error=exc,
            source_type=job.source_type,
            source_id=source_id,
            job_id=job.pk,
        )
        _fail_or_retry_job(
            job.pk,
            error_code="semantic_indexing_error",
            error_summary="OWL could not publish this semantic index; exact search remains active.",
        )
    completed = SemanticIndexJob.objects.filter(pk=job.pk).first()
    if completed is not None:
        log_event(
            logger,
            logging.DEBUG,
            "semantic_index_finished",
            source_type=job.source_type,
            source_id=source_id,
            job_id=job.pk,
            status=completed.status,
            elapsed_ms=math.ceil((perf_counter() - started) * 1000),
        )
    return completed


def work_one_semantic_job(*, provider: EmbeddingProvider | None = None) -> SemanticIndexJob | None:
    job = claim_next_semantic_job()
    if job is None:
        return None
    return execute_semantic_job(job.pk, provider=provider)


def semantic_queue_snapshot() -> SemanticQueueSnapshot:
    statuses = dict(
        SemanticIndexJob.objects.order_by().values_list("status").annotate(total=Count("id"))
    )
    return SemanticQueueSnapshot(
        queued=statuses.get(SemanticIndexJobStatus.QUEUED, 0),
        running=statuses.get(SemanticIndexJobStatus.RUNNING, 0),
        succeeded=statuses.get(SemanticIndexJobStatus.SUCCEEDED, 0),
        failed=statuses.get(SemanticIndexJobStatus.FAILED, 0),
        indexed_bookmarks=SemanticIndex.objects.filter(
            source_type=SemanticSourceType.BOOKMARK
        ).count(),
        embedded_chunks=SemanticChunk.objects.count(),
    )
