from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from bookmark_manager.models import Bookmark, ConfluencePageNode, ConnectionStatus
from bookmark_manager.services import bookmark_application
from bookmark_manager.services.bookmark_application import (
    BookmarkActionError,
    save_bookmark_input,
    validated_open_url,
)
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.configuration import ActiveConfluenceProfile
from bookmark_manager.services.confluence_adapter import (
    ConfluenceAncestor,
    ConfluencePage,
    ConfluenceResult,
    ConfluenceResultCode,
)
from bookmark_manager.services.confluence_validation import CanonicalOrigin

pytestmark = pytest.mark.django_db

SYNTHETIC_PAT = "owl-test-pat-never-valid-bookmark-application"


def active_profile() -> ActiveConfluenceProfile:
    return ActiveConfluenceProfile(
        origin=CanonicalOrigin(
            scheme="https",
            host="confluence.example.invalid",
            port=443,
            context_path="/wiki",
            is_test_target=True,
        ),
        token=SYNTHETIC_PAT,
        auth_mode="bearer",
        source="environment",
    )


def adapter_page(page_id: str = "300") -> ConfluencePage:
    return ConfluencePage(
        page_id=page_id,
        title="Private DNS Architecture",
        url=(
            "https://confluence.example.invalid/wiki/spaces/ENG/pages/"
            f"{page_id}/private-dns-architecture"
        ),
        space_key="ENG",
        space_name="Engineering",
        version=7,
        created_at=datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 8, 24, 16, 15, tzinfo=UTC),
        creator_name="Synthetic Creator",
        author_name="Synthetic Author",
        last_modifier_name="Synthetic Modifier",
        ancestors=(
            ConfluenceAncestor(
                page_id="100",
                title="Engineering",
                url="https://confluence.example.invalid/wiki/spaces/ENG/pages/100",
                position=0,
            ),
            ConfluenceAncestor(
                page_id="200",
                title="Networking",
                url="https://confluence.example.invalid/wiki/spaces/ENG/pages/200",
                position=3,
            ),
        ),
    )


def domain_snapshot(page_id: str = "300") -> ConfluencePageSnapshot:
    return ConfluencePageSnapshot(
        page_id=page_id,
        title="Private DNS Architecture",
        url=f"https://confluence.example.invalid/wiki/spaces/ENG/pages/{page_id}",
        space_name="Engineering",
        space_key="ENG",
        version=7,
        ancestors=(
            ConfluenceNodeSnapshot(
                page_id="100",
                title="Engineering",
                url="https://confluence.example.invalid/wiki/spaces/ENG/pages/100",
                space_key="ENG",
            ),
        ),
    )


class RecordingClient:
    def __init__(self, result: ConfluenceResult) -> None:
        self.result = result
        self.page_ids: list[str] = []

    def get_page(self, page_id: str) -> ConfluenceResult:
        self.page_ids.append(page_id)
        return self.result


class DescendantForbiddenClient(RecordingClient):
    def __init__(self, result: ConfluenceResult) -> None:
        super().__init__(result)
        self.descendant_page_ids: list[str] = []

    def get_descendant_pages(self, page_id: str):
        self.descendant_page_ids.append(page_id)
        raise AssertionError("A single bookmark save must not request descendant pages.")


@pytest.fixture
def configured_profile(monkeypatch) -> ActiveConfluenceProfile:
    profile = active_profile()
    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        lambda **_kwargs: profile,
    )
    return profile


