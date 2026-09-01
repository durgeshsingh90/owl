"""Reversible local PDF exclusions and exact-target deletion with file recovery."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    PDFTextRevision,
    RepositoryOperationLogChannel,
    RepositoryOperationLogEntry,
    RepositoryOperationLogSeverity,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.logging_events import get_logger, log_event, logging_context
from bitbucket_search.services.operation_logs import build_operation_log_entry
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    pdf_extraction_claim_lock,
    repository_checkout_lock,
)
from bitbucket_search.services.repository_sync import reserve_repository_write

if TYPE_CHECKING:
    from bitbucket_search.services.document_actions import DocumentActionError

logger = get_logger("policy")
_EXPECTED_REJECTIONS = {
    "repository_busy",
    "pdf_extraction_busy",
    "repository_not_ready",
    "document_unavailable",
    "invalid_document_path",
    "invalid_pdf_policy",
    "invalid_pdf_snapshot_path",
    "pdf_delete_confirmation_required",
    "dirty_working_tree",
}
_FROZEN_STATES = (PDFLocalPolicyState.EXCLUDED, PDFLocalPolicyState.RESUMING)
_ACTIVE_REPOSITORY_STATES = (
    RepositorySyncState.QUEUED,
    RepositorySyncState.CLONING,
    RepositorySyncState.FETCHING,
    RepositorySyncState.UPDATING,
)


@contextmanager
def _logged_policy(operation: str, document_id: int):
    from bitbucket_search.services.document_actions import DocumentActionError

    started = perf_counter()
    context = {"operation": operation, "document_id": document_id}
    log_event(logger, logging.INFO, "pdf_policy_requested", **context)
    try:
        with logging_context(document_id=document_id):
            yield context
    except Exception as error:
        code = error.code if isinstance(error, DocumentActionError) else "pdf_policy_failed"
        log_event(
            logger,
            logging.WARNING if code in _EXPECTED_REJECTIONS else logging.ERROR,
            "pdf_policy_failed",
            error=error,
            error_code=code,
            elapsed_ms=round((perf_counter() - started) * 1000),
            **context,
        )
        raise
    else:
        log_event(
            logger,
            logging.INFO,
            "pdf_policy_completed",
            elapsed_ms=round((perf_counter() - started) * 1000),
            **context,
        )


def _error(code: str, summary: str) -> DocumentActionError:
    # document_actions resolves frozen paths through this module too.
    from bitbucket_search.services.document_actions import DocumentActionError

    return DocumentActionError(code, summary)


def _private_root(*, create: bool = False) -> Path:
    media_root = Path(settings.MEDIA_ROOT)
    if media_root.is_symlink() or media_root.absolute() != media_root.resolve(strict=False):
        raise _error("invalid_pdf_snapshot_path", "The private PDF storage path is not safe.")
    media_root = media_root.resolve(strict=False)
    current = media_root
    for part in (None, "bitbucket", "excluded"):
        if part is not None:
            current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise _error("invalid_pdf_snapshot_path", "The private PDF storage path is not safe.")
        if create:
            current.mkdir(mode=0o700, parents=True, exist_ok=True)
    return current


def _policy_snapshot_path(policy: PDFLocalPolicy, *, require_exists: bool = False) -> Path:
    """Return the sole allowed snapshot path; never follow a symlink."""

    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (policy.pk, policy.repository_id)
    ):
        raise _error("invalid_pdf_snapshot_path", "The private PDF snapshot is not registered.")
    root = _private_root()
    parent = root / str(policy.repository_id)
    candidate = parent / f"{policy.pk}.pdf"
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()) or candidate.is_symlink():
        raise _error("invalid_pdf_snapshot_path", "The private PDF snapshot path is not safe.")
    if candidate.exists():
        if not stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
            raise _error(
                "invalid_pdf_snapshot_path", "The private PDF snapshot is not a regular file."
            )
    elif require_exists:
        raise _error(
            "pdf_snapshot_unavailable", "The frozen PDF copy is missing from private storage."
        )
    return candidate


def policy_snapshot_path(policy: PDFLocalPolicy, *, require_exists: bool = False) -> Path:
    """Expose a safe snapshot path without propagating private filesystem errors."""

    try:
        return _policy_snapshot_path(policy, require_exists=require_exists)
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "pdf_snapshot_access_failed",
            error=exc,
            repository_id=policy.repository_id,
            document_id=policy.document_id,
            stage="snapshot_validation",
        )
        raise _error(
            "pdf_snapshot_unavailable", "OWL could not safely access the frozen PDF copy."
        ) from exc


def frozen_pdf_path(document: PDFDocument) -> Path | None:
    """Resolve an excluded/resuming document to its frozen bytes, not Git's file."""

    policy = getattr(document, "local_policy", None)
    if policy is None or policy.state not in _FROZEN_STATES:
        return None
    if (
        policy.repository_id != document.repository_id
        or policy.relative_path != document.relative_path
    ):
        raise _error("invalid_pdf_policy", "The local PDF rule does not match this document.")
    return policy_snapshot_path(policy, require_exists=True)


