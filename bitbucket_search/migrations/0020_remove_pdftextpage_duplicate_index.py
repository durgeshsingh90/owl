from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("bitbucket_search", "0019_pdf_recovery_event_correlation"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="pdftextpage",
            name="bb_pdf_page_lookup_idx",
        ),
    ]
