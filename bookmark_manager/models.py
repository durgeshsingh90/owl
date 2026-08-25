from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

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
    """Non-secret Confluence settings.

    The PAT and any credential-store identifier deliberately do not belong in this model.
    A fixed primary key enforces the single-user, single-profile product boundary.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    base_url = models.URLField(max_length=2048, blank=True)
    auth_mode = models.CharField(max_length=32, default="bearer")
    credential_source = models.CharField(
        max_length=20,
        choices=CredentialSource,
        default=CredentialSource.NONE,
    )
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


class BookmarkImportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures", "Completed with failures"
    FAILED = "failed", "Failed"


class ConfluencePageNode(models.Model):
    """One real node in the locally reconstructed Confluence hierarchy.

    A node is source-owned hierarchy metadata. It can exist solely because it is an
    ancestor of a saved bookmark, in which case it deliberately has no OWL number.
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
        ]
        indexes = [
            models.Index(
                fields=["parent", "sibling_position", "title"],
                name="bookmark_node_tree_order",
            )
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


class Bookmark(models.Model):
    """A saved Confluence page with a permanent OWL number.

    The primary key is the displayed OWL number. Source synchronization must update
    only the Confluence-owned fields and diagnostic state; OWL-owned fields remain
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
            kwargs["update_fields"] = set(update_fields) | {"page_id", "reason"}
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.page_id = _sanitized_single_line(self.page_id)
        self.reason = _sanitized_single_line(self.reason, fallback="Invalid import record.")
        errors: dict[str, str] = {}
        if self.record_number < 1:
            errors["record_number"] = "Import record numbers start at one."
        if len(self.page_id) > self._meta.get_field("page_id").max_length:
            errors["page_id"] = "The Page ID in a failure cannot exceed 64 characters."
        if len(self.reason) > self._meta.get_field("reason").max_length:
            errors["reason"] = "The failure reason cannot exceed 500 characters."
        if errors:
            raise ValidationError(errors)
