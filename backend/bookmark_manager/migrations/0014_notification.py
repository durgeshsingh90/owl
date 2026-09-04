import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0013_bookmarkfolder_bookmark_manual_folder")]

    operations = [
        migrations.CreateModel(
            name="Notification",
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
                ("event_key", models.CharField(max_length=128, unique=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("bookmark_import", "Bookmark import"),
                            ("bookmark_export", "Bookmark export"),
                            ("confluence_refresh", "Confluence refresh"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("success", "Success"),
                            ("warning", "Warning"),
                            ("error", "Error"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(max_length=200)),
                ("message", models.CharField(blank=True, max_length=500)),
                ("target_path", models.CharField(blank=True, max_length=2048)),
                (
                    "occurred_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("read_at", models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={
                "ordering": ["-occurred_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["read_at", "-occurred_at"],
                        name="bmk_notif_unread_time_idx",
                    )
                ],
            },
        )
    ]
