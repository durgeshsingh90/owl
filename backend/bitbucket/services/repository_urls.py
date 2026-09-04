"""HTTPS-only repository URL parsing without a hostname allow-list."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit


class RepositoryURLValidationError(ValueError):
    pass


class ServerURLValidationError(ValueError):
    pass


class ProjectURLValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServerURL:
    api_base_url: str
    origin: str
    web_base_url: str
    host: str

    def repository_url(self, project: str, slug: str) -> str:
        encoded_project = quote(project, safe="")
        encoded_slug = quote(slug, safe="")
        return f"{self.web_base_url}/scm/{encoded_project}/{encoded_slug}.git"


@dataclass(frozen=True, slots=True)
class ProjectURL:
    url: str
    origin: str
    project: str


@dataclass(frozen=True, slots=True)
class RepositoryURL:
    url: str
    origin: str
    base_url: str
    host: str
    project: str
    slug: str
    authentication_url: str

    @property
    def api_repository_url(self) -> str:
        project = quote(self.project, safe="")
        repository = quote(self.slug, safe="")
        return f"{self.base_url}/rest/api/latest/projects/{project}/repos/{repository}"

    def browse_url(self, path: str = "") -> str:
        project = quote(self.project, safe="")
        repository = quote(self.slug, safe="")
        root = f"{self.base_url}/projects/{project}/repos/{repository}/browse"
        normalized = str(PurePosixPath(path)) if path else ""
        return f"{root}/{quote(normalized, safe='/')}" if normalized and normalized != "." else root


def _validated_https_url(raw_value: str, *, label: str) -> tuple[SplitResult, str, int | None]:
    value = (raw_value or "").strip()
    if not value or any(ord(character) < 32 for character in value):
        raise ServerURLValidationError(f"Enter a valid HTTPS {label} URL.")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https":
        raise ServerURLValidationError("Only HTTPS URLs are supported.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ServerURLValidationError(
            f"The {label} URL must have a host and must not contain credentials."
        )
    if parsed.query or parsed.fragment:
        raise ServerURLValidationError(
            f"Remove query parameters and fragments from the {label} URL."
        )
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ServerURLValidationError("Enter a valid HTTPS host.") from exc
    return parsed, host, port


def parse_api_base_url(raw_value: str) -> ServerURL:
    """Parse a config.ini-style Bitbucket REST root such as ``.../rest/api/1.0``."""

    parsed, host, port = _validated_https_url(raw_value, label="Bitbucket API base")
    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    lowered = [part.casefold() for part in parts]
    try:
        rest_index = lowered.index("rest")
    except ValueError as exc:
        raise ServerURLValidationError(
            "Enter the REST API base URL ending in /rest/api/1.0 or /rest/api/latest."
        ) from exc
    if (
        rest_index + 2 >= len(parts)
        or lowered[rest_index + 1] != "api"
        or lowered[rest_index + 2] not in {"1.0", "latest"}
        or rest_index + 3 != len(parts)
    ):
        raise ServerURLValidationError(
            "Enter the REST API base URL ending in /rest/api/1.0 or /rest/api/latest."
        )
    netloc = host if port in {None, 443} else f"{host}:{port}"
    origin = urlunsplit(SplitResult("https", netloc, "", "", ""))
    context_path = "/" + "/".join(parts[:rest_index]) if rest_index else ""
    web_base_url = urlunsplit(SplitResult("https", netloc, context_path, "", ""))
    api_base_url = urlunsplit(SplitResult("https", netloc, path, "", ""))
    return ServerURL(
        api_base_url=api_base_url,
        origin=origin,
        web_base_url=web_base_url,
        host=host,
    )


def parse_project_url(raw_value: str) -> ProjectURL:
    try:
        parsed, host, port = _validated_https_url(raw_value, label="Bitbucket project")
    except ServerURLValidationError as exc:
        raise ProjectURLValidationError(str(exc)) from exc
    parts = [part for part in parsed.path.rstrip("/").split("/") if part]
    lowered = [part.casefold() for part in parts]
    project = ""
    for marker_name in ("projects", "scm"):
        if marker_name in lowered:
            marker = lowered.index(marker_name)
            if marker + 1 < len(parts):
                project = parts[marker + 1]
                break
    if not project:
        raise ProjectURLValidationError(
            "Enter a Bitbucket project URL such as https://server.example/projects/PROJECT."
        )
    netloc = host if port in {None, 443} else f"{host}:{port}"
    origin = urlunsplit(SplitResult("https", netloc, "", "", ""))
    canonical = urlunsplit(SplitResult("https", netloc, parsed.path.rstrip("/"), "", ""))
    return ProjectURL(url=canonical, origin=origin, project=project)


def _repository_parts(path: str) -> tuple[str, str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise RepositoryURLValidationError("Enter a repository URL with a project and name.")
    lowered = [part.casefold() for part in parts]
    if "scm" in lowered and lowered.index("scm") + 2 < len(parts):
        marker = lowered.index("scm")
        project, repository = parts[marker + 1 : marker + 3]
        context_path = "/" + "/".join(parts[:marker]) if marker else ""
    else:
        project, repository = parts[-2:]
        context_path = "/" + "/".join(parts[:-2]) if len(parts) > 2 else ""
    slug = repository[:-4] if repository.casefold().endswith(".git") else repository
    if not project or not slug:
        raise RepositoryURLValidationError("Enter a repository URL with a project and name.")
    return project, slug, context_path


def parse_repository_url(raw_value: str) -> RepositoryURL:
    value = (raw_value or "").strip()
    if not value or any(ord(character) < 32 for character in value):
        raise RepositoryURLValidationError("Enter a valid HTTPS repository URL.")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https":
        raise RepositoryURLValidationError("Only HTTPS repository URLs are supported.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RepositoryURLValidationError(
            "The repository URL must have a host and must not contain credentials."
        )
    if parsed.query or parsed.fragment:
        raise RepositoryURLValidationError(
            "Remove query parameters and fragments from the repository URL."
        )
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise RepositoryURLValidationError("Enter a valid HTTPS repository host.") from exc
    path = parsed.path.rstrip("/")
    if not path.startswith("/") or not path:
        raise RepositoryURLValidationError("Enter the full HTTPS Git repository URL.")
    project, slug, context_path = _repository_parts(path)
    netloc = host if port in {None, 443} else f"{host}:{port}"
    origin = urlunsplit(SplitResult("https", netloc, "", "", ""))
    base_url = urlunsplit(SplitResult("https", netloc, context_path, "", ""))
    canonical = urlunsplit(SplitResult("https", netloc, path, "", ""))
    return RepositoryURL(
        url=canonical,
        origin=origin,
        base_url=base_url,
        host=host,
        project=project,
        slug=slug,
        authentication_url=f"{base_url}/",
    )
