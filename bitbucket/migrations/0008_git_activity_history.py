from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

_fts = import_module("bitbucket.migrations.0004_pdf_fts_indexes")
_drop_metadata_triggers = _fts.DROP_METADATA_FTS[:-1]
_create_metadata_triggers = _fts.CREATE_METADATA_FTS[2:]


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0007_pdflocalpolicy")]

    operations = [
        # SQLite rebuilds these tables for new non-null fields. Keep cross-table
        # triggers out of the rename window, without dropping searchable FTS data.
        migrations.RunSQL(_drop_metadata_triggers, _create_metadata_triggers),
        migrations.AddField(
            model_name="bitbucketrepository",
            name="activity_indexed_commit",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="bitbucketrepository",
            name="activity_indexed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="gitcommit",
            name="in_activity_history",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.CreateModel(
            name="GitCommitFolder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("folder_path", models.CharField(blank=True, max_length=2048)),
                (
                    "commit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="folders",
                        to="bitbucket.gitcommit",
                    ),
                ),
            ],
            options={
                "ordering": ["folder_path", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("commit", "folder_path"),
                        name="bitbucket_app_unique_commit_folder",
                    )
                ],
            },
        ),
        migrations.RunSQL(_create_metadata_triggers, _drop_metadata_triggers),
    ]
