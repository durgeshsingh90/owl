from urllib.parse import urlsplit

import django.db.models.deletion
from django.db import migrations, models


def populate_url_identity_and_categories(apps, schema_editor):
    Bookmark = apps.get_model("bookmark_manager", "Bookmark")
    BookmarkCategory = apps.get_model("bookmark_manager", "BookmarkCategory")
    seen_urls = set()
    for bookmark in Bookmark.objects.order_by("id").iterator():
        hostname = (urlsplit(bookmark.url).hostname or "").casefold().rstrip(".")
        if hostname:
            default_name = hostname.removeprefix("www.") or hostname
            category, _created = BookmarkCategory.objects.get_or_create(
                domain=hostname,
                defaults={"name": default_name},
            )
            bookmark.category_id = category.pk
        canonical_url = bookmark.url if bookmark.url not in seen_urls else None
        if canonical_url:
            seen_urls.add(canonical_url)
        bookmark.canonical_url = canonical_url
        bookmark.save(update_fields=("category", "canonical_url"))


class Migration(migrations.Migration):
    dependencies = [("bookmark_manager", "0003_phase3_bookmark_productivity")]

    operations = [
        migrations.CreateModel(
            name="BookmarkCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=253, unique=True)),
                ("name", models.CharField(max_length=253)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name_plural": "bookmark categories", "ordering": ["name", "domain", "id"]},
        ),
        migrations.AddField(
            model_name="bookmark",
            name="canonical_url",
            field=models.URLField(blank=True, max_length=2048, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="bookmark",
            name="category",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="bookmarks", to="bookmark_manager.bookmarkcategory"),
        ),
        migrations.AddField(
            model_name="bookmark",
            name="page_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="bookmark",
            name="source_type",
            field=models.CharField(choices=[("confluence", "Confluence"), ("web", "Web")], db_index=True, default="confluence", max_length=20),
        ),
        migrations.RunPython(populate_url_identity_and_categories, migrations.RunPython.noop),
    ]
