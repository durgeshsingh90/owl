"""Validate and normalize user-supplied Git repository URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from bitbucket_search.services.repository_hosts import (
    RepositoryHostNotAllowed,
    require_repository_hostname_allowed,
    require_repository_https_origin_allowed,
)

_SCP_STYLE_URL = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s?#]+)$"
)
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


class RepositoryURLValidationError(ValueError):
    """A repository address cannot cross OWL's safe Git boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedRepositoryURL:
    remote_url: str
    canonical_remote_key: str
    display_name: str
    hostname: str


def _validate_host(hostname: str) -> str:
    try:
        return require_repository_hostname_allowed(hostname)
    except RepositoryHostNotAllowed as error:
        raise RepositoryURLValidationError(
            "host_not_allowed",
            "This repository host is not approved in OWL Settings.",
        ) from error


def _normalize_path(raw_path: str) -> tuple[str, str]:
    path = raw_path.strip().strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    segments = path.split("/") if path else []
    if len(segments) < 2 or any(
        segment in {"", ".", ".."} or not _SAFE_PATH_SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        raise RepositoryURLValidationError(
            "invalid_repository_path",
            "Enter a repository URL with a valid owner and repository path.",
        )
    normalized_path = "/".join(segments)
    return normalized_path, segments[-1]


def normalize_repository_url(value: object) -> NormalizedRepositoryURL:
    """Return a credential-free SSH/HTTPS URL and stable repository identity."""

    raw_value = str(value or "").strip()
    if (
        not raw_value
        or len(raw_value) > 2048
        or any(character in raw_value for character in "\r\n\0")
    ):
        raise RepositoryURLValidationError(
            "repository_url_required",
            "Enter one SSH or HTTPS Git repository URL.",
        )

    scp_match = _SCP_STYLE_URL.fullmatch(raw_value)
    if scp_match is not None:
        if scp_match.group("user").casefold() != "git":
            raise RepositoryURLValidationError(
                "unsupported_ssh_user",
                "SSH repository URLs must use the read-only Git account.",
            )
        hostname = _validate_host(scp_match.group("host"))
        path, display_name = _normalize_path(scp_match.group("path"))
        remote_url = f"ssh://git@{hostname}/{path}.git"
    else:
        try:
            parsed = urlsplit(raw_value)
            port = parsed.port
        except ValueError as exc:
            raise RepositoryURLValidationError(
                "invalid_repository_url",
                "Enter a valid SSH or HTTPS Git repository URL.",
            ) from exc
        scheme = parsed.scheme.casefold()
        if scheme not in {"https", "ssh"}:
            raise RepositoryURLValidationError(
                "unsupported_repository_protocol",
                "Repository URLs must use SSH or HTTPS.",
            )
        if parsed.password is not None or (scheme == "https" and parsed.username is not None):
            raise RepositoryURLValidationError(
                "credential_bearing_repository_url",
                "Remove credentials from the repository URL before adding it.",
            )
        if scheme == "ssh" and parsed.username != "git":
            raise RepositoryURLValidationError(
                "unsupported_ssh_user",
                "SSH repository URLs must use the read-only Git account.",
            )
        if parsed.query or parsed.fragment or not parsed.hostname:
            raise RepositoryURLValidationError(
                "invalid_repository_url",
                "Enter a valid SSH or HTTPS Git repository URL without query parameters.",
            )
        hostname = _validate_host(parsed.hostname)
        path, display_name = _normalize_path(parsed.path)
        port_suffix = f":{port}" if port is not None else ""
        netloc = f"git@{hostname}{port_suffix}" if scheme == "ssh" else f"{hostname}{port_suffix}"
        remote_url = urlunsplit((scheme, netloc, f"/{path}.git", "", ""))
        if scheme == "https":
            try:
                require_repository_https_origin_allowed(
                    f"https://{hostname}:{port or 443}"
                )
            except RepositoryHostNotAllowed as error:
                raise RepositoryURLValidationError(
                    "host_not_allowed",
                    "This repository host and HTTPS port are not approved in OWL Settings.",
                ) from error

    return NormalizedRepositoryURL(
        remote_url=remote_url,
        canonical_remote_key=(
            f"{hostname}:{port}/{path.casefold()}"
            if not scp_match and port is not None
            else f"{hostname}/{path.casefold()}"
        ),
        display_name=display_name,
        hostname=hostname,
    )
