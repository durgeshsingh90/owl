from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0005_bookmark_outline_numbering")]

    operations = [
        migrations.AddField(
            model_name="confluenceconfiguration",
            name="credential_ciphertext",
            field=models.TextField(blank=True, editable=False),
        ),
        migrations.AlterField(
            model_name="confluenceconfiguration",
            name="credential_source",
            field=models.CharField(
                choices=[
                    ("none", "Not configured"),
                    ("keyring", "Operating-system credential store"),
                    ("database", "Encrypted local database"),
                    ("environment", "Managed externally"),
                ],
                default="none",
                max_length=20,
            ),
        ),
    ]
