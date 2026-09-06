"""Read-only Bitbucket Data Center client with bounded requests and pagination."""

import asyncio
from urllib.parse import quote

import httpx


class BitbucketError(RuntimeError):
    pass


class BitbucketClient:
    def __init__(self, settings):
        self.base = settings.base_url
        self.api = self.base + "/rest/api/1.0"
        self.client = httpx.AsyncClient(
            auth=(settings.username, settings.token.get_secret_value()),
            verify=settings.verify_ssl,
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, path, params=None, raw=False):
        for attempt in range(3):
            try:
                async with self.client.stream(
                    "GET", self.api + path, params=params
                ) as response:
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
                            raise BitbucketError(
                                "Bitbucket did not return JSON."
                            ) from None
                        if not isinstance(data, dict):
                            raise BitbucketError("Unexpected Bitbucket response.")
                        return data
            except httpx.HTTPError:
                if attempt == 2:
                    raise BitbucketError(
                        "Cannot reach Bitbucket. Check connection, credentials and TLS settings."
                    ) from None
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
