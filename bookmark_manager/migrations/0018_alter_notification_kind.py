from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0017_alter_notification_kind")]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("bookmark_import", "Bookmark import"),
                    ("bookmark_export", "Bookmark export"),
                    ("confluence_refresh", "Confluence refresh"),
                    ("bitbucket_refresh", "Bitbucket refresh"),
                    ("pdf_pipeline_recovery", "PDF pipeline recovery"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