def _copy_regular_file(source: Path, destination: Path) -> None:
    """Copy only a regular file into a new private file, without following links."""

    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise _error("invalid_document_path", "The PDF is not a regular file.")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with (
                os.fdopen(source_fd, "rb", closefd=False) as input_file,
                os.fdopen(destination_fd, "wb", closefd=False) as output_file,
            ):
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


class _FileChanges:
    """Keep exact originals until the database commit has succeeded."""

    def __init__(self) -> None:
        self.directory: Path | None = None
        self.originals: dict[Path, Path | None] = {}
        self.original_modes: dict[Path, int] = {}

    def remember(self, target: Path) -> None:
        if target in self.originals:
            return
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise _error("invalid_document_path", "The PDF path is not a regular file.")
        if not target.exists():
            self.originals[target] = None
            return
        if self.directory is None:
            self.directory = Path(
                tempfile.mkdtemp(prefix=".pdf-action-", dir=_private_root(create=True))
            )
        backup = self.directory / f"{len(self.originals)}.pdf"
        original_mode = stat.S_IMODE(target.stat(follow_symlinks=False).st_mode)
        try:
            _copy_regular_file(target, backup)
        except Exception:
            # Never restore a partial backup over the intact original.
            try:
                backup.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "pdf_policy_cleanup_failed",
                    error=cleanup_error,
                    stage="partial_backup_cleanup",
                )
                raise
            raise
        self.originals[target] = backup
        self.original_modes[target] = original_mode

    def restore(self) -> None:
        for target, backup in self.originals.items():
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise _error(
                    "pdf_policy_rollback_failed",
                    "OWL could not restore a PDF safely. Its private recovery copy was retained.",
                )
            if backup is None:
                target.unlink(missing_ok=True)
            elif backup.exists():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(backup, target)
                os.chmod(target, self.original_modes[target])

    def discard(self) -> None:
        for backup in self.originals.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        if self.directory is not None:
            self.directory.rmdir()


def _locked_document(document_id: int) -> tuple[PDFDocument, BitbucketRepository]:
    try:
        initial = PDFDocument.objects.only("repository_id").get(pk=document_id)
        repository = BitbucketRepository.objects.select_for_update().get(pk=initial.repository_id)
        document = PDFDocument.objects.select_for_update().get(
            pk=document_id, repository=repository
        )
    except (PDFDocument.DoesNotExist, BitbucketRepository.DoesNotExist) as exc:
        raise _error("document_unavailable", "This registered PDF is no longer available.") from exc
    document.repository = repository
    if document.lifecycle_state != PDFDocumentLifecycle.ACTIVE:
        raise _error("document_unavailable", "This PDF is no longer active in its repository.")
    if (
        repository.sync_state in _ACTIVE_REPOSITORY_STATES
        or repository.sync_jobs.filter(
            status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING)
        ).exists()
    ):
        raise _error(
            "repository_busy",
            "Wait for this repository's refresh to finish before changing the PDF.",
        )
    if document.extraction_jobs.filter(status=PDFExtractionJobStatus.RUNNING).exists():
        raise _error(
            "pdf_extraction_busy",
            "Wait for this PDF's text extraction to finish before changing it.",
        )
    observed_at = timezone.now()
    queued_jobs = tuple(
        PDFExtractionJob.objects.select_for_update()
        .filter(document=document, status=PDFExtractionJobStatus.QUEUED)
        .order_by("id")
        .values(
            "id",
            "repository_sync_job_id",
            "phase",
            "progress",
            "worker_pid",
        )
    )
    if queued_jobs:
        queued_ids = tuple(job["id"] for job in queued_jobs)
        updated = PDFExtractionJob.objects.filter(
            pk__in=queued_ids,
            status=PDFExtractionJobStatus.QUEUED,
        ).update(
            status=PDFExtractionJobStatus.CANCELLED,
            completed_at=observed_at,
            error_code="pdf_local_policy_changed",
            error_summary="This PDF's local refresh rule changed before extraction started.",
        )
        if updated != len(queued_ids):
            # The checkout gate, claim gate, row locks, and SQLite writer
            # reservation make this defensive. Roll back rather than publish a
            # local policy whose queue transition is ambiguous.
            raise DatabaseError("The queued PDF extraction transition changed concurrently.")
        RepositoryOperationLogEntry.objects.bulk_create(
            [
                build_operation_log_entry(
                    repository_id=document.repository_id,
                    sync_job_id=job["repository_sync_job_id"],
                    extraction_job_id=job["id"],
                    channel=RepositoryOperationLogChannel.INDEXING,
                    severity=RepositoryOperationLogSeverity.WARNING,
                    phase=job["phase"],
                    event="indexing_cancelled",
                    message=("PDF indexing was cancelled because its local refresh rule changed."),
                    progress=job["progress"],
                    worker_pid=job["worker_pid"],
                    occurred_at=observed_at,
                )
                for job in queued_jobs
            ]
        )
    return document, repository


