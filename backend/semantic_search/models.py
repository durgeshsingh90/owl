"""Durable shared semantic indexes and their background job state."""

from django.db import models
from django.utils import timezone


class SemanticSourceType(models.TextChoices):
    """Canonical content families supported by the semantic index."""

    BOOKMARK = "bookmark", "Bookmark"


class SemanticIndexJobStatus(models.TextChoices):
    """Lifecycle state for one durable semantic indexing request."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


def _matching_source_constraint(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(
            source_type=SemanticSourceType.BOOKMARK,
            bookmark__isnull=False,
        ),
        name=name,
    )


class SemanticIndex(models.Model):
    """The currently published semantic chunks for one canonical source."""

    source_type = models.CharField(max_length=16, choices=SemanticSourceType, db_index=True)
    bookmark = models.OneToOneField(
        "bookmark_manager.Bookmark",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="semantic_index",
    )
    content_hash = models.CharField(max_length=64, db_index=True)
    model_version = models.CharField(max_length=160)
    chunker_version = models.CharField(max_length=64)
    dimensions = models.PositiveSmallIntegerField()
    centroid_vector = models.BinaryField()
    chunk_count = models.PositiveIntegerField(default=0)
    character_count = models.PositiveBigIntegerField(default=0)
    published_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_type", "id"]
        constraints = [
            _matching_source_constraint("semantic_index_source_matches"),
        ]

    def __str__(self) -> str:
        return f"{self.get_source_type_display()} {self.bookmark_id} — {self.model_version}"


class SemanticChunk(models.Model):
    """One page-aware text chunk and its serialized embedding vector."""

    index = models.ForeignKey(
        SemanticIndex,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    ordinal = models.PositiveIntegerField()
    page_number = models.PositiveIntegerField(null=True, blank=True)
    chunk_text = models.TextField()
    text_hash = models.CharField(max_length=64)
    vector = models.BinaryField()
    character_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["index_id", "ordinal", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("index", "ordinal"),
                name="semantic_unique_index_chunk",
            ),
        ]
        indexes = [
            models.Index(
                fields=("index", "page_number"),
                name="semantic_chunk_page_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.index_id} — chunk {self.ordinal}"


class SemanticIndexJob(models.Model):
    """One durable request to publish the current embedding for a source."""

    source_type = models.CharField(max_length=16, choices=SemanticSourceType, db_index=True)
    bookmark = models.ForeignKey(
        "bookmark_manager.Bookmark",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="semantic_index_jobs",
    )
    target_content_hash = models.CharField(max_length=64)
    target_model_version = models.CharField(max_length=160)
    target_chunker_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=SemanticIndexJobStatus,
        default=SemanticIndexJobStatus.QUEUED,
        db_index=True,
    )
    retry_count = models.PositiveSmallIntegerField(default=0)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["requested_at", "id"]
        constraints = [
            _matching_source_constraint("semantic_job_source_matches"),
            models.UniqueConstraint(
                fields=("bookmark",),
                condition=models.Q(
                    bookmark__isnull=False,
                    status__in=(
                        SemanticIndexJobStatus.QUEUED,
                        SemanticIndexJobStatus.RUNNING,
                    ),
                ),
                name="semantic_one_active_bm_job",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "requested_at"),
                name="semantic_job_queue_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_source_type_display()} {self.bookmark_id} — {self.get_status_display()}"

    @property
    def is_active(self) -> bool:
        return self.status in {
            SemanticIndexJobStatus.QUEUED,
            SemanticIndexJobStatus.RUNNING,
        }


class SemanticCorpusState(models.Model):
    """Generation marker used to invalidate cached results for one source family."""

    source_type = models.CharField(max_length=16, choices=SemanticSourceType, unique=True)
    generation = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_type"]

    def __str__(self) -> str:
        return f"{self.get_source_type_display()} — generation {self.generation}"
