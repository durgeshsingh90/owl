from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0012_bitbucket_https_credential")]

    operations = [
        migrations.AddField(
            model_name="pdfextractionjob",
            name="run_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
    ]
