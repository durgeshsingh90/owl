"""Durable repository and background synchronization state."""

import unicodedata
import uuid

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
    CHECKING_CONNECTION = "checking_connection", "Checking connection"
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


class PDFPipelineRunState(models.TextChoices):
    """End-to-end lifecycle for one accepted repository refresh/index run."""

    QUEUED = "queued", "Added to queue"
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    COMPLETE = "complete", "Complete"
    COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"
    CANCELLED = "cancelled", "Cancelled"


class PDFPipelineRunTrigger(models.TextChoices):
    """Stable reasons why a durable PDF pipeline run was accepted."""

    REPOSITORY_ADD = "repository_add", "Repository added"
    REPOSITORY_REFRESH = "repository_refresh", "Repository refresh"
    REFRESH_ALL = "refresh_all", "Refresh all"
    DAILY = "daily", "Daily refresh"
    RECOVERY = "recovery", "Recovery"
    REINDEX = "reindex", "Reindex"


class PDFPipelineRepositoryPhase(models.TextChoices):
    """Truthful current phase for one repository accepted into a pipeline run."""

    QUEUED = "queued", "Added to queue"
    CHECKING_CONNECTION = "checking_connection", "Checking connection"
    CLONING = "cloning", "Cloning"
    PULLING = "pulling", "Pulling"
    DISCOVERING = "discovering", "Discovering PDFs"
    VALIDATING = "validating", "Validating"
    HASHING = "hashing", "Hashing"
    EXTRACTING = "extracting", "Extracting"
    WRITING = "writing", "Writing"
    EXTRACTING_AND_WRITING = "extracting_and_writing", "Extracting + writing"
    REUSING_CACHED = "reusing_cached", "Reusing cached text"
    RETRY_WAIT = "retry_wait", "Waiting to retry"
    RECOVERING = "recovering", "Recovering"
    COMPLETING = "completing", "Completing"
    PAUSED = "paused", "Paused"
    COMPLETE = "complete", "Complete"
    COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"
    CANCELLED = "cancelled", "Cancelled"


class PDFPipelineCompletionKind(models.TextChoices):
    """Once-only boundary that made one PDF exact-searchable for its run."""

    NORMAL_PUBLICATION = "normal_publication", "Normal publication"
    CACHE_REUSE = "cache_reuse", "Cache reuse"


class PDFPipelineRecoveryState(models.TextChoices):
    """Durable component-recovery circuit state."""

    HEALTHY = "healthy", "Healthy"
    RETRY_WAIT = "retry_wait", "Retry wait"
    RECOVERING = "recovering", "Recovering"
    PAUSED = "paused", "Paused"
    RESUME_REQUESTED = "resume_requested", "Resume requested"
    RECOVERING_HALF_OPEN = "recovering_half_open", "Recovering half-open"


class PDFPipelineRecoveryEventKind(models.TextChoices):
    """Sparse audit events for recovery transitions and attempts."""

    EPISODE_OPENED = "episode_opened", "Episode opened"
    ATTEMPT_STARTED = "attempt_started", "Attempt started"
    ATTEMPT_FAILED = "attempt_failed", "Attempt failed"
    ATTEMPT_SUCCEEDED = "attempt_succeeded", "Attempt succeeded"
    RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
    PAUSED = "paused", "Paused"
    RESUME_REQUESTED = "resume_requested", "Resume requested"
    RECOVERED = "recovered", "Recovered"
    SUPERSEDED = "superseded", "Superseded"


class TrustedRepositoryHostSource(models.TextChoices):
    """Provenance for durable non-secret repository-host approvals."""

    UI = "ui", "Added in Settings"
    LEGACY = "legacy", "Migrated compatibility approval"


class PDFPipelineTuningAction(models.TextChoices):
    """Sparse controller recommendation/application history."""

    RECOMMEND = "recommend", "Recommend"
    APPLY = "apply", "Apply"
    ROLLBACK = "rollback", "Rollback"
    SAFETY_OVERRIDE = "safety_override", "Safety override"


