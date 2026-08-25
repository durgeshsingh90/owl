from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkImportFailure,
    BookmarkImportRun,
    BookmarkImportStatus,
    BookmarkRecency,
    ConfluencePageNode,
    SavedBookmarkView,
    Tag,
)

pytestmark = pytest.mark.django_db


def make_bookmark(page_id: str = "300", *, updated_at=None) -> Bookmark:
    node = ConfluencePageNode.objects.create(
        page_id=page_id,
        title=f"Synthetic page {page_id}",
        url=f"https://confluence.example.invalid/wiki/pages/{page_id}",
        space_key="ENG",
    )
    return Bookmark.objects.create(
        page_id=page_id,
        tree_node=node,
        title=node.title,
        url=node.url,
        space_name="Engineering",
        space_key="ENG",
        version=7,
        updated_at=updated_at,
    )


def test_tag_identity_is_unicode_aware_case_insensitive_and_many_to_many():
    first, created = Tag.objects.get_or_create_normalized("  Private   DNS  ")
    same, created_again = Tag.objects.get_or_create_normalized("PRIVATE DNS")
    street, _ = Tag.objects.get_or_create_normalized("Straße")
    same_street, street_created_again = Tag.objects.get_or_create_normalized("STRASSE")

    bookmark = make_bookmark()
    bookmark.tags.add(first, street)

    assert created is True
    assert created_again is False
    assert same == first
    assert first.name == "Private DNS"
    assert first.normalized_name == "private dns"
    assert street_created_again is False
    assert same_street == street
    assert list(first.bookmarks.all()) == [bookmark]
    assert set(bookmark.tags.values_list("normalized_name", flat=True)) == {
        "private dns",
        "strasse",
    }


def test_tag_normalization_is_enforced_at_save_and_database_boundaries():
    tag = Tag.objects.create(name="  ＡＷＳ  Networking ")

    assert tag.name == "AWS Networking"
    assert tag.normalized_name == "aws networking"

    with pytest.raises(ValidationError, match="cannot be empty"):
        Tag.objects.create(name=" \t ")

    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.create(name="aws NETWORKING")


def test_saved_view_persists_only_durable_query_state_with_normalized_identity():
    saved_view = SavedBookmarkView.objects.create(
        name="  My   Network View ",
        search_text="  private dns  ",
        filters={"favorite": True, "tags": ["networking"]},
        sort="most_opened",
        visible_columns=["status", "updated_at", "tags"],
    )

    assert saved_view.name == "My Network View"
    assert saved_view.normalized_name == "my network view"
    assert SavedBookmarkView.normalize_name("MY network view") == saved_view.normalized_name
    assert saved_view.search_text == "private dns"
    assert saved_view.filters == {"favorite": True, "tags": ["networking"]}
    assert saved_view.visible_columns == ["status", "updated_at", "tags"]
    assert not hasattr(saved_view, "selected_bookmarks")
    assert not hasattr(saved_view, "expanded_nodes")

    saved_view.name = "Renamed View"
    saved_view.save(update_fields=["name"])
    saved_view.refresh_from_db()
    assert saved_view.normalized_name == "renamed view"


def test_saved_view_rejects_case_insensitive_duplicates_and_invalid_json_shapes():
    SavedBookmarkView.objects.create(name="Recently Changed")

    with pytest.raises(IntegrityError), transaction.atomic():
        SavedBookmarkView.objects.create(name="RECENTLY   CHANGED")

    with pytest.raises(ValidationError, match="filters must be a JSON object"):
        SavedBookmarkView.objects.create(name="Bad filters", filters=["favorite"])

    with pytest.raises(ValidationError, match="JSON list of names"):
        SavedBookmarkView.objects.create(name="Bad columns", visible_columns=["title", 7])