def _repository_id(document_id: int) -> int:
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id <= 0:
        raise _error("document_unavailable", "This registered PDF is no longer available.")
    try:
        value = (
            PDFDocument.objects.filter(pk=document_id)
            .values_list("repository_id", flat=True)
            .first()
        )
    except DatabaseError as exc:
        raise _error(
            "pdf_policy_failed", "OWL could not safely load this registered PDF. Try again."
        ) from exc
    if value is None:
        raise _error("document_unavailable", "This registered PDF is no longer available.")
    return value


def _change_pdf(document_id: int, *, delete: bool) -> PDFDocument | None:
    from bitbucket_search.services.document_actions import (
        DocumentActionError,
        validated_checkout_pdf_path,
    )
    from bitbucket_search.services.git_sync import (
        RepositorySyncError,
        apply_document_checkout_policy,
        require_clean_document_checkout,
    )

    repository_id = _repository_id(document_id)
    log_event(
        logger,
        logging.DEBUG,
        "pdf_policy_validation_started",
        repository_id=repository_id,
        document_id=document_id,
        operation="delete" if delete else "exclude",
    )
    changes = _FileChanges()
    checkout_changed = False
    repository = None
    try:
        with repository_checkout_lock(repository_id, blocking=False):
            try:
                with transaction.atomic(durable=True):
                    # Hold the global claim gate only around the queue
                    # transition, not the following snapshot and Git work.
                    # The open transaction keeps the job/document rows stable
                    # until the complete policy change commits.
                    with pdf_extraction_claim_lock():
                        # Reserve SQLite's writer before _locked_document reads
                        # the queue. A simultaneous sweep then waits and observes
                        # the completed transition instead of duplicating it.
                        reserve_repository_write()
                        document, repository = _locked_document(document_id)
                    policy = (
                        PDFLocalPolicy.objects.select_for_update().filter(document=document).first()
                    )
                    if (
                        not delete
                        and policy is not None
                        and policy.state == PDFLocalPolicyState.EXCLUDED
                    ):
                        policy_snapshot_path(policy, require_exists=True)
                        log_event(
                            logger,
                            logging.DEBUG,
                            "pdf_policy_already_excluded",
                            repository_id=repository_id,
                            document_id=document_id,
                        )
                        return document
                    if (
                        not delete
                        and policy is None
                        and repository.sync_state != RepositorySyncState.READY
                    ):
                        raise _error(
                            "repository_not_ready",
                            "Refresh this repository successfully before freezing a PDF, so its file and indexed information match.",
                        )
                    working_path = validated_checkout_pdf_path(document, allow_missing=True)
                    require_clean_document_checkout(repository)
                    changes.remember(working_path)
                    if policy is None:
                        policy = PDFLocalPolicy.objects.create(
                            repository=repository,
                            relative_path=document.relative_path,
                            document=document,
                            state=PDFLocalPolicyState.DELETED
                            if delete
                            else PDFLocalPolicyState.EXCLUDED,
                        )
                    elif (
                        policy.repository_id != repository.pk
                        or policy.relative_path != document.relative_path
                    ):
                        raise _error(
                            "invalid_pdf_policy", "The local PDF rule does not match this document."
                        )
                    snapshot = policy_snapshot_path(policy)
                    changes.remember(snapshot)
                    if not delete and not snapshot.exists():
                        _private_root(create=True)
                        snapshot.parent.mkdir(mode=0o700, exist_ok=True)
                        _copy_regular_file(working_path, snapshot)
                    policy.state = (
                        PDFLocalPolicyState.DELETED if delete else PDFLocalPolicyState.EXCLUDED
                    )
                    policy.save(update_fields=("state", "updated_at"))
                    checkout_changed = True
                    apply_document_checkout_policy(repository)
                    if working_path.exists() or working_path.is_symlink():
                        raise _error(
                            "pdf_policy_checkout_failed",
                            "Git could not safely remove this PDF from the refresh working tree.",
                        )
                    if delete:
                        snapshot.unlink(missing_ok=True)
                        revision_id = document.indexed_revision_id
                        removed_bytes = document.file_size
                        document.delete()
                        if revision_id is not None:
                            revision = (
                                PDFTextRevision.objects.select_for_update()
                                .filter(pk=revision_id)
                                .first()
                            )
                            if revision is not None and not revision.documents.exists():
                                revision.delete()
                        repository.pdf_count = repository.pdf_documents.filter(
                            lifecycle_state=PDFDocumentLifecycle.ACTIVE
                        ).count()
                        repository.document_bytes = max(
                            0, repository.document_bytes - removed_bytes
                        )
                        repository.save(update_fields=("pdf_count", "document_bytes", "updated_at"))
                        document = None
            except Exception as exc:
                restoration_failed = False
                if checkout_changed and repository is not None:
                    try:
                        apply_document_checkout_policy(repository)
                    except Exception as restore_error:
                        log_event(
                            logger,
                            logging.ERROR,
                            "pdf_policy_rollback_failed",
                            error=restore_error,
                            repository_id=repository_id,
                            document_id=document_id,
                            stage="checkout_restore",
                        )
                        restoration_failed = True
                try:
                    changes.restore()
                except Exception as restore_error:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_policy_rollback_failed",
                        error=restore_error,
                        repository_id=repository_id,
                        document_id=document_id,
                        stage="file_restore",
                    )
                    restoration_failed = True
                if restoration_failed:
                    raise _error(
                        "pdf_policy_rollback_failed",
                        "The PDF change could not be completed safely. Original file data was retained for recovery; refresh this repository after resolving its local state.",
                    ) from exc
                try:
                    changes.discard()
                except OSError as cleanup_error:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_policy_cleanup_failed",
                        error=cleanup_error,
                        repository_id=repository_id,
                        document_id=document_id,
                        stage="rollback_cleanup",
                    )
                    raise
                if isinstance(exc, DocumentActionError):
                    raise
                if isinstance(exc, RepositorySyncError):
                    raise _error(exc.code, exc.summary) from exc
                raise _error(
                    "pdf_policy_failed",
                    "OWL could not complete this PDF change. Its previous data was preserved.",
                ) from exc
            try:
                changes.discard()
            except OSError as exc:
                raise _error(
                    "pdf_policy_cleanup_failed",
                    "The PDF change was recorded, but a private recovery copy could not be removed. The operation is not fully complete.",
                ) from exc
            return document
    except RepositoryCheckoutBusy as exc:
        raise _error(
            "repository_busy",
            "Wait for this repository's current operation to finish before changing the PDF.",
        ) from exc
    except OSError as exc:
        raise _error(
            "pdf_policy_storage_unavailable",
            "OWL could not safely access the PDF's private storage.",
        ) from exc


