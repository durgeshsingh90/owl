"""List Confluence pages with their metadata only: title, path, link, who and when.

A tracked tree is read in pages of 100 straight from Confluence's page lists, with the
fields expanded in the same request, so no page content is ever downloaded.
"""

from collections import deque
from urllib.parse import parse_qsl, urlsplit

from app.bookmarks import confluence

EXPAND = "version,history,ancestors,space"
LIMIT = 50000
# Statuses some servers return for a list they cannot produce; a slower list is used.
UNSUPPORTED = {400, 404, 405, 500, 501, 502, 503, 504}


def summary(settings, data):
    """The fields the tracker keeps for one page, in the bookmarks' metadata format."""
    version, history = data.get("version") or {}, data.get("history") or {}
    ancestors = [
        {
            "page_id": str(item["id"]),
            "title": item.get("title", ""),
            "url": settings.base_url + "/pages/viewpage.action?pageId=" + str(item["id"]),
        }
        for item in data.get("ancestors") or []
    ]
    page_id = str(data["id"])
    if not page_id.isdecimal():
        raise ValueError("Confluence returned an invalid page ID.")
    return {
        "page_id": page_id,
        "title": data.get("title", "") or "Page " + page_id,
        "url": settings.base_url + "/pages/viewpage.action?pageId=" + page_id,
        "sourceType": "confluence",
        "confluenceBaseUrl": settings.base_url,
        "space": (data.get("space") or {}).get("name", ""),
        "spaceKey": (data.get("space") or {}).get("key", ""),
        "author": (history.get("createdBy") or {}).get("displayName", ""),
        "writtenAt": history.get("createdDate"),
        "lastEditor": (version.get("by") or {}).get("displayName", ""),
        "confluenceUpdatedAt": version.get("when"),
        "version": version.get("number"),
        "versionMessage": version.get("message", ""),
        "ancestors": ancestors,
        "breadcrumb": [item["title"] for item in ancestors],
    }


async def listed(settings, path, params, found, progress=None):
    """Every item of a paged Confluence list, added to found by page ID."""
    paging = {"start": 0, "limit": 100}
    seen = set()
    added = []
    while True:
        signature = tuple(sorted((key, str(value)) for key, value in paging.items()))
        if signature in seen:
            raise ValueError("Confluence pagination repeated a page.")
        seen.add(signature)
        data = await confluence.get(settings, path, {**params, **paging, "expand": EXPAND})
        results = data.get("results")
        if not isinstance(results, list):
            raise TypeError("Confluence returned an invalid page list.")
        for item in results:
            if item.get("type", "page") != "page":
                continue
            page = summary(settings, item)
            if page["page_id"] not in found:
                added.append(page["page_id"])
            found[page["page_id"]] = page
            if len(found) > LIMIT:
                raise ValueError("This page tree has more than 50,000 pages.")
        if progress:
            progress(len(found))
        next_link = (data.get("_links") or {}).get("next")
        if not next_link:
            return added
        if not results:
            raise ValueError("Confluence pagination did not advance.")
        paging = dict(parse_qsl(urlsplit(next_link).query))
        paging.pop("expand", None)
        if not paging:
            raise ValueError("Confluence returned no next-page cursor.")


async def tree(settings, root_id, progress=None):
    """The root page and every page below it."""
    found = {}
    root = await confluence.get(settings, "content/" + str(root_id), {"expand": EXPAND})
    found[str(root["id"])] = summary(settings, root)
    try:
        await listed(settings, f"content/{root_id}/descendant/page", {}, found, progress)
    except confluence.ConfluenceRequestError as error:
        if error.upstream_status not in UNSUPPORTED:
            raise
        # Some servers cannot list all descendants at once; walk the children instead.
        pending, walked = deque([str(root_id)]), set()
        while pending:
            parent = pending.popleft()
            if parent in walked:
                continue
            walked.add(parent)
            try:
                children = await listed(settings, f"content/{parent}/child/page", {}, found, progress)
            except confluence.ConfluenceRequestError as child_error:
                # A restricted or just-deleted page hides only its own branch.
                if child_error.upstream_status not in {403, 404}:
                    raise
                continue
            pending.extend(child for child in children if child not in walked)
    return list(found.values())


async def updated_since(settings, root_id, day, progress=None):
    """Pages in the tree created or updated on or after day (YYYY-MM-DD)."""
    found = {}
    cql = f'(id = {int(root_id)} or ancestor = {int(root_id)}) and type = page and lastmodified >= "{day}"'
    await listed(settings, "content/search", {"cql": cql}, found, progress)
    return list(found.values())
