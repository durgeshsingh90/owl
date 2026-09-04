import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="bitbucketrepository",
            name="history_is_shallow",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="bitbucketrepository",
            name="metadata_indexed_commit",
            field=models.CharField(blank=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name="GitCommit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("commit_hash", models.CharField(max_length=64)),
                ("author_name", models.CharField(max_length=255)),
                ("committer_name", models.CharField(max_length=255)),
                ("authored_at", models.DateTimeField()),
                ("committed_at", models.DateTimeField()),
                ("is_shallow_boundary", models.BooleanField(default=False)),
                (
                    "repository",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="git_commits",
                        to="bitbucket.bitbucketrepository",
                    ),
                ),
            ],
            options={
                "ordering": ["-committed_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["repository", "-committed_at"],
                        name="ba_commit_repo_date_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("repository", "commit_hash"),
                        name="bitbucket_app_unique_repository_commit",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="PDFDocument",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("filename", models.CharField(max_length=512)),
                ("relative_path", models.CharField(max_length=2048)),
                ("file_size", models.PositiveBigIntegerField(default=0)),
                ("git_blob_id", models.CharField(blank=True, max_length=64)),
                (
                    "lifecycle_state",
                    models.CharField(
                        choices=[("active", "Active"), ("removed", "Removed")],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("discovered_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                ("last_seen_commit", models.CharField(blank=True, max_length=64)),
                (
                    "added_evidence",
                    models.CharField(
                        choices=[
                            ("confirmed", "Confirmed"),
                            ("before_available_history", "Before available history"),
                            ("not_found", "Not found"),
                        ],
                        default="not_found",
                        max_length=32,
                    ),
                ),
                ("timeline_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "timeline_basis",
                    models.CharField(
                        choices=[
                            ("git_added", "Git addition"),
                            ("owl_discovered", "Discovered by OWL"),
                        ],
                        default="owl_discovered",
                        max_length=24,
                    ),
                ),
                ("open_count", models.PositiveBigIntegerField(default=0)),
                ("first_opened_at", models.DateTimeField(blank=True, null=True)),
                ("last_opened_at", models.DateTimeField(blank=True, null=True)),
                (
                    "added_commit",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="added_documents",
                        to="bitbucket.gitcommit",
                    ),
                ),
                (
                    "last_commit",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="last_changed_documents",
                        to="bitbucket.gitcommit",
                    ),
                ),
                (
                    "repository",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pdf_documents",
                        to="bitbucket.bitbucketrepository",
                    ),
                ),
            ],
            options={
                "ordering": ["-timeline_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["lifecycle_state", "-timeline_at", "-id"],
                        name="ba_pdf_active_timeline_idx",
                    ),
                    models.Index(
                        fields=["repository", "lifecycle_state", "-timeline_at"],
                        name="ba_pdf_repo_timeline_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("repository", "relative_path"),
                        name="bitbucket_app_unique_repository_pdf_path",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(("first_opened_at__isnull", True))
                            | models.Q(("last_opened_at__isnull", True))
                            | models.Q(
                                ("first_opened_at__lte", models.F("last_opened_at"))
                            )
                        ),
                        name="bitbucket_app_pdf_open_timestamps_ordered",
                    ),
                ],
            },
        ),
    ]
