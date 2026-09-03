from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket_search", "0018_trustedrepositoryhost_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdfpipelinerecoveryevent",
            name="correlation_id",
            field=models.UUIDField(blank=True, db_index=True, editable=False, null=True),
        ),
    ]
