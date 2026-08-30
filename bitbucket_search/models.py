"""Durable repository and background synchronization state."""

import unicodedata

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class InvalidPeopleName(ValueError):
    """Raised when a persisted People label cannot be represented safely."""


def canonical_people_name(value: object) -> str:
    """Return one safe NFKC, single-line People label with display casing intact."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in normalized):
        raise InvalidPeopleName("People names cannot contain control characters.")
    display_name = " ".join(normalized.split())
    if not display_name:
        raise InvalidPeopleName("People names cannot be empty.")
    if len(display_name) > 255 or len(display_name.casefold()) > 255:
        raise InvalidPeopleName("People names cannot exceed 255 characters.")
    return display_name


def normalize_people_name(value: object) -> str:
    """Return OWL's Unicode-aware, case-insensitive identity for a People label."""

    return canonical_people_name(value).casefold()


class RepositorySyncState(models.TextChoices):
    """Current user-facing state of one managed repository."""

    NOT_CLONED = "not_cloned", "Not cloned"
    QUEUED = "queued", "Queued"
    CLONING = "cloning", "Cloning"
    FETCHING = "fetching", "Fetching"
    UPDATING = "updating", "Updating"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"
    DISABLED = "disabled", "Disabled"
    BLOCKED_DIRTY = "blocked_dirty", "Blocked by local changes"


class RepositorySyncJobStatus(models.TextChoices):
    """Lifecycle state for one durable repository synchronization job."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"
    CANCELLED = "cancelled", "Cancelled"


class RepositorySyncOperation(models.TextChoices):
    """Git operation selected when a repository job is queued."""

    CLONE = "clone", "Clone"
    REFRESH = "refresh", "Refresh"


class RepositorySyncTrigger(models.TextChoices):
    """Why one durable repository synchronization job was queued."""

    MANUAL = "manual", "Manual"
    DAILY = "daily", "Daily"
    RETRY = "retry", "Automatic retry"


class RepositorySyncPhase(models.TextChoices):
    """Fine-grained progress phase exposed by the background worker."""

    QUEUED = "queued", "Queued"
    VALIDATING = "validating", "Validating"
    CLONING = "cloning", "Cloning"
    FETCHING = "fetching", "Fetching"
    UPDATING = "updating", "Updating working tree"
    DISCOVERING = "discovering", "Discovering documents"
    FINALIZING = "finalizing", "Finalizing"
    COMPLETED = "completed", "Completed"


class PDFDocumentLifecycle(models.TextChoices):
    """Whether a discovered PDF still exists at the repository's published commit."""

    ACTIVE = "active", "Active"
    REMOVED = "removed", "Removed"


class PDFLocalPolicyState(models.TextChoices):
    """A local per-path override that remains independent of Git history."""

    EXCLUDED = "excluded", "Excluded from refresh"
    DELETED = "deleted", "Deleted locally"
    RESUMING = "resuming", "Waiting to resume refresh"


class PDFDocumentAddedEvidence(models.TextChoices):
    """Coverage behind the recorded Git addition attribution."""

    CONFIRMED = "confirmed", "Confirmed"
    BEFORE_AVAILABLE_HISTORY = (
        "before_available_history",
        "Before available history",
    )
    NOT_FOUND = "not_found", "Not found"


class PDFDocumentTimelineBasis(models.TextChoices):
    """Truthful source used to place a PDF in the chronological result list."""

    GIT_ADDED = "git_added", "Git addition"
    OWL_DISCOVERED = "owl_discovered", "Discovered by OWL"


class PDFIndexState(models.TextChoices):
    """Relationship between one PDF and its last atomically published text."""

    PENDING = "pending", "Pending"
    READY = "ready", "Indexed"
    NO_TEXT = "no_text", "No readable text"
    PARTIAL = "partial", "Partially indexed"
    FAILED = "failed", "Index failed"
    STALE_ERROR = "stale_error", "Stale searchable text"


class PDFTextRevisionState(models.TextChoices):
    """Terminal extraction quality for immutable, reusable PDF text."""

    READY = "ready", "Indexed"
    NO_TEXT = "no_text", "No readable text"
    PARTIAL = "partial", "Partially indexed"


class PDFPageExtractionState(models.TextChoices):
    """Extraction outcome for one one-based page in a text revision."""

    READY = "ready", "Indexed"
    NO_TEXT = "no_text", "No readable text"
    FAILED = "failed", "Extraction failed"


