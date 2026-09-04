from importlib import import_module

from django.db import migrations, models

_fts = import_module("bitbucket.migrations.0004_pdf_fts_indexes")
_drop_metadata_triggers = _fts.DROP_METADATA_FTS[:-1]
_create_metadata_triggers = _fts.CREATE_METADATA_FTS[2:]


def move_file_exclusions_to_repositories(apps, schema_editor):
    repository = apps.get_model("bitbucket", "BitbucketRepository")
    policy = apps.get_model("bitbucket", "PDFLocalPolicy")
    alias = schema_editor.connection.alias
    excluded = policy.objects.using(alias).filter(state="excluded")
    repository.objects.using(alias).filter(pk__in=excluded.values("repository_id")).update(
        exclude_from_refresh=True
    )
    # Retain frozen snapshots until a successful explicit refresh publishes the
    # current checkout. Migrations must not access or remove any local files.
    excluded.update(state="resuming")


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0009_repository_git_output")]

    operations = [
        migrations.RunSQL(_drop_metadata_triggers, _create_metadata_triggers),
        migrations.AddField(
            model_name="bitbucketrepository",
            name="exclude_from_refresh",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.CreateModel(
            name="RepositoryRemovalRecovery",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("repository_id", models.PositiveBigIntegerField(unique=True)),
                ("display_name", models.CharField(max_length=200)),
                ("quarantine_manifest", models.JSONField(default=list)),
                ("database_deleted", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["display_name", "id"]},
        ),
        migrations.RunPython(move_file_exclusions_to_repositories, migrations.RunPython.noop),
        migrations.RunSQL(_create_metadata_triggers, _drop_metadata_triggers),
    ]
