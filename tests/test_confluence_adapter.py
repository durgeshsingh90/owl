from __future__ import annotations

import json
import ssl
from collections import deque
from dataclasses import dataclass, field

import pytest

from bookmark_manager.services import confluence_adapter as confluence_module
from bookmark_manager.services.confluence_adapter import (
    ConfluenceAdapter,
    ConfluenceErrorKind,
    ConfluenceResultCode,
    HttpRequest,
    HttpResponse,
    ResponseTooLargeError,
    StdlibHttpTransport,
    UnsafeRequestError,
)
from bookmark_manager.services.confluence_validation import validate_confluence_origin


@dataclass
class ScriptedTransport:
    outcomes: deque[HttpResponse | BaseException]
    request_count: int = 0
    safe_request_facts: list[tuple[str, str, float, int]] = field(default_factory=list)
    saw_expected_authorization: bool = False

    def send(self, request: HttpRequest) -> HttpResponse:
        self.request_count += 1
        self.safe_request_facts.append(
            (request.method, request.url, request.timeout_seconds, request.max_response_bytes)
        )
        self.saw_expected_authorization = request.headers.get("Authorization") == (
            "Bearer owl-test-pat-never-valid"
        )
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(status: int, payload: object = None, **headers: str) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode()
    return HttpResponse(status=status, headers=headers, body=body)


@pytest.fixture
def origin():
    return validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )


def adapter_with(origin, *outcomes: HttpResponse | BaseException):
    transport = ScriptedTransport(deque(outcomes))
    adapter = ConfluenceAdapter(
        origin,
        "owl-test-pat-never-valid",
        timeout_seconds=7,
        max_response_bytes=4096,
        transport=transport,
    )
    return adapter, transport


def test_connection_is_explicit_single_bounded_read_only_request(origin):
    adapter, transport = adapter_with(origin, response(200, {"displayName": "Synthetic OWL User"}))

    assert transport.request_count == 0
    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.CONNECTED
    assert result.ok is True
    assert result.page is None
    assert transport.request_count == 1
    assert transport.saw_expected_authorization is True
    assert transport.safe_request_facts == [
        (
            "GET",
            "https://confluence.example.invalid/wiki/rest/api/user/current",
            7.0,
            4096,
        )
    ]
    assert "owl-test-pat-never-valid" not in repr(adapter)
    assert "owl-test-pat-never-valid" not in repr(result)


def test_adapter_rejects_non_ascii_or_control_bearing_authorization_values(origin):
    transport = ScriptedTransport(deque())

    with pytest.raises(ValueError):
        ConfluenceAdapter(origin, "non-ascii-\N{SNOWMAN}", transport=transport)
    with pytest.raises(ValueError):
        ConfluenceAdapter(origin, "line\nbreak", transport=transport)

    assert transport.request_count == 0


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_kind"),
    [
        (response(401, {"upstream": "private"}), ConfluenceResultCode.INVALID_CREDENTIAL, None),
        (response(403, {"upstream": "private"}), ConfluenceResultCode.ACCESS_DENIED, None),
        (response(429, None, **{"Retry-After": "17"}), ConfluenceResultCode.RATE_LIMITED, None),
        (
            response(503, {"upstream": "private"}),
            ConfluenceResultCode.UNREACHABLE,
            ConfluenceErrorKind.SERVER,
        ),
        (
            TimeoutError("private diagnostic"),
            ConfluenceResultCode.UNREACHABLE,
            ConfluenceErrorKind.TIMEOUT,
        ),
        (
            ssl.SSLCertVerificationError("private diagnostic"),
            ConfluenceResultCode.UNREACHABLE,
            ConfluenceErrorKind.TLS,
        ),
        (
            ConnectionError("private diagnostic"),
            ConfluenceResultCode.UNREACHABLE,
            ConfluenceErrorKind.CONNECTIVITY,
        ),
        (
            ResponseTooLargeError("private diagnostic"),
            ConfluenceResultCode.UNSUPPORTED_RESPONSE,
            ConfluenceErrorKind.RESPONSE_TOO_LARGE,
        ),
    ],
)
def test_connection_maps_failures_without_upstream_body_or_exception(
    origin, outcome, expected_code, expected_kind
):
    adapter, _transport = adapter_with(origin, outcome)

    result = adapter.test_connection()

    assert result.code == expected_code
    assert result.error_kind == expected_kind
    assert "private" not in repr(result)
    assert "owl-test-pat-never-valid" not in repr(result)
    if result.code == ConfluenceResultCode.RATE_LIMITED:
        assert result.retry_after_seconds == 17
        assert "17 seconds" in result.message


