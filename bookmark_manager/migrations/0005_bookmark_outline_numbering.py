from django.db import migrations, models


def assign_existing_outline_positions(apps, schema_editor):
    node_model = apps.get_model("bookmark_manager", "ConfluencePageNode")
    positions = {}
    changed = []
    nodes = node_model.objects.order_by(
        "parent_id",
        "sibling_position",
        "title",
        "id",
    )
    for node in nodes.iterator():
        position = positions.get(node.parent_id, 0) + 1
        positions[node.parent_id] = position
        node.outline_position = position
        changed.append(node)
    if changed:
        node_model.objects.bulk_update(changed, ("outline_position",))


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0004_general_web_bookmarks")]

    operations = [
        migrations.AddField(
            model_name="confluencepagenode",
            name="outline_position",
            field=models.PositiveIntegerField(
                blank=True,
                editable=False,
                help_text="Stable local position used for Word-style bookmark numbering.",
                null=True,
            ),
        ),
        migrations.RunPython(assign_existing_outline_positions, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="confluencepagenode",
            index=models.Index(
                fields=["parent", "outline_position"],
                name="bookmark_node_outline_order",
            ),
        ),
    ]