@pytest.mark.parametrize(
    "value",
    [
        " 00300 ",
        "https://confluence.example.invalid/wiki/spaces/ENG/pages/00300/private-dns",
        "https://confluence.example.invalid/wiki/pages/viewpage.action"
        "?pageId=00300&spaceKey=ENG&title=Untrusted%20URL%20Title",
    ],
    ids=("raw-page-id", "modern-url", "legacy-url"),
)
def test_supported_page_inputs_map_adapter_page_and_build_hierarchy(
    configured_profile,
    value,
):
    client = RecordingClient(
        ConfluenceResult(
            ConfluenceResultCode.SUCCESS,
            "Page loaded.",
            page=replace(
                adapter_page(),
                body_text="Private DNS searchable architecture text",
            ),
        )
    )
    factory_profiles = []

    def client_factory(profile):
        factory_profiles.append(profile)
        return client

    result = save_bookmark_input(value, client_factory=client_factory)

    assert result.created is True
    assert result.source_requested is True
    assert client.page_ids == ["300"]
    assert factory_profiles == [configured_profile]

    bookmark = result.bookmark
    assert bookmark.page_id == "300"
    assert bookmark.title == "Private DNS Architecture"
    assert bookmark.page_text == "Private DNS searchable architecture text"
    assert bookmark.space_name == "Engineering"
    assert bookmark.space_key == "ENG"
    assert bookmark.version == 7
    assert bookmark.created_by_name == "Synthetic Creator"
    assert bookmark.modified_by_name == "Synthetic Modifier"
    assert bookmark.author_name == "Synthetic Author"
    assert bookmark.created_at == datetime(2025, 1, 2, 9, 30, tzinfo=UTC)
    assert bookmark.updated_at == datetime(2026, 8, 24, 16, 15, tzinfo=UTC)

    root = ConfluencePageNode.objects.get(page_id="100")
    section = ConfluencePageNode.objects.get(page_id="200")
    page_node = ConfluencePageNode.objects.get(page_id="300")
    assert root.parent is None
    assert root.title == "Engineering"
    assert root.sibling_position == 0
    assert section.parent == root
    assert section.title == "Networking"
    assert section.sibling_position == 3
    assert page_node.parent == section
    assert page_node.title == "Private DNS Architecture"
    assert bookmark.tree_node == page_node


def test_existing_page_with_missing_text_refreshes_only_that_page(configured_profile):
    existing = upsert_bookmark(domain_snapshot()).bookmark
    root = adapter_page("300")
    client = DescendantForbiddenClient(
        ConfluenceResult(ConfluenceResultCode.SUCCESS, "Page loaded.", page=root)
    )
    result = save_bookmark_input(
        "https://confluence.example.invalid/wiki/spaces/ENG/pages/000300/renamed",
        client_factory=lambda _profile: client,
    )

    assert result.created is False
    assert result.bookmark.pk == existing.pk
    assert result.source_requested is True
    assert result.descendant_count == 0
    assert client.page_ids == ["300"]
    assert client.descendant_page_ids == []
    assert Bookmark.objects.count() == 1


def test_new_page_saves_only_selected_bookmark_and_hierarchy_ancestors(configured_profile):
    root = adapter_page("300")
    client = DescendantForbiddenClient(
        ConfluenceResult(ConfluenceResultCode.SUCCESS, "Page loaded.", page=root)
    )

    result = save_bookmark_input("300", client_factory=lambda _profile: client)

    assert result.descendant_count == 0
    assert result.descendants_created == 0
    assert client.page_ids == ["300"]
    assert client.descendant_page_ids == []
    assert list(Bookmark.objects.values_list("page_id", flat=True)) == ["300"]
    assert list(
        ConfluencePageNode.objects.values_list("page_id", flat=True).order_by("page_id")
    ) == ["100", "200", "300"]


def test_wrong_origin_is_rejected_before_client_creation(configured_profile):
    factory_calls = []

    def forbidden_client_factory(profile):
        factory_calls.append(profile)
        raise AssertionError("An unsafe URL must not create a Confluence client")

    with pytest.raises(BookmarkActionError) as captured:
        save_bookmark_input(
            f"https://other.example.invalid/wiki/spaces/ENG/pages/300?token={SYNTHETIC_PAT}",
            client_factory=forbidden_client_factory,
        )

    assert captured.value.code == "disallowed_origin"
    assert factory_calls == []
    assert SYNTHETIC_PAT not in str(captured.value)
    assert SYNTHETIC_PAT not in repr(captured.value)
    assert Bookmark.objects.count() == 0


def test_confluence_save_indexes_only_the_requested_page_text(configured_profile):
    root = replace(adapter_page("300"), body_text="Root page searchable architecture text")
    client = DescendantForbiddenClient(
        ConfluenceResult(
            ConfluenceResultCode.SUCCESS,
            "Loaded page.",
            page=root,
        )
    )

    result = save_bookmark_input("300", client_factory=lambda _profile: client)

    assert result.bookmark.page_text == "Root page searchable architecture text"
    assert client.descendant_page_ids == []
    assert Bookmark.objects.count() == 1


