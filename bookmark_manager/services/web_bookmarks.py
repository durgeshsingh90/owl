"""General web bookmark identity, domain categories, and local persistence."""

from __future__ import annotations

import hashlib
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.db import transaction

from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkSource,
    ConfluencePageNode,
)
from bookmark_manager.services.bookmark_domain import BookmarkSaveResult


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
        defaults={"title": category.name, "url": "", "space_key": hostname},
    )
    node, _created = ConfluencePageNode.objects.update_or_create(
        provisional_key=f"url:{digest}",
        defaults={
            "title": _default_bookmark_title(canonical_url, hostname),
            "url": canonical_url,
            "space_key": hostname,
            "parent": category_node,
        },
    )
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
    return BookmarkSaveResult(bookmark, created=True, source_requested=False)


@transaction.atomic
def rename_bookmark_category(category: BookmarkCategory, name: str) -> BookmarkCategory:
    category.name = name
    try:
        category.save(update_fields=("name", "updated_at"))
    except ValidationError as exc:
        message = exc.message_dict.get("name", ["Enter a valid category name."])[0]
        raise WebBookmarkError(message) from exc
    ConfluencePageNode.objects.filter(
        provisional_key=f"domain:{hashlib.sha256(category.domain.encode('utf-8')).hexdigest()}"
    ).update(title=category.name)
    return category