class RepositoryOperationLogChannel(models.TextChoices):
    """Stable source categories for one repository's user-facing operation log."""

    GIT = "git", "Git"
    CATALOGUE = "catalogue", "Catalogue"
    INDEXING = "indexing", "PDF indexing"
    WORKER = "worker", "Background worker"


class RepositoryOperationLogSeverity(models.TextChoices):
    """Small fixed severity vocabulary safe to expose in the local log viewer."""

    DEBUG = "debug", "Debug"
    INFO = "info", "Information"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"


class BitbucketHTTPSCredentialKind(models.TextChoices):
    """Supported non-interactive Bitbucket HTTPS credential shapes."""

    CLOUD_API_TOKEN = "cloud_api_token", "Bitbucket Cloud API token"
    CLOUD_ACCESS_TOKEN = "cloud_access_token", "Bitbucket Cloud access token"
    USERNAME_TOKEN = "basic_token", "Username and access token"


class BitbucketHTTPSCredentialSource(models.TextChoices):
    """Secure backend containing one Bitbucket HTTPS credential envelope."""

    KEYRING = "keyring", "Operating-system credential store"
    DATABASE = "database", "Encrypted local database"


class BitbucketHTTPSCredentialState(models.TextChoices):
    """User-facing verification state for one saved HTTPS credential."""

    STORED_UNVERIFIED = "stored_unverified", "Stored — not verified"
    CONNECTED = "connected", "Connected"
    INVALID_CREDENTIAL = "invalid_credential", "Invalid credential"


class BitbucketHTTPSCredential(models.Model):
    """One origin-bound Bitbucket HTTPS secret with no plaintext credential fields."""

    origin = models.URLField(max_length=2048, unique=True)
    kind = models.CharField(max_length=32, choices=BitbucketHTTPSCredentialKind)
    credential_source = models.CharField(
        max_length=20,
        choices=BitbucketHTTPSCredentialSource,
        default=BitbucketHTTPSCredentialSource.DATABASE,
    )
    credential_ciphertext = models.TextField(blank=True, editable=False)
    state = models.CharField(
        max_length=32,
        choices=BitbucketHTTPSCredentialState,
        default=BitbucketHTTPSCredentialState.STORED_UNVERIFIED,
    )
    configured_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["origin"]

    def __str__(self) -> str:
        return self.origin


class TrustedRepositoryHost(models.Model):
    """One non-secret, exact HTTPS origin approved through local Settings."""

    canonical_origin = models.URLField(max_length=2048, unique=True)
    hostname = models.CharField(max_length=253, db_index=True)
    port = models.PositiveIntegerField(default=443)
    source = models.CharField(
        max_length=16,
        choices=TrustedRepositoryHostSource,
        default=TrustedRepositoryHostSource.UI,
        db_index=True,
    )
    enabled = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hostname", "port", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("hostname", "port"),
                name="bitbucket_app_unique_trusted_host_port",
            ),
            models.CheckConstraint(
                condition=models.Q(port__gte=1, port__lte=65_535),
                name="bitbucket_app_trusted_host_port_range",
            ),
        ]

    def __str__(self) -> str:
        return self.canonical_origin


class BitbucketRepository(models.Model):
    """One approved Git repository managed in OWL's private media area."""

    display_name = models.CharField(max_length=200)
    canonical_remote_key = models.CharField(max_length=1024, unique=True)
    remote_url = models.CharField(max_length=2048)
    local_path = models.CharField(max_length=1024, blank=True)
    default_branch = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True, db_index=True)
    exclude_from_refresh = models.BooleanField(default=False, db_index=True)
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
    activity_indexed_commit = models.CharField(max_length=64, blank=True)
    activity_indexed_at = models.DateTimeField(null=True, blank=True)
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
                name="bitbucket_app_repository_progress_lte_100",
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