class PDFExtractionJobStatus(models.TextChoices):
    """Durable lifecycle for one PDF revision extraction request."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"
    CANCELLED = "cancelled", "Cancelled"


class PDFExtractionJobPhase(models.TextChoices):
    """Fine-grained extraction phase exposed to the local progress UI."""

    QUEUED = "queued", "Queued"
    VALIDATING = "validating", "Validating"
    HASHING = "hashing", "Hashing"
    EXTRACTING = "extracting", "Extracting"
    PUBLISHING = "publishing", "Publishing"
    COMPLETED = "completed", "Completed"


class BitbucketRepository(models.Model):
    """One approved Git repository managed in OWL's private media area."""

    display_name = models.CharField(max_length=200)
    canonical_remote_key = models.CharField(max_length=1024, unique=True)
    remote_url = models.CharField(max_length=2048)
    local_path = models.CharField(max_length=1024, blank=True)
    default_branch = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True, db_index=True)
    sync_state = models.CharField(
        max_length=32,
        choices=RepositorySyncState,
        default=RepositorySyncState.NOT_CLONED,
        db_index=True,
    )
    sync_progress = models.PositiveSmallIntegerField(default=0)
    status_message = models.CharField(max_length=500, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_summary = models.CharField(max_length=500, blank=True)
    pdf_count = models.PositiveIntegerField(default=0)
    vsdx_count = models.PositiveIntegerField(default=0)
    document_bytes = models.PositiveBigIntegerField(default=0)
    last_synced_commit = models.CharField(max_length=64, blank=True)
    history_is_shallow = models.BooleanField(default=True)
    metadata_indexed_commit = models.CharField(max_length=64, blank=True)
    last_sync_started_at = models.DateTimeField(null=True, blank=True)
    last_sync_completed_at = models.DateTimeField(null=True, blank=True)
    last_sync_successful_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sync_progress__lte=100),
                name="bitbucket_repository_progress_lte_100",
            )
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def document_count(self) -> int:
        return self.pdf_count + self.vsdx_count

    @property
    def has_active_sync(self) -> bool:
        return bool(getattr(self, "_has_active_sync_job", False)) or self.sync_state in {
            RepositorySyncState.QUEUED,
            RepositorySyncState.CLONING,
            RepositorySyncState.FETCHING,
            RepositorySyncState.UPDATING,
        }


