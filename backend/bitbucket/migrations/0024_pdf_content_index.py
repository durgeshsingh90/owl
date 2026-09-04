from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0023_remote_api_credentials")]

    operations = [
        migrations.AddField(
            model_name="repository",
            name="failed_pdf_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="repository",
            name="indexed_pdf_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="httpscredential",
            name="username",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="document",
            name="content_sha256",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="document",
            name="extracted_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="document",
            name="file_size",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="document",
            name="index_error",
            field=models.CharField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="document",
            name="index_state",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("indexed", "Indexed"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="last_scanned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="latest_commit_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="latest_commit_author",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="document",
            name="latest_commit_id",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="document",
            name="latest_commit_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="document",
            name="page_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="document",
            name="text_truncated",
            field=models.BooleanField(default=False),
        ),
    ]
