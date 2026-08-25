"""Thin application service joining secure configuration, Confluence, and bookmarks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings

from bookmark_manager.models import Bookmark, ConnectionStatus
from bookmark_manager.services.bookmark_domain import (
    BookmarkSaveResult,
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    save_bookmark_by_page_id,
)
from bookmark_manager.services.configuration import (
    ActiveConfluenceProfile,
    ConfigurationUnavailable,
    get_active_profile,
)
from bookmark_manager.services.confluence_adapter import (
    ConfluenceAdapter,
    ConfluenceClient,
    ConfluencePage,
    ConfluenceResultCode,
)
from bookmark_manager.services.confluence_validation import PageInputError, parse_page_input
from bookmark_manager.services.secret_store import SecretStore

type ClientFactory = Callable[[ActiveConfluenceProfile], ConfluenceClient]


@dataclass(frozen=True, slots=True)
class BookmarkActionError(Exception):
    code: str
    message: str
    configuration_state: str = ""

    def __str__(self) -> str:
        return self.message


def _default_client_factory(profile: ActiveConfluenceProfile) -> ConfluenceClient:
    return ConfluenceAdapter(
        profile.origin,
        profile.token,
        auth_mode=profile.auth_mode,
        timeout_seconds=settings.CONFLUENCE_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes=settings.CONFLUENCE_MAX_RESPONSE_BYTES,
    )


def _snapshot(page: ConfluencePage) -> ConfluencePageSnapshot:
    ancestors = tuple(
        ConfluenceNodeSnapshot(
            page_id=ancestor.page_id,
            title=ancestor.title,
            url=ancestor.url,
            space_key=page.space_key,
            sibling_position=ancestor.position,
        )
        for ancestor in page.ancestors
    )
    return ConfluencePageSnapshot(
        page_id=page.page_id,
        title=page.title,
        url=page.url,
        space_name=page.space_name,
        space_key=page.space_key,
        version=page.version,
        created_at=page.created_at,
        updated_at=page.updated_at,
        created_by_name=page.creator_name,
        modified_by_name=page.last_modifier_name,
        author_name=page.author_name,
        ancestors=ancestors,
    )


def _result_error(code: ConfluenceResultCode, message: str) -> BookmarkActionError:
    mapping = {
        ConfluenceResultCode.INVALID_CREDENTIAL: (
            "invalid_credential",
            ConnectionStatus.INVALID_CREDENTIAL,
        ),
        ConfluenceResultCode.ACCESS_DENIED: ("access_denied", ConnectionStatus.ACCESS_DENIED),
        ConfluenceResultCode.NOT_FOUND: ("not_found", ""),
        ConfluenceResultCode.RATE_LIMITED: ("rate_limited", ConnectionStatus.RATE_LIMITED),
        ConfluenceResultCode.UNREACHABLE: ("unreachable", ConnectionStatus.UNREACHABLE),
        ConfluenceResultCode.UNSUPPORTED_RESPONSE: (
            "unsupported_response",
            ConnectionStatus.UNSUPPORTED_RESPONSE,
        ),
        ConfluenceResultCode.CONNECTED: ("missing_page", ""),
        ConfluenceResultCode.SUCCESS: ("missing_page", ""),
    }
    error_code, configuration_state = mapping[code]
    return BookmarkActionError(error_code, message, configuration_state)


def save_bookmark_input(
    value: str,
    *,
    secret_store: SecretStore | None = None,
    client_factory: ClientFactory | None = None,
) -> BookmarkSaveResult:
    """Validate one supported input and save/reveal its stable Page ID."""

    try:
        profile = get_active_profile(secret_store=secret_store)
    except ConfigurationUnavailable as exc:
        raise BookmarkActionError(
            "configuration_required",
            str(exc),
            ConnectionStatus.NOT_CONFIGURED,
        ) from exc
    try:
        parsed = parse_page_input(value, profile.origin)
    except PageInputError as exc:
        raise BookmarkActionError(exc.code, str(exc)) from exc

    def load_metadata(page_id: str) -> ConfluencePageSnapshot:
        client = (client_factory or _default_client_factory)(profile)
        result = client.get_page(page_id)
        if not result.ok or result.page is None:
            raise _result_error(result.code, result.message)
        return _snapshot(result.page)

    return save_bookmark_by_page_id(parsed.page_id, load_metadata)


def validated_open_url(
    bookmark: Bookmark,
    *,
    secret_store: SecretStore | None = None,
) -> str:
    """Return an existing bookmark URL only when it remains inside the active origin."""

    try:
        profile = get_active_profile(secret_store=secret_store)
    except ConfigurationUnavailable as exc:
        raise BookmarkActionError(
            "configuration_required",
            str(exc),
            ConnectionStatus.NOT_CONFIGURED,
        ) from exc
    if not profile.origin.contains_application_url(bookmark.url):
        raise BookmarkActionError(
            "unsafe_bookmark_url",
            "The saved URL is outside the active Confluence origin and was not opened.",
        )
    return bookmark.url
