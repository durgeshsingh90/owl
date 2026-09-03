from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket_search", "0020_remove_pdftextpage_duplicate_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdfpipelinetuningevent",
            name="expected_effect_code",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
