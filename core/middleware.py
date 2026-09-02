"""Security middleware for OWL's loopback-only browser interface."""

from __future__ import annotations

import ipaddress

from django.core.exceptions import DisallowedHost
from django.http import HttpRequest
from django.http.request import split_domain_port
from django.middleware.csrf import CsrfViewMiddleware
from django.urls import Resolver404, resolve

_OPAQUE_ORIGIN = "null"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_LOCAL_BITBUCKET_MUTATIONS = frozenset(
    {
        "bitbucket_search:people_group_create",
        "bitbucket_search:document_open",
        "bitbucket_search:documents_open_all",
        "bitbucket_search:document_reveal",
        "bitbucket_search:document_exclude",
        "bitbucket_search:document_resume",
        "bitbucket_search:document_delete",
        "bitbucket_search:repository_add",
        "bitbucket_search:repository_exclude",
        "bitbucket_search:repository_remove",
        "bitbucket_search:repositories_selected",
        "bitbucket_search:repository_refresh",
        "bitbucket_search:repository_schedule_tick",
        "bitbucket_search:repositories_refresh_all",
        "bookmark_manager:bitbucket_https_credential_save",
        "bookmark_manager:bitbucket_https_credential_remove",
    }
)


def _is_loopback_address(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value.split("%", maxsplit=1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _has_loopback_host(request: HttpRequest) -> bool:
    try:
        host = request.get_host()
    except DisallowedHost:
        return False
    domain, port = split_domain_port(host)
    if domain not in _LOOPBACK_HOSTS:
        return False
    if not port:
        return True
    try:
        return 1 <= int(port) <= 65_535
    except ValueError:
        return False


def _is_local_bitbucket_mutation(request: HttpRequest) -> bool:
    match = getattr(request, "resolver_match", None)
    if match is None:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
    return match.view_name in _LOCAL_BITBUCKET_MUTATIONS


def _may_accept_opaque_origin(request: HttpRequest) -> bool:
    """Identify one local browser-shell case before normal CSRF token checks.

    Some sandboxed desktop browser surfaces serialize their opaque origin as
    ``Origin: null`` even while visibly serving OWL from localhost. This
    exception remains limited to named Bitbucket POST actions over loopback
    HTTP. The inherited CSRF middleware still compares the CSRF cookie and
    submitted form token after this origin decision.
    """

    return bool(
        request.method == "POST"
        and request.META.get("HTTP_ORIGIN") == _OPAQUE_ORIGIN
        and not request.is_secure()
        and _is_loopback_address(request.META.get("REMOTE_ADDR"))
        and _has_loopback_host(request)
        and _is_local_bitbucket_mutation(request)
    )


class LoopbackOpaqueOriginCsrfMiddleware(CsrfViewMiddleware):
    """Keep Django CSRF intact while accepting a narrow local opaque origin."""

    def _origin_verified(self, request: HttpRequest) -> bool:
        if _may_accept_opaque_origin(request):
            return True
        return super()._origin_verified(request)
