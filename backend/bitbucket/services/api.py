"""Read-only Bitbucket Data Center REST API client."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
import requests
from django.conf import settings
from urllib3.exceptions import InsecureRequestWarning

from bitbucket.models import Repository
from bitbucket.services.repository_urls import ServerURL, parse_api_base_url, parse_repository_url


class BitbucketAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "api_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AddedMetadata:
    commit_id: str
    author: str
    email: str
    authored_at: datetime | None


@dataclass(frozen=True, slots=True)
class CommitMetadata:
    commit_id: str
    message: str
    author: str
    email: str
    authored_at: datetime | None


def _timestamp(value: object) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 100_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _commit_metadata(value: object) -> CommitMetadata | None:
    if not isinstance(value, dict):
        return None
    author = value.get("author") if isinstance(value.get("author"), dict) else {}
    return CommitMetadata(
        commit_id=str(value.get("id") or "")[:64],
        message=str(value.get("message") or "")[:10_000],
        author=str(author.get("displayName") or author.get("name") or "")[:255],
        email=str(author.get("emailAddress") or "")[:320],
        authored_at=_timestamp(value.get("authorTimestamp")),
    )


def _http_client(
    token: str,
    *,
    username: str,
    verify_ssl: bool,
    transport: httpx.BaseTransport | None,
) -> httpx.Client | requests.Session:
    headers = {
        "Accept": "application/json",
        "User-Agent": "OWL-Bitbucket-pdf-crawler/1",
    }
    if transport is not None:
        auth = httpx.BasicAuth(username, token) if username else None
        if auth is None:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            headers=headers,
            auth=auth,
            timeout=float(getattr(settings, "BITBUCKET_APP_API_TIMEOUT_SECONDS", 30)),
            follow_redirects=False,
            verify=verify_ssl,
            transport=transport,
        )

    # Match the proven standalone crawler on real machines. Requests uses the
    # operating environment's proxy settings and sends (username, token) as
    # HTTP Basic auth, which is required by some Bitbucket Data Center setups.
    client = requests.Session()
    client.headers.update(headers)
    if username:
        client.auth = (username, token)
    else:
        client.headers["Authorization"] = f"Bearer {token}"
    client.verify = verify_ssl
    return client


def _client_get(
    client: httpx.Client | requests.Session,
    url: str,
    *,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> httpx.Response | requests.Response:
    request_timeout = timeout or float(getattr(settings, "BITBUCKET_APP_API_TIMEOUT_SECONDS", 30))
    if isinstance(client, requests.Session):
        with warnings.catch_warnings():
            if client.verify is False:
                warnings.simplefilter("ignore", InsecureRequestWarning)
            return client.get(
                url,
                params=params,
                headers=headers,
                timeout=request_timeout,
                allow_redirects=False,
            )
    return client.get(url, params=params, headers=headers, timeout=request_timeout)


def _raise_for_response(response: httpx.Response | requests.Response) -> None:
    if response.status_code in {401, 403}:
        raise BitbucketAPIError(
            "The HTTP access token is invalid or lacks repository-read permission.",
            code="authentication_required",
        )
    if 300 <= response.status_code < 400:
        raise BitbucketAPIError(
            "The Bitbucket API redirected the request; check the API base URL.",
            code="unsafe_redirect",
        )
    if response.status_code == 404:
        raise BitbucketAPIError(
            "The Bitbucket project, repository, or REST API endpoint was not found.",
            code="repository_not_found",
        )
    if response.status_code >= 400:
        raise BitbucketAPIError(
            f"The Bitbucket API returned HTTP {response.status_code}.",
            code="api_response_error",
        )


def _get_json_url(
    client: httpx.Client | requests.Session,
    url: str,
    *,
    params: dict[str, object] | None = None,
) -> dict[str, Any]:
    try:
        response = _client_get(client, url, params=params)
    except (httpx.TimeoutException, requests.Timeout) as exc:
        raise BitbucketAPIError("The Bitbucket API request timed out.", code="api_timeout") from exc
    except (httpx.ConnectError, requests.ConnectionError) as exc:
        raise BitbucketAPIError(
            "The Bitbucket API could not be reached. Check the URL, VPN, and SSL verification setting.",
            code="connection_failed",
        ) from exc
    except (httpx.HTTPError, requests.RequestException) as exc:
        raise BitbucketAPIError("The Bitbucket API could not be reached.") from exc
    _raise_for_response(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise BitbucketAPIError("The Bitbucket API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise BitbucketAPIError("The Bitbucket API returned an unexpected response.")
    return payload


def _pages_from_url(
    client: httpx.Client | requests.Session,
    url: str,
    *,
    params: dict[str, object] | None = None,
) -> Iterator[dict[str, Any]]:
    request_params = dict(params or {})
    request_params.setdefault("limit", int(getattr(settings, "BITBUCKET_APP_API_PAGE_SIZE", 1000)))
    seen_starts: set[int] = set()
    while True:
        page = _get_json_url(client, url, params=request_params)
        yield page
        if bool(page.get("isLastPage", True)):
            return
        try:
            next_start = int(page["nextPageStart"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BitbucketAPIError("Bitbucket returned invalid pagination metadata.") from exc
        if next_start in seen_starts:
            raise BitbucketAPIError("Bitbucket returned a repeating pagination cursor.")
        seen_starts.add(next_start)
        request_params["start"] = next_start


class BitbucketServerClient:
    """Authenticated client for testing a server and expanding a project into repositories."""

    def __init__(
        self,
        api_base_url: str,
        token: str,
        *,
        username: str = "",
        verify_ssl: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server: ServerURL = parse_api_base_url(api_base_url)
        self._client = _http_client(
            token,
            username=username,
            verify_ssl=verify_ssl,
            transport=transport,
        )

    def __enter__(self) -> BitbucketServerClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def test_connection(self) -> None:
        _get_json_url(self._client, f"{self.server.api_base_url}/projects")

    def repositories(self, project: str) -> tuple[str, ...]:
        encoded_project = quote(project, safe="")
        url = f"{self.server.api_base_url}/projects/{encoded_project}/repos"
        slugs: list[str] = []
        for page in _pages_from_url(self._client, url):
            values = page.get("values", ())
            if not isinstance(values, list):
                raise BitbucketAPIError("Bitbucket returned an invalid repository listing.")
            for value in values:
                if not isinstance(value, dict):
                    continue
                slug = str(value.get("slug") or value.get("name") or "").strip()
                if slug:
                    slugs.append(slug)
        return tuple(dict.fromkeys(slugs))


class BitbucketAPIClient:
    """An exact-origin Bitbucket client using Bearer or username/token auth."""

    def __init__(
        self,
        repository: Repository,
        token: str,
        *,
        username: str = "",
        api_base_url: str = "",
        verify_ssl: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository = repository
        self.parsed = parse_repository_url(repository.url)
        self._sleep = sleep
        configured_server = parse_api_base_url(api_base_url) if api_base_url else None
        if configured_server is not None and configured_server.origin != self.parsed.origin:
            raise BitbucketAPIError(
                "The saved Bitbucket API base URL does not match the repository server.",
                code="configuration_mismatch",
            )
        self.api_repository_url = (
            f"{configured_server.api_base_url}/projects/{quote(self.parsed.project, safe='')}"
            f"/repos/{quote(self.parsed.slug, safe='')}"
            if configured_server is not None
            else self.parsed.api_repository_url
        )
        self._client = _http_client(
            token,
            username=username,
            verify_ssl=(
                bool(getattr(settings, "BITBUCKET_APP_VERIFY_SSL", True))
                if verify_ssl is None
                else verify_ssl
            ),
            transport=transport,
        )

    def __enter__(self) -> BitbucketAPIClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _raise_for_response(response: httpx.Response | requests.Response) -> None:
        _raise_for_response(response)

    def _get_json(
        self,
        relative_path: str = "",
        *,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = self.api_repository_url
        if relative_path:
            url = f"{url}/{relative_path.lstrip('/')}"
        return _get_json_url(self._client, url, params=params)

    def _pages(
        self,
        relative_path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> Iterator[dict[str, Any]]:
        url = self.api_repository_url
        if relative_path:
            url = f"{url}/{relative_path.lstrip('/')}"
        yield from _pages_from_url(self._client, url, params=params)

    def test_connection(self) -> None:
        self._get_json()

    def document_paths(self) -> tuple[tuple[str, ...], int]:
        pdfs: list[str] = []
        vsdx_count = 0
        for page in self._pages("files"):
            values = page.get("values", ())
            if not isinstance(values, list):
                raise BitbucketAPIError("Bitbucket returned an invalid file listing.")
            for item in values:
                path = (
                    item
                    if isinstance(item, str)
                    else item.get("path")
                    if isinstance(item, dict)
                    else None
                )
                if not isinstance(path, str) or not path:
                    continue
                suffix = PurePosixPath(path).suffix.casefold()
                if suffix == ".pdf":
                    pdfs.append(path)
                elif suffix == ".vsdx":
                    vsdx_count += 1
        return tuple(dict.fromkeys(pdfs)), vsdx_count

    def latest_metadata(self, path: str) -> CommitMetadata | None:
        payload = self._get_json(
            "commits",
            params={
                "path": path,
                "followRenames": "false",
                "ignoreMissing": "true",
                "limit": 1,
            },
        )
        values = payload.get("values", ())
        if not isinstance(values, list):
            raise BitbucketAPIError("Bitbucket returned invalid commit history.")
        return _commit_metadata(values[0]) if values else None

    def added_metadata(self, path: str) -> AddedMetadata | None:
        oldest: dict[str, Any] | None = None
        for page in self._pages(
            "commits",
            params={"path": path, "followRenames": "false", "ignoreMissing": "true"},
        ):
            values = page.get("values", ())
            if not isinstance(values, list):
                raise BitbucketAPIError("Bitbucket returned invalid commit history.")
            for item in values:
                if isinstance(item, dict):
                    oldest = item
        if oldest is None:
            return None
        metadata = _commit_metadata(oldest)
        if metadata is None:
            return None
        return AddedMetadata(
            commit_id=metadata.commit_id,
            author=metadata.author,
            email=metadata.email,
            authored_at=metadata.authored_at,
        )

    def download_pdf(self, path: str) -> bytes:
        """Download one PDF from Bitbucket's raw endpoint with bounded retries."""

        encoded_path = quote(path, safe="/")
        url = f"{self.api_repository_url}/raw/{encoded_path}"
        retries = int(getattr(settings, "BITBUCKET_APP_PDF_DOWNLOAD_RETRIES", 4))
        timeout = float(getattr(settings, "BITBUCKET_APP_PDF_DOWNLOAD_TIMEOUT_SECONDS", 120))
        base_wait = float(getattr(settings, "BITBUCKET_APP_PDF_RETRY_BASE_SECONDS", 10))
        max_wait = float(getattr(settings, "BITBUCKET_APP_PDF_RETRY_MAX_SECONDS", 60))
        max_bytes = int(getattr(settings, "BITBUCKET_APP_PDF_MAX_BYTES", 104_857_600))

        for attempt in range(retries + 1):
            try:
                response = _client_get(
                    self._client,
                    url,
                    headers={"Accept": "application/pdf"},
                    timeout=timeout,
                )
            except (httpx.TimeoutException, requests.Timeout) as exc:
                if attempt < retries:
                    self._sleep(min(max_wait, base_wait * (attempt + 1)))
                    continue
                raise BitbucketAPIError(
                    "The PDF download timed out.", code="pdf_download_timeout"
                ) from exc
            except (httpx.HTTPError, requests.RequestException) as exc:
                raise BitbucketAPIError(
                    "The PDF could not be downloaded.", code="pdf_download_failed"
                ) from exc

            if response.status_code == 429 and attempt < retries:
                retry_after = response.headers.get("Retry-After", "").strip()
                try:
                    requested_wait = float(retry_after)
                except ValueError:
                    requested_wait = base_wait * (attempt + 1)
                self._sleep(min(max_wait, max(0, requested_wait)))
                continue
            if response.status_code == 429:
                raise BitbucketAPIError(
                    "Bitbucket rate-limited the PDF download.", code="rate_limited"
                )
            self._raise_for_response(response)
            content_length = response.headers.get("Content-Length", "").strip()
            if content_length.isdigit() and int(content_length) > max_bytes:
                raise BitbucketAPIError(
                    "The PDF exceeds the configured download size limit.",
                    code="pdf_too_large",
                )
            content = response.content
            if len(content) > max_bytes:
                raise BitbucketAPIError(
                    "The PDF exceeds the configured download size limit.",
                    code="pdf_too_large",
                )
            if not content[:1024].lstrip().startswith(b"%PDF-"):
                raise BitbucketAPIError(
                    "Bitbucket did not return a valid PDF file.",
                    code="invalid_pdf_response",
                )
            return content

        raise BitbucketAPIError("The PDF could not be downloaded.", code="pdf_download_failed")
