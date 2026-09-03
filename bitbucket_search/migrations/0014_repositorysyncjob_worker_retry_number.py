from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bitbucket_search", "0013_pdfextractionjob_run_id")]

    operations = [
        migrations.AddField(
            model_name="repositorysyncjob",
            name="worker_retry_number",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RemoveConstraint(
            model_name="repositorysyncjob",
            name="bitbucket_unique_scheduled_repository_attempt",
        ),
        migrations.AddConstraint(
            model_name="repositorysyncjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("scheduled_day__isnull", False)),
                fields=(
                    "repository",
                    "scheduled_day",
                    "automatic_retry_number",
                    "worker_retry_number",
                ),
                name="bitbucket_unique_scheduled_repository_attempt",
            ),
        ),
    ]
