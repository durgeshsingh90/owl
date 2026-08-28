from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0006_encrypted_database_credential")]

    operations = [
        migrations.CreateModel(
            name="BookmarkRefreshRun",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("succeeded_with_errors", "Succeeded with errors"),
                            ("failed", "Failed"),
                            ("interrupted", "Interrupted"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=32,
                    ),
                ),
                ("total_bookmarks", models.PositiveIntegerField(default=0)),
                ("processed_bookmarks", models.PositiveIntegerField(default=0)),
                ("succeeded_bookmarks", models.PositiveIntegerField(default=0)),
                ("failed_bookmarks", models.PositiveIntegerField(default=0)),
                ("requested_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("worker_pid", models.PositiveIntegerField(blank=True, null=True)),
                ("last_error_message", models.CharField(blank=True, max_length=500)),
            ],
            options={"ordering": ["-requested_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="bookmarkrefreshrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("processed_bookmarks__lte", models.F("total_bookmarks"))),
                name="bookmark_refresh_processed_within_total",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkrefreshrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "processed_bookmarks",
                        models.F("succeeded_bookmarks") + models.F("failed_bookmarks"),
                    )
                ),
                name="bookmark_refresh_results_match_processed",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkrefreshrun",
            constraint=models.UniqueConstraint(
                models.Value(1),
                condition=models.Q(("status__in", ("queued", "running"))),
                name="bookmark_refresh_one_active_run",
            ),
        ),
    ]
