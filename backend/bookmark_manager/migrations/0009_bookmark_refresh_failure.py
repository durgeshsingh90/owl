import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0008_bookmark_home_analytics")]

    operations = [
        migrations.CreateModel(
            name="BookmarkRefreshFailure",
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
                ("page_id", models.CharField(max_length=64)),
                ("url", models.URLField(blank=True, max_length=2048)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("reason", models.CharField(max_length=500)),
                ("attempt_count", models.PositiveSmallIntegerField(default=3)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "bookmark",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refresh_failures",
                        to="bookmark_manager.bookmark",
                    ),
                ),
                (
                    "refresh_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="failures",
                        to="bookmark_manager.bookmarkrefreshrun",
                    ),
                ),
            ],
            options={"ordering": ["bookmark_id", "id"]},
        ),
        migrations.AddConstraint(
            model_name="bookmarkrefreshfailure",
            constraint=models.UniqueConstraint(
                fields=("refresh_run", "bookmark"),
                name="bookmark_refresh_one_failure_per_bookmark",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkrefreshfailure",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempt_count__gte", 1)),
                name="bookmark_refresh_failure_attempt_positive",
            ),
        ),
    ]