class PDFPipelineRun(models.Model):
    """One durable accepted refresh/index generation across repositories."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trigger = models.CharField(
        max_length=32,
        choices=PDFPipelineRunTrigger,
        default=PDFPipelineRunTrigger.REPOSITORY_REFRESH,
        db_index=True,
    )
    state = models.CharField(
        max_length=32,
        choices=PDFPipelineRunState,
        default=PDFPipelineRunState.QUEUED,
        db_index=True,
    )
    accepted_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    last_progress_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    accepted_repository_count = models.PositiveIntegerField(default=0)
    schema_version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-accepted_at", "-id"]
        indexes = [models.Index(fields=("state", "-accepted_at"), name="ba_pipeline_run_state_idx")]

    def __str__(self) -> str:
        return f"{self.id} — {self.get_state_display()}"


class PDFPipelineRunRepository(models.Model):
    """Durable membership and end-to-end PDF inventory for one run repository."""

    run = models.ForeignKey(
        PDFPipelineRun,
        on_delete=models.CASCADE,
        related_name="repository_memberships",
    )
    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="pipeline_run_memberships",
    )
    lifecycle_state = models.CharField(
        max_length=32,
        choices=PDFPipelineRunState,
        default=PDFPipelineRunState.QUEUED,
        db_index=True,
    )
    phase = models.CharField(
        max_length=32,
        choices=PDFPipelineRepositoryPhase,
        default=PDFPipelineRepositoryPhase.QUEUED,
        db_index=True,
    )
    repository_revision = models.CharField(max_length=64, blank=True)
    accepted_at = models.DateTimeField(default=timezone.now, db_index=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    last_progress_at = models.DateTimeField(null=True, blank=True, db_index=True)
    inventory_final = models.BooleanField(default=False, db_index=True)
    inventory_final_at = models.DateTimeField(null=True, blank=True)
    total_pdfs = models.PositiveIntegerField(default=0)
    successful_pdfs = models.PositiveIntegerField(default=0)
    permanent_failed_pdfs = models.PositiveIntegerField(default=0)
    cancelled_pdfs = models.PositiveIntegerField(default=0)
    remaining_pdfs = models.PositiveIntegerField(default=0)
    unresolved_failures = models.PositiveIntegerField(default=0)
    terminal_outcome = models.CharField(
        max_length=32,
        choices=PDFPipelineRunState,
        blank=True,
    )
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["accepted_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("run", "repository"),
                name="bitbucket_app_unique_pipeline_run_repository",
            )
        ]
        indexes = [
            models.Index(
                fields=("run", "lifecycle_state"),
                name="ba_pipeline_member_state_idx",
            ),
            models.Index(
                fields=("repository", "-accepted_at"),
                name="ba_pipeline_repo_latest_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} — repository {self.repository_id}"


class PDFPipelineRecovery(models.Model):
    """Sparse canonical circuit state for one supervised PDF component scope."""

    scope = models.CharField(max_length=128, unique=True)
    state = models.CharField(
        max_length=32,
        choices=PDFPipelineRecoveryState,
        default=PDFPipelineRecoveryState.HEALTHY,
        db_index=True,
    )
    episode_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    generation = models.PositiveBigIntegerField(default=0)
    pause_generation = models.PositiveBigIntegerField(default=0)
    reason_family = models.CharField(max_length=64, blank=True)
    reason_code = models.CharField(max_length=64, blank=True)
    consecutive_failed_attempts = models.PositiveIntegerField(default=0)
    lifetime_attempts = models.PositiveBigIntegerField(default=0)
    pause_after_attempts = models.PositiveIntegerField(default=25)
    first_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    current_backoff_seconds = models.PositiveIntegerField(default=0)
    paused_reason = models.CharField(max_length=500, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True, db_index=True)
    popup_acknowledged_generation = models.PositiveBigIntegerField(default=0)
    popup_claimed_generation = models.PositiveBigIntegerField(default=0)
    resume_requested_at = models.DateTimeField(null=True, blank=True)
    resume_idempotency_key_hash = models.CharField(max_length=64, blank=True)
    resume_predecessor_generation = models.PositiveBigIntegerField(null=True, blank=True)
    recovered_at = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=500, blank=True)
    active_attempt_id = models.UUIDField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scope"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(popup_acknowledged_generation__lte=models.F("pause_generation")),
                name="ba_recovery_ack_lte_pause_gen",
            ),
            models.CheckConstraint(
                condition=models.Q(popup_claimed_generation__lte=models.F("pause_generation")),
                name="ba_recovery_claim_lte_pause_gen",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scope} — {self.get_state_display()}"


class PDFPipelineRecoveryEvent(models.Model):
    """Append-only redacted history for recovery attempts and transitions."""

    recovery = models.ForeignKey(
        PDFPipelineRecovery,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    correlation_id = models.UUIDField(null=True, blank=True, db_index=True, editable=False)
    attempt_id = models.UUIDField(null=True, blank=True, db_index=True)
    generation = models.PositiveBigIntegerField()
    pause_generation = models.PositiveBigIntegerField(default=0)
    kind = models.CharField(max_length=32, choices=PDFPipelineRecoveryEventKind)
    reason_code = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(max_length=500, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(
                fields=("recovery", "-occurred_at"),
                name="ba_recovery_event_time_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.recovery.scope} — {self.get_kind_display()}"


class PDFPipelineTuningEvent(models.Model):
    """Sparse, redacted controller recommendation and outcome history."""

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    mode = models.CharField(max_length=16)
    action = models.CharField(max_length=24, choices=PDFPipelineTuningAction)
    previous_target = models.PositiveSmallIntegerField()
    proposed_target = models.PositiveSmallIntegerField()
    reason_code = models.CharField(max_length=64)
    reason = models.CharField(max_length=500)
    expected_effect_code = models.CharField(max_length=64, blank=True)
    confidence = models.CharField(max_length=16)
    observation_window_seconds = models.PositiveIntegerField()
    evidence = models.JSONField(default=dict)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.previous_target} to {self.proposed_target}"


class RepositoryRemovalRecovery(models.Model):
    """Durable ownership of quarantined local data until removal fully completes."""

    repository_id = models.PositiveBigIntegerField(unique=True)
    display_name = models.CharField(max_length=200)
    quarantine_manifest = models.JSONField(default=list)
    database_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_name", "id"]

    def __str__(self) -> str:
        return f"{self.display_name} — removal pending"


class GitCommit(models.Model):
    """One Git commit retained for available history or PDF provenance."""

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
    in_activity_history = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-committed_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("repository", "commit_hash"),
                name="bitbucket_app_unique_repository_commit",
            )
        ]
        indexes = [
            models.Index(
                fields=("repository", "-committed_at"),
                name="ba_commit_repo_date_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.repository.display_name} — {self.commit_hash[:12]}"


class GitCommitFolder(models.Model):
    """One escaped direct-folder display path touched by a commit; empty means root."""

    commit = models.ForeignKey(GitCommit, on_delete=models.CASCADE, related_name="folders")
    folder_path = models.CharField(max_length=2048, blank=True)

    class Meta:
        ordering = ["folder_path", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("commit", "folder_path"),
                name="bitbucket_app_unique_commit_folder",
            )
        ]

    def __str__(self) -> str:
        return f"{self.commit_id} — {self.folder_path or '/'}"


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
                name="bitbucket_app_people_group_name_not_blank",
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
                name="bitbucket_app_unique_people_group_member",
            ),
            models.CheckConstraint(
                condition=~models.Q(normalized_person_name=""),
                name="bitbucket_app_people_member_name_not_blank",
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


class BitbucketStarredPerson(models.Model):
    """One OWL-owned star attached to a normalized Git committer identity."""

    person_name = models.CharField(max_length=255)
    normalized_person_name = models.CharField(max_length=255, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["normalized_person_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(normalized_person_name=""),
                name="bitbucket_app_starred_person_name_not_blank",
            )
        ]

    def __str__(self) -> str:
        return self.person_name

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
    starred = models.BooleanField(default=False, db_index=True)
    open_count = models.PositiveBigIntegerField(default=0)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-timeline_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("repository", "relative_path"),
                name="bitbucket_app_unique_repository_pdf_path",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(first_opened_at__isnull=True)
                    | models.Q(last_opened_at__isnull=True)
                    | models.Q(first_opened_at__lte=models.F("last_opened_at"))
                ),
                name="bitbucket_app_pdf_open_timestamps_ordered",
            ),
        ]
        indexes = [
            models.Index(
                fields=("lifecycle_state", "-timeline_at", "-id"),
                name="ba_pdf_active_timeline_idx",
            ),
            models.Index(
                fields=("repository", "lifecycle_state", "-timeline_at"),
                name="ba_pdf_repo_timeline_idx",
            ),
            models.Index(
                fields=("lifecycle_state", "index_state", "repository"),
                name="ba_pdf_index_state_idx",
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
                name="bitbucket_app_unique_pdf_local_policy_path",
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
                name="bitbucket_app_unique_pdf_text_revision",
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
                name="bitbucket_app_unique_pdf_revision_page",
            ),
            models.CheckConstraint(
                condition=models.Q(page_number__gte=1),
                name="bitbucket_app_pdf_page_number_gte_1",
            ),
        ]

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
    run_repository = models.ForeignKey(
        PDFPipelineRunRepository,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extraction_jobs",
    )
    run_id = models.UUIDField(null=True, blank=True, db_index=True)
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
    staged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    publication_started_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completion_kind = models.CharField(
        max_length=24,
        choices=PDFPipelineCompletionKind,
        blank=True,
        db_index=True,
    )
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["requested_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress__lte=100),
                name="bitbucket_app_extraction_job_progress_lte_100",
            ),
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(status__in=("queued", "running")),
                name="bitbucket_app_one_active_extraction_job_per_pdf",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "requested_at"),
                name="ba_extract_queue_idx",
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
    run_repository = models.ForeignKey(
        PDFPipelineRunRepository,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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
    worker_retry_number = models.PositiveSmallIntegerField(default=0)
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
    output_log = models.TextField(blank=True, default="")
    output_log_truncated = models.BooleanField(default=False)
    output_log_updated_at = models.DateTimeField(null=True, blank=True)
    operation_log_truncated = models.BooleanField(default=False)
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
                name="bitbucket_app_sync_job_progress_lte_100",
            ),
            models.UniqueConstraint(
                fields=("repository",),
                condition=models.Q(status__in=("queued", "running")),
                name="bitbucket_app_one_active_job_per_repository",
            ),
            models.UniqueConstraint(
                fields=(
                    "repository",
                    "scheduled_day",
                    "automatic_retry_number",
                    "worker_retry_number",
                ),
                condition=models.Q(scheduled_day__isnull=False),
                name="bitbucket_app_unique_scheduled_repository_attempt",
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
                name="bitbucket_app_sync_job_trigger_consistent",
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


class RepositoryOperationLogEntry(models.Model):
    """One immutable, cursor-ordered repository operation log record.

    Git output is sanitized before it reaches this model. Indexing messages use a
    fixed application vocabulary and reference the durable extraction job rather
    than storing PDF text or an absolute local path.
    """

    repository = models.ForeignKey(
        BitbucketRepository,
        on_delete=models.CASCADE,
        related_name="operation_log_entries",
    )
    sync_job = models.ForeignKey(
        RepositorySyncJob,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operation_log_entries",
    )
    extraction_job = models.ForeignKey(
        PDFExtractionJob,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operation_log_entries",
    )
    channel = models.CharField(max_length=16, choices=RepositoryOperationLogChannel)
    severity = models.CharField(
        max_length=8,
        choices=RepositoryOperationLogSeverity,
        default=RepositoryOperationLogSeverity.INFO,
    )
    phase = models.CharField(max_length=32, blank=True)
    event = models.CharField(max_length=64)
    message = models.CharField(max_length=1024)
    progress = models.PositiveSmallIntegerField(null=True, blank=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(sync_job__isnull=False) | models.Q(extraction_job__isnull=False)
                ),
                name="bitbucket_app_log_entry_has_job",
            ),
            models.CheckConstraint(
                condition=models.Q(progress__isnull=True) | models.Q(progress__lte=100),
                name="bitbucket_app_log_progress_lte_100",
            ),
        ]
        indexes = [
            models.Index(
                fields=("repository", "channel", "id"),
                name="ba_log_repo_channel_idx",
            ),
            models.Index(fields=("sync_job", "id"), name="ba_log_sync_cursor_idx"),
            models.Index(
                fields=("extraction_job", "id"),
                name="ba_log_extract_cursor_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.repository_id} — {self.channel} — {self.event}"
