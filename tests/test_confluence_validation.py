from __future__ import annotations

import socket

import pytest

from bookmark_manager.services.confluence_validation import (
    OriginResolutionError,
    OriginValidationError,
    PageInputError,
    PageInputKind,
    parse_page_input,
    resolve_safe_addresses,
    validate_confluence_origin,
)


def private_dc_resolver(host: str, port: int) -> tuple[str, ...]:
    assert host == "confluence.corp.example"
    assert port in {443, 8443}
    return ("10.40.8.12",)


def test_canonical_https_origin_preserves_context_and_allows_approved_private_dc():
    origin = validate_confluence_origin(
        "  HTTPS://Confluence.Corp.Example:443/wiki/  ",
        resolver=private_dc_resolver,
    )

    assert origin.base_url == "https://confluence.corp.example/wiki"
    assert origin.origin == "https://confluence.corp.example"
    assert origin.host == "confluence.corp.example"
    assert origin.context_path == "/wiki"
    assert origin.is_test_target is False


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("http://confluence.corp.example", "https_required"),
        ("https://person:" + "password@confluence.corp.example", "embedded_credentials"),
        ("https://confluence.corp.example/wiki?next=/", "unexpected_url_parts"),
        ("https://confluence.corp.example/wiki#settings", "unexpected_url_parts"),
        ("https://confluence.corp.example:22", "unsafe_port"),
        ("https://127.0.0.1", "disallowed_target"),
        ("https://[::1]", "disallowed_target"),
        ("https://169.254.169.254", "disallowed_target"),
        ("https://metadata.google.internal", "disallowed_target"),
        ("https://confluence.corp.example/wiki/../admin", "invalid_context_path"),
    ],
)
def test_origin_rejects_unsafe_shapes_before_any_external_request(value: str, code: str):
    resolver_calls = 0

    def resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ("10.40.8.12",)

    with pytest.raises(OriginValidationError) as captured:
        validate_confluence_origin(value, resolver=resolver)

    assert captured.value.code == code
    assert resolver_calls == 0
    assert value not in str(captured.value)


def test_default_profile_rejects_non_resolving_synthetic_origin():
    def no_such_host(host: str, port: int) -> tuple[str, ...]:
        raise socket.gaierror("synthetic DNS failure")

    with pytest.raises(OriginResolutionError) as captured:
        validate_confluence_origin("https://confluence.example.invalid/wiki", resolver=no_such_host)

    assert captured.value.code == "dns_failure"


def test_explicit_test_profile_allows_https_invalid_without_dns():
    resolver_called = False

    def forbidden_resolver(host: str, port: int) -> tuple[str, ...]:
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("Synthetic validation must not perform DNS.")

    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki",
        resolver=forbidden_resolver,
        allow_test_targets=True,
    )

    assert origin.base_url == "https://confluence.example.invalid/wiki"
    assert origin.is_test_target is True
    assert resolver_called is False


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8765/wiki",
        "http://127.0.0.1:8765/wiki",
        "http://[::1]:8765/wiki",
    ],
)
def test_explicit_test_profile_allows_http_only_for_exact_loopback(value: str):
    origin = validate_confluence_origin(value, allow_test_targets=True)

    assert origin.scheme == "http"
    assert origin.port == 8765
    assert origin.is_test_target is True


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8765/wiki",
        "http://127.0.0.1:8765/wiki",
        "http://confluence.example.invalid/wiki",
        "http://localhost.example.com:8765/wiki",
    ],
)
def test_http_test_escape_hatch_is_impossible_by_default_or_for_non_loopback(value: str):
    with pytest.raises(OriginValidationError) as captured:
        validate_confluence_origin(value)

    assert captured.value.code in {"disallowed_target", "https_required"}


def test_dns_guard_rejects_mixed_trust_classes_as_rebinding_pattern():
    with pytest.raises(OriginResolutionError) as captured:
        resolve_safe_addresses(
            "confluence.corp.example",
            443,
            resolver=lambda host, port: ("10.40.8.12", "8.8.8.8"),
        )

    assert captured.value.code == "mixed_dns_target"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "fe80::1",
        "0.0.0.0",
        "100.100.100.200",
        "192.0.2.10",
        "198.18.0.1",
        "2001:db8::1",
        "224.0.0.1",
    ],
)
def test_dns_guard_rejects_special_socket_targets(address: str):
    with pytest.raises(OriginResolutionError) as captured:
        resolve_safe_addresses(
            "confluence.corp.example",
            443,
            resolver=lambda host, port: (address,),
        )

    assert captured.value.code == "disallowed_target"


def test_page_input_parses_raw_modern_and_legacy_identity():
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )

    raw = parse_page_input(" 001234 ", origin)
    modern = parse_page_input(
        "https://confluence.example.invalid/wiki/spaces/OWL/pages/1234/Private+DNS",
        origin,
    )
    legacy = parse_page_input(
        "https://confluence.example.invalid/wiki/pages/viewpage.action?pageId=1234",
        origin,
    )

    assert raw.page_id == modern.page_id == legacy.page_id == "1234"
    assert raw.kind == PageInputKind.PAGE_ID
    assert modern.kind == PageInputKind.MODERN_URL
    assert legacy.kind == PageInputKind.LEGACY_URL


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("not a page", "invalid_page_url"),
        ("0", "invalid_page_id"),
        ("https://other.example.invalid/wiki/spaces/X/pages/123/Title", "disallowed_origin"),
        ("https://confluence.example.invalid/spaces/X/pages/123/Title", "disallowed_context"),
        (
            "https://confluence.example.invalid/wiki/spaces/X/pages/123/Title?to" + "ken=value",
            "unsupported_page_url",
        ),
        (
            "https://confluence.example.invalid/wiki/pages/viewpage.action?pageId=123&pageId=456",
            "unsupported_page_url",
        ),
        (
            "https://confluence.example.invalid/wiki/pages/viewpage.action?pageId=123&src=nav",
            "unsupported_page_url",
        ),
    ],
)
def test_page_input_rejects_unsupported_or_cross_origin_values(value: str, code: str):
    origin = validate_confluence_origin(
        "https://confluence.example.invalid/wiki", allow_test_targets=True
    )

    with pytest.raises(PageInputError) as captured:
        parse_page_input(value, origin)

    assert captured.value.code == code
    assert value not in str(captured.value)
