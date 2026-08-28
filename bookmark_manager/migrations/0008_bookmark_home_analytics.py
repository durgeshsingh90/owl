from collections import Counter
from datetime import UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def backfill_bookmark_analytics(apps, schema_editor):
    Bookmark = apps.get_model("bookmark_manager", "Bookmark")
    BookmarkActivityCoverage = apps.get_model(
        "bookmark_manager",
        "BookmarkActivityCoverage",
    )
    BookmarkDailyActivity = apps.get_model(
        "bookmark_manager",
        "BookmarkDailyActivity",
    )

    try:
        local_timezone = ZoneInfo(settings.TIME_ZONE)
    except ZoneInfoNotFoundError:
        local_timezone = UTC

    added_by_day = Counter()
    size_updates = []
    for bookmark in Bookmark.objects.order_by("pk").iterator(chunk_size=100):
        bookmark.page_text_size_bytes = len((bookmark.page_text or "").encode("utf-8"))
        size_updates.append(bookmark)
        if len(size_updates) == 100:
            Bookmark.objects.bulk_update(
                size_updates,
                ("page_text_size_bytes",),
                batch_size=100,
            )
            size_updates.clear()
        if bookmark.saved_at is not None:
            saved_at = bookmark.saved_at
            if timezone.is_naive(saved_at):
                saved_at = saved_at.replace(tzinfo=UTC)
            added_by_day[saved_at.astimezone(local_timezone).date()] += 1

    if size_updates:
        Bookmark.objects.bulk_update(
            size_updates,
            ("page_text_size_bytes",),
            batch_size=100,
        )

    BookmarkDailyActivity.objects.bulk_create(
        [
            BookmarkDailyActivity(
                activity_date=activity_date,
                activity_type="added",
                count=count,
            )
            for activity_date, count in sorted(added_by_day.items())
        ]
    )
    BookmarkActivityCoverage.objects.update_or_create(
        pk=1,
        defaults={"detailed_tracking_started_at": timezone.now()},
    )


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0007_bookmark_refresh_run")]

    operations = [
        migrations.CreateModel(
            name="BookmarkActivityCoverage",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "detailed_tracking_started_at",
                    models.DateTimeField(default=timezone.now, editable=False),
                ),
            ],
            options={
                "verbose_name": "bookmark activity coverage",
                "verbose_name_plural": "bookmark activity coverage",
            },
        ),
        migrations.CreateModel(
            name="BookmarkDailyActivity",
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
                ("activity_date", models.DateField(db_index=True)),
                (
                    "activity_type",
                    models.CharField(
                        choices=[
                            ("added", "Added"),
                            ("opened", "Opened"),
                            ("refreshed", "Refreshed"),
                            ("notes", "Notes updated"),
                        ],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                ("count", models.PositiveBigIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-activity_date", "activity_type"]},
        ),
        migrations.AddField(
            model_name="bookmark",
            name="page_text_size_bytes",
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                help_text="UTF-8 byte size of the locally indexed page text only.",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkactivitycoverage",
            constraint=models.CheckConstraint(
                condition=models.Q(("id", 1)),
                name="bookmark_activity_single_coverage",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkdailyactivity",
            constraint=models.UniqueConstraint(
                fields=("activity_date", "activity_type"),
                name="bookmark_activity_one_counter_per_day",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookmarkdailyactivity",
            constraint=models.CheckConstraint(
                condition=models.Q(("count__gte", 1)),
                name="bookmark_activity_count_positive",
            ),
        ),
        migrations.RunPython(backfill_bookmark_analytics, migrations.RunPython.noop),
    ]