@pytest.mark.parametrize("payload", [None, [], {}, {"unexpected": "shape"}])
def test_connection_rejects_malformed_or_incompatible_success(origin, payload):
    raw_body = b"not-json" if payload is None else json.dumps(payload).encode()
    adapter, _transport = adapter_with(origin, HttpResponse(status=200, body=raw_body))

    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.error_kind == ConfluenceErrorKind.MALFORMED_RESPONSE


def test_adapter_enforces_response_limit_even_when_an_injected_transport_misbehaves(origin):
    adapter = ConfluenceAdapter(
        origin,
        "owl-test-pat-never-valid",
        max_response_bytes=32,
        transport=ScriptedTransport(deque([HttpResponse(status=200, body=b"x" * 33)])),
    )

    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.error_kind == ConfluenceErrorKind.RESPONSE_TOO_LARGE


def test_cross_origin_redirect_is_rejected_before_authorization_can_be_reused(origin):
    adapter, transport = adapter_with(
        origin,
        response(302, None, Location="https://other.example.invalid/rest/api/user/current"),
        response(200, {"displayName": "Must not be reached"}),
    )

    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.error_kind == ConfluenceErrorKind.UNSAFE_REDIRECT
    assert transport.request_count == 1
    assert all("other.example.invalid" not in fact[1] for fact in transport.safe_request_facts)


def test_same_origin_redirect_cannot_move_a_credential_into_a_url(origin):
    unsafe_location = "/wiki/rest/api/user/current?access_" + "token=upstream-value"
    adapter, transport = adapter_with(
        origin,
        response(302, None, Location=unsafe_location),
        response(200, {"displayName": "Must not be reached"}),
    )

    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.error_kind == ConfluenceErrorKind.UNSAFE_REDIRECT
    assert transport.request_count == 1
    assert all("upstream-value" not in fact[1] for fact in transport.safe_request_facts)


def test_same_origin_redirect_is_followed_with_a_bounded_read_only_get(origin):
    adapter, transport = adapter_with(
        origin,
        response(307, None, Location="/wiki/rest/api/user/current?redirected=1"),
        response(200, {"displayName": "Synthetic OWL User"}),
    )

    result = adapter.test_connection()

    assert result.code == ConfluenceResultCode.CONNECTED
    assert transport.request_count == 2
    assert transport.safe_request_facts[-1][1].endswith("?redirected=1")
    assert all(fact[0] == "GET" for fact in transport.safe_request_facts)


