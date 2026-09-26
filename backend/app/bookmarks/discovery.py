"""Shared Confluence page-ID discovery, before any content downloads."""

from collections import deque
from urllib.parse import parse_qsl, urlsplit

from app.bookmarks import confluence


async def discover_pages(settings, root_ids, space_key="", progress=None):
    page_ids = dict.fromkeys(str(root) for root in root_ids)
    paths = [("content/" + root + "/descendant/page", {}) for root in root_ids]
    if space_key:
        paths = [
            (
                "content",
                {"spaceKey": space_key, "type": "page", "status": "current"},
            )
        ]

    async def discover(path, params):
        discovered = []
        paging = {"start": 0, "limit": 100}
        visited = set()
        while True:
            signature = tuple(sorted((k, str(v)) for k, v in paging.items()))
            if signature in visited:
                raise ValueError("Confluence pagination repeated a page.")
            visited.add(signature)
            data = await confluence.get(settings, path, {**params, **paging})
            results = data.get("results")
            if not isinstance(results, list):
                raise TypeError("Confluence returned an invalid page list.")
            for item in results:
                page_id = str(item["id"])
                if not page_id.isdecimal():
                    raise ValueError("Confluence returned an invalid page ID.")
                page_ids[page_id] = None
                discovered.append(page_id)
                if len(page_ids) > 50000:
                    raise ValueError("Folder exceeds the 50,000 page download limit.")
            if progress:
                progress(len(page_ids), path)
            if not data.get("_links", {}).get("next"):
                break
            if not results:
                raise ValueError("Confluence pagination did not advance.")
            paging = dict(parse_qsl(urlsplit(data["_links"]["next"]).query))
            if not paging:
                raise ValueError("Confluence returned no next-page cursor.")
        return discovered

    for path, params in paths:
        try:
            await discover(path, params)
        except confluence.ConfluenceRequestError as error:
            if not path.endswith("/descendant/page") or error.upstream_status not in {
                404,
                405,
                500,
                501,
                502,
                503,
                504,
            }:
                raise
            # Some servers fail the bulk descendant listing. Walk direct
            # children instead, retaining pagination and cycle protection.
            pending = deque([path.split("/")[1]])
            traversed = set()
            while pending:
                parent = pending.popleft()
                if parent in traversed:
                    continue
                traversed.add(parent)
                children = await discover("content/" + parent + "/child/page", {})
                pending.extend(child for child in children if child not in traversed)
    return list(page_ids)