def test_recency_is_calculated_from_elapsed_windows_and_new_yields_over_updated():
    observed_at = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    bookmark = make_bookmark(updated_at=observed_at - timedelta(days=1))

    Bookmark.objects.filter(pk=bookmark.pk).update(saved_at=observed_at - timedelta(days=30))
    bookmark.refresh_from_db()
    assert bookmark.recency_at(at=observed_at) == BookmarkRecency.NEW

    Bookmark.objects.filter(pk=bookmark.pk).update(saved_at=observed_at - timedelta(days=31))
    bookmark.refresh_from_db()
    assert bookmark.recency_at(at=observed_at) == BookmarkRecency.UPDATED

    bookmark.availability_status = BookmarkAvailability.ACCESS_DENIED
    assert bookmark.recency_at(at=observed_at) == BookmarkRecency.NORMAL

    bookmark.availability_status = BookmarkAvailability.ACTIVE
    bookmark.updated_at = observed_at - timedelta(days=31)
    assert bookmark.recency_at(at=observed_at) == BookmarkRecency.NORMAL


def test_recency_rejects_ambiguous_time_inputs_and_invalid_windows():
    bookmark = make_bookmark()

    with pytest.raises(ValueError, match="timezone-aware"):
        bookmark.recency_at(at=datetime(2026, 8, 25, 12, 0))
    with pytest.raises(ValueError, match="positive days"):
        bookmark.recency_at(
            at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
            new_duration_days=0,
        )


def test_import_run_sanitizes_provenance_and_enforces_consistent_progress():
    source_hash = "AB" * 32
    run = BookmarkImportRun.objects.create(
        filename=r"C:\private\legacy-bookmarks.json",
        schema_version="  2 ",
        source_sha256=source_hash,
        total_records=4,
        processed_records=3,
        imported_records=2,
        skipped_records=1,
        outcome="Import\nprogress\taccepted",
    )

    assert run.filename == "legacy-bookmarks.json"
    assert run.schema_version == "2"
    assert run.source_sha256 == source_hash.casefold()
    assert run.outcome == "Import progress accepted"
    assert run.status == BookmarkImportStatus.PENDING

    with pytest.raises(ValidationError, match="SHA-256"):
        BookmarkImportRun.objects.create(
            filename="bad.json",
            source_sha256="not-a-hash",
        )

    with pytest.raises(ValidationError, match="cannot exceed the total"):
        BookmarkImportRun.objects.create(
            filename="bad-progress.json",
            total_records=1,
            processed_records=2,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        BookmarkImportRun.objects.filter(pk=run.pk).update(imported_records=4)


def test_finished_import_requires_a_completion_timestamp():
    with pytest.raises(ValidationError, match="record when it completed"):
        BookmarkImportRun.objects.create(
            filename="finished.json",
            status=BookmarkImportStatus.COMPLETED,
        )


def test_import_failure_is_sanitized_ordered_and_unique_per_record():
    run = BookmarkImportRun.objects.create(filename="legacy.json", total_records=3)
    third = BookmarkImportFailure.objects.create(
        import_run=run,
        record_number=3,
        page_id="  300\n",
        reason="Malformed\nrecord\tcontent",
    )
    first = BookmarkImportFailure.objects.create(
        import_run=run,
        record_number=1,
        reason="Missing Page ID",
    )

    assert third.page_id == "300"
    assert third.reason == "Malformed record content"
    assert list(run.failures.all()) == [first, third]

    with pytest.raises(IntegrityError), transaction.atomic():
        BookmarkImportFailure.objects.create(
            import_run=run,
            record_number=3,
            reason="A second reason for the same source record",
        )

    with pytest.raises(ValidationError, match="start at one"):
        BookmarkImportFailure.objects.create(
            import_run=run,
            record_number=0,
            reason="Invalid ordinal",
        )


def test_bookmark_import_provenance_is_owl_owned_and_run_deletion_is_non_destructive():
    run = BookmarkImportRun.objects.create(
        filename="legacy.json",
        source_sha256="12" * 32,
        total_records=1,
    )
    bookmark = make_bookmark()
    bookmark.import_run = run
    bookmark.import_record_number = 8
    bookmark.legacy_number = "legacy-42"
    bookmark.notes = "Keep this local note."
    bookmark.favorite = True
    bookmark.save(
        update_fields=[
            "import_run",
            "import_record_number",
            "legacy_number",
            "notes",
            "favorite",
        ]
    )

    run.delete()
    bookmark.refresh_from_db()

    assert bookmark.import_run is None
    assert bookmark.import_record_number == 8
    assert bookmark.legacy_number == "legacy-42"
    assert bookmark.notes == "Keep this local note."
    assert bookmark.favorite is True