def exclude_registered_pdf(document_id: int) -> PDFDocument:
    """Freeze this PDF's current bytes and exclude its Git path from refresh."""

    with _logged_policy("exclude", document_id) as context:
        document = _change_pdf(document_id, delete=False)
        assert document is not None
        context.update(repository_id=document.repository_id, status="excluded")
        return document


def resume_registered_pdf(document_id: int) -> PDFDocument:
    """Keep frozen bytes readable until a later successful refresh replaces them."""

    with _logged_policy("resume", document_id) as context:
        document = _resume_registered_pdf(document_id)
        context.update(repository_id=document.repository_id, status="awaiting_refresh")
        return document


def _resume_registered_pdf(document_id: int) -> PDFDocument:
    try:
        with (
            repository_checkout_lock(_repository_id(document_id), blocking=False),
            transaction.atomic(durable=True),
        ):
            with pdf_extraction_claim_lock():
                reserve_repository_write()
                document, _repository = _locked_document(document_id)
            policy = PDFLocalPolicy.objects.select_for_update().filter(document=document).first()
            if policy is None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "pdf_policy_already_included",
                    repository_id=document.repository_id,
                    document_id=document_id,
                )
                return document
            if policy.state not in _FROZEN_STATES:
                raise _error(
                    "invalid_pdf_policy",
                    "This PDF cannot resume refreshing from its current local state.",
                )
            if (
                policy.repository_id != document.repository_id
                or policy.relative_path != document.relative_path
            ):
                raise _error(
                    "invalid_pdf_policy", "The local PDF rule does not match this document."
                )
            policy_snapshot_path(policy, require_exists=True)
            policy.state = PDFLocalPolicyState.RESUMING
            policy.save(update_fields=("state", "updated_at"))
            return document
    except RepositoryCheckoutBusy as exc:
        raise _error(
            "repository_busy",
            "Wait for this repository's current operation to finish before changing the PDF.",
        ) from exc
    except OSError as exc:
        raise _error(
            "pdf_policy_storage_unavailable",
            "OWL could not safely access the PDF's private storage.",
        ) from exc
    except DatabaseError as exc:
        raise _error(
            "pdf_policy_failed",
            "OWL could not resume refreshing this PDF. Its frozen copy is unchanged.",
        ) from exc


