from django.db import migrations, models


def normalize_active_states(apps, schema_editor) -> None:
    repository = apps.get_model("bitbucket", "Repository")
    sync_job = apps.get_model("bitbucket", "SyncJob")
    repository.objects.filter(state__in=("cloning", "pulling")).update(state="fetching")
    sync_job.objects.filter(operation="clone").update(operation="initial")
    sync_job.objects.filter(operation="pull").update(operation="refresh")


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0022_ensure_independent_document_schema")]

    operations = [
        migrations.CreateModel(
            name="HTTPSCredential",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("origin", models.URLField(max_length=2048, unique=True)),
                ("token_ciphertext", models.TextField(editable=False)),
                ("configured_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("origin",)},
        ),
        migrations.RemoveConstraint(
            model_name="syncjob",
            name="bitbucket_one_scheduled_pull_per_day",
        ),
        migrations.RenameField(
            model_name="repository",
            old_name="last_successful_pull_on",
            new_name="last_successful_refresh_on",
        ),
        migrations.RunPython(normalize_active_states, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="repository",
            name="state",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("testing", "Testing connection"),
                    ("fetching", "Fetching metadata"),
                    ("ready", "Ready"),
                    ("auth_required", "Authentication required"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="queued",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="syncjob",
            name="operation",
            field=models.CharField(
                choices=[("initial", "Initial fetch"), ("refresh", "Refresh")],
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="syncjob",
            constraint=models.UniqueConstraint(
                fields=("repository", "operation", "scheduled_for"),
                name="bitbucket_one_scheduled_refresh_per_day",
            ),
        ),
    ]