def test_normalizes_page_and_ordered_ancestor_metadata(origin):
    payload = {
        "id": "4242",
        "type": "page",
        "title": "Private DNS Design",
        "space": {"key": "OWL", "name": "OWL Architecture"},
        "version": {
            "number": 7,
            "when": "2026-08-24T10:30:00Z",
            "by": {"displayName": "Last Modifier"},
        },
        "history": {
            "createdDate": "2025-01-02T09:00:00+00:00",
            "createdBy": {"displayName": "Creator"},
        },
        "author": {"displayName": "Author"},
        "ancestors": [
            {
                "id": "100",
                "title": "Architecture",
                "_links": {"webui": "/wiki/spaces/OWL/pages/100/Architecture"},
                "extensions": {"position": 1},
            },
            {
                "id": "200",
                "title": "Networking",
                "_links": {"webui": "/wiki/spaces/OWL/pages/200/Networking"},
                "extensions": {"position": 4},
            },
        ],
        "body": {
            "storage": {"value": "<h1>Private DNS</h1><p>Resolver <strong>runbook</strong></p>"}
        },
        "_links": {"webui": "/wiki/spaces/OWL/pages/4242/Private+DNS+Design"},
    }
    adapter, transport = adapter_with(origin, response(200, payload))

    result = adapter.get_page("004242")

    assert result.code == ConfluenceResultCode.SUCCESS
    assert result.ok is True
    assert result.page is not None
    assert result.page.page_id == "4242"
    assert result.page.title == "Private DNS Design"
    assert result.page.space_key == "OWL"
    assert result.page.space_name == "OWL Architecture"
    assert result.page.version == 7
    assert result.page.created_at.isoformat() == "2025-01-02T09:00:00+00:00"
    assert result.page.updated_at.isoformat() == "2026-08-24T10:30:00+00:00"
    assert result.page.creator_name == "Creator"
    assert result.page.author_name == "Author"
    assert result.page.last_modifier_name == "Last Modifier"
    assert result.page.body_text == "Private DNS\nResolver\nrunbook"
    assert [ancestor.page_id for ancestor in result.page.ancestors] == ["100", "200"]
    assert [ancestor.position for ancestor in result.page.ancestors] == [1, 4]
    assert transport.request_count == 1
    assert "/rest/api/content/4242?expand=" in transport.safe_request_facts[0][1]
    assert "%2C" in transport.safe_request_facts[0][1]


def test_find_page_normalizes_one_exact_space_and_title_match(origin):
    payload = {
        "id": "4242",
        "type": "page",
        "title": "Private DNS Design",
        "space": {"key": "OWL", "name": "OWL Architecture"},
        "version": {"number": 7, "when": "2026-08-24T10:30:00Z"},
        "history": {"createdDate": "2025-01-02T09:00:00Z"},
        "ancestors": [],
        "body": {"storage": {"value": "<p>Exact searchable match</p>"}},
        "_links": {"webui": "/wiki/spaces/OWL/pages/4242/Private+DNS+Design"},
    }
    adapter, transport = adapter_with(origin, response(200, {"results": [payload]}))

    result = adapter.find_page("OWL", "Private DNS Design")

    assert result.code == ConfluenceResultCode.SUCCESS
    assert result.page is not None
    assert result.page.page_id == "4242"
    assert result.page.title == "Private DNS Design"
    assert result.page.body_text == "Exact searchable match"
    assert transport.request_count == 1
    request_url = transport.safe_request_facts[0][1]
    assert "/rest/api/content?" in request_url
    assert "spaceKey=OWL" in request_url
    assert "title=Private+DNS+Design" in request_url


def test_find_page_rejects_multiple_distinct_space_title_matches(origin):
    adapter, transport = adapter_with(
        origin,
        response(
            200,
            {
                "results": [
                    {
                        "id": "4242",
                        "type": "page",
                        "title": "Shared Runbook",
                        "space": {"key": "OWL"},
                    },
                    {
                        "id": "4243",
                        "type": "page",
                        "title": "Shared Runbook",
                        "space": {"key": "OWL"},
                    },
                ]
            },
        ),
    )

    result = adapter.find_page("OWL", "Shared Runbook")

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.page is None
    assert "more than one" in result.message.casefold()
    assert transport.request_count == 1


def test_recursively_loads_normalized_descendant_pages(origin):
    child = {
        "id": "301",
        "type": "page",
        "title": "Private DNS Runbook",
        "space": {"key": "OWL", "name": "OWL Architecture"},
        "version": {"number": 2, "when": "2026-08-24T10:30:00Z", "by": {}},
        "history": {"createdDate": "2025-01-02T09:00:00+00:00", "createdBy": {}},
        "author": {},
        "ancestors": [
            {
                "id": "300",
                "title": "Private DNS Design",
                "_links": {"webui": "/wiki/spaces/OWL/pages/300/Private-DNS-Design"},
            }
        ],
        "_links": {"webui": "/wiki/spaces/OWL/pages/301/Private-DNS-Runbook"},
    }
    adapter, transport = adapter_with(
        origin,
        response(200, {"results": [child]}),
        response(200, {"results": []}),
    )

    result = adapter.get_descendant_pages("300")

    assert result.ok is True
    assert [page.page_id for page in result.pages] == ["301"]
    assert result.pages[0].ancestors[-1].page_id == "300"
    assert transport.request_count == 2
    assert "/rest/api/content/300/child/page?" in transport.safe_request_facts[0][1]
    assert "/rest/api/content/301/child/page?" in transport.safe_request_facts[1][1]