def test_bookmark_save_logs_safe_console_progress(configured_profile, caplog):
    page_text = "Searchable page text that must not be written to logs"
    pasted_url = (
        "https://confluence.example.invalid/wiki/pages/viewpage.action"
        "?pageId=300&spaceKey=ENG&title=Private%20DNS"
    )
    client = DescendantForbiddenClient(
        ConfluenceResult(
            ConfluenceResultCode.SUCCESS,
            "Loaded page.",
            page=replace(adapter_page("300"), body_text=page_text),
        )
    )

    with caplog.at_level(logging.INFO, logger="owl.bookmarks"):
        result = save_bookmark_input(pasted_url, client_factory=lambda _profile: client)

    assert result.bookmark.page_text == page_text
    assert "Confluence page identified page_id=300 input_kind=legacy_url" in caplog.text
    assert "Fetching selected Confluence page page_id=300 descendants=false" in caplog.text
    assert "ancestors=2" in caplog.text
    assert f"page_text_characters={len(page_text)}" in caplog.text
    assert "Bookmark save completed page_id=300" in caplog.text
    assert pasted_url not in caplog.text
    assert page_text not in caplog.text
    assert SYNTHETIC_PAT not in caplog.text


def test_readding_an_old_confluence_bookmark_backfills_missing_page_text(configured_profile):
    existing = upsert_bookmark(domain_snapshot()).bookmark
    page = replace(adapter_page("300"), body_text="Backfilled searchable page text")
    client = RecordingClient(
        ConfluenceResult(ConfluenceResultCode.SUCCESS, "Loaded page.", page=page)
    )

    result = save_bookmark_input("300", client_factory=lambda _profile: client)

    assert result.bookmark.pk == existing.pk
    assert result.bookmark.page_text == "Backfilled searchable page text"
    assert client.page_ids == ["300"]


@pytest.mark.parametrize(
    ("result_code", "expected_code", "expected_configuration_state"),
    [
        (
            ConfluenceResultCode.INVALID_CREDENTIAL,
            "invalid_credential",
            ConnectionStatus.INVALID_CREDENTIAL,
        ),
        (
            ConfluenceResultCode.ACCESS_DENIED,
            "access_denied",
            ConnectionStatus.ACCESS_DENIED,
        ),
        (ConfluenceResultCode.NOT_FOUND, "not_found", ""),
        (
            ConfluenceResultCode.RATE_LIMITED,
            "rate_limited",
            ConnectionStatus.RATE_LIMITED,
        ),
        (
            ConfluenceResultCode.UNREACHABLE,
            "unreachable",
            ConnectionStatus.UNREACHABLE,
        ),
        (
            ConfluenceResultCode.UNSUPPORTED_RESPONSE,
            "unsupported_response",
            ConnectionStatus.UNSUPPORTED_RESPONSE,
        ),
        (ConfluenceResultCode.CONNECTED, "missing_page", ""),
        (ConfluenceResultCode.SUCCESS, "missing_page", ""),
    ],
)
def test_adapter_result_codes_map_to_safe_action_errors(
    configured_profile,
    result_code,
    expected_code,
    expected_configuration_state,
):
    client = RecordingClient(ConfluenceResult(result_code, "Safe synthetic result."))

    with pytest.raises(BookmarkActionError) as captured:
        save_bookmark_input("300", client_factory=lambda _profile: client)

    error = captured.value
    assert client.page_ids == ["300"]
    assert error.code == expected_code
    assert error.configuration_state == expected_configuration_state
    assert str(error) == "Safe synthetic result."
    assert SYNTHETIC_PAT not in str(error)
    assert SYNTHETIC_PAT not in repr(error)
    assert Bookmark.objects.count() == 0


def test_validated_open_url_returns_only_a_url_inside_active_application_origin(
    configured_profile,
):
    bookmark = upsert_bookmark(domain_snapshot()).bookmark

    assert validated_open_url(bookmark) == bookmark.url

    unsafe = replace(
        domain_snapshot(),
        url="https://other.example.invalid/wiki/spaces/ENG/pages/300",
    )
    bookmark.url = unsafe.url

    with pytest.raises(BookmarkActionError) as captured:
        validated_open_url(bookmark)

    assert captured.value.code == "unsafe_bookmark_url"
    assert SYNTHETIC_PAT not in str(captured.value)
    assert SYNTHETIC_PAT not in repr(captured.value)


def test_profile_and_invalid_input_errors_do_not_reveal_credentials(configured_profile):
    assert SYNTHETIC_PAT not in repr(configured_profile)

    with pytest.raises(BookmarkActionError) as captured:
        save_bookmark_input(SYNTHETIC_PAT, client_factory=lambda _profile: None)

    assert captured.value.code == "invalid_page_url"
    assert SYNTHETIC_PAT not in str(captured.value)
    assert SYNTHETIC_PAT not in repr(captured.value)