class GitCommit(models.Model):
    """One commit reachable in a repository's locally available branch history."""

    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="git_commits",
    )
    commit_hash = models.CharField(max_length=64)
    author_name = models.CharField(max_length=255)
    committer_name = models.CharField(max_length=255)
    authored_at = models.DateTimeField()
    committed_at = models.DateTimeField()
    is_shallow_boundary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-committed_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("repository", "commit_hash"),
                name="bitbucket_unique_repository_commit",
            )
        ]
        indexes = [
            models.Index(
                fields=("repository", "-committed_at"),
                name="bb_commit_repo_date_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.repository.display_name} — {self.commit_hash[:12]}"


class BitbucketPeopleGroup(models.Model):
    """A named, user-owned set of Git committer identities."""

    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(normalized_name=""),
                name="bitbucket_people_group_name_not_blank",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "name",
                "normalized_name",
                "updated_at",
            }
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        try:
            self.name = canonical_people_name(self.name)
        except InvalidPeopleName as error:
            raise ValidationError({"name": str(error)}) from error
        self.normalized_name = self.name.casefold()


class BitbucketPeopleGroupMember(models.Model):
    """One normalized Git committer identity retained in a People group."""

    group = models.ForeignKey(
        BitbucketPeopleGroup,
        on_delete=models.CASCADE,
        related_name="members",
    )
    person_name = models.CharField(max_length=255)
    normalized_person_name = models.CharField(max_length=255, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["normalized_person_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("group", "normalized_person_name"),
                name="bitbucket_unique_people_group_member",
            ),
            models.CheckConstraint(
                condition=~models.Q(normalized_person_name=""),
                name="bitbucket_people_member_name_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.group.name} — {self.person_name}"

    def save(self, *args, **kwargs):
        self.full_clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "person_name",
                "normalized_person_name",
            }
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        try:
            self.person_name = canonical_people_name(self.person_name)
        except InvalidPeopleName as error:
            raise ValidationError({"person_name": str(error)}) from error
        self.normalized_person_name = self.person_name.casefold()


class PDFDocument(models.Model):
    """One durable PDF identity within a managed repository."""

    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="pdf_documents",
    )
    filename = models.CharField(max_length=512)
    relative_path = models.CharField(max_length=2048)
    file_size = models.PositiveBigIntegerField(default=0)
    git_blob_id = models.CharField(max_length=64, blank=True)
    lifecycle_state = models.CharField(
        max_length=16,
        choices=PDFDocumentLifecycle,
        default=PDFDocumentLifecycle.ACTIVE,
        db_index=True,
    )
    discovered_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    removed_at = models.DateTimeField(null=True, blank=True)
    last_seen_commit = models.CharField(max_length=64, blank=True)
    added_evidence = models.CharField(
        max_length=32,
        choices=PDFDocumentAddedEvidence,
        default=PDFDocumentAddedEvidence.NOT_FOUND,
    )
    added_commit = models.ForeignKey(
        GitCommit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_documents",
    )
    last_commit = models.ForeignKey(
        GitCommit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_changed_documents",
    )
    timeline_at = models.DateTimeField(default=timezone.now, db_index=True)
    timeline_basis = models.CharField(
        max_length=24,
        choices=PDFDocumentTimelineBasis,
        default=PDFDocumentTimelineBasis.OWL_DISCOVERED,
    )
    content_sha256 = models.CharField(max_length=64, blank=True)
    indexed_revision = models.ForeignKey(
        "PDFTextRevision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    indexed_git_blob_id = models.CharField(max_length=64, blank=True)
    indexed_source_commit = models.CharField(max_length=64, blank=True)
    page_count = models.PositiveIntegerField(default=0)
    extracted_character_count = models.PositiveBigIntegerField(default=0)
    index_state = models.CharField(
        max_length=24,
        choices=PDFIndexState,
        default=PDFIndexState.PENDING,
        db_index=True,
    )
    first_indexed_at = models.DateTimeField(null=True, blank=True)
    last_indexed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_index_attempt_at = models.DateTimeField(null=True, blank=True)
    index_version = models.PositiveIntegerField(default=0)
    extractor_version = models.CharField(max_length=64, blank=True)
    extraction_error_code = models.CharField(max_length=64, blank=True)
    extraction_error_summary = models.CharField(max_length=500, blank=True)
    open_count = models.PositiveBigIntegerField(default=0)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-timeline_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("repository", "relative_path"),
                name="bitbucket_unique_repository_pdf_path",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(first_opened_at__isnull=True)
                    | models.Q(last_opened_at__isnull=True)
                    | models.Q(first_opened_at__lte=models.F("last_opened_at"))
                ),
                name="bitbucket_pdf_open_timestamps_ordered",
            ),
        ]
        indexes = [
            models.Index(
                fields=("lifecycle_state", "-timeline_at", "-id"),
                name="bb_pdf_active_timeline_idx",
            ),
            models.Index(
                fields=("repository", "lifecycle_state", "-timeline_at"),
                name="bb_pdf_repo_timeline_idx",
            ),
            models.Index(
                fields=("lifecycle_state", "index_state", "repository"),
                name="bb_pdf_index_state_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.repository.display_name} — {self.relative_path}"


class PDFLocalPolicy(models.Model):
    """A minimal exclusion rule, retained as a tombstone after local deletion."""

    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="pdf_local_policies",
    )
    relative_path = models.CharField(max_length=2048)
    document = models.OneToOneField(
        PDFDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="local_policy",
    )
    state = models.CharField(max_length=16, choices=PDFLocalPolicyState)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["repository_id", "relative_path", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("repository", "relative_path"),
                name="bitbucket_unique_pdf_local_policy_path",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.repository_id} — {self.relative_path} — {self.get_state_display()}"


class PDFTextRevision(models.Model):
    """Immutable extracted text reusable by byte-identical PDF documents."""

    content_sha256 = models.CharField(max_length=64)
    extractor_version = models.CharField(max_length=64)
    source_byte_size = models.PositiveBigIntegerField(default=0)
    state = models.CharField(max_length=16, choices=PDFTextRevisionState)
    page_count = models.PositiveIntegerField(default=0)
    extracted_character_count = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("content_sha256", "extractor_version"),
                name="bitbucket_unique_pdf_text_revision",
            )
        ]

    def __str__(self) -> str:
        return f"{self.content_sha256[:12]} — {self.get_state_display()}"


