"""Read-only Bitbucket Data Center client with bounded requests and pagination."""

import asyncio
import os
import time
from urllib.parse import quote

import httpx
from app.core.logging import error_details, event


class BitbucketError(RuntimeError):
    pass


class BitbucketClient:
    def __init__(self, settings):
        self.snapshot_refs = {}
        self.base = settings.base_url
        self.api = self.base + "/rest/api/1.0"
        event(
            "bitbucket.client",
            api_base=self.api,
            verify_ssl=False,
            auth="basic",
            proxy_environment=[
                key
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                )
                if os.environ.get(key)
            ],
        )
        self.client = httpx.AsyncClient(
            auth=(settings.username, settings.token.get_secret_value()),
            verify=False,
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, path, params=None, raw=False):
        url = str(httpx.URL(self.api + path, params=params))
        try:
            return await self._request(path, params, raw)
        except BitbucketError as error:
            error.request_url = url
            event(
                "bitbucket.request_failed",
                level=40,
                path=path,
                request_url=url,
                error=str(error),
            )
            raise

    async def _request(self, path, params=None, raw=False):
        for attempt in range(3):
            started = time.monotonic()
            event("bitbucket.attempt", path=path, attempt=attempt + 1)
            try:
                async with self.client.stream(
                    "GET", self.api + path, params=params
                ) as response:
                    event(
                        "bitbucket.response",
                        path=path,
                        attempt=attempt + 1,
                        status=response.status_code,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        redirect=response.is_redirect,
                    )
                    if response.status_code in (429, 502, 503, 504) and attempt < 2:
                        delay = (
                            min(
                                10,
                                max(0, int(response.headers.get("retry-after", "1"))),
                            )
                            if response.headers.get("retry-after", "1").isdigit()
                            else 1
                        )
                    elif response.status_code != 200:
                        raise BitbucketError(
                            f"Bitbucket returned HTTP {response.status_code}."
                        )
                    else:
                        if getattr(self, "on_connected", None):
                            self.on_connected()
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 50 * 1024 * 1024:
                                raise BitbucketError(
                                    "Response exceeds the 50 MB limit."
                                )
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        if raw:
                            return content
                        import json

                        try:
                            data = json.loads(content)
                        except ValueError:
                            event(
                                "bitbucket.invalid_json",
                                level=30,
                                path=path,
                                bytes=len(content),
                            )
                            raise BitbucketError(
                                "Bitbucket did not return JSON."
                            ) from None
                        if not isinstance(data, dict):
                            raise BitbucketError("Unexpected Bitbucket response.")
                        return data
            except httpx.HTTPError as error:
                details = error_details(error)
                event(
                    "bitbucket.transport_failed",
                    level=40,
                    path=path,
                    attempt=attempt + 1,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    **details,
                )
                if details["dns_failure"]:
                    message = (
                        "Bitbucket hostname could not be resolved. Check DNS and VPN."
                    )
                elif isinstance(error, httpx.ProxyError):
                    message = "Bitbucket proxy connection failed. Check the backend proxy configuration."
                elif isinstance(error, httpx.TimeoutException):
                    message = "Bitbucket network request timed out. Check VPN, proxy and server reachability."
                elif isinstance(error, httpx.ConnectError):
                    message = "Connection to Bitbucket failed. Check VPN, proxy and firewall; see backend logs for OS error codes."
                else:
                    message = "Bitbucket transport failed. See backend logs for the error type."
                if attempt == 2:
                    raise BitbucketError(message) from None
                delay = 1
            await asyncio.sleep(delay)
        raise BitbucketError("Bitbucket retry limit reached.")

    async def pages(self, path, params=None, nested=None):
        start, seen = 0, set()
        while True:
            if start in seen:
                raise BitbucketError("Bitbucket repeated a pagination cursor.")
            seen.add(start)
            data = await self.request(
                path, {**(params or {}), "limit": 100, "start": start}
            )
            page = data.get(nested, {}) if nested else data
            if not isinstance(page.get("values"), list):
                raise BitbucketError("Bitbucket response is missing page values.")
            for item in page["values"]:
                yield item
            if page.get("isLastPage", True):
                break
            next_start = page.get("nextPageStart")
            if not isinstance(next_start, int) or next_start <= start:
                raise BitbucketError("Invalid Bitbucket pagination cursor.")
            start = next_start

    @staticmethod
    def repo_path(project, repo):
        return f"/projects/{quote(project, safe='')}/repos/{quote(repo, safe='')}"

    async def test(self):
        data = await asyncio.wait_for(
            self.request("/projects", {"limit": 1}), timeout=8
        )
        if not isinstance(data.get("values"), list):
            raise BitbucketError("Unexpected projects response.")
        return {
            "state": "success",
            "ok": True,
            "detail": "Bitbucket connection successful.",
        }
