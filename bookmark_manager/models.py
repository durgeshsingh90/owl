from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def _canonical_personal_name(value: object) -> str:
    """Return a safe, human-readable label without changing its chosen casing."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def _normalized_personal_name(value: object) -> str:
    return _canonical_personal_name(value).casefold()


def _sanitized_single_line(value: object, *, fallback: str = "") -> str:
    """Keep import diagnostics useful without retaining control characters or paths."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(character if character.isprintable() else " " for character in normalized)
    sanitized = " ".join(normalized.split())
    return sanitized or fallback


class CredentialSource(models.TextChoices):
    NONE = "none", "Not configured"
    KEYRING = "keyring", "Operating-system credential store"
    DATABASE = "database", "Encrypted local database"
    ENVIRONMENT = "environment", "Managed externally"


class ConnectionStatus(models.TextChoices):
    NOT_CONFIGURED = "not_configured", "Not configured"
    STORED_UNVERIFIED = "stored_unverified", "Stored — not verified"
    CONNECTED = "connected", "Connected"
    INVALID_CREDENTIAL = "invalid_credential", "Invalid credential"
    ACCESS_DENIED = "access_denied", "Access denied"
    RATE_LIMITED = "rate_limited", "Rate limited"
    UNREACHABLE = "unreachable", "Unreachable"
    UNSUPPORTED_RESPONSE = "unsupported_response", "Unsupported response"
    CREDENTIAL_STORE_UNAVAILABLE = (
        "credential_store_unavailable",
        "Credential store unavailable",
    )
    MANAGED_EXTERNALLY = "managed_externally", "Managed externally"
    CONFIGURATION_ERROR = "configuration_error", "Configuration error"


class ConfluenceConfiguration(models.Model):
    """The single local Confluence profile and optional encrypted credential payload."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    base_url = models.URLField(max_length=2048, blank=True)
    auth_mode = models.CharField(max_length=32, default="bearer")
    credential_source = models.CharField(
        max_length=20,
        choices=CredentialSource,
        default=CredentialSource.NONE,
    )
    credential_ciphertext = models.TextField(blank=True, editable=False)
    connection_status = models.CharField(
        max_length=32,
        choices=ConnectionStatus,
        default=ConnectionStatus.NOT_CONFIGURED,
    )
    configured_at = models.DateTimeField(null=True, blank=True)
    last_test_attempt_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Confluence configuration"
        verbose_name_plural = "Confluence configuration"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="bookmark_manager_single_confluence_configuration",
            )
        ]

    def __str__(self) -> str:
        return self.get_connection_status_display()

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)


class BookmarkAvailability(models.TextChoices):
    ACTIVE = "active", "Active"
    NOT_FOUND = "not_found", "Not found"
    ACCESS_DENIED = "access_denied", "Access denied"
    AUTH_ERROR = "auth_error", "Authentication error"
    REFRESH_ERROR = "refresh_error", "Refresh error"


class BookmarkRecency(models.TextChoices):
    """Calculated bookmark freshness; values are never persisted on Bookmark."""

    NEW = "new", "New"
    UPDATED = "updated", "Updated"
    NORMAL = "normal", "Normal"


class BookmarkSource(models.TextChoices):
    CONFLUENCE = "confluence", "Confluence"
    WEB = "web", "Web"


class BookmarkCategory(models.Model):
    """A user-renamable group whose stable identity is a URL hostname."""

    domain = models.CharField(max_length=253, unique=True)
    name = models.CharField(max_length=253)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "domain", "id"]
        verbose_name_plural = "bookmark categories"

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"domain", "name"}
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.domain = str(self.domain or "").strip().rstrip(".").casefold()
        self.name = _canonical_personal_name(self.name)
        errors: dict[str, str] = {}
        if not self.domain or len(self.domain) > 253:
            errors["domain"] = "Enter a valid domain name."
        if not self.name:
            errors["name"] = "A category name cannot be empty."
        elif len(self.name) > 253:
            errors["name"] = "A category name cannot exceed 253 characters."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def default_name_for_url(cls, url: str) -> str:
        hostname = (urlsplit(url).hostname or "").casefold()
        return hostname.removeprefix("www.") or hostname


class BookmarkImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures", "Completed with failures"
    FAILED = "failed", "Failed"


class BookmarkRefreshStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    SUCCEEDED_WITH_ERRORS = "succeeded_with_errors", "Succeeded with errors"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"


class BookmarkActivityType(models.TextChoices):
    """Exact local activity kinds retained as daily aggregate counters."""

    ADDED = "added", "Added"
    OPENED = "opened", "Opened"
    REFRESHED = "refreshed", "Refreshed"
    NOTES = "notes", "Notes updated"


class ConfluencePageNode(models.Model):
    """One real node in the locally reconstructed Confluence hierarchy.

    A node is source-owned hierarchy metadata. It can exist solely because it is an
    ancestor of a saved bookmark, in which case it deliberately has no bookmark row.
    Every node still receives a local outline position so the whole tree can use
    Word-style dotted numbering.
    """

    page_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    provisional_key = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=2048, blank=True)
    space_key = models.CharField(max_length=255, blank=True, db_index=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    sibling_position = models.PositiveIntegerField(null=True, blank=True)
    outline_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text="Stable local position used for Word-style bookmark numbering.",
    )
    metadata_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["parent_id", "sibling_position", "title", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(page_id__isnull=False) | models.Q(provisional_key__isnull=False)
                ),
                name="bookmark_node_has_source_or_provisional_identity",
            ),
            models.CheckConstraint(
                condition=~models.Q(id=models.F("parent_id")),
                name="bookmark_node_is_not_its_own_parent",
            ),
            models.UniqueConstraint(
                fields=["outline_position"],
                condition=models.Q(
                    parent__isnull=True,
                    outline_position__isnull=False,
                ),
                name="bmk_node_root_outline_uniq",
            ),
            models.UniqueConstraint(
                fields=["parent", "outline_position"],
                condition=models.Q(
                    parent__isnull=False,
                    outline_position__isnull=False,
                ),
                name="bmk_node_child_outline_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["parent", "sibling_position", "title"],
                name="bookmark_node_tree_order",
            ),
            models.Index(
                fields=["parent", "outline_position"],
                name="bookmark_node_outline_order",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class TagManager(models.Manager):
    def get_or_create_normalized(self, name: object) -> tuple[Tag, bool]:
        """Get or create a tag using OWL's Unicode-aware case-insensitive identity."""

        display_name = _canonical_personal_name(name)
        normalized_name = _normalized_personal_name(display_name)
        if not normalized_name:
            raise ValidationError({"name": "A tag name cannot be empty."})
        if len(display_name) > Tag._meta.get_field("name").max_length:
            raise ValidationError({"name": "A tag name cannot exceed 100 characters."})
        return self.get_or_create(
            normalized_name=normalized_name,
            defaults={"name": display_name},
        )


