"""Read-only, secret-safe Confluence REST adapter.

The concrete transport uses only the Python standard library.  Tests and browser
journeys inject an :class:`HttpTransport`, so they never need a real host or the
user's credential store.
"""

from __future__ import annotations

import errno
import http.client
import json
import socket
import ssl
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from bookmark_manager.services.confluence_validation import (
    AddressResolver,
    CanonicalOrigin,
    OriginResolutionError,
    PageInputError,
    resolve_safe_addresses,
)

DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
MAX_ALLOWED_RESPONSE_BYTES = 8_388_608
MAX_TIMEOUT_SECONDS = 60.0
MAX_REDIRECTS = 3
CHILD_PAGE_LIMIT = 50
MAX_DESCENDANT_PAGES = 500


class ConfluenceResultCode(StrEnum):
    CONNECTED = "connected"
    SUCCESS = "success"
    INVALID_CREDENTIAL = "invalid_credential"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    UNSUPPORTED_RESPONSE = "unsupported_response"


class ConfluenceErrorKind(StrEnum):
    TIMEOUT = "timeout"
    TLS = "tls"
    CONNECTIVITY = "connectivity"
    SERVER = "server"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    UNSAFE_REDIRECT = "unsafe_redirect"


@dataclass(frozen=True, slots=True)
class ConfluenceAncestor:
    page_id: str
    title: str
    url: str
    position: int | None = None


@dataclass(frozen=True, slots=True)
class ConfluencePage:
    page_id: str
    title: str
    url: str
    space_key: str
    space_name: str
    version: int
    created_at: datetime | None
    updated_at: datetime | None
    creator_name: str
    author_name: str
    last_modifier_name: str
    ancestors: tuple[ConfluenceAncestor, ...]
    body_text: str = ""


@dataclass(frozen=True, slots=True)
class ConfluenceResult:
    code: ConfluenceResultCode
    message: str
    page: ConfluencePage | None = None
    error_kind: ConfluenceErrorKind | None = None
    retry_after_seconds: int | None = None
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.code in {ConfluenceResultCode.CONNECTED, ConfluenceResultCode.SUCCESS}


@dataclass(frozen=True, slots=True)
class ConfluenceDescendantsResult:
    """A bounded, normalized recursive descendant crawl for one saved page."""

    code: ConfluenceResultCode
    message: str
    pages: tuple[ConfluencePage, ...] = ()
    error_kind: ConfluenceErrorKind | None = None

    @property
    def ok(self) -> bool:
        return self.code is ConfluenceResultCode.SUCCESS


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One bounded GET.  Sensitive headers are deliberately excluded from repr."""

    url: str
    origin: CanonicalOrigin
    timeout_seconds: float
    max_response_bytes: int
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class HttpTransport(Protocol):
    """Injection boundary; fakes may expose ``request_count`` for journey checks."""

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send one request or raise a standard connectivity/TLS exception."""


class ResponseTooLargeError(ValueError):
    """A response exceeded the caller's explicit byte limit."""


