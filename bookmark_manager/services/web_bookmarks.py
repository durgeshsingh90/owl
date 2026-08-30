"""General web bookmark identity, domain categories, and local persistence."""

from __future__ import annotations

import hashlib
import logging
from functools import wraps
from time import perf_counter
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.db import transaction

from bookmark_manager.models import (
    Bookmark,
    BookmarkActivityType,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkSource,
    ConfluencePageNode,
)
from bookmark_manager.services.bookmark_analytics import record_daily_activity
from bookmark_manager.services.bookmark_domain import BookmarkSaveResult
from bookmark_manager.services.bookmark_outline import (
    ensure_outline_position,
    next_outline_position,
)
from bookmark_manager.services.logging_events import get_logger, log_event

logger = get_logger("actions")


def _logged_web_bookmark(operation: str):
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            started = perf_counter()
            log_event(logger, logging.INFO, "web_bookmark_requested", operation=operation)
            try:
                result = function(*args, **kwargs)
            except WebBookmarkError as error:
                log_event(
                    logger,
                    logging.WARNING,
                    "web_bookmark_rejected",
                    error=error,
                    operation=operation,
                    reason="invalid_input",
                )
                raise
            except Exception as error:
                log_event(
                    logger, logging.ERROR, "web_bookmark_failed", error=error, operation=operation
                )
                raise
            context = (
                {
                    "bookmark_id": result.bookmark.pk,
                    "status": "created" if result.created else "existing",
                }
                if isinstance(result, BookmarkSaveResult)
                else {"status": "updated"}
            )
            log_event(
                logger,
                logging.INFO,
                "web_bookmark_completed",
                operation=operation,
                elapsed_ms=round((perf_counter() - started) * 1000),
                **context,
            )
            return result

        return invoke

    return decorate


class WebBookmarkError(ValueError):
    pass


def canonicalize_web_url(value: str) -> tuple[str, str]:
    """Return a fragment-free HTTP(S) URL and its normalized hostname."""

    candidate = str(value or "").strip()
    if len(candidate) > 2048:
        raise WebBookmarkError("The URL is too long.")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise WebBookmarkError("Enter a valid HTTP or HTTPS URL.") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise WebBookmarkError("Enter a complete HTTP or HTTPS URL.")
    if parsed.username is not None or parsed.password is not None:
        raise WebBookmarkError("URLs containing usernames or passwords cannot be saved.")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise WebBookmarkError("The URL contains an invalid domain name.") from exc
    if not hostname or len(hostname) > 253:
        raise WebBookmarkError("The URL contains an invalid domain name.")

    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    port_suffix = "" if port is None or default_port else f":{port}"
    netloc = f"[{hostname}]{port_suffix}" if ":" in hostname else f"{hostname}{port_suffix}"
    path = parsed.path or "/"
    canonical = urlunsplit(SplitResult(parsed.scheme.casefold(), netloc, path, parsed.query, ""))
    return canonical, hostname


def _default_bookmark_title(canonical_url: str, hostname: str) -> str:
    parsed = urlsplit(canonical_url)
    final_segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).strip()
    if final_segment and final_segment.casefold() not in {"index.html", "index.htm"}:
        readable = " ".join(final_segment.replace("-", " ").replace("_", " ").split())
        if readable:
            return readable[:500]
    return hostname.removeprefix("www.")[:500]


def category_for_url(canonical_url: str, hostname: str | None = None) -> BookmarkCategory:
    domain = hostname or canonicalize_web_url(canonical_url)[1]
    category, _created = BookmarkCategory.objects.get_or_create(
        domain=domain,
        defaults={"name": domain.removeprefix("www.")},
    )
    return category


@_logged_web_bookmark("save_web")
@transaction.atomic
def save_web_bookmark(value: str) -> BookmarkSaveResult:
    canonical_url, hostname = canonicalize_web_url(value)
    existing = Bookmark.objects.filter(canonical_url=canonical_url).first()
    if existing is not None:
        return BookmarkSaveResult(existing, created=False, source_requested=False)

    category = category_for_url(canonical_url, hostname)
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    category_node, _created = ConfluencePageNode.objects.get_or_create(
        provisional_key=f"domain:{hashlib.sha256(hostname.encode('utf-8')).hexdigest()}",
        defaults={
            "title": category.name,
            "url": "",
            "space_key": hostname,
            "outline_position": next_outline_position(parent_id=None),
        },
    )
    ensure_outline_position(category_node, parent_id=None)
    node, _created = ConfluencePageNode.objects.update_or_create(
        provisional_key=f"url:{digest}",
        defaults={
            "title": _default_bookmark_title(canonical_url, hostname),
            "url": canonical_url,
            "space_key": hostname,
            "parent": category_node,
        },
        create_defaults={
            "title": _default_bookmark_title(canonical_url, hostname),
            "url": canonical_url,
            "space_key": hostname,
            "parent": category_node,
            "outline_position": next_outline_position(parent_id=category_node.pk),
        },
    )
    ensure_outline_position(node, parent_id=category_node.pk)
    bookmark = Bookmark.objects.create(
        page_id=f"w{digest[:63]}",
        tree_node=node,
        title=node.title,
        url=canonical_url,
        canonical_url=canonical_url,
        category=category,
        source_type=BookmarkSource.WEB,
        availability_status=BookmarkAvailability.ACTIVE,
    )
    record_daily_activity(
        BookmarkActivityType.ADDED,
        occurred_at=bookmark.saved_at,
    )
    return BookmarkSaveResult(bookmark, created=True, source_requested=False)


@_logged_web_bookmark("rename_category")
@transaction.atomic
def rename_bookmark_category(
    category: BookmarkCategory,
    name: str,
    *,
    description: str | None = None,
) -> BookmarkCategory:
    category.name = name
    if description is not None:
        category.description = description
    try:
        category.save(update_fields=("name", "description", "updated_at"))
    except ValidationError as exc:
        message = next(
            iter(exc.message_dict.get("name", ()) or exc.message_dict.get("description", ())),
            "Enter valid domain details.",
        )
        raise WebBookmarkError(message) from exc
    ConfluencePageNode.objects.filter(
        provisional_key=f"domain:{hashlib.sha256(category.domain.encode('utf-8')).hexdigest()}"
    ).update(title=category.name)
    return category