class Tag(models.Model):
    """A user-owned bookmark label with one Unicode-aware, casefolded identity."""

    name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=255, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TagManager()

    class Meta:
        ordering = ["normalized_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(normalized_name=""),
                name="bookmark_tag_normalized_name_not_blank",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"name", "normalized_name"}
        return super().save(*args, **kwargs)

    @staticmethod
    def normalize_name(value: object) -> str:
        return _normalized_personal_name(value)

    def clean(self) -> None:
        super().clean()
        self.name = _canonical_personal_name(self.name)
        self.normalized_name = self.normalize_name(self.name)
        if not self.normalized_name:
            raise ValidationError({"name": "A tag name cannot be empty."})
        if len(self.name) > self._meta.get_field("name").max_length:
            raise ValidationError({"name": "A tag name cannot exceed 100 characters."})


class SavedBookmarkView(models.Model):
    """A durable local bookmark query, excluding transient tree and selection state."""

    name = models.CharField(max_length=120)
    normalized_name = models.CharField(max_length=255, unique=True, editable=False)
    search_text = models.TextField(blank=True)
    filters = models.JSONField(default=dict, blank=True)
    sort = models.CharField(max_length=64, default="added_newest")
    visible_columns = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(normalized_name=""),
                name="bookmark_saved_view_name_not_blank",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "name",
                "normalized_name",
                "search_text",
                "sort",
                "updated_at",
            }
        return super().save(*args, **kwargs)

    @staticmethod
    def normalize_name(value: object) -> str:
        return _normalized_personal_name(value)

    def clean(self) -> None:
        super().clean()
        self.name = _canonical_personal_name(self.name)
        self.normalized_name = self.normalize_name(self.name)
        self.search_text = str(self.search_text or "").strip()
        self.sort = str(self.sort or "").strip()

        errors: dict[str, str] = {}
        if not self.normalized_name:
            errors["name"] = "A saved-view name cannot be empty."
        elif len(self.name) > self._meta.get_field("name").max_length:
            errors["name"] = "A saved-view name cannot exceed 120 characters."
        if not self.sort:
            errors["sort"] = "A saved view must include a sort key."
        if not isinstance(self.filters, dict):
            errors["filters"] = "Saved-view filters must be a JSON object."
        if not isinstance(self.visible_columns, list) or any(
            not isinstance(column, str) for column in self.visible_columns
        ):
            errors["visible_columns"] = "Visible columns must be a JSON list of names."
        if errors:
            raise ValidationError(errors)


