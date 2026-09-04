"""Validation helpers for the server-side Confluence boundary.

The functions in this module are deliberately independent of Django and HTTP.  A
synthetic test profile can therefore validate an ``.invalid`` origin without a DNS
lookup, while the real transport can require a fresh safe-address resolution
immediately before opening a socket.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_ALLOWED_HTTPS_PORTS = frozenset({443, 8443, 9443})
MAX_PAGE_ID_DIGITS = 32
APPROVED_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

type AddressResolver = Callable[[str, int], Iterable[str]]


class OriginValidationError(ValueError):
    """A safe, user-correctable problem with a configured Confluence origin."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OriginResolutionError(OriginValidationError):
    """The origin could not be proven to resolve only to approved addresses."""


class PageInputError(ValueError):
    """A safe page-input error that never echoes the untrusted value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PageInputKind(StrEnum):
    PAGE_ID = "page_id"
    MODERN_URL = "modern_url"
    LEGACY_URL = "legacy_url"


@dataclass(frozen=True, slots=True)
class CanonicalOrigin:
    """Canonical HTTPS origin plus the optional Confluence application path."""

    scheme: str
    host: str
    port: int
    context_path: str
    is_test_target: bool = False

    def __post_init__(self) -> None:
        if self.scheme not in {"http", "https"}:
            raise OriginValidationError("https_required", "Confluence must use HTTPS.")
        if _canonical_host(self.host) != self.host:
            raise OriginValidationError("invalid_host", "Enter a canonical Confluence host.")
        if not 1 <= self.port <= 65535:
            raise OriginValidationError("invalid_port", "Enter a valid Confluence port.")
        if _validate_context_path(self.context_path) != self.context_path:
            raise OriginValidationError(
                "invalid_context_path", "Enter a canonical Confluence application path."
            )
        if self.scheme == "http" and not (
            self.is_test_target and self.host in {"localhost", "127.0.0.1", "::1"}
        ):
            raise OriginValidationError("https_required", "Confluence must use HTTPS.")

    @property
    def authority(self) -> str:
        display_host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        if self.port == default_port:
            return display_host
        return f"{display_host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"

    @property
    def base_url(self) -> str:
        return f"{self.origin}{self.context_path}"

    def build_url(self, path: str, *, query: str = "") -> str:
        """Build a URL below the configured application context."""

        if not path.startswith("/") or "\\" in path or "#" in path or "?" in path:
            raise ValueError("A root-relative path without a query or fragment is required.")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        return url

    def is_same_origin_url(self, value: str) -> bool:
        """Return whether a URL uses this exact scheme, host, and effective port."""

        try:
            parsed = urlsplit(value)
            port = parsed.port
            host = _canonical_host(parsed.hostname or "")
        except (OriginValidationError, ValueError):
            return False
        return (
            parsed.scheme.casefold() == self.scheme
            and parsed.username is None
            and parsed.password is None
            and host == self.host
            and (port or (443 if parsed.scheme.casefold() == "https" else 80)) == self.port
            and not parsed.fragment
        )

    def contains_application_url(self, value: str) -> bool:
        """Return whether a same-origin URL remains inside the context path."""

        if not self.is_same_origin_url(value):
            return False
        parsed = urlsplit(value)
        return _path_is_within_context(parsed.path, self.context_path)


@dataclass(frozen=True, slots=True)
class ParsedPageInput:
    page_id: str
    kind: PageInputKind


@dataclass(frozen=True, slots=True)
class ParsedPageLookup:
    """A same-origin legacy URL whose page identity must be resolved upstream."""

    space_key: str
    title: str


def _canonical_host(host: str) -> str:
    candidate = host.strip().rstrip(".").casefold()
    if not candidate or len(candidate) > 253:
        raise OriginValidationError("invalid_host", "Enter a valid Confluence host name.")
    if "%" in candidate:
        raise OriginValidationError("invalid_host", "IPv6 zone identifiers are not allowed.")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OriginValidationError("invalid_host", "Enter a valid Confluence host name.") from exc
    labels = ascii_host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise OriginValidationError("invalid_host", "Enter a valid Confluence host name.")
    return ascii_host


def _validate_context_path(raw_path: str) -> str:
    if not raw_path or raw_path == "/":
        return ""
    if not raw_path.startswith("/") or "\\" in raw_path or "//" in raw_path:
        raise OriginValidationError(
            "invalid_context_path", "Enter a valid Confluence application path."
        )
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(character) < 32 for character in decoded):
        raise OriginValidationError(
            "invalid_context_path", "Enter a valid Confluence application path."
        )
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise OriginValidationError(
            "invalid_context_path", "The Confluence application path cannot traverse folders."
        )
    return raw_path.rstrip("/")


def _is_forbidden_hostname(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return host in {
        "instance-data",
        "instance-data.ec2.internal",
        "metadata",
        "metadata.google.internal",
    }


def _is_allowed_target_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allow internal Data Center addresses but reject special/local socket targets."""

    return bool(
        (address.is_global or _is_approved_internal_address(address))
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _is_approved_internal_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(
        address.version == network.version and address in network
        for network in APPROVED_INTERNAL_NETWORKS
    )


def _system_resolver(host: str, port: int) -> Iterable[str]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return (record[4][0] for record in records)


def resolve_safe_addresses(
    host: str,
    port: int,
    *,
    resolver: AddressResolver | None = None,
) -> tuple[str, ...]:
    """Resolve a host and reject the entire result if any address is unsafe.

    RFC1918 and unique-local addresses are valid for an approved internal Data Center
    deployment.  A mixture of public and private answers is rejected as a rebinding
    pattern rather than allowing socket fallback to change trust classes.
    """

    canonical_host = _canonical_host(host)
    if _is_forbidden_hostname(canonical_host):
        raise OriginResolutionError(
            "disallowed_target", "The Confluence host is not an approved network target."
        )

    try:
        literal = ipaddress.ip_address(canonical_host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_allowed_target_ip(literal):
            raise OriginResolutionError(
                "disallowed_target", "The Confluence host is not an approved network target."
            )
        return (str(literal),)

    try:
        answers = tuple((resolver or _system_resolver)(canonical_host, port))
    except (OSError, socket.gaierror) as exc:
        raise OriginResolutionError(
            "dns_failure", "The Confluence host could not be resolved."
        ) from exc
    if not answers:
        raise OriginResolutionError("dns_failure", "The Confluence host could not be resolved.")

    normalized: list[str] = []
    for raw_answer in answers:
        try:
            address = ipaddress.ip_address(raw_answer)
        except ValueError as exc:
            raise OriginResolutionError(
                "dns_failure", "The Confluence host returned an invalid network address."
            ) from exc
        if not _is_allowed_target_ip(address):
            raise OriginResolutionError(
                "disallowed_target", "The Confluence host resolved to a disallowed target."
            )
        value = str(address)
        if value not in normalized:
            normalized.append(value)
    address_classes = {
        _is_approved_internal_address(ipaddress.ip_address(value)) for value in normalized
    }
    if len(address_classes) > 1:
        raise OriginResolutionError(
            "mixed_dns_target",
            "The Confluence host returned an unsafe mixture of network addresses.",
        )
    return tuple(normalized)


def validate_confluence_origin(
    value: str,
    *,
    resolver: AddressResolver | None = None,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_HTTPS_PORTS,
    allow_test_targets: bool = False,
) -> CanonicalOrigin:
    """Validate and canonicalize a base URL without retaining untrusted text.

    Normal validation resolves the target before accepting it.  Deterministic
    ``.invalid`` and exact-loopback fixtures require the explicit test-only switch.
    The real HTTPS transport also resolves again immediately before opening a socket.
    """

    raw = value.strip()
    if not raw or any(ord(character) < 32 for character in raw) or "\\" in raw:
        raise OriginValidationError("invalid_origin", "Enter a valid HTTPS Confluence base URL.")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise OriginValidationError("invalid_port", "Enter a valid Confluence HTTPS port.") from exc

    scheme = parsed.scheme.casefold()
    if parsed.username is not None or parsed.password is not None:
        raise OriginValidationError(
            "embedded_credentials", "Do not put credentials in the Confluence URL."
        )
    if parsed.query or parsed.fragment:
        raise OriginValidationError(
            "unexpected_url_parts", "The Confluence base URL cannot contain a query or fragment."
        )
    host = _canonical_host(parsed.hostname or "")
    exact_loopback_host = host in {"localhost", "127.0.0.1", "::1"}
    synthetic_invalid_host = host.endswith(".invalid")
    loopback_http_fixture = allow_test_targets and scheme == "http" and exact_loopback_host
    synthetic_https_fixture = allow_test_targets and scheme == "https" and synthetic_invalid_host
    if scheme != "https" and not loopback_http_fixture:
        raise OriginValidationError("https_required", "Confluence must use HTTPS.")
    if _is_forbidden_hostname(host) and not loopback_http_fixture:
        raise OriginValidationError(
            "disallowed_target", "The Confluence host is not an approved network target."
        )
    effective_port = port or (443 if scheme == "https" else 80)
    if not loopback_http_fixture and effective_port not in allowed_ports:
        raise OriginValidationError("unsafe_port", "Use an approved Confluence HTTPS port.")
    if loopback_http_fixture and not 1 <= effective_port <= 65535:
        raise OriginValidationError("invalid_port", "Enter a valid fixture port.")
    context_path = _validate_context_path(parsed.path)
    origin = CanonicalOrigin(
        scheme,
        host,
        effective_port,
        context_path,
        is_test_target=loopback_http_fixture or synthetic_https_fixture,
    )

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _is_allowed_target_ip(literal) and not loopback_http_fixture:
        raise OriginValidationError(
            "disallowed_target", "The Confluence host is not an approved network target."
        )
    if not synthetic_https_fixture and not loopback_http_fixture:
        resolve_safe_addresses(host, effective_port, resolver=resolver)
    return origin


def _path_is_within_context(path: str, context_path: str) -> bool:
    if not context_path:
        return path.startswith("/")
    return path == context_path or path.startswith(f"{context_path}/")


def _validated_page_id(value: str) -> str:
    if not value.isascii() or not value.isdigit() or len(value) > MAX_PAGE_ID_DIGITS:
        raise PageInputError("invalid_page_id", "Enter a numeric Confluence Page ID.")
    normalized = value.lstrip("0") or "0"
    if normalized == "0":
        raise PageInputError("invalid_page_id", "Enter a positive Confluence Page ID.")
    return normalized


_PAGE_ID_QUERY_KEYS = frozenset({"pageid", "contentid"})
_PAGE_ID_PATH_MARKERS = frozenset({"page", "pages", "content"})


def _normalized_query_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _page_id_from_url_parts(relative_path: str, query: str) -> tuple[str, bool]:
    """Return one unambiguous page identity and whether it came from the URL path."""

    try:
        query_fields = parse_qs(query, keep_blank_values=True)
    except ValueError as exc:
        raise PageInputError(
            "unsupported_page_url", "The Confluence page URL is malformed."
        ) from exc

    candidates: list[tuple[str, bool]] = []
    for key, values in query_fields.items():
        if _normalized_query_key(key) not in _PAGE_ID_QUERY_KEYS:
            continue
        if len(values) != 1:
            raise PageInputError(
                "unsupported_page_url", "The Confluence URL must contain one Page ID."
            )
        candidates.append((_validated_page_id(values[0]), False))

    segments = [unquote(segment) for segment in relative_path.split("/") if segment]
    marker_candidates: list[str] = []
    for index, segment in enumerate(segments[:-1]):
        next_segment = segments[index + 1]
        if (
            segment.casefold() in _PAGE_ID_PATH_MARKERS
            and next_segment.isascii()
            and next_segment.isdigit()
        ):
            marker_candidates.append(_validated_page_id(next_segment))

    if marker_candidates:
        candidates.extend((page_id, True) for page_id in marker_candidates)
    else:
        # Future Confluence routes may rename the marker before the ID. Accept a
        # path only when it contains exactly one numeric segment, so dates and
        # other ambiguous paths never silently select the wrong page.
        numeric_segments = [
            _validated_page_id(segment)
            for segment in segments
            if segment.isascii() and segment.isdigit()
        ]
        if len(numeric_segments) == 1:
            candidates.append((numeric_segments[0], True))
        elif len(set(numeric_segments)) > 1:
            raise PageInputError(
                "unsupported_page_url",
                "The Confluence URL contains multiple numeric Page ID candidates.",
            )

    identities = {page_id for page_id, _from_path in candidates}
    if not identities:
        raise PageInputError(
            "unsupported_page_url", "The Confluence URL does not contain a Page ID."
        )
    if len(identities) != 1:
        raise PageInputError(
            "unsupported_page_url", "The Confluence URL contains conflicting Page IDs."
        )
    page_id = identities.pop()
    return page_id, any(candidate == page_id and from_path for candidate, from_path in candidates)


def extract_page_id_from_url(value: str) -> str | None:
    """Return an unambiguous Page ID from any HTTP(S) URL without contacting Confluence.

    This deliberately does not validate the URL against the configured Confluence
    origin.  It is only a local bookmark-discovery helper; ``parse_page_input``
    remains the security boundary before a URL can be fetched or saved.
    """

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        page_id, _from_path = _page_id_from_url_parts(parsed.path, parsed.query)
    except PageInputError:
        return None
    return page_id


def parse_page_input(value: str, configured_origin: CanonicalOrigin) -> ParsedPageInput:
    """Parse a raw Page ID or a supported same-origin Confluence page URL."""

    raw = value.strip()
    if not raw or any(ord(character) < 32 for character in raw) or "\\" in raw:
        raise PageInputError(
            "invalid_page_input", "Enter a Page ID or supported Confluence page URL."
        )
    if raw.isascii() and raw.isdigit():
        return ParsedPageInput(_validated_page_id(raw), PageInputKind.PAGE_ID)

    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise PageInputError("invalid_page_url", "Enter a valid Confluence page URL.") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PageInputError("invalid_page_url", "Enter a valid Confluence page URL.")
    origin_check_url = parsed._replace(fragment="").geturl()
    if not configured_origin.is_same_origin_url(origin_check_url):
        raise PageInputError(
            "disallowed_origin", "The page URL must use the configured Confluence origin."
        )
    if not configured_origin.contains_application_url(origin_check_url):
        raise PageInputError(
            "disallowed_context", "The page URL must use the configured Confluence path."
        )

    relative_path = parsed.path[len(configured_origin.context_path) :]
    page_id, from_path = _page_id_from_url_parts(relative_path, parsed.query)
    legacy_path = relative_path.casefold() in {"/pages/viewpage.action", "/viewpage.action"}
    kind = PageInputKind.LEGACY_URL if legacy_path and not from_path else PageInputKind.MODERN_URL
    return ParsedPageInput(page_id, kind)


def parse_page_lookup_input(value: str, configured_origin: CanonicalOrigin) -> ParsedPageLookup:
    """Parse an exact title lookup from a trusted legacy Confluence page URL.

    The lookup form is deliberately narrow.  It accepts only Confluence's legacy
    ``viewpage.action`` route, requires one non-empty title, and never treats an
    arbitrary same-origin URL as a page search.  The configured origin remains
    the trust boundary before any authenticated request is made.
    """

    raw = str(value or "").strip()
    if not raw or len(raw) > 8192 or any(ord(character) < 32 for character in raw) or "\\" in raw:
        raise PageInputError("invalid_page_url", "Enter a valid Confluence page URL.")

    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise PageInputError("invalid_page_url", "Enter a valid Confluence page URL.") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PageInputError("invalid_page_url", "Enter a valid Confluence page URL.")

    origin_check_url = parsed._replace(fragment="").geturl()
    if not configured_origin.is_same_origin_url(origin_check_url):
        raise PageInputError(
            "disallowed_origin", "The page URL must use the configured Confluence origin."
        )
    if not configured_origin.contains_application_url(origin_check_url):
        raise PageInputError(
            "disallowed_context", "The page URL must use the configured Confluence path."
        )

    relative_path = parsed.path[len(configured_origin.context_path) :]
    if relative_path.casefold() not in {"/pages/viewpage.action", "/viewpage.action"}:
        raise PageInputError(
            "unsupported_page_url",
            "The Confluence URL must contain a Page ID or an exact page title.",
        )

    try:
        query_fields = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise PageInputError(
            "unsupported_page_url", "The Confluence page URL is malformed."
        ) from exc

    title_values: list[str] = []
    space_values: list[str] = []
    for key, values in query_fields.items():
        normalized_key = _normalized_query_key(key)
        if normalized_key == "title":
            title_values.extend(values)
        elif normalized_key == "spacekey":
            space_values.extend(values)

    if len(title_values) != 1:
        raise PageInputError(
            "unsupported_page_url",
            "The Confluence URL must contain one exact page title.",
        )
    if len(space_values) > 1:
        raise PageInputError(
            "unsupported_page_url",
            "The Confluence URL must contain at most one space key.",
        )

    raw_title = title_values[0]
    raw_space_key = space_values[0] if space_values else ""
    if any(ord(character) < 32 for character in raw_title + raw_space_key):
        raise PageInputError(
            "unsupported_page_url",
            "The Confluence URL contains unsupported title or space-key characters.",
        )
    title = " ".join(raw_title.split())
    space_key = " ".join(raw_space_key.split())
    if (
        not title
        or len(title) > 500
        or any(ord(character) < 32 for character in title)
        or "\\" in title
    ):
        raise PageInputError(
            "unsupported_page_url", "The Confluence URL must contain one exact page title."
        )
    if (
        len(space_key) > 255
        or any(ord(character) < 32 for character in space_key)
        or "\\" in space_key
    ):
        raise PageInputError(
            "unsupported_page_url", "The Confluence URL contains an invalid space key."
        )
    return ParsedPageLookup(space_key=space_key, title=title)
