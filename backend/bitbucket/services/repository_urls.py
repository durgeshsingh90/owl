"""HTTPS-only repository URL parsing without a hostname allow-list."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit


class RepositoryURLValidationError(ValueError):
    pass


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