class BookmarkImportRun(models.Model):
    """Progress and sanitized outcome for one explicitly requested JSON import."""

    filename = models.CharField(max_length=255)
    schema_version = models.CharField(max_length=32, default="legacy")
    source_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    total_records = models.PositiveIntegerField(default=0)
    processed_records = models.PositiveIntegerField(default=0)
    imported_records = models.PositiveIntegerField(default=0)
    skipped_records = models.PositiveIntegerField(default=0)
    failed_records = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=32,
        choices=BookmarkImportStatus,
        default=BookmarkImportStatus.PENDING,
        db_index=True,
    )
    outcome = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(processed_records__lte=models.F("total_records")),
                name="bookmark_import_processed_within_total",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    processed_records__gte=(
                        models.F("imported_records")
                        + models.F("skipped_records")
                        + models.F("failed_records")
                    )
                ),
                name="bookmark_import_results_within_processed",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.filename} — {self.get_status_display()}"

    def save(self, *args, **kwargs):
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "filename",
                "schema_version",
                "source_sha256",
                "outcome",
            }
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        filename = str(self.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        self.filename = _sanitized_single_line(filename, fallback="import.json")
        self.schema_version = _sanitized_single_line(self.schema_version, fallback="legacy")
        self.source_sha256 = str(self.source_sha256 or "").strip().casefold()
        self.outcome = _sanitized_single_line(self.outcome)

        errors: dict[str, str] = {}
        if self.source_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            errors["source_sha256"] = "The source fingerprint must be a SHA-256 hex digest."
        if len(self.filename) > self._meta.get_field("filename").max_length:
            errors["filename"] = "An import filename cannot exceed 255 characters."
        if len(self.schema_version) > self._meta.get_field("schema_version").max_length:
            errors["schema_version"] = "An import schema version cannot exceed 32 characters."
        if len(self.outcome) > self._meta.get_field("outcome").max_length:
            errors["outcome"] = "An import outcome cannot exceed 500 characters."
        if self.status not in BookmarkImportStatus.values:
            errors["status"] = "The import status is not supported."
        if self.processed_records > self.total_records:
            errors["processed_records"] = "Processed records cannot exceed the total."
        if (
            self.imported_records + self.skipped_records + self.failed_records
            > self.processed_records
        ):
            errors["processed_records"] = "Result counts cannot exceed processed records."
        if (
            self.status
            in {
                BookmarkImportStatus.COMPLETED,
                BookmarkImportStatus.COMPLETED_WITH_FAILURES,
                BookmarkImportStatus.FAILED,
            }
            and self.completed_at is None
        ):
            errors["completed_at"] = "A finished import must record when it completed."
        if (
            self.status
            in {
                BookmarkImportStatus.PENDING,
                BookmarkImportStatus.RUNNING,
            }
            and self.completed_at is not None
        ):
            errors["completed_at"] = "An unfinished import cannot have a completion time."
        if errors:
            raise ValidationError(errors)


class BookmarkRefreshRun(models.Model):
    """Durable progress for one user-requested global Confluence refresh."""

    status = models.CharField(
        max_length=32,
        choices=BookmarkRefreshStatus,
        default=BookmarkRefreshStatus.QUEUED,
        db_index=True,
    )
    total_bookmarks = models.PositiveIntegerField(default=0)
    processed_bookmarks = models.PositiveIntegerField(default=0)
    succeeded_bookmarks = models.PositiveIntegerField(default=0)
    failed_bookmarks = models.PositiveIntegerField(default=0)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)
    last_error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(processed_bookmarks__lte=models.F("total_bookmarks")),
                name="bookmark_refresh_processed_within_total",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    processed_bookmarks=(
                        models.F("succeeded_bookmarks") + models.F("failed_bookmarks")
                    )
                ),
                name="bookmark_refresh_results_match_processed",
            ),
            models.UniqueConstraint(
                models.Value(1),
                condition=models.Q(status__in=("queued", "running")),
                name="bookmark_refresh_one_active_run",
            ),
        ]

    def __str__(self) -> str:
        return f"Refresh #{self.pk} — {self.get_status_display()}"

    @property
    def is_active(self) -> bool:
        return self.status in {
            BookmarkRefreshStatus.QUEUED,
            BookmarkRefreshStatus.RUNNING,
        }


