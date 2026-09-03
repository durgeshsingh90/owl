"""Durable current-run membership and end-to-end repository progress.

The pipeline remains executable without a browser.  This module records the
repositories accepted into a refresh before Git work begins, then derives
truthful terminal counts from the durable jobs attached to that membership.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from django.db import transaction
from django.db.models import Count, F, OuterRef, Subquery
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFLocalPolicyState,
    PDFPipelineRepositoryPhase,
    PDFPipelineRun,
    PDFPipelineRunRepository,
    PDFPipelineRunState,
    PDFPipelineRunTrigger,
    RepositorySyncJob,
    RepositorySyncOperation,
    RepositorySyncPhase,
)

ACTIVE_JOB_STATUSES = (PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
TERMINAL_RUN_STATES = (
    PDFPipelineRunState.COMPLETE,
    PDFPipelineRunState.COMPLETED_WITH_ERRORS,
    PDFPipelineRunState.CANCELLED,
)


def _timestamp(value: datetime | None = None) -> datetime:
    observed_at = value or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError("Pipeline timestamps must include a timezone.")
    return observed_at


def _unique_positive_ids(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        )
    )


@transaction.atomic
def accept_pipeline_run(
    repository_ids: Iterable[int],
    *,
    trigger: str = PDFPipelineRunTrigger.REPOSITORY_REFRESH,
    accepted_at: datetime | None = None,
) -> PDFPipelineRun:
    """Persist one ordered repository generation before expensive work starts."""

    if trigger not in PDFPipelineRunTrigger.values:
        raise ValueError("Unsupported PDF pipeline run trigger.")
    observed_at = _timestamp(accepted_at)
    requested_ids = _unique_positive_ids(repository_ids)
    repositories = tuple(
        BitbucketRepository.objects.filter(pk__in=requested_ids).order_by("id").only("id")
    )
    run = PDFPipelineRun.objects.create(
        trigger=trigger,
        state=PDFPipelineRunState.QUEUED,
        accepted_at=observed_at,
        accepted_repository_count=len(repositories),
    )
    PDFPipelineRunRepository.objects.bulk_create(
        [
            PDFPipelineRunRepository(
                run=run,
                repository=repository,
                accepted_at=observed_at,
                last_progress_at=observed_at,
            )
            for repository in repositories
        ]
    )
    return run


def run_memberships_by_repository(
    run: PDFPipelineRun | object,
) -> dict[int, PDFPipelineRunRepository]:
    run_id = run.pk if isinstance(run, PDFPipelineRun) else run
    return {
        membership.repository_id: membership
        for membership in PDFPipelineRunRepository.objects.filter(run_id=run_id).order_by("id")
    }


@transaction.atomic
def attach_sync_job_to_membership(
    job: RepositorySyncJob,
    membership: PDFPipelineRunRepository | None,
) -> RepositorySyncJob:
    """Attach an accepted repository generation to its one current Git job."""

    if membership is None:
        return job
    if job.repository_id != membership.repository_id:
        raise ValueError("A repository job can only join its own pipeline membership.")
    RepositorySyncJob.objects.filter(pk=job.pk).update(run_repository=membership)
    job.run_repository_id = membership.pk
    return job


def _sync_phase(job: RepositorySyncJob, phase: str) -> str:
    if phase == RepositorySyncPhase.CHECKING_CONNECTION:
        return PDFPipelineRepositoryPhase.CHECKING_CONNECTION
    if phase == RepositorySyncPhase.CLONING or (
        phase == RepositorySyncPhase.VALIDATING and job.operation == RepositorySyncOperation.CLONE
    ):
        return PDFPipelineRepositoryPhase.CLONING
    if phase in (RepositorySyncPhase.FETCHING, RepositorySyncPhase.UPDATING) or (
        phase == RepositorySyncPhase.VALIDATING and job.operation == RepositorySyncOperation.REFRESH
    ):
        return PDFPipelineRepositoryPhase.PULLING
    if phase in (RepositorySyncPhase.DISCOVERING, RepositorySyncPhase.FINALIZING):
        return PDFPipelineRepositoryPhase.DISCOVERING
    return PDFPipelineRepositoryPhase.QUEUED


def mark_sync_progress(
    job: RepositorySyncJob,
    *,
    phase: str | None = None,
    at: datetime | None = None,
) -> None:
    """Activate one run membership from authoritative Git worker progress."""

    if not job.run_repository_id:
        return
    observed_at = _timestamp(at)
    effective_phase = _sync_phase(job, phase or job.phase)
    updated = (
        PDFPipelineRunRepository.objects.filter(pk=job.run_repository_id)
        .exclude(lifecycle_state__in=TERMINAL_RUN_STATES)
        .update(
            lifecycle_state=PDFPipelineRunState.ACTIVE,
            phase=effective_phase,
            last_progress_at=observed_at,
        )
    )
    if updated:
        PDFPipelineRunRepository.objects.filter(
            pk=job.run_repository_id,
            activated_at__isnull=True,
        ).update(activated_at=observed_at)
        run_id = (
            PDFPipelineRunRepository.objects.filter(pk=job.run_repository_id)
            .values_list("run_id", flat=True)
            .first()
        )
        if run_id is None:
            return
        PDFPipelineRun.objects.filter(pk=run_id).exclude(state__in=TERMINAL_RUN_STATES).update(
            state=PDFPipelineRunState.ACTIVE,
            last_progress_at=observed_at,
        )
        PDFPipelineRun.objects.filter(
            pk=run_id,
            started_at__isnull=True,
        ).update(started_at=observed_at)


@transaction.atomic
def finalize_repository_inventory(
    job: RepositorySyncJob,
    *,
    total_pdfs: int,
    repository_revision: str,
    at: datetime | None = None,
) -> PDFPipelineRunRepository | None:
    """Commit the final inventory for the accepted repository generation."""

    if not job.run_repository_id:
        return None
    observed_at = _timestamp(at)
    total = max(0, int(total_pdfs))
    PDFPipelineRunRepository.objects.filter(pk=job.run_repository_id).update(
        lifecycle_state=PDFPipelineRunState.ACTIVE,
        phase=PDFPipelineRepositoryPhase.COMPLETING,
        repository_revision=str(repository_revision or "")[:64],
        inventory_final=True,
        inventory_final_at=observed_at,
        total_pdfs=total,
        remaining_pdfs=total,
        last_progress_at=observed_at,
    )
    PDFPipelineRunRepository.objects.filter(
        pk=job.run_repository_id,
        activated_at__isnull=True,
    ).update(activated_at=observed_at)
    return reconcile_run_repository(job.run_repository_id, at=observed_at)


@transaction.atomic
def mark_sync_terminal(
    job: RepositorySyncJob,
    *,
    cancelled: bool = False,
    at: datetime | None = None,
) -> None:
    """Finish an accepted repository whose Git prerequisite ended terminally."""

    if not job.run_repository_id:
        return
    observed_at = _timestamp(at)
    state = (
        PDFPipelineRunState.CANCELLED if cancelled else PDFPipelineRunState.COMPLETED_WITH_ERRORS
    )
    phase = (
        PDFPipelineRepositoryPhase.CANCELLED
        if cancelled
        else PDFPipelineRepositoryPhase.COMPLETED_WITH_ERRORS
    )
    PDFPipelineRunRepository.objects.filter(pk=job.run_repository_id).update(
        lifecycle_state=state,
        phase=phase,
        terminal_outcome=state,
        unresolved_failures=0 if cancelled else 1,
        completed_at=observed_at,
        last_progress_at=observed_at,
    )
    run_id = (
        PDFPipelineRunRepository.objects.filter(pk=job.run_repository_id)
        .values_list("run_id", flat=True)
        .first()
    )
    if run_id is not None:
        reconcile_pipeline_run(run_id, at=observed_at)


def attach_extraction_jobs(
    jobs: Sequence[PDFExtractionJob],
    membership: PDFPipelineRunRepository | None,
) -> None:
    """Associate new or reused PDF attempts with the accepted run generation."""

    if membership is None or not jobs:
        return
    job_ids = tuple(job.pk for job in jobs if job.pk is not None)
    if not job_ids:
        return
    PDFExtractionJob.objects.filter(
        pk__in=job_ids,
        document__repository_id=membership.repository_id,
    ).update(run_repository=membership, run_id=membership.run_id)
    for job in jobs:
        if job.pk in job_ids:
            job.run_repository_id = membership.pk
            job.run_id = membership.run_id


@transaction.atomic
def reconcile_run_repository(
    membership: PDFPipelineRunRepository | int,
    *,
    at: datetime | None = None,
) -> PDFPipelineRunRepository:
    """Refresh mutually exclusive counts and terminal state from durable boundaries."""

    observed_at = _timestamp(at)
    membership_id = (
        membership.pk if isinstance(membership, PDFPipelineRunRepository) else membership
    )
    current = PDFPipelineRunRepository.objects.select_related("run").get(pk=membership_id)
    current_documents = PDFDocument.objects.filter(
        repository_id=current.repository_id,
        lifecycle_state=PDFDocumentLifecycle.ACTIVE,
    ).exclude(local_policy__state=PDFLocalPolicyState.DELETED)
    attempts = PDFExtractionJob.objects.filter(
        run_repository_id=current.pk,
        document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
        target_git_blob_id=F("document__git_blob_id"),
        target_relative_path=F("document__relative_path"),
        target_file_size=F("document__file_size"),
    ).exclude(document__local_policy__state=PDFLocalPolicyState.DELETED)
    # A membership normally has one attempt per PDF.  Retries can append rows;
    # choose the newest ID per document explicitly so old failures do not count.
    latest_attempt = (
        PDFExtractionJob.objects.filter(
            run_repository_id=current.pk,
            document_id=OuterRef("document_id"),
        )
        .order_by("-requested_at", "-id")
        .values("id")[:1]
    )
    latest = attempts.annotate(_latest_id=Subquery(latest_attempt)).filter(id=F("_latest_id"))
    counts = {
        row["status"]: row["total"]
        for row in latest.values("status").annotate(total=Count("id")).order_by()
    }
    queued = counts.get(PDFExtractionJobStatus.QUEUED, 0)
    running = counts.get(PDFExtractionJobStatus.RUNNING, 0)
    raw_failed = counts.get(PDFExtractionJobStatus.FAILED, 0) + counts.get(
        PDFExtractionJobStatus.INTERRUPTED, 0
    )
    raw_cancelled = counts.get(PDFExtractionJobStatus.CANCELLED, 0)
    attempted_documents = attempts.order_by().values("document_id")
    documents_without_attempt = current_documents.exclude(pk__in=Subquery(attempted_documents))
    confirmed_current = documents_without_attempt.filter(
        index_state__in=(PDFIndexState.READY, PDFIndexState.NO_TEXT, PDFIndexState.PARTIAL),
        indexed_revision__isnull=False,
        indexed_git_blob_id=F("git_blob_id"),
    ).count()
    failed_without_attempt = documents_without_attempt.filter(
        index_state__in=(PDFIndexState.FAILED, PDFIndexState.STALE_ERROR)
    ).count()
    raw_succeeded = counts.get(PDFExtractionJobStatus.SUCCEEDED, 0) + confirmed_current
    raw_failed += failed_without_attempt
    raw_remaining = queued + running

    if current.inventory_final:
        # Explicit cancellation/failure and known live work take precedence
        # over success when a later catalogue makes the imperfect document
        # lookup larger than the frozen run total.  Any unclassified inventory
        # remains outstanding rather than becoming an assumed success.
        capacity = current.total_pdfs
        cancelled = min(raw_cancelled, capacity)
        capacity -= cancelled
        failed = min(raw_failed, capacity)
        capacity -= failed
        known_remaining = min(raw_remaining, capacity)
        capacity -= known_remaining
        succeeded = min(raw_succeeded, capacity)
        capacity -= succeeded
        remaining = known_remaining + capacity
    else:
        cancelled = raw_cancelled
        failed = raw_failed
        succeeded = raw_succeeded
        remaining = raw_remaining
    unresolved = failed
    lifecycle = PDFPipelineRunState.ACTIVE
    phase = PDFPipelineRepositoryPhase.COMPLETING
    terminal_outcome = ""
    completed_at = None

    running_phases = set(
        latest.filter(status=PDFExtractionJobStatus.RUNNING).values_list("phase", flat=True)
    )
    has_extracting = bool(
        running_phases
        & {
            PDFExtractionJobPhase.VALIDATING,
            PDFExtractionJobPhase.HASHING,
            PDFExtractionJobPhase.EXTRACTING,
        }
    )
    has_writing = PDFExtractionJobPhase.PUBLISHING in running_phases
    if has_extracting and has_writing:
        phase = PDFPipelineRepositoryPhase.EXTRACTING_AND_WRITING
    elif has_writing:
        phase = PDFPipelineRepositoryPhase.WRITING
    elif has_extracting:
        phase = PDFPipelineRepositoryPhase.EXTRACTING
    elif queued:
        phase = PDFPipelineRepositoryPhase.QUEUED

    if current.inventory_final and remaining == 0:
        completed_at = current.completed_at or observed_at
        if cancelled:
            lifecycle = PDFPipelineRunState.CANCELLED
            phase = PDFPipelineRepositoryPhase.CANCELLED
        elif failed or unresolved or succeeded != current.total_pdfs:
            lifecycle = PDFPipelineRunState.COMPLETED_WITH_ERRORS
            phase = PDFPipelineRepositoryPhase.COMPLETED_WITH_ERRORS
        else:
            lifecycle = PDFPipelineRunState.COMPLETE
            phase = PDFPipelineRepositoryPhase.COMPLETE
        terminal_outcome = lifecycle

    values = {
        "lifecycle_state": lifecycle,
        "phase": phase,
        "successful_pdfs": succeeded,
        "permanent_failed_pdfs": failed,
        "cancelled_pdfs": cancelled,
        "remaining_pdfs": remaining,
        "unresolved_failures": unresolved,
        "terminal_outcome": terminal_outcome,
        "completed_at": completed_at,
    }
    changed = any(getattr(current, field) != value for field, value in values.items())
    if changed:
        values["last_progress_at"] = observed_at
        PDFPipelineRunRepository.objects.filter(pk=current.pk).update(**values)
    reconcile_pipeline_run(
        current.run_id,
        at=observed_at,
        progress_observed=changed,
    )
    return PDFPipelineRunRepository.objects.get(pk=current.pk)


@transaction.atomic
def reconcile_pipeline_run(
    run: PDFPipelineRun | object,
    *,
    at: datetime | None = None,
    progress_observed: bool = False,
) -> PDFPipelineRun:
    """Aggregate repository buckets with cancelled/error/clean terminal precedence."""

    observed_at = _timestamp(at)
    run_id = run.pk if isinstance(run, PDFPipelineRun) else run
    current = PDFPipelineRun.objects.get(pk=run_id)
    states = list(current.repository_memberships.values_list("lifecycle_state", flat=True))
    if states and all(state in TERMINAL_RUN_STATES for state in states):
        if PDFPipelineRunState.CANCELLED in states:
            state = PDFPipelineRunState.CANCELLED
        elif PDFPipelineRunState.COMPLETED_WITH_ERRORS in states:
            state = PDFPipelineRunState.COMPLETED_WITH_ERRORS
        else:
            state = PDFPipelineRunState.COMPLETE
        completed_at = current.completed_at or observed_at
    elif PDFPipelineRunState.PAUSED in states:
        state = PDFPipelineRunState.PAUSED
        completed_at = None
    elif any(state == PDFPipelineRunState.ACTIVE for state in states):
        state = PDFPipelineRunState.ACTIVE
        completed_at = None
    else:
        state = PDFPipelineRunState.QUEUED
        completed_at = None
    changed = current.state != state or current.completed_at != completed_at
    if changed or progress_observed:
        PDFPipelineRun.objects.filter(pk=current.pk).update(
            state=state,
            last_progress_at=observed_at,
            completed_at=completed_at,
        )
        current.refresh_from_db()
    return current


def reconcile_open_pipeline_runs(
    *,
    at: datetime | None = None,
    limit: int = 250,
) -> dict[str, int]:
    """Repair bounded nonterminal summaries from durable job boundaries.

    This is intended for the resident supervisor, never a request handler. It
    is idempotent and skips unchanged rows, so an observation tick does not
    fabricate progress or perform an unconditional SQLite write.
    """

    observed_at = _timestamp(at)
    bounded_limit = max(1, min(int(limit), 1_000))
    membership_ids = tuple(
        PDFPipelineRunRepository.objects.filter(
            run__state__in=(
                PDFPipelineRunState.QUEUED,
                PDFPipelineRunState.ACTIVE,
                PDFPipelineRunState.PAUSED,
            ),
            lifecycle_state__in=(PDFPipelineRunState.QUEUED, PDFPipelineRunState.ACTIVE),
            inventory_final=True,
        )
        .order_by("accepted_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )
    for membership_id in membership_ids:
        reconcile_run_repository(membership_id, at=observed_at)

    run_ids = tuple(
        PDFPipelineRun.objects.exclude(state__in=TERMINAL_RUN_STATES)
        .order_by("accepted_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )
    for run_id in run_ids:
        reconcile_pipeline_run(run_id, at=observed_at)
    return {
        "membershipsExamined": len(membership_ids),
        "runsExamined": len(run_ids),
    }


def latest_current_run() -> PDFPipelineRun | None:
    """Return the newest nonterminal run, otherwise the newest terminal run."""

    active = (
        PDFPipelineRun.objects.exclude(state__in=TERMINAL_RUN_STATES)
        .order_by("-accepted_at", "-id")
        .first()
    )
    return active or PDFPipelineRun.objects.order_by("-accepted_at", "-id").first()