def delete_registered_pdf(document_id: int, *, confirmed: bool = False) -> None:
    """Delete only the selected local PDF and its unshared registered text."""

    with _logged_policy("delete", document_id) as context:
        if confirmed is not True:
            raise _error(
                "pdf_delete_confirmation_required",
                "Confirm deletion of this PDF and its local indexed data first.",
            )
        _change_pdf(document_id, delete=True)
        context.update(status="deleted", removed_count=1)


def complete_resumed_policies(repository_id: int, relative_paths) -> None:
    """Clear resumed rules in the publisher transaction; clean snapshots after commit."""

    policies = tuple(
        PDFLocalPolicy.objects.select_for_update().filter(
            repository_id=repository_id,
            relative_path__in=tuple(relative_paths),
            state=PDFLocalPolicyState.RESUMING,
        )
    )
    for policy in policies:
        snapshot = policy_snapshot_path(policy)
        policy_id = policy.pk
        policy.delete()

        def cleanup(path=snapshot, target_repository_id=repository_id, target_policy_id=policy_id):
            try:
                checked = policy_snapshot_path(
                    PDFLocalPolicy(id=target_policy_id, repository_id=target_repository_id)
                )
                if checked != path:
                    log_event(
                        logger,
                        logging.ERROR,
                        "pdf_snapshot_cleanup_failed",
                        repository_id=target_repository_id,
                        policy_id=target_policy_id,
                        reason="snapshot_path_mismatch",
                    )
                    return
                checked.unlink(missing_ok=True)
            except Exception as error:
                # A completed catalogue remains authoritative. Never leak raw
                # filesystem errors or make a committed sync look rolled back.
                log_event(
                    logger,
                    logging.ERROR,
                    "pdf_snapshot_cleanup_failed",
                    error=error,
                    repository_id=target_repository_id,
                    policy_id=target_policy_id,
                    operation="resume",
                    stage="post_commit_cleanup",
                )

        transaction.on_commit(cleanup)
    if policies:
        log_event(
            logger,
            logging.DEBUG,
            "pdf_policies_resume_staged",
            repository_id=repository_id,
            count=len(policies),
        )
        transaction.on_commit(
            lambda: log_event(
                logger,
                logging.INFO,
                "pdf_policies_resumed",
                repository_id=repository_id,
                count=len(policies),
            )
        )