class BookmarkRefreshFailure(models.Model):
    """One final, sanitized bookmark failure from a completed refresh run."""

    refresh_run = models.ForeignKey(
        BookmarkRefreshRun,
        on_delete=models.CASCADE,
        related_name="failures",
    )
    bookmark = models.ForeignKey(
        "Bookmark",
        on_delete=models.CASCADE,
        related_name="refresh_failures",
    )
    page_id = models.CharField(max_length=64)
    url = models.URLField(max_length=2048, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=500)
    attempt_count = models.PositiveSmallIntegerField(default=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["bookmark_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("refresh_run", "bookmark"),
                name="bookmark_refresh_one_failure_per_bookmark",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__gte=1),
                name="bookmark_refresh_failure_attempt_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"Refresh #{self.refresh_run_id}, bookmark #{self.bookmark_id}: {self.reason}"


class BookmarkActivityCoverage(models.Model):
    """Singleton boundary separating backfilled saves from new exact activity."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    detailed_tracking_started_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        verbose_name = "bookmark activity coverage"
        verbose_name_plural = "bookmark activity coverage"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="bookmark_activity_single_coverage",
            )
        ]

    def __str__(self) -> str:
        return f"Detailed activity tracked from {self.detailed_tracking_started_at}"

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)


class BookmarkDailyActivity(models.Model):
    """One exact local counter per calendar day and activity kind.

    The table deliberately stores aggregate counts rather than URLs, titles, page
    content, or personal notes. This keeps the Home timeline useful without creating
    another source of retained bookmark data.
    """

    activity_date = models.DateField(db_index=True)
    activity_type = models.CharField(
        max_length=20,
        choices=BookmarkActivityType,
        db_index=True,
    )
    count = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-activity_date", "activity_type"]
        constraints = [
            models.UniqueConstraint(
                fields=("activity_date", "activity_type"),
                name="bookmark_activity_one_counter_per_day",
            ),
            models.CheckConstraint(
                condition=models.Q(count__gte=1),
                name="bookmark_activity_count_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.activity_date}: {self.get_activity_type_display()} × {self.count}"


class Bookmark(models.Model):
    """A saved page with a permanent internal OWL ID.

    The primary key remains the immutable internal identity. The user-facing bookmark
    number comes from the saved node's local outline path. Source synchronization must
    update only source-owned fields and diagnostic state; OWL-owned fields remain
    independent.
    """

    page_id = models.CharField(max_length=64, unique=True)
    tree_node = models.OneToOneField(
        ConfluencePageNode,
        on_delete=models.PROTECT,
        related_name="bookmark",
    )

    # Confluence-owned metadata.
    title = models.CharField(max_length=500, db_index=True)
    url = models.URLField(max_length=2048)
    space_name = models.CharField(max_length=255, blank=True)
    space_key = models.CharField(max_length=255, blank=True, db_index=True)
    version = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_by_id = models.CharField(max_length=255, blank=True)
    created_by_name = models.CharField(max_length=500, blank=True)
    modified_by_id = models.CharField(max_length=255, blank=True)
    modified_by_name = models.CharField(max_length=500, blank=True)
    author_id = models.CharField(max_length=255, blank=True, db_index=True)
    author_name = models.CharField(max_length=500, blank=True)

    # Cross-source identity and locally searchable content.
    source_type = models.CharField(
        max_length=20,
        choices=BookmarkSource,
        default=BookmarkSource.CONFLUENCE,
        db_index=True,
    )
    canonical_url = models.URLField(max_length=2048, null=True, blank=True, unique=True)
    category = models.ForeignKey(
        BookmarkCategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bookmarks",
    )
    page_text = models.TextField(blank=True)
    page_text_size_bytes = models.PositiveBigIntegerField(
        default=0,
        editable=False,
        help_text="UTF-8 byte size of the locally indexed page text only.",
    )

    # OWL-owned identity, personal state, and usage.
    saved_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    favorite = models.BooleanField(default=False, db_index=True)
    pinned = models.BooleanField(default=False, db_index=True)
    tags = models.ManyToManyField(Tag, related_name="bookmarks", blank=True)
    notes = models.TextField(blank=True)
    notes_updated_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveBigIntegerField(default=0)
    first_opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_viewed_version = models.PositiveBigIntegerField(null=True, blank=True)

    # Refresh and availability diagnostics contain no upstream response body.
    last_refresh_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_change_detected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    availability_status = models.CharField(
        max_length=32,
        choices=BookmarkAvailability,
        default=BookmarkAvailability.ACTIVE,
        db_index=True,
    )
    last_error_code = models.CharField(max_length=64, blank=True)
    last_error_message = models.CharField(max_length=255, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)

    # OWL-owned provenance for bookmarks first created through an import.
    import_run = models.ForeignKey(
        BookmarkImportRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bookmarks",
    )
    import_record_number = models.PositiveIntegerField(null=True, blank=True)
    legacy_number = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-saved_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(page_id=""),
                name="bookmark_page_id_is_not_blank",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(import_record_number__isnull=True)
                    | models.Q(import_record_number__gte=1)
                ),
                name="bookmark_import_record_number_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["favorite", "-saved_at"],
                name="bookmark_favorite_saved_idx",
            ),
            models.Index(
                fields=["pinned", "-saved_at"],
                name="bookmark_pinned_saved_idx",
            ),
            models.Index(
                fields=["availability_status", "-updated_at"],
                name="bookmark_avail_updated_idx",
            ),
            models.Index(
                fields=["-last_viewed_at", "-open_count"],
                name="bookmark_usage_viewed_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.title}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "page_text" in update_fields:
            self.page_text_size_bytes = len((self.page_text or "").encode("utf-8"))
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"page_text_size_bytes"}
        return super().save(*args, **kwargs)

    @property
    def changed_since_viewed(self) -> bool:
        return self.last_viewed_version is not None and self.version != self.last_viewed_version

    def recency_at(
        self,
        *,
        at=None,
        new_duration_days: int | None = None,
        updated_duration_days: int | None = None,
    ) -> BookmarkRecency:
        """Calculate expiring NEW/UPDATED state at a deterministic point in time."""

        observed_at = at or timezone.now()
        if timezone.is_naive(observed_at):
            raise ValueError("Bookmark recency requires a timezone-aware timestamp.")
        new_days = settings.NEW_DURATION_DAYS if new_duration_days is None else new_duration_days
        updated_days = (
            settings.UPDATED_DURATION_DAYS
            if updated_duration_days is None
            else updated_duration_days
        )
        if new_days < 1 or updated_days < 1:
            raise ValueError("Bookmark recency durations must be positive days.")

        if self.saved_at and self.saved_at >= observed_at - timedelta(days=new_days):
            return BookmarkRecency.NEW
        if (
            self.availability_status == BookmarkAvailability.ACTIVE
            and self.updated_at is not None
            and self.updated_at >= observed_at - timedelta(days=updated_days)
        ):
            return BookmarkRecency.UPDATED
        return BookmarkRecency.NORMAL

    @property
    def recency_status(self) -> BookmarkRecency:
        return self.recency_at()


class BookmarkImportFailure(models.Model):
    """One sanitized, record-level failure that never aborts sibling records."""

    import_run = models.ForeignKey(
        BookmarkImportRun,
        on_delete=models.CASCADE,
        related_name="failures",
    )
    record_number = models.PositiveIntegerField()
    page_id = models.CharField(max_length=64, blank=True)
    source_url = models.CharField(max_length=2048, blank=True)
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["record_number", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(record_number__gte=1),
                name="bookmark_import_failure_record_positive",
            ),
            models.UniqueConstraint(
                fields=["import_run", "record_number"],
                name="bookmark_import_one_failure_per_record",
            ),
        ]

    def __str__(self) -> str:
        return f"Record {self.record_number}: {self.reason}"

    def save(self, *args, **kwargs):
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "page_id",
                "source_url",
                "reason",
            }
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.page_id = _sanitized_single_line(self.page_id)
        self.source_url = _sanitized_single_line(self.source_url)
        self.reason = _sanitized_single_line(self.reason, fallback="Invalid import record.")
        errors: dict[str, str] = {}
        if self.record_number < 1:
            errors["record_number"] = "Import record numbers start at one."
        if len(self.page_id) > self._meta.get_field("page_id").max_length:
            errors["page_id"] = "The Page ID in a failure cannot exceed 64 characters."
        if len(self.source_url) > self._meta.get_field("source_url").max_length:
            errors["source_url"] = "The source URL in a failure cannot exceed 2048 characters."
        if len(self.reason) > self._meta.get_field("reason").max_length:
            errors["reason"] = "The failure reason cannot exceed 500 characters."
        if errors:
            raise ValidationError(errors)
