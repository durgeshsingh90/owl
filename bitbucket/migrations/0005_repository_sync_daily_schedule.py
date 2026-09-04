from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket", "0004_pdf_fts_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="repositorysyncjob",
            name="automatic_retry_number",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="repositorysyncjob",
            name="scheduled_day",
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="repositorysyncjob",
            name="trigger",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("daily", "Daily"),
                    ("retry", "Automatic retry"),
                ],
                db_index=True,
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="repositorysyncjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(scheduled_day__isnull=False),
                fields=("repository", "scheduled_day", "automatic_retry_number"),
                name="bitbucket_app_unique_scheduled_repository_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="repositorysyncjob",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        trigger="manual",
                        scheduled_day__isnull=True,
                        automatic_retry_number=0,
                    )
                    | models.Q(
                        trigger="daily",
                        scheduled_day__isnull=False,
                        automatic_retry_number=0,
                    )
                    | models.Q(
                        trigger="retry",
                        scheduled_day__isnull=False,
                        automatic_retry_number__gte=1,
                    )
                ),
                name="bitbucket_app_sync_job_trigger_consistent",
            ),
        ),
    ]
