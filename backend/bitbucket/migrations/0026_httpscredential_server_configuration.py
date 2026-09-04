from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0025_document_fts")]

    operations = [
        migrations.AddField(
            model_name="httpscredential",
            name="api_base_url",
            field=models.URLField(blank=True, max_length=2048),
        ),
        migrations.AddField(
            model_name="httpscredential",
            name="verify_ssl",
            field=models.BooleanField(default=True),
        ),
    ]