def test_page_not_found_is_distinct_but_403_is_not_not_found(origin):
    missing_adapter, _ = adapter_with(origin, response(404, {"private": "body"}))
    denied_adapter, _ = adapter_with(origin, response(403, {"private": "body"}))

    missing = missing_adapter.get_page("123")
    denied = denied_adapter.get_page("123")

    assert missing.code == ConfluenceResultCode.NOT_FOUND
    assert denied.code == ConfluenceResultCode.ACCESS_DENIED


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": "999"},
        {"type": "blogpost"},
        {"title": ""},
        {"title": "unsafe\x00title"},
        {"_links": {"webui": "https://other.example.invalid/wiki/pages/123"}},
        {"ancestors": [{"id": "123", "title": "Self"}]},
    ],
)
def test_malformed_page_metadata_is_safely_rejected(origin, mutation):
    payload = {
        "id": "123",
        "type": "page",
        "title": "Synthetic page",
        "space": {"key": "OWL", "name": "OWL"},
        "version": {"number": 1},
        "history": {},
        "ancestors": [],
        "_links": {"webui": "/wiki/spaces/OWL/pages/123/Synthetic"},
    }
    payload.update(mutation)
    adapter, _transport = adapter_with(origin, response(200, payload))

    result = adapter.get_page("123")

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert result.error_kind == ConfluenceErrorKind.MALFORMED_RESPONSE
    assert result.page is None


def test_invalid_page_id_makes_no_request(origin):
    adapter, transport = adapter_with(origin, response(200, {}))

    result = adapter.get_page("not-a-number")

    assert result.code == ConfluenceResultCode.UNSUPPORTED_RESPONSE
    assert transport.request_count == 0


def test_stdlib_transport_rejects_cross_origin_before_dns_or_socket(origin):
    resolver_called = False

    def forbidden_resolver(host: str, port: int):
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("DNS must not run for a rejected URL.")

    transport = StdlibHttpTransport(resolver=forbidden_resolver)
    request = HttpRequest(
        url="https://other.example.invalid/wiki/rest/api/user/current",
        origin=origin,
        timeout_seconds=5,
        max_response_bytes=1024,
    )

    with pytest.raises(UnsafeRequestError):
        transport.send(request)

    assert resolver_called is False


def test_stdlib_transport_pins_the_freshly_validated_address_and_bounds_read(origin, monkeypatch):
    connection_facts: dict[str, object] = {}

    class SyntheticResponse:
        status = 200

        def getheader(self, name: str):
            return None

        def getheaders(self):
            return [("Content-Type", "application/json")]

        def read(self, amount: int):
            connection_facts["read_amount"] = amount
            return b'{"displayName":"Synthetic"}'

    class SyntheticConnection:
        def __init__(self, host, port, address, **kwargs):
            connection_facts["endpoint"] = (host, port, address)
            connection_facts["timeout"] = kwargs["timeout"]

        def request(self, method, target, headers):
            connection_facts["request"] = (method, target)
            connection_facts["authorization_present"] = bool(headers.get("Authorization"))

        def getresponse(self):
            return SyntheticResponse()

        def close(self):
            connection_facts["closed"] = True

    monkeypatch.setattr(confluence_module, "_PinnedHTTPSConnection", SyntheticConnection)
    transport = StdlibHttpTransport(resolver=lambda host, port: ("10.40.8.12",))
    request = HttpRequest(
        url=origin.build_url("/rest/api/user/current"),
        origin=origin,
        timeout_seconds=5,
        max_response_bytes=1024,
        headers={"Authorization": "placeholder"},
    )

    result = transport.send(request)

    assert result.status == 200
    assert connection_facts == {
        "endpoint": ("confluence.example.invalid", 443, "10.40.8.12"),
        "timeout": 5,
        "request": ("GET", "/wiki/rest/api/user/current"),
        "authorization_present": True,
        "read_amount": 1025,
        "closed": True,
    }