class UnsafeRequestError(ValueError):
    """The transport refused a request before opening a socket."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection pinned to a just-validated address while retaining SNI."""

    def __init__(self, host: str, port: int, address: str, **kwargs) -> None:
        super().__init__(host, port, **kwargs)
        self._validated_address = address

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = self._create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:
            raise UnsafeRequestError("Proxy tunnels are not supported by this transport.")
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP is available only for explicit exact-loopback test fixtures."""

    def __init__(self, host: str, port: int, address: str, **kwargs) -> None:
        super().__init__(host, port, **kwargs)
        self._validated_address = address

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = self._create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:
            raise UnsafeRequestError("Proxy tunnels are not supported by this transport.")


class StdlibHttpTransport:
    """Bounded transport that connects only to freshly validated addresses."""

    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._resolver = resolver
        self._ssl_context = ssl_context or ssl.create_default_context()

    def _addresses(self, origin: CanonicalOrigin) -> tuple[str, ...]:
        if origin.scheme == "http":
            if not origin.is_test_target or origin.host not in {"localhost", "127.0.0.1", "::1"}:
                raise UnsafeRequestError("Plain HTTP is restricted to explicit loopback fixtures.")
            try:
                records = tuple((self._resolver or _loopback_resolver)(origin.host, origin.port))
                addresses = tuple(dict.fromkeys(str(value) for value in records))
                if not addresses or any(not _is_loopback_address(value) for value in addresses):
                    raise UnsafeRequestError("The loopback fixture resolved outside loopback.")
                return addresses
            except (OSError, ValueError) as exc:
                raise UnsafeRequestError(
                    "The loopback fixture could not be resolved safely."
                ) from exc
        return resolve_safe_addresses(origin.host, origin.port, resolver=self._resolver)

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.method != "GET":
            raise UnsafeRequestError("The Confluence adapter is read-only.")
        if not request.origin.is_same_origin_url(request.url):
            raise UnsafeRequestError("The request URL is outside the configured origin.")
        if not request.origin.contains_application_url(request.url):
            raise UnsafeRequestError("The request URL is outside the configured application path.")
        if not 0 < request.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise UnsafeRequestError("The request timeout is outside the safe limit.")
        if not 0 < request.max_response_bytes <= MAX_ALLOWED_RESPONSE_BYTES:
            raise UnsafeRequestError("The response limit is outside the safe limit.")

        parsed = urlsplit(request.url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        addresses = self._addresses(request.origin)
        last_error: OSError | ssl.SSLError | None = None
        for address in addresses:
            connection: http.client.HTTPConnection
            if request.origin.scheme == "https":
                connection = _PinnedHTTPSConnection(
                    request.origin.host,
                    request.origin.port,
                    address,
                    timeout=request.timeout_seconds,
                    context=self._ssl_context,
                )
            else:
                connection = _PinnedHTTPConnection(
                    request.origin.host,
                    request.origin.port,
                    address,
                    timeout=request.timeout_seconds,
                )
            try:
                connection.request("GET", target, headers=dict(request.headers))
                response = connection.getresponse()
                length_header = response.getheader("Content-Length")
                if length_header:
                    try:
                        declared_length = int(length_header)
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > request.max_response_bytes:
                        raise ResponseTooLargeError("The response exceeded the byte limit.")
                body = response.read(request.max_response_bytes + 1)
                if len(body) > request.max_response_bytes:
                    raise ResponseTooLargeError("The response exceeded the byte limit.")
                headers = {name.casefold(): value for name, value in response.getheaders()}
                return HttpResponse(status=response.status, headers=headers, body=body)
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
            finally:
                connection.close()
        if last_error is not None:
            raise last_error
        raise ConnectionError("No validated Confluence address was available.")


def _loopback_resolver(host: str, port: int) -> tuple[str, ...]:
    if host == "localhost":
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(record[4][0] for record in records)
    return (host,)


def _is_loopback_address(value: str) -> bool:
    try:
        import ipaddress

        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class ConfluenceClient(Protocol):
    """Small adapter interface that a request-counting fake can implement."""

    def test_connection(self) -> ConfluenceResult: ...

    def get_page(self, page_id: str) -> ConfluenceResult: ...

    def get_descendant_pages(self, page_id: str) -> ConfluenceDescendantsResult: ...


class ConfluenceAdapter:
    """Bearer-authenticated, read-only adapter for Confluence REST API v1."""

    def __init__(
        self,
        origin: CanonicalOrigin,
        token: str,
        *,
        auth_mode: str = "bearer",
        timeout_seconds: float = 30.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: HttpTransport | None = None,
    ) -> None:
        normalized_token = token.strip()
        if (
            not normalized_token
            or len(normalized_token) > 16_384
            or not normalized_token.isascii()
            or any(ord(character) < 32 for character in normalized_token)
        ):
            raise ValueError("A valid server-side Confluence credential is required.")
        if auth_mode.casefold() != "bearer":
            raise ValueError("Only bearer authentication is supported by this adapter.")
        timeout = float(timeout_seconds)
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError("The Confluence timeout must be between 0 and 60 seconds.")
        if not 0 < max_response_bytes <= MAX_ALLOWED_RESPONSE_BYTES:
            raise ValueError("The Confluence response limit is outside the safe range.")

        self.origin = origin
        self._authorization = f"Bearer {normalized_token}"
        self.timeout_seconds = timeout
        self.max_response_bytes = int(max_response_bytes)
        self._transport = transport or StdlibHttpTransport()

    def __repr__(self) -> str:
        return (
            f"ConfluenceAdapter(origin={self.origin!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_response_bytes={self.max_response_bytes!r})"
        )

    def test_connection(self) -> ConfluenceResult:
        """Make one explicit logical GET and classify the sanitized outcome."""

        url = self.origin.build_url("/rest/api/user/current")
        response_or_result = self._perform_get(url)
        if isinstance(response_or_result, ConfluenceResult):
            return response_or_result
        response = response_or_result
        failure = _classify_status(response, not_found_supported=False)
        if failure is not None:
            return failure
        payload = _decode_object(response.body)
        if (
            payload is None
            or not payload
            or not any(
                key in payload
                for key in ("accountId", "displayName", "type", "userKey", "username")
            )
        ):
            return _malformed_result()
        return ConfluenceResult(
            code=ConfluenceResultCode.CONNECTED,
            message="Connected. Confluence accepted the read-only request.",
            http_status=response.status,
        )

    def get_page(self, page_id: str) -> ConfluenceResult:
        """Fetch and normalize one page and its ordered ancestor chain."""

        try:
            normalized_page_id = _normalize_requested_page_id(page_id)
        except PageInputError:
            return ConfluenceResult(
                code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                message="The requested Confluence Page ID is invalid.",
                error_kind=ConfluenceErrorKind.MALFORMED_RESPONSE,
            )
        query = urlencode({"expand": "ancestors,space,version,history,body.storage"})
        url = self.origin.build_url(
            f"/rest/api/content/{quote(normalized_page_id, safe='')}", query=query
        )
        response_or_result = self._perform_get(url)
        if isinstance(response_or_result, ConfluenceResult):
            return response_or_result
        response = response_or_result
        failure = _classify_status(response, not_found_supported=True)
        if failure is not None:
            return failure
        payload = _decode_object(response.body)
        if payload is None:
            return _malformed_result()
        try:
            page = _normalize_page(payload, normalized_page_id, self.origin)
        except KeyError, TypeError, ValueError:
            return _malformed_result()
        return ConfluenceResult(
            code=ConfluenceResultCode.SUCCESS,
            message="Confluence page metadata was loaded.",
            page=page,
            http_status=response.status,
        )

    def get_descendant_pages(self, page_id: str) -> ConfluenceDescendantsResult:
        """Load child pages recursively, with a fixed 500-page safety limit."""

        try:
            root_page_id = _normalize_requested_page_id(page_id)
        except PageInputError:
            return ConfluenceDescendantsResult(
                code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                message="The requested Confluence Page ID is invalid.",
                error_kind=ConfluenceErrorKind.MALFORMED_RESPONSE,
            )

        pending = [root_page_id]
        seen_page_ids = {root_page_id}
        descendants: list[ConfluencePage] = []
        while pending:
            parent_page_id = pending.pop(0)
            start = 0
            while True:
                query = urlencode(
                    {
                        "expand": "ancestors,space,version,history",
                        "start": start,
                        "limit": CHILD_PAGE_LIMIT,
                    }
                )
                url = self.origin.build_url(
                    f"/rest/api/content/{quote(parent_page_id, safe='')}/child/page",
                    query=query,
                )
                response_or_result = self._perform_get(url)
                if isinstance(response_or_result, ConfluenceResult):
                    return ConfluenceDescendantsResult(
                        code=response_or_result.code,
                        message=response_or_result.message,
                        error_kind=response_or_result.error_kind,
                    )
                failure = _classify_status(response_or_result, not_found_supported=True)
                if failure is not None:
                    return ConfluenceDescendantsResult(
                        code=failure.code,
                        message=failure.message,
                        error_kind=failure.error_kind,
                    )
                payload = _decode_object(response_or_result.body)
                raw_results = payload.get("results") if payload is not None else None
                if not isinstance(raw_results, list) or len(raw_results) > CHILD_PAGE_LIMIT:
                    return _malformed_descendants_result()
                for raw_page in raw_results:
                    if not isinstance(raw_page, dict):
                        return _malformed_descendants_result()
                    try:
                        child_id = _normalize_requested_page_id(
                            _required_text(raw_page.get("id"), max_length=32)
                        )
                        child = _normalize_page(raw_page, child_id, self.origin)
                    except KeyError, TypeError, ValueError:
                        return _malformed_descendants_result()
                    if child.page_id in seen_page_ids:
                        return _malformed_descendants_result()
                    seen_page_ids.add(child.page_id)
                    descendants.append(child)
                    pending.append(child.page_id)
                    if len(descendants) > MAX_DESCENDANT_PAGES:
                        return ConfluenceDescendantsResult(
                            code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                            message=(
                                "This page has more than 500 descendant pages; "
                                "OWL did not save a partial tree."
                            ),
                            error_kind=ConfluenceErrorKind.RESPONSE_TOO_LARGE,
                        )
                if len(raw_results) < CHILD_PAGE_LIMIT:
                    break
                start += len(raw_results)

        return ConfluenceDescendantsResult(
            code=ConfluenceResultCode.SUCCESS,
            message=f"Loaded {len(descendants)} descendant pages.",
            pages=tuple(descendants),
        )

    def _perform_get(self, initial_url: str) -> HttpResponse | ConfluenceResult:
        url = initial_url
        for redirect_count in range(MAX_REDIRECTS + 1):
            request = HttpRequest(
                url=url,
                origin=self.origin,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Authorization": self._authorization,
                    "User-Agent": "OWL/0.1 Confluence read-only adapter",
                },
            )
            try:
                response = self._transport.send(request)
            except ResponseTooLargeError:
                return ConfluenceResult(
                    code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                    message="Confluence returned more data than OWL can safely inspect.",
                    error_kind=ConfluenceErrorKind.RESPONSE_TOO_LARGE,
                )
            except ssl.SSLError:
                return _unreachable_result(ConfluenceErrorKind.TLS)
            except TimeoutError:
                return _unreachable_result(ConfluenceErrorKind.TIMEOUT)
            except OriginResolutionError, UnsafeRequestError, OSError, http.client.HTTPException:
                return _unreachable_result(ConfluenceErrorKind.CONNECTIVITY)

            if (
                isinstance(response.status, bool)
                or not isinstance(response.status, int)
                or not 100 <= response.status <= 599
                or not isinstance(response.body, bytes)
            ):
                return _malformed_result()
            if len(response.body) > self.max_response_bytes:
                return ConfluenceResult(
                    code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                    message="Confluence returned more data than OWL can safely inspect.",
                    error_kind=ConfluenceErrorKind.RESPONSE_TOO_LARGE,
                )
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            if redirect_count == MAX_REDIRECTS:
                return ConfluenceResult(
                    code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                    message="Confluence returned too many redirects.",
                    error_kind=ConfluenceErrorKind.UNSAFE_REDIRECT,
                    http_status=response.status,
                )
            location = _header(response.headers, "location")
            if (
                not location
                or len(location) > 4096
                or "\\" in location
                or any(ord(character) < 32 for character in location)
            ):
                return ConfluenceResult(
                    code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                    message="Confluence returned an incomplete redirect.",
                    error_kind=ConfluenceErrorKind.UNSAFE_REDIRECT,
                    http_status=response.status,
                )
            redirected_url = urljoin(url, location)
            if not self.origin.is_same_origin_url(redirected_url) or _has_sensitive_query(
                redirected_url
            ):
                return ConfluenceResult(
                    code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
                    message="Confluence returned a redirect outside the configured origin.",
                    error_kind=ConfluenceErrorKind.UNSAFE_REDIRECT,
                    http_status=response.status,
                )
            url = redirected_url
        raise AssertionError("The bounded redirect loop must always return.")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == folded:
            return value
    return None


def _has_sensitive_query(value: str) -> bool:
    sensitive_names = {
        "access_token",
        "api_key",
        "authorization",
        "password",
        "pat",
        "secret",
        "token",
    }
    try:
        pairs = parse_qsl(urlsplit(value).query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return True
    return any(name.casefold().replace("-", "_") in sensitive_names for name, _ in pairs)


def _classify_status(
    response: HttpResponse, *, not_found_supported: bool
) -> ConfluenceResult | None:
    status = response.status
    if 200 <= status < 300:
        return None
    if status == 401:
        return ConfluenceResult(
            code=ConfluenceResultCode.INVALID_CREDENTIAL,
            message="Confluence did not accept the credential.",
            http_status=status,
        )
    if status == 403:
        return ConfluenceResult(
            code=ConfluenceResultCode.ACCESS_DENIED,
            message="The credential does not have access to this Confluence resource.",
            http_status=status,
        )
    if status == 404 and not_found_supported:
        return ConfluenceResult(
            code=ConfluenceResultCode.NOT_FOUND,
            message="Confluence could not find this page.",
            http_status=status,
        )
    if status == 429:
        retry_after = _retry_after_seconds(_header(response.headers, "retry-after"))
        message = "Confluence is rate limited. Try again later."
        if retry_after is not None:
            message = f"Confluence is rate limited. Try again in {retry_after} seconds."
        return ConfluenceResult(
            code=ConfluenceResultCode.RATE_LIMITED,
            message=message,
            retry_after_seconds=retry_after,
            http_status=status,
        )
    if 500 <= status <= 599:
        return ConfluenceResult(
            code=ConfluenceResultCode.UNREACHABLE,
            message="Confluence is temporarily unavailable. Try again later.",
            error_kind=ConfluenceErrorKind.SERVER,
            http_status=status,
        )
    return ConfluenceResult(
        code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
        message="Confluence returned a response OWL does not support.",
        error_kind=ConfluenceErrorKind.MALFORMED_RESPONSE,
        http_status=status,
    )


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return min(int(stripped), 86_400)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except TypeError, ValueError, OverflowError:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0, min(int((retry_at - datetime.now(UTC)).total_seconds()), 86_400))


def _decode_object(body: bytes) -> dict[str, object] | None:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _normalize_requested_page_id(page_id: str) -> str:
    value = str(page_id).strip()
    if not value.isascii() or not value.isdigit() or len(value) > 32:
        raise PageInputError("invalid_page_id", "Enter a numeric Confluence Page ID.")
    normalized = value.lstrip("0") or "0"
    if normalized == "0":
        raise PageInputError("invalid_page_id", "Enter a positive Confluence Page ID.")
    return normalized


def _required_text(value: object, *, max_length: int = 4096) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected text metadata.")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError("Metadata text contains unsupported control characters.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        raise ValueError("Metadata text is missing or too long.")
    return normalized


def _optional_text(value: object, *, max_length: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError("Metadata text contains unsupported control characters.")
    normalized = " ".join(value.split())
    if len(normalized) > max_length:
        raise ValueError("Metadata text is too long.")
    return normalized


def _person_name(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for field_name in ("displayName", "publicName", "username"):
        name = _optional_text(value.get(field_name), max_length=255)
        if name:
            return name
    return ""


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Expected a positive integer.")
    return value


def _optional_position(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamp(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Expected an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Expected an ISO timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _page_url(origin: CanonicalOrigin, page_id: str, links: object) -> str:
    raw_link = links.get("webui") if isinstance(links, dict) else None
    if raw_link is None or raw_link == "":
        return origin.build_url("/pages/viewpage.action", query=urlencode({"pageId": page_id}))
    if not isinstance(raw_link, str) or len(raw_link) > 4096:
        raise ValueError("Invalid page URL metadata.")
    candidate = (
        urljoin(f"{origin.origin}/", raw_link)
        if raw_link.startswith("/")
        else urljoin(f"{origin.base_url}/", raw_link)
    )
    if not origin.contains_application_url(candidate):
        raise ValueError("Page metadata URL is outside the configured origin.")
    return candidate


def _normalize_page(
    payload: dict[str, object], requested_page_id: str, origin: CanonicalOrigin
) -> ConfluencePage:
    page_id = _normalize_requested_page_id(_required_text(payload.get("id"), max_length=32))
    if page_id != requested_page_id:
        raise ValueError("Confluence returned a different page identity.")
    page_type = payload.get("type")
    if page_type not in {None, "page"}:
        raise ValueError("Confluence returned a non-page resource.")

    space = payload.get("space")
    version = payload.get("version")
    history = payload.get("history")
    if (
        not isinstance(space, dict)
        or not isinstance(version, dict)
        or not isinstance(history, dict)
    ):
        raise TypeError("Required page metadata is missing.")
    last_updated = history.get("lastUpdated")
    if last_updated is not None and not isinstance(last_updated, dict):
        raise TypeError("Last-updated metadata is malformed.")

    created_at = _timestamp(history.get("createdDate"))
    updated_at = _timestamp(version.get("when"))
    if updated_at is None and isinstance(last_updated, dict):
        updated_at = _timestamp(last_updated.get("when"))

    creator = _person_name(history.get("createdBy"))
    author = _person_name(payload.get("author")) or creator
    modifier = _person_name(version.get("by"))
    if not modifier and isinstance(last_updated, dict):
        modifier = _person_name(last_updated.get("by"))

    raw_ancestors = payload.get("ancestors", [])
    if not isinstance(raw_ancestors, list) or len(raw_ancestors) > 1000:
        raise TypeError("Ancestor metadata is malformed.")
    ancestors: list[ConfluenceAncestor] = []
    seen_ids: set[str] = set()
    for raw_ancestor in raw_ancestors:
        if not isinstance(raw_ancestor, dict):
            raise TypeError("Ancestor metadata is malformed.")
        ancestor_id = _normalize_requested_page_id(
            _required_text(raw_ancestor.get("id"), max_length=32)
        )
        if ancestor_id in seen_ids or ancestor_id == page_id:
            raise ValueError("Ancestor identities are malformed.")
        seen_ids.add(ancestor_id)
        extensions = raw_ancestor.get("extensions")
        position = (
            _optional_position(extensions.get("position")) if isinstance(extensions, dict) else None
        )
        ancestors.append(
            ConfluenceAncestor(
                page_id=ancestor_id,
                title=_required_text(raw_ancestor.get("title")),
                url=_page_url(origin, ancestor_id, raw_ancestor.get("_links")),
                position=position,
            )
        )

    return ConfluencePage(
        page_id=page_id,
        title=_required_text(payload.get("title")),
        url=_page_url(origin, page_id, payload.get("_links")),
        space_key=_required_text(space.get("key"), max_length=255),
        space_name=_required_text(space.get("name"), max_length=255),
        version=_positive_integer(version.get("number")),
        created_at=created_at,
        updated_at=updated_at,
        creator_name=creator,
        author_name=author,
        last_modifier_name=modifier,
        ancestors=tuple(ancestors),
        body_text=_page_body_text(payload.get("body")),
    )


class _ConfluenceTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        candidate = " ".join(data.split())
        if candidate:
            self.parts.append(candidate)


def _page_body_text(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, dict):
        raise TypeError("Page body metadata is malformed.")
    storage = value.get("storage")
    if storage is None or storage == "":
        return ""
    if not isinstance(storage, dict):
        raise TypeError("Page storage metadata is malformed.")
    html = storage.get("value", "")
    if not isinstance(html, str) or len(html) > MAX_ALLOWED_RESPONSE_BYTES:
        raise ValueError("Page storage content is malformed or too large.")
    parser = _ConfluenceTextExtractor()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.parts)


def _malformed_result() -> ConfluenceResult:
    return ConfluenceResult(
        code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
        message="Confluence returned metadata OWL could not safely understand.",
        error_kind=ConfluenceErrorKind.MALFORMED_RESPONSE,
    )


def _malformed_descendants_result() -> ConfluenceDescendantsResult:
    return ConfluenceDescendantsResult(
        code=ConfluenceResultCode.UNSUPPORTED_RESPONSE,
        message="Confluence returned child-page data OWL could not safely understand.",
        error_kind=ConfluenceErrorKind.MALFORMED_RESPONSE,
    )


def _unreachable_result(kind: ConfluenceErrorKind) -> ConfluenceResult:
    messages = {
        ConfluenceErrorKind.TIMEOUT: "Confluence did not respond before the request timed out.",
        ConfluenceErrorKind.TLS: "OWL could not establish a trusted TLS connection to Confluence.",
        ConfluenceErrorKind.CONNECTIVITY: "OWL could not reach the configured Confluence origin.",
    }
    return ConfluenceResult(
        code=ConfluenceResultCode.UNREACHABLE,
        message=messages[kind],
        error_kind=kind,
    )
