"""Small data model owned exclusively by the Bitbucket app."""

from __future__ import annotations

import uuid

from django.db import models


class RepositoryState(models.TextChoices):
    QUEUED = "queued", "Queued"
    TESTING = "testing", "Testing connection"
    FETCHING = "fetching", "Fetching metadata"
    READY = "ready", "Ready"
    AUTH_REQUIRED = "auth_required", "Authentication required"
    FAILED = "failed", "Failed"


class Repository(models.Model):
    url = models.URLField(max_length=2048, unique=True)
    canonical_url = models.CharField(max_length=2048, unique=True)
    host = models.CharField(max_length=255, db_index=True)
    project = models.CharField(max_length=255, db_index=True)
    slug = models.CharField(max_length=255, db_index=True)
    state = models.CharField(
        max_length=24,
        choices=RepositoryState.choices,
        default=RepositoryState.QUEUED,
        db_index=True,
    )
    status_message = models.CharField(max_length=500, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    pdf_count = models.PositiveIntegerField(default=0)
    indexed_pdf_count = models.PositiveIntegerField(default=0)
    failed_pdf_count = models.PositiveIntegerField(default=0)
    vsdx_count = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    last_successful_refresh_on = models.DateField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("project", "slug", "pk")

    def __str__(self) -> str:
        return f"{self.project}/{self.slug}"


class SyncOperation(models.TextChoices):
    INITIAL = "initial", "Initial fetch"
    REFRESH = "refresh", "Refresh"


class SyncJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    AUTH_REQUIRED = "auth_required", "Authentication required"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class SyncJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="sync_jobs")
    operation = models.CharField(max_length=8, choices=SyncOperation.choices)
    status = models.CharField(
        max_length=24,
        choices=SyncJobStatus.choices,
        default=SyncJobStatus.QUEUED,
        db_index=True,
    )
    scheduled_for = models.DateField(null=True, blank=True, db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=40, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)
    output = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        constraints = (
            models.UniqueConstraint(
                fields=("repository", "operation", "scheduled_for"),
                name="bitbucket_one_scheduled_refresh_per_day",
            ),
        )

    def __str__(self) -> str:
        return f"{self.operation} {self.repository} ({self.status})"


class DocumentKind(models.TextChoices):
    PDF = "pdf", "PDF"
    VSDX = "vsdx", "VSDX"


class DocumentIndexState(models.TextChoices):
    PENDING = "pending", "Pending"
    INDEXED = "indexed", "Indexed"
    FAILED = "failed", "Failed"


class Document(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=4, choices=DocumentKind.choices, db_index=True)
    relative_path = models.CharField(max_length=2048)
    filename = models.CharField(max_length=500, db_index=True)
    added_at = models.DateTimeField(null=True, blank=True, db_index=True)
    added_by = models.CharField(max_length=255, blank=True, db_index=True)
    added_by_email = models.EmailField(max_length=320, blank=True)
    commit_id = models.CharField(max_length=64, blank=True)
    latest_commit_id = models.CharField(max_length=64, blank=True, db_index=True)
    latest_commit_message = models.TextField(blank=True)
    latest_commit_author = models.CharField(max_length=255, blank=True)
    latest_commit_at = models.DateTimeField(null=True, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    content_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    extracted_text = models.TextField(blank=True)
    text_truncated = models.BooleanField(default=False)
    index_state = models.CharField(
        max_length=16,
        choices=DocumentIndexState.choices,
        default=DocumentIndexState.PENDING,
        db_index=True,
    )
    index_error = models.CharField(max_length=1000, blank=True)
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveIntegerField(default=0)
    discovered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-added_at", "filename", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("repository", "relative_path"),
                name="bitbucket_unique_document_path",
            ),
        )
        indexes = (models.Index(fields=("kind", "added_at"), name="bb_doc_kind_added_idx"),)

    def __str__(self) -> str:
        return self.relative_path


class HTTPSCredential(models.Model):
    """One encrypted Bitbucket Data Center HTTP credential per exact HTTPS origin."""

    origin = models.URLField(max_length=2048, unique=True)
    username = models.CharField(max_length=255, blank=True)
    token_ciphertext = models.TextField(editable=False)
    configured_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("origin",)

    def __str__(self) -> str:
        return self.origin


class Contributor(models.Model):
    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="contributors"
    )
    name = models.CharField(max_length=255)
    email = models.EmailField(max_length=320, blank=True)
    identity_key = models.CharField(max_length=600)
    pdf_commit_count = models.PositiveIntegerField(default=0)
    last_pdf_commit_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-pdf_commit_count", "name", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("repository", "identity_key"),
                name="bitbucket_unique_contributor",
            ),
        )

    def __str__(self) -> str:
        return self.name or self.email
