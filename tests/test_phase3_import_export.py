from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from django.test import override_settings

from bookmark_manager.models import (
    Bookmark,
    BookmarkImportFailure,
    BookmarkImportStatus,
    ConfluenceConfiguration,
    ConfluencePageNode,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.confluence_validation import validate_confluence_origin
from bookmark_manager.services.deletion import (
    DeleteConfirmationRequired,
    delete_local_bookmark,
    delete_local_bookmarks,
)
from bookmark_manager.services.import_export import (
    DOCUMENT_TYPE,
    EXPORT_SCHEMA_VERSION,
    ImportDocumentError,
    export_bookmarks_document,
    export_bookmarks_json,
    import_bookmarks_document,
    import_bookmarks_text,
)

pytestmark = pytest.mark.django_db


def test_text_import_extracts_unique_urls_and_continues_after_incomplete_confluence_link():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    saved_urls = []

    def saver(url):
        saved_urls.append(url)
        return SimpleNamespace(created=True)

    result = import_bookmarks_text(
        """
        Complete https://confluence.example.invalid/wiki/spaces/ENG/pages/910001/Complete
        Truncated https://confluence.example.invalid/wiki/spac...
        Missing ID https://confluence.example.invalid/wiki/spaces/ENG/pages/
        Web https://example.com/guides/complete?view=all
        Duplicate https://example.com/guides/complete?view=all
        """,
        filename="meeting-chat.txt",
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.schema_version == "text-urls-v1"
    assert result.run.total_records == 4
    assert result.run.imported_records == 2
    assert result.run.failed_records == 2
    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert saved_urls == [
        "https://confluence.example.invalid/wiki/spaces/ENG/pages/910001/Complete",
        "https://example.com/guides/complete?view=all",
    ]
    failures = list(result.run.failures.all())
    assert [failure.record_number for failure in failures] == [2, 3]
    assert "confluence.example.invalid/wiki/spac..." in failures[0].reason
    assert "Incomplete or truncated URL" in failures[0].reason
    assert "confluence.example.invalid/wiki/spaces/ENG/pages/" in failures[1].reason
    assert "incomplete Confluence URL" in failures[1].reason


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"plain meeting transcript without a link", "does not contain"),
        (b"\xff\xfe", "valid UTF-8"),
    ],
)
def test_text_import_rejects_files_without_extractable_utf8_urls(payload, message):
    with pytest.raises(ImportDocumentError, match=message):
        import_bookmarks_text(payload)


def _legacy_record(page_id: str, title: str, saved_at: str, **overrides):
    record = {
        "pageId": page_id,
        "pageTitle": title,
        "pageUrl": f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
        "spaceKey": "ENG",
        "savedAt": saved_at,
        "breadcrumb": "Engineering > Network",
    }
    record.update(overrides)
    return record


def _snapshot(page_id: str, title: str) -> ConfluencePageSnapshot:
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
        space_name="Engineering",
        space_key="ENG",
        version=7,
        created_at=datetime(2025, 1, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
        ancestors=(
            ConfluenceNodeSnapshot(
                page_id="100",
                title="Engineering",
                url="https://confluence.example.invalid/wiki/pages/100",
                space_key="ENG",
            ),
            ConfluenceNodeSnapshot(
                page_id="200",
                title="Network",
                url="https://confluence.example.invalid/wiki/pages/200",
                space_key="ENG",
            ),
        ),
    )


