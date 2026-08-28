"""Thin application service joining secure configuration, Confluence, and bookmarks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlsplit

from django.conf import settings

from bookmark_manager.models import Bookmark, ConnectionStatus
from bookmark_manager.services.bookmark_domain import (
    BookmarkSaveResult,
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    save_bookmark_by_page_id,
    upsert_bookmark,
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
from bookmark_manager.services.web_bookmarks import (
    WebBookmarkError,
    canonicalize_web_url,
    category_for_url,
    save_web_bookmark,
)

type ClientFactory = Callable[[ActiveConfluenceProfile], ConfluenceClient]

logger = logging.getLogger("owl.bookmarks")


def _looks_like_confluence_page_url(value: str) -> bool:
    parsed = urlsplit(value)
    path = parsed.path.casefold()
    query = {key.casefold() for key in parse_qs(parsed.query)}
    return (
        ("/spaces/" in path and "/pages/" in path)
        or "/content/" in path
        or (path.endswith("/pages/viewpage.action") and "pageid" in query)
    )


@dataclass(frozen=True, slots=True)
class BookmarkActionError(Exception):
    code: str
    message: str
    configuration_state: str = ""

    def __str__(self) -> str:
        return self.message


def build_confluence_client(profile: ActiveConfluenceProfile) -> ConfluenceClient:
    return ConfluenceAdapter(
        profile.origin,
        profile.token,
        auth_mode=profile.auth_mode,
        timeout_seconds=settings.CONFLUENCE_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes=settings.CONFLUENCE_MAX_RESPONSE_BYTES,
    )


def snapshot_from_confluence_page(page: ConfluencePage) -> ConfluencePageSnapshot:
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
        page_text=page.body_text,
    )


def finish_confluence_bookmark(result: BookmarkSaveResult) -> BookmarkSaveResult:
    canonical_url, hostname = canonicalize_web_url(result.bookmark.url)
    category = category_for_url(canonical_url, hostname)
    changed = []
    for field_name, value in (
        ("canonical_url", canonical_url),
        ("category", category),
        ("source_type", "confluence"),
    ):
        if getattr(result.bookmark, field_name) != value:
            setattr(result.bookmark, field_name, value)
            changed.append(field_name)
    if changed:
        result.bookmark.save(update_fields=changed)
    return result


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

    candidate = str(value or "").strip()
    logger.info(
        "Bookmark save started input_type=%s",
        "url" if candidate.casefold().startswith(("http://", "https://")) else "page_id",
    )
    profile = None
    try:
        profile = get_active_profile(secret_store=secret_store)
    except ConfigurationUnavailable as exc:
        if candidate.casefold().startswith(("http://", "https://")):
            try:
                return save_web_bookmark(candidate)
            except WebBookmarkError as exc:
                raise BookmarkActionError("invalid_url", str(exc)) from exc
        raise BookmarkActionError(
            "configuration_required",
            "Connect Confluence before saving a numeric Page ID.",
            ConnectionStatus.NOT_CONFIGURED,
        ) from exc

    origin_check_candidate = candidate
    if candidate.casefold().startswith(("http://", "https://")):
        origin_check_candidate = urlsplit(candidate)._replace(fragment="").geturl()
    if candidate.casefold().startswith(
        ("http://", "https://")
    ) and not profile.origin.contains_application_url(origin_check_candidate):
        if _looks_like_confluence_page_url(candidate):
            try:
                parse_page_input(candidate, profile.origin)
            except PageInputError as exc:
                raise BookmarkActionError(exc.code, str(exc)) from exc
        try:
            return save_web_bookmark(candidate)
        except WebBookmarkError as exc:
            raise BookmarkActionError("invalid_url", str(exc)) from exc
    if candidate.casefold().startswith(("http://", "https://")):
        try:
            canonical_candidate, _hostname = canonicalize_web_url(candidate)
        except WebBookmarkError as exc:
            raise BookmarkActionError("invalid_url", str(exc)) from exc
        existing_by_url = Bookmark.objects.filter(canonical_url=canonical_candidate).first()
        if existing_by_url is not None and (
            existing_by_url.source_type == "web" or existing_by_url.page_text
        ):
            logger.info(
                "Bookmark already exists bookmark_id=%s page_id=%s source_requested=false",
                existing_by_url.pk,
                existing_by_url.page_id,
            )
            return BookmarkSaveResult(
                existing_by_url,
                created=False,
                source_requested=False,
            )
    try:
        parsed = parse_page_input(value, profile.origin)
    except PageInputError as exc:
        logger.warning("Bookmark input rejected reason=%s", exc.code)
        raise BookmarkActionError(exc.code, str(exc)) from exc

    logger.info(
        "Confluence page identified page_id=%s input_kind=%s",
        parsed.page_id,
        parsed.kind.value,
    )

    client = (client_factory or build_confluence_client)(profile)

    def load_metadata(page_id: str) -> ConfluencePageSnapshot:
        logger.info("Fetching selected Confluence page page_id=%s descendants=false", page_id)
        result = client.get_page(page_id)
        if not result.ok or result.page is None:
            logger.warning(
                "Confluence page fetch failed page_id=%s result=%s",
                page_id,
                result.code.value,
            )
            raise _result_error(result.code, result.message)
        snapshot = snapshot_from_confluence_page(result.page)
        logger.info(
            "Confluence page loaded page_id=%s ancestors=%d page_text_characters=%d",
            page_id,
            len(snapshot.ancestors),
            len(snapshot.page_text),
        )
        return snapshot

    existing = Bookmark.objects.filter(page_id=parsed.page_id).first()
    if existing is not None and not existing.page_text:
        result = replace(
            upsert_bookmark(load_metadata(parsed.page_id)),
            source_requested=True,
        )
    else:
        result = save_bookmark_by_page_id(parsed.page_id, load_metadata)
    result = finish_confluence_bookmark(result)
    logger.info(
        "Bookmark save completed page_id=%s bookmark_id=%s created=%s source_requested=%s",
        parsed.page_id,
        result.bookmark.pk,
        result.created,
        result.source_requested,
    )
    return result


def validated_open_url(
    bookmark: Bookmark,
    *,
    secret_store: SecretStore | None = None,
) -> str:
    """Return an existing bookmark URL only when it remains inside the active origin."""

    if bookmark.source_type == "web":
        try:
            canonical_url, _hostname = canonicalize_web_url(bookmark.url)
        except WebBookmarkError as exc:
            raise BookmarkActionError("unsafe_bookmark_url", str(exc)) from exc
        if canonical_url != bookmark.canonical_url:
            raise BookmarkActionError(
                "unsafe_bookmark_url",
                "The saved URL no longer matches its canonical identity.",
            )
        return canonical_url

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
