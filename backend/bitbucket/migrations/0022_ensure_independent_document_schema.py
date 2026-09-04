"""Repair databases that previously applied the removed Bitbucket draft's migrations."""

from django.db import migrations


CURRENT_MODELS = ("Repository", "Document", "Contributor", "SyncJob")


def ensure_document_desk_tables(apps, schema_editor) -> None:
    """Create only missing current tables; preserve every legacy table and its data."""

    existing_tables = set(schema_editor.connection.introspection.table_names())
    for model_name in CURRENT_MODELS:
        model = apps.get_model("bitbucket", model_name)
        if model._meta.db_table in existing_tables:
            continue
        schema_editor.create_model(model)
        existing_tables = set(schema_editor.connection.introspection.table_names())


class Migration(migrations.Migration):
    dependencies = [("bitbucket", "0001_initial")]

    operations = [
        migrations.RunPython(
            ensure_document_desk_tables,
            reverse_code=migrations.RunPython.noop,
        )
    ]
