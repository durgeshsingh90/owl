import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bitbucket_search", "0006_bitbucket_people_groups")]

    operations = [
        migrations.CreateModel(
            name="PDFLocalPolicy",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("relative_path", models.CharField(max_length=2048)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("excluded", "Excluded from refresh"),
                            ("deleted", "Deleted locally"),
                            ("resuming", "Waiting to resume refresh"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "document",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="local_policy",
                        to="bitbucket_search.pdfdocument",
                    ),
                ),
                (
                    "repository",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pdf_local_policies",
                        to="bitbucket_search.bitbucketrepository",
                    ),
                ),
            ],
            options={
                "ordering": ["repository_id", "relative_path", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("repository", "relative_path"),
                        name="bitbucket_unique_pdf_local_policy_path",
                    )
                ],
            },
        ),
    ]
