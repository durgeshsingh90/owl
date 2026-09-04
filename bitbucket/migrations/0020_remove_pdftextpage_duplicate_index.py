from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket", "0019_pdf_recovery_event_correlation"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="pdftextpage",
            name="ba_pdf_page_lookup_idx",
        ),
    ]
