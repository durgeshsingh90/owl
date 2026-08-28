from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from django.test import override_settings

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkImportFailure,
    BookmarkImportStatus,
    ConfluenceConfiguration,
    ConfluencePageNode,
)
from bookmark_manager.services import bookmark_application
from bookmark_manager.services.bookmark_application import BookmarkActionError, save_bookmark_input
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.configuration import ActiveConfluenceProfile
from bookmark_manager.services.confluence_adapter import (
    ConfluencePage,
    ConfluenceResult,
    ConfluenceResultCode,
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
        Recoverable https://confluence.example.invalid/wiki/spaces/ENG/pages/910002/Long-page-title...
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
    assert result.run.total_records == 5
    assert result.run.imported_records == 3
    assert result.run.failed_records == 2
    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert saved_urls == [
        "https://confluence.example.invalid/wiki/spaces/ENG/pages/910001/Complete",
        "910002",
        "https://example.com/guides/complete?view=all",
    ]
    failures = list(result.run.failures.all())
    assert [failure.record_number for failure in failures] == [3, 4]
    assert failures[0].source_url == "https://confluence.example.invalid/wiki/spac..."
    assert "Incomplete Confluence URL" in failures[0].reason
    assert failures[1].source_url == ("https://confluence.example.invalid/wiki/spaces/ENG/pages/")
    assert "Incomplete Confluence URL" in failures[1].reason


@pytest.mark.parametrize(
    "url",
    [
        "https://confluence.example.invalid/wiki/spaces/ENG/pages/910003/Page-title…",
        "https://confluence.example.invalid/wiki/pages/viewpage.action?pageId=910003...",
    ],
    ids=("modern-unicode-ellipsis", "legacy-query-ellipsis"),
)
def test_text_import_recovers_truncated_confluence_url_by_page_id(url):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    saved_inputs = []

    result = import_bookmarks_text(
        f"Meeting link {url}",
        bookmark_saver=lambda value: saved_inputs.append(value) or SimpleNamespace(created=True),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.run.imported_records == 1
    assert result.run.failed_records == 0
    assert saved_inputs == ["910003"]


def test_text_import_sends_trusted_exact_title_url_through_normal_save_flow():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    title_url = (
        "https://confluence.example.invalid/wiki/pages/viewpage.action"
        "?spaceKey=ENG&title=Firewall+Authentication+Page"
    )
    saved_inputs = []

    result = import_bookmarks_text(
        f"Meeting page {title_url}",
        bookmark_saver=lambda value: saved_inputs.append(value) or SimpleNamespace(created=True),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.run.imported_records == 1
    assert result.run.failed_records == 0
    assert saved_inputs == [title_url]


def test_text_import_recovery_fetches_and_saves_authoritative_confluence_url(monkeypatch):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    profile = ActiveConfluenceProfile(
        origin=origin,
        token="<synthetic-test-token>",
        auth_mode="bearer",
        source="environment",
    )
    page = ConfluencePage(
        page_id="910004",
        title="Authoritative page title",
        url=(
            "https://confluence.example.invalid/wiki/spaces/ENG/pages/910004/"
            "authoritative-page-title"
        ),
        space_key="ENG",
        space_name="Engineering",
        version=12,
        created_at=datetime(2025, 1, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
        creator_name="Creator",
        author_name="Author",
        last_modifier_name="Modifier",
        ancestors=(),
        body_text="Fresh searchable Confluence page text",
    )
    requested_page_ids = []

    def get_page(page_id):
        requested_page_ids.append(page_id)
        return ConfluenceResult(
            ConfluenceResultCode.SUCCESS,
            "Page loaded.",
            page=page,
        )

    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        lambda **_kwargs: profile,
    )

    result = import_bookmarks_text(
        (
            "https://confluence.example.invalid/wiki/spaces/ENG/pages/910004/"
            "visible-title-was-cut-off..."
        ),
        profile_loader=lambda: profile,
        bookmark_saver=lambda value: save_bookmark_input(
            value,
            client_factory=lambda _profile: SimpleNamespace(get_page=get_page),
        ),
    )

    bookmark = Bookmark.objects.get(page_id="910004")
    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert requested_page_ids == ["910004"]
    assert bookmark.url == page.url
    assert bookmark.canonical_url == page.url
    assert bookmark.title == "Authoritative page title"
    assert bookmark.page_text == "Fresh searchable Confluence page text"


@pytest.mark.parametrize(
    ("truncated_url", "reason_fragment"),
    [
        (
            "https://other-confluence.example.invalid/wiki/spaces/ENG/pages/910003/"
            "Outside+Origin...",
            "configured Confluence origin",
        ),
        (
            "https://confluence.example.invalid/spaces/ENG/pages/910004/Outside+Context...",
            "configured Confluence path",
        ),
    ],
)
def test_text_import_does_not_recover_truncated_confluence_url_outside_profile(
    truncated_url,
    reason_fragment,
):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    complete_url = "https://example.com/guides/remaining"
    saved_inputs = []

    def saver(value):
        saved_inputs.append(value)
        return SimpleNamespace(created=True)

    result = import_bookmarks_text(
        f"Rejected {truncated_url}\nRemaining {complete_url}",
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.run.imported_records == 1
    assert result.run.failed_records == 1
    assert saved_inputs == [complete_url]
    failure = result.run.failures.get()
    assert failure.source_url == truncated_url
    assert reason_fragment in failure.reason


def test_text_import_keeps_unknown_confluence_reference_and_continues_remaining_urls():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    unknown_url = "https://confluence.example.invalid/wiki/spaces/ENG/pages/999999/Unknown+Page..."
    web_url = "https://example.com/guides/remaining"
    saved_inputs = []

    def saver(value):
        saved_inputs.append(value)
        if value == "999999":
            raise BookmarkActionError("not_found", "The Confluence page was not found.")
        return SimpleNamespace(created=True)

    result = import_bookmarks_text(
        f"Unknown {unknown_url}\nRemaining {web_url}",
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.run.imported_records == 2
    assert result.run.failed_records == 0
    assert saved_inputs == ["999999", web_url]
    deleted_reference = Bookmark.objects.get(page_id="999999")
    assert deleted_reference.availability_status == BookmarkAvailability.NOT_FOUND
    assert deleted_reference.last_error_code == "not_found"
    assert deleted_reference.last_error_message == "The Confluence page was not found."
    assert deleted_reference.page_text == ""


def test_text_import_does_not_recover_truncated_non_confluence_url():
    truncated_url = "https://example.com/guides/important-archi..."
    complete_url = "https://example.com/guides/remaining"
    saved_inputs = []

    def saver(value):
        saved_inputs.append(value)
        return SimpleNamespace(created=True)

    result = import_bookmarks_text(
        f"Truncated {truncated_url}\nComplete {complete_url}",
        bookmark_saver=saver,
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.run.imported_records == 1
    assert result.run.failed_records == 1
    assert saved_inputs == [complete_url]
    failure = result.run.failures.get()
    assert failure.source_url == truncated_url
    assert failure.reason == "Incomplete or truncated URL."


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


def _snapshot(page_id: str, title: str, *, page_text: str = "") -> ConfluencePageSnapshot:
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=title,
        url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
        space_name="Engineering",
        space_key="ENG",
        version=7,
        created_at=datetime(2025, 1, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
        page_text=page_text,
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


@pytest.mark.parametrize(
    ("leaf_url", "hierarchy"),
    [
        (
            (
                "https://confluence.example.invalid/wiki/spaces/SIB/pages/817229951/"
                "Akamai+Edge+Routing+%E2%80%94+Design+Requirements"
                "#:~:text=selected%20architecture"
            ),
            [],
        ),
        (
            (
                "https://confluence.example.invalid/wiki/spaces/SIB/pages/817229951/"
                "Akamai+Edge+Routing+%E2%80%94+Design+Requirements"
            ),
            [
                {
                    "pageId": "700001",
                    "title": "Stale imported ancestor",
                    "url": "/relative/confluence/path/that-must-not-be-trusted",
                }
            ],
        ),
        (
            (
                "https://"
                "username:password@confluence.example.invalid/wiki/spaces/SIB/"
                "pages/817229951/Untrusted"
            ),
            [],
        ),
        (
            "https://confluence.example.invalid/wiki/spaces/SIB/pages/817229951/"
            "Untrusted?token=private-token",
            [],
        ),
        ("not an absolute URL", []),
    ],
    ids=(
        "leaf-text-fragment",
        "invalid-imported-hierarchy-url",
        "ignored-userinfo-url",
        "ignored-credential-query",
        "ignored-malformed-url",
    ),
)
def test_json_import_recovers_valid_confluence_page_by_id_from_untrusted_url_metadata(
    leaf_url,
    hierarchy,
):
    """Legacy URL metadata must not prevent an authoritative Page-ID retrieval.

    Browser exports can contain text fragments or stale hierarchy URLs even though
    their numeric Confluence Page ID remains usable.  The JSON importer should use
    only that validated identity and let the normal Confluence save path supply the
    canonical URL, hierarchy, title, and searchable text.
    """

    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []
    canonical_url = (
        "https://confluence.example.invalid/wiki/spaces/SIB/pages/817229951/"
        "authoritative-page-title"
    )

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(
            ConfluencePageSnapshot(
                page_id=page_id,
                title="Authoritative page title",
                url=canonical_url,
                space_name="SIB Service Delivery",
                space_key="SIB",
                version=42,
                page_text="Fresh searchable text fetched from Confluence.",
            )
        )

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Stale exported page title",
                "pageUrl": leaf_url,
                "spaceKey": "SIB",
                "version": 41,
                "hierarchy": hierarchy,
            }
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == ["817229951"]
    bookmark = Bookmark.objects.get(page_id="817229951")
    assert bookmark.url == canonical_url
    assert bookmark.title == "Authoritative page title"
    assert bookmark.page_text == "Fresh searchable text fetched from Confluence."
    assert bookmark.version == 42


@pytest.mark.parametrize(
    "unsafe_url",
    [
        (
            "https://"
            "username:password@confluence.example.invalid/wiki/spaces/SIB/"
            "pages/817229951/Page"
        ),
        (
            "https://confluence.example.invalid/wiki/spaces/SIB/pages/817229951/Page"
            "?token=private-token"
        ),
        "not an absolute URL",
    ],
    ids=("userinfo", "credential-query", "malformed"),
)
def test_json_url_only_identity_does_not_use_unsafe_leaf_url(unsafe_url):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageTitle": "Unsafe imported page",
                "pageUrl": unsafe_url,
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.imported_records == 0
    assert result.failed_records == 1
    assert requested_page_ids == []
    assert not Bookmark.objects.filter(page_id="817229951").exists()


@pytest.mark.parametrize(
    ("explicit_page_id", "page_url", "expected_page_id"),
    [
        (
            "",
            (
                "https://confluence.example.invalid/wiki/spaces/APIGW/pages/810938483/"
                "API+Gateway+%E2%80%94+Outbound"
            ),
            "810938483",
        ),
        (
            "not-a-page-id",
            (
                "https://confluence.example.invalid/wiki/pages/viewpage.action"
                "?pageId=2327367518&spaceKey=APIGW&title=Outbound"
            ),
            "2327367518",
        ),
    ],
    ids=("empty-explicit-modern-url", "invalid-explicit-legacy-url"),
)
def test_json_import_uses_safe_url_identity_when_explicit_page_id_is_unusable(
    explicit_page_id,
    page_url,
    expected_page_id,
):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(_snapshot(page_id, "Fetched by URL identity"))

    result = import_bookmarks_document(
        [
            {
                "pageId": explicit_page_id,
                "pageTitle": "Untrusted imported title",
                "pageUrl": page_url,
            }
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == [expected_page_id]
    assert Bookmark.objects.filter(page_id=expected_page_id).exists()


def test_json_import_uses_one_generic_numeric_candidate_from_trusted_confluence_url():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(_snapshot(page_id, "Fetched from generic URL identity"))

    result = import_bookmarks_document(
        [
            {
                "pageTitle": "Legacy link without a recognised Confluence route",
                "pageUrl": (
                    "https://confluence.example.invalid/wiki/display/ADR/archive/"
                    "2492786022/How+to+SoAR"
                ),
            }
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == ["2492786022"]
    assert Bookmark.objects.filter(page_id="2492786022").exists()


def test_json_url_only_exact_title_lookup_uses_normal_authoritative_save_flow():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    title_url = (
        "https://confluence.example.invalid/wiki/pages/viewpage.action"
        "?spaceKey=ENG&title=Firewall+Authentication+Page"
    )
    saved_inputs = []

    def saver(value):
        saved_inputs.append(value)
        return upsert_bookmark(
            _snapshot(
                "910005",
                "Firewall Authentication Page",
                page_text="Searchable text for Firewall Authentication Page",
            )
        )

    result = import_bookmarks_document(
        [{"pageTitle": "Imported stale title", "pageUrl": title_url}],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert saved_inputs == [title_url]
    bookmark = Bookmark.objects.get(page_id="910005")
    assert bookmark.title == "Firewall Authentication Page"
    assert bookmark.page_text == "Searchable text for Firewall Authentication Page"


def test_json_import_reports_not_found_for_generic_numeric_confluence_candidate():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        raise BookmarkActionError("not_found", "Confluence could not find this page.")

    source_url = (
        "https://confluence.example.invalid/wiki/display/ADR/archive/2492786022/How+to+SoAR"
    )
    result = import_bookmarks_document(
        [{"pageTitle": "Missing legacy page", "pageUrl": source_url}],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == ["2492786022"]
    missing_reference = Bookmark.objects.get(page_id="2492786022")
    assert missing_reference.url == source_url
    assert missing_reference.availability_status == BookmarkAvailability.NOT_FOUND
    assert missing_reference.last_error_code == "not_found"
    assert missing_reference.last_error_message == "Confluence could not find this page."


def test_json_import_rejects_multiple_generic_numeric_candidates_without_fetching():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageTitle": "Ambiguous legacy Confluence link",
                "pageUrl": (
                    "https://confluence.example.invalid/wiki/display/ADR/archive/"
                    "2492786022/revision/31"
                ),
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.imported_records == 0
    assert result.failed_records == 1
    assert requested_page_ids == []
    assert "multiple numeric" in result.run.failures.get().reason.lower()


def test_json_import_does_not_infer_generic_numeric_candidate_from_external_url():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageTitle": "Untrusted external page",
                "pageUrl": "https://external.example.invalid/archive/2492786022/page",
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.imported_records == 0
    assert result.failed_records == 1
    assert requested_page_ids == []
    assert not Bookmark.objects.exists()


@pytest.mark.parametrize(
    "record",
    [
        {
            "page_id": "",
            "pageId": "not-a-page-id",
            "pageTitle": "Masked source identity",
            "pageUrl": "not an absolute URL",
            "source": {"confluencePageId": "817229951"},
        },
        {
            "page_id": "not-a-page-id",
            "pageTitle": "Masked Confluence identity",
            "pageUrl": "not an absolute URL",
            "confluence": {"pageID": "817229951"},
        },
    ],
    ids=("nested-source-alias", "nested-confluence-alias"),
)
def test_json_import_uses_later_valid_explicit_page_id_alias(record):
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(_snapshot(page_id, "Fetched from the valid Page ID alias"))

    result = import_bookmarks_document([record], bookmark_saver=saver)

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == ["817229951"]
    assert Bookmark.objects.filter(page_id="817229951").exists()


@pytest.mark.parametrize("nested_key", ["source", "confluence"])
def test_json_import_rejects_conflicting_explicit_page_id_aliases(nested_key):
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Conflicting explicit identities",
                "pageUrl": "not an absolute URL",
                nested_key: {"confluence_page_id": "810938483"},
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.imported_records == 0
    assert result.failed_records == 1
    assert requested_page_ids == []
    assert result.sanitized_failure_report()[0]["reason"] == (
        "The record contains conflicting Confluence Page IDs."
    )
    assert not Bookmark.objects.exists()


def test_json_import_rejects_conflicting_explicit_and_url_page_ids_without_fetching():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Ambiguous identity",
                "pageUrl": (
                    "https://confluence.example.invalid/wiki/spaces/SIB/pages/"
                    "810938483/Different-page"
                ),
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED_WITH_FAILURES
    assert result.imported_records == 0
    assert result.failed_records == 1
    assert requested_page_ids == []
    assert not Bookmark.objects.exists()


def test_json_import_ignores_invalid_source_metadata_when_page_id_can_be_fetched():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(
            ConfluencePageSnapshot(
                page_id=page_id,
                title="Live Confluence title",
                url=(
                    f"https://confluence.example.invalid/wiki/spaces/SIB/pages/{page_id}/live-title"
                ),
                space_name="Live space",
                space_key="SIB",
                version=9,
                page_text="Live searchable page text",
            )
        )

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": {"not": "text"},
                "pageUrl": "not an absolute URL",
                "version": -10,
                "hierarchy": {"not": "an array"},
            }
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert requested_page_ids == ["817229951"]
    bookmark = Bookmark.objects.get(page_id="817229951")
    assert bookmark.title == "Live Confluence title"
    assert bookmark.version == 9
    assert bookmark.page_text == "Live searchable page text"


def test_json_import_skips_complete_existing_page_without_fetching_or_overwriting_state():
    existing = upsert_bookmark(
        ConfluencePageSnapshot(
            page_id="817229951",
            title="Existing authoritative title",
            url="https://confluence.example.invalid/wiki/pages/817229951/existing",
            version=8,
            page_text="Already indexed searchable text",
        )
    ).bookmark
    existing.notes = "Keep my local note"
    existing.favorite = True
    existing.open_count = 27
    existing.save(update_fields=["notes", "favorite", "open_count"])
    requested_page_ids = []

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Do not overwrite",
                "pageUrl": "not an absolute URL",
                "notes": "Do not replace the local note",
                "favorite": False,
                "opens": 999,
            }
        ],
        bookmark_saver=lambda page_id: requested_page_ids.append(page_id),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 0
    assert result.skipped_records == 1
    assert result.failed_records == 0
    assert requested_page_ids == []
    existing.refresh_from_db()
    assert existing.title == "Existing authoritative title"
    assert existing.notes == "Keep my local note"
    assert existing.favorite is True
    assert existing.open_count == 27
    assert existing.page_text == "Already indexed searchable text"


def test_json_import_fetches_existing_page_when_searchable_text_is_missing():
    existing = upsert_bookmark(
        ConfluencePageSnapshot(
            page_id="817229951",
            title="Existing authoritative title",
            url="https://confluence.example.invalid/wiki/pages/817229951/existing",
            version=8,
            page_text="",
        )
    ).bookmark
    existing.notes = "Keep this personal note while backfilling"
    existing.save(update_fields=["notes"])
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        return upsert_bookmark(
            ConfluencePageSnapshot(
                page_id=page_id,
                title="Fresh title from Confluence",
                url=(
                    "https://confluence.example.invalid/wiki/spaces/SIB/pages/"
                    f"{page_id}/fresh-title"
                ),
                version=9,
                page_text="Backfilled searchable page text",
            )
        )

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Stale imported title",
                "pageUrl": "not an absolute URL",
                "notes": "Do not replace personal state",
            }
        ],
        bookmark_saver=saver,
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 0
    assert result.skipped_records == 1
    assert requested_page_ids == ["817229951"]
    existing.refresh_from_db()
    assert existing.title == "Fresh title from Confluence"
    assert existing.version == 9
    assert existing.page_text == "Backfilled searchable page text"
    assert existing.notes == "Keep this personal note while backfilling"


def test_json_import_keeps_not_found_reference_when_other_fields_cannot_be_normalized():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    requested_page_ids = []

    def saver(page_id):
        requested_page_ids.append(page_id)
        if page_id == "817229951":
            raise BookmarkActionError("not_found", "The Confluence page was not found.")
        return upsert_bookmark(_snapshot(page_id, "Remaining fetched page"))

    result = import_bookmarks_document(
        [
            {
                "pageId": "817229951",
                "pageTitle": "Missing page",
                "pageUrl": (
                    "https://confluence.example.invalid/wiki/pages/817229951/"
                    "Missing?token=must-not-appear"
                ),
            },
            {
                "pageId": "810938483",
                "pageTitle": "Remaining page",
                "pageUrl": "not an absolute URL",
            },
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 2
    assert result.failed_records == 0
    assert requested_page_ids == ["817229951", "810938483"]
    assert Bookmark.objects.filter(page_id="810938483").exists()
    missing_reference = Bookmark.objects.get(page_id="817229951")
    assert missing_reference.availability_status == BookmarkAvailability.NOT_FOUND
    assert missing_reference.last_error_code == "not_found"
    assert missing_reference.url == (
        "https://confluence.example.invalid/wiki/pages/817229951/Missing"
    )
    assert "must-not-appear" not in json.dumps(result.sanitized_failure_report())


def test_json_import_not_found_marks_existing_reference_without_changing_outline_number():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    existing = upsert_bookmark(_snapshot("817229951", "Existing local reference")).bookmark
    existing_node_pk = existing.tree_node_id
    existing_outline_position = existing.tree_node.outline_position
    existing.notes = "Keep this local reference note."
    existing.open_count = 7
    existing.save(update_fields=("notes", "open_count"))

    def saver(_page_id):
        raise BookmarkActionError("not_found", "Confluence could not find this page.")

    result = import_bookmarks_document(
        [
            _legacy_record(
                "817229951",
                "Existing imported title",
                "2026-08-20T10:00:00Z",
                legacyNumber="4.2",
            )
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 0
    assert result.skipped_records == 1
    assert result.failed_records == 0
    existing.refresh_from_db()
    existing.tree_node.refresh_from_db()
    assert existing.tree_node_id == existing_node_pk
    assert existing.tree_node.outline_position == existing_outline_position
    assert existing.availability_status == BookmarkAvailability.NOT_FOUND
    assert existing.last_error_code == "not_found"
    assert existing.last_error_message == "Confluence could not find this page."
    assert existing.last_error_at is not None
    assert existing.page_text == ""
    assert existing.notes == "Keep this local reference note."
    assert existing.open_count == 7
    assert existing.legacy_number == "4.2"


def test_json_import_not_found_creates_reference_with_safe_lowest_free_outline_number():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )
    first = upsert_bookmark(_snapshot("810000001", "First local child")).bookmark
    second = upsert_bookmark(_snapshot("810000002", "Second local child")).bookmark
    deleted_outline_position = first.tree_node.outline_position
    occupied_outline_position = second.tree_node.outline_position
    delete_local_bookmark(first, confirmed=True)

    def saver(_page_id):
        raise BookmarkActionError("not_found", "Confluence could not find this page.")

    result = import_bookmarks_document(
        [
            {
                "pageId": "2492786022",
                "pageTitle": "Deleted Confluence page reference",
                "pageUrl": (
                    "https://confluence.example.invalid/wiki/spaces/ADR/pages/"
                    "2492786022/How+to+SoAR"
                ),
                "spaceKey": "ENG",
                "savedAt": "2026-08-20T10:00:00Z",
                "owl_number": second.pk,
                "pageText": "Imported source text that must not be retained",
                "hierarchy": [
                    {
                        "pageId": "100",
                        "title": "Engineering",
                        "url": "https://confluence.example.invalid/wiki/pages/100",
                    },
                    {
                        "pageId": "200",
                        "title": "Network",
                        "url": "https://confluence.example.invalid/wiki/pages/200",
                    },
                ],
            }
        ],
        bookmark_saver=saver,
        profile_loader=lambda: SimpleNamespace(origin=origin),
    )

    assert result.run.status == BookmarkImportStatus.COMPLETED
    assert result.imported_records == 1
    assert result.skipped_records == 0
    assert result.failed_records == 0
    deleted_reference = Bookmark.objects.get(page_id="2492786022")
    second.refresh_from_db()
    second.tree_node.refresh_from_db()
    assert deleted_reference.pk != second.pk
    assert deleted_reference.tree_node.parent_id == second.tree_node.parent_id
    assert deleted_reference.tree_node.outline_position == deleted_outline_position
    assert second.tree_node.outline_position == occupied_outline_position
    assert deleted_reference.availability_status == BookmarkAvailability.NOT_FOUND
    assert deleted_reference.last_error_code == "not_found"
    assert deleted_reference.page_text == ""
    assert deleted_reference.page_text_size_bytes == 0
    assert deleted_reference.legacy_number == str(second.pk)


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
            "source_url": "",
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
    combined = f"{result.run.filename} {failure.page_id} {failure.source_url} {failure.reason}"
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
    unsafe_failure = BookmarkImportFailure.objects.get(import_run=unsafe_url.run)
    assert unsafe_failure.source_url == ("https://confluence.example.invalid/wiki/pages/300")
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