def test_bmk_015_heterogeneous_import_continues_and_assigns_deterministic_numbers():
    payload = [
        _legacy_record(
            "300",
            "Later bookmark",
            "2026-08-20T10:00:00Z",
            favourite="yes",
            pin=1,
            tag_names="Architecture; Network",
            note="Local migration note",
            opens="4",
            breadcrumb="Engineering > Network > Later bookmark",
        ),
        _legacy_record("400", "Earlier bookmark", "20/08/2025 09:30:00"),
        _legacy_record("00400", "Duplicate identity", "2026-08-21T09:30:00Z"),
        "a malformed record that must not abort the valid records",
    ]

    result = import_bookmarks_document(
        json.dumps(payload),
        filename="/private/migration/bookmarks.json",
        imported_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        batch_size=1,
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.run.filename == "bookmarks.json"
    assert result.total_records == 4
    assert result.imported_records == 2
    assert result.skipped_records == 1
    assert result.failed_records == 1
    assert result.run.processed_records == 4
    assert Bookmark.objects.get(page_id="400").pk < Bookmark.objects.get(page_id="300").pk

    imported = Bookmark.objects.get(page_id="300")
    assert imported.favorite is True
    assert imported.pinned is True
    assert imported.notes == "Local migration note"
    assert imported.open_count == 4
    assert list(imported.tags.values_list("normalized_name", flat=True)) == [
        "architecture",
        "network",
    ]
    assert imported.tree_node.parent.title == "Network"
    assert imported.tree_node.parent.provisional_key.startswith("import:v1:")
    assert not ConfluencePageNode.objects.filter(
        page_id__isnull=True,
        title="Later bookmark",
    ).exists()

    report = result.sanitized_failure_report()
    assert report == (
        {
            "record_number": 4,
            "page_id": "",
            "reason": "The record must be a JSON object.",
        },
    )


def test_bmk_015_reimport_is_idempotent_and_existing_owl_owned_state_wins():
    original_payload = [
        _legacy_record(
            "300",
            "Imported bookmark",
            "2026-08-20T10:00:00Z",
            notes="Imported note",
            tags=["Imported tag"],
            favorite=True,
            pinned=True,
            open_count=8,
        )
    ]
    first = import_bookmarks_document(original_payload)
    bookmark = Bookmark.objects.get(page_id="300")
    original_pk = bookmark.pk
    original_run_id = bookmark.import_run_id
    local_tag = bookmark.tags.model.objects.get_or_create_normalized("Local tag")[0]
    bookmark.notes = "OWL note wins"
    bookmark.favorite = False
    bookmark.pinned = False
    bookmark.open_count = 99
    bookmark.save(update_fields=["notes", "favorite", "pinned", "open_count"])
    bookmark.tags.set([local_tag])

    second = import_bookmarks_document(original_payload)

    bookmark.refresh_from_db()
    assert first.imported_records == 1
    assert second.imported_records == 0
    assert second.skipped_records == 1
    assert Bookmark.objects.count() == 1
    assert bookmark.pk == original_pk
    assert bookmark.notes == "OWL note wins"
    assert bookmark.favorite is False
    assert bookmark.pinned is False
    assert bookmark.open_count == 99
    assert list(bookmark.tags.values_list("name", flat=True)) == ["Local tag"]
    assert bookmark.import_run_id == original_run_id


def test_import_fills_empty_notes_and_tags_without_overwriting_other_personal_state():
    bookmark = upsert_bookmark(_snapshot("300", "Existing bookmark")).bookmark
    bookmark.favorite = True
    bookmark.open_count = 3
    bookmark.save(update_fields=["favorite", "open_count"])

    result = import_bookmarks_document(
        [
            _legacy_record(
                "300",
                "Imported title must not replace source title",
                "2026-08-20T10:00:00Z",
                notes="Fill an empty note",
                tags=["Filled tag"],
                favorite=False,
                open_count=50,
                author_name="Imported author metadata",
            )
        ]
    )

    bookmark.refresh_from_db()
    assert result.skipped_records == 1
    assert bookmark.title == "Existing bookmark"
    assert bookmark.notes == "Fill an empty note"
    assert list(bookmark.tags.values_list("name", flat=True)) == ["Filled tag"]
    assert bookmark.favorite is True
    assert bookmark.open_count == 3
    assert bookmark.author_name == "Imported author metadata"


def test_breadcrumb_provisional_nodes_merge_when_real_ancestor_ids_arrive():
    first = import_bookmarks_document([_legacy_record("300", "First leaf", "2026-08-20T10:00:00Z")])
    provisional_ids = list(
        ConfluencePageNode.objects.filter(page_id__isnull=True)
        .order_by("pk")
        .values_list("pk", flat=True)
    )

    second = import_bookmarks_document(
        [
            {
                "page_id": "301",
                "title": "Second leaf",
                "url": "https://confluence.example.invalid/wiki/pages/301",
                "space_key": "ENG",
                "saved_at": "2026-08-21T10:00:00Z",
                "hierarchy": [
                    {"page_id": "100", "title": "Engineering"},
                    {"page_id": "200", "title": "Network"},
                ],
            }
        ]
    )

    assert first.imported_records == second.imported_records == 1
    assert (
        list(
            ConfluencePageNode.objects.filter(page_id__in=["100", "200"])
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        == provisional_ids
    )
    assert not ConfluencePageNode.objects.filter(page_id__isnull=True).exists()
    assert Bookmark.objects.get(page_id="300").tree_node.parent.page_id == "200"
    assert Bookmark.objects.get(page_id="301").tree_node.parent.page_id == "200"


@override_settings(CONFLUENCE_PAT="synthetic-export-pat-never-valid-value")
def test_bmk_016_versioned_export_round_trips_supported_data_without_credentials():
    result = import_bookmarks_document(
        [
            _legacy_record(
                "300",
                "Round-trip bookmark",
                "2025-07-06T08:30:00+01:00",
                notes="A personal but synthetic note",
                tags=["Network", "Architecture"],
                favorite=True,
                pinned=True,
                open_count=5,
                first_opened_at="2025-07-07T08:00:00Z",
                last_viewed_at="2026-08-24T17:00:00Z",
                last_viewed_version=3,
                availability_status="refresh_error",
                last_error_code="timeout",
                last_error_message=("authorization: Bearer synthetic-export-pat-never-valid-value"),
            )
        ]
    )
    assert result.imported_records == 1
    bookmark = Bookmark.objects.get(page_id="300")
    Bookmark.objects.filter(pk=bookmark.pk).update(
        url=(
            "https://confluence.example.invalid/wiki/viewpage.action"
            "?pageId=300&token=synthetic-export-pat-never-valid-value"
        )
    )
    ConfluencePageNode.objects.filter(pk=bookmark.tree_node_id).update(
        url=(
            "https://synthetic-export-pat-never-valid-value:password@"
            "confluence.example.invalid/wiki/pages/300"
        )
    )
    ConfluenceConfiguration.objects.create(
        base_url="https://configured-host.example.invalid/wiki",
    )

    generated_at = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    document = export_bookmarks_document(generated_at=generated_at)
    rendered = export_bookmarks_json(generated_at=generated_at)

    assert document["document_type"] == DOCUMENT_TYPE
    assert document["schema_version"] == EXPORT_SCHEMA_VERSION
    assert document["generated_at"] == "2026-08-25T12:00:00Z"
    assert document["record_count"] == 1
    assert document["integrity"]["algorithm"] == "sha256"
    assert len(document["integrity"]["content_sha256"]) == 64
    assert "synthetic-export-pat-never-valid-value" not in rendered
    assert "configured-host.example.invalid" not in rendered
    assert "credential" not in rendered.casefold()
    assert "authorization" in rendered.casefold()
    assert "[REDACTED]" in rendered
    assert document["bookmarks"][0]["source"]["url"].endswith("?pageId=300")
    assert document["bookmarks"][0]["hierarchy"][-1]["url"] == ""
    original_owl_number = document["bookmarks"][0]["owl_number"]

    delete_local_bookmark(Bookmark.objects.get(page_id="300"), confirmed=True)
    round_trip = import_bookmarks_document(document, filename="round-trip.json")

    restored = Bookmark.objects.get(page_id="300")
    assert round_trip.imported_records == 1
    assert restored.title == "Round-trip bookmark"
    assert restored.saved_at == datetime(2025, 7, 6, 7, 30, tzinfo=UTC)
    assert restored.favorite is True
    assert restored.pinned is True
    assert restored.notes == "A personal but synthetic note"
    assert restored.open_count == 5
    assert restored.last_viewed_version == 3
    assert restored.availability_status == "refresh_error"
    assert list(restored.tags.values_list("normalized_name", flat=True)) == [
        "architecture",
        "network",
    ]
    assert restored.tree_node.parent.title == "Network"
    assert restored.legacy_number == str(original_owl_number)


def test_current_export_integrity_tampering_is_rejected_before_writes():
    import_bookmarks_document([_legacy_record("300", "Original", "2026-08-20T10:00:00Z")])
    document = export_bookmarks_document()
    document["bookmarks"][0]["source"]["title"] = "Tampered"
    delete_local_bookmark(Bookmark.objects.get(page_id="300"), confirmed=True)

    with pytest.raises(ImportDocumentError, match="integrity check failed"):
        import_bookmarks_document(document)

    assert Bookmark.objects.count() == 0


@override_settings(CONFLUENCE_PAT="synthetic-failure-pat-never-valid-value")
def test_import_validates_utf8_size_and_sanitizes_failure_surfaces():
    with pytest.raises(ImportDocumentError, match="UTF-8"):
        import_bookmarks_document(b"\xff\xfe")
    with pytest.raises(ImportDocumentError, match="size limit"):
        import_bookmarks_document("[]", max_bytes=1)
    with pytest.raises(ImportDocumentError, match="batch size"):
        import_bookmarks_document([], batch_size="not-a-number")

    result = import_bookmarks_document(
        [
            {
                "page_id": "not-a-page-id",
                "title": "synthetic-failure-pat-never-valid-value",
            }
        ],
        filename="synthetic-failure-pat-never-valid-value.json",
    )

    assert result.failed_records == 1
    failure = BookmarkImportFailure.objects.get(import_run=result.run)
    combined = f"{result.run.filename} {failure.page_id} {failure.reason}"
    assert "synthetic-failure-pat-never-valid-value" not in combined
    assert "[REDACTED]" in result.run.filename

    unsafe_url = import_bookmarks_document(
        [
            {
                "page_id": "300",
                "title": "Unsafe URL",
                "url": (
                    "https://confluence.example.invalid/wiki/pages/300"
                    "?token=synthetic-failure-pat-never-valid-value"
                ),
            }
        ]
    )
    assert unsafe_url.failed_records == 1
    assert "synthetic-failure-pat-never-valid-value" not in json.dumps(
        unsafe_url.sanitized_failure_report()
    )
    assert not Bookmark.objects.filter(page_id="300").exists()


def test_bmk_017_local_delete_requires_confirmation_and_preserves_shared_tree():
    first = upsert_bookmark(_snapshot("300", "First leaf")).bookmark
    second = upsert_bookmark(_snapshot("301", "Second leaf")).bookmark
    root_id = ConfluencePageNode.objects.get(page_id="100").pk
    shared_parent_id = ConfluencePageNode.objects.get(page_id="200").pk
    first_leaf_id = first.tree_node_id

    with pytest.raises(DeleteConfirmationRequired):
        delete_local_bookmark(first)
    assert Bookmark.objects.filter(pk=first.pk).exists()

    result = delete_local_bookmark(first, confirmed=True)

    assert result.owl_number == first.pk
    assert result.page_id == "300"
    assert result.pruned_node_count == 1
    assert not Bookmark.objects.filter(pk=first.pk).exists()
    assert not ConfluencePageNode.objects.filter(pk=first_leaf_id).exists()
    assert Bookmark.objects.filter(pk=second.pk).exists()
    assert ConfluencePageNode.objects.filter(pk=root_id).exists()
    assert ConfluencePageNode.objects.filter(pk=shared_parent_id).exists()

    final = delete_local_bookmark(second.pk, confirmed=True)
    assert final.pruned_node_count == 3
    assert Bookmark.objects.count() == 0
    assert ConfluencePageNode.objects.count() == 0


def test_bulk_delete_is_atomic_when_any_selected_bookmark_is_missing():
    first = upsert_bookmark(_snapshot("310", "First selected leaf")).bookmark
    second = upsert_bookmark(_snapshot("311", "Second selected leaf")).bookmark

    with pytest.raises(Bookmark.DoesNotExist):
        delete_local_bookmarks((first.pk, 999_999, second.pk), confirmed=True)

    assert Bookmark.objects.filter(pk__in=(first.pk, second.pk)).count() == 2


def test_bulk_delete_removes_selected_bookmarks_and_prunes_their_branch():
    first = upsert_bookmark(_snapshot("320", "First selected leaf")).bookmark
    second = upsert_bookmark(_snapshot("321", "Second selected leaf")).bookmark

    result = delete_local_bookmarks((first.pk, second.pk, first.pk), confirmed=True)

    assert result.deleted_count == 2
    assert result.pruned_node_count == 4
    assert Bookmark.objects.count() == 0
    assert ConfluencePageNode.objects.count() == 0