class PDFTextPage(models.Model):
    """Canonical normalized text for one one-based page of a revision."""

    revision = models.ForeignKey(
        PDFTextRevision,
        on_delete=models.CASCADE,
        related_name="pages",
    )
    page_number = models.PositiveIntegerField()
    extracted_text = models.TextField(blank=True)
    character_count = models.PositiveIntegerField(default=0)
    extraction_state = models.CharField(max_length=16, choices=PDFPageExtractionState)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["page_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("revision", "page_number"),
                name="bitbucket_unique_pdf_revision_page",
            ),
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1),
                name="bitbucket_pdf_page_number_gte_1",
            ),
        ]
        indexes = [models.Index(fields=("revision", "page_number"), name="bb_pdf_page_lookup_idx")]

    def __str__(self) -> str:
        return f"{self.revision_id} — page {self.page_number}"


class PDFExtractionJob(models.Model):
    """One durable request to extract and publish a PDF revision."""

    document = models.ForeignKey(
        PDFDocument,
        on_delete=models.CASCADE,
        related_name="extraction_jobs",
    )
    repository_sync_job = models.ForeignKey(
        "RepositorySyncJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extraction_jobs",
    )
    target_git_blob_id = models.CharField(max_length=64)
    target_source_commit = models.CharField(max_length=64)
    target_relative_path = models.CharField(max_length=2048)
    target_file_size = models.PositiveBigIntegerField(default=0)
    target_extractor_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=PDFExtractionJobStatus,
        default=PDFExtractionJobStatus.QUEUED,
        db_index=True,
    )
    phase = models.CharField(
        max_length=24,
        choices=PDFExtractionJobPhase,
        default=PDFExtractionJobPhase.QUEUED,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    retry_count = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    pages_processed = models.PositiveIntegerField(default=0)
    characters_extracted = models.PositiveBigIntegerField(default=0)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["requested_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress__lte=100),
                name="bitbucket_extraction_job_progress_lte_100",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(status__in=("queued", "running")),
                name="bitbucket_one_active_extraction_job_per_pdf",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "requested_at"),
                name="bb_extract_queue_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_id} — {self.get_status_display()}"

    @property
    def is_active(self) -> bool:
        return self.status in {
            PDFExtractionJobStatus.QUEUED,
            PDFExtractionJobStatus.RUNNING,
        }


class RepositorySyncJob(models.Model):
    """Durable progress and diagnostics for one background clone or refresh."""

    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="sync_jobs",
    )
    operation = models.CharField(max_length=16, choices=RepositorySyncOperation)
    trigger = models.CharField(
        max_length=16,
        choices=RepositorySyncTrigger,
        default=RepositorySyncTrigger.MANUAL,
        db_index=True,
    )
    scheduled_day = models.DateField(null=True, blank=True, db_index=True)
    automatic_retry_number = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=RepositorySyncJobStatus,
        default=RepositorySyncJobStatus.QUEUED,
        db_index=True,
    )
    phase = models.CharField(
        max_length=32,
        choices=RepositorySyncPhase,
        default=RepositorySyncPhase.QUEUED,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    status_message = models.CharField(max_length=500, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    source_commit = models.CharField(max_length=64, blank=True)
    result_commit = models.CharField(max_length=64, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress__lte=100),
                name="bitbucket_sync_job_progress_lte_100",
            ),
            models.UniqueConstraint(
                fields=("repository",),
                condition=models.Q(status__in=("queued", "running")),
                name="bitbucket_one_active_job_per_repository",
            ),
            models.UniqueConstraint(
                fields=("repository", "scheduled_day", "automatic_retry_number"),
                condition=models.Q(scheduled_day__isnull=False),
                name="bitbucket_unique_scheduled_repository_attempt",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        trigger=RepositorySyncTrigger.MANUAL,
                        scheduled_day__isnull=True,
                        automatic_retry_number=0,
                    )
                    | models.Q(
                        trigger=RepositorySyncTrigger.DAILY,
                        scheduled_day__isnull=False,
                        automatic_retry_number=0,
                    )
                    | models.Q(
                        trigger=RepositorySyncTrigger.RETRY,
                        scheduled_day__isnull=False,
                        automatic_retry_number__gte=1,
                    )
                ),
                name="bitbucket_sync_job_trigger_consistent",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.repository.display_name} — "
            f"{self.get_operation_display()} {self.get_status_display()}"
        )

    @property
    def is_active(self) -> bool:
        return self.status in {
            RepositorySyncJobStatus.QUEUED,
            RepositorySyncJobStatus.RUNNING,
        }
