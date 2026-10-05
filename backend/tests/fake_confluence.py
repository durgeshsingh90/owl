"""A small in-memory Confluence answering the REST calls the tracker makes."""

import re


class FakeConfluence:
    """pages: page_id -> dict(title, ancestors=[ids], created, updated, creator, editor, version)."""

    def __init__(self, pages, page_size=2):
        self.pages = pages
        self.page_size = page_size
        self.calls = []
        self.unsupported = set()  # paths, or "search", answered with HTTP 501
        self.denied = set()  # page IDs whose children are hidden (HTTP 403)

    def item(self, page_id):
        page = self.pages[page_id]
        return {
            "id": page_id,
            "type": "page",
            "title": page["title"],
            "ancestors": [{"id": a, "title": self.pages[a]["title"]} for a in page.get("ancestors", [])],
            "version": {"number": page.get("version", 1), "when": page.get("updated"), "by": {"displayName": page.get("editor", "")}, "message": page.get("message", "")},
            "history": {"createdBy": {"displayName": page.get("creator", "")}, "createdDate": page.get("created")},
            "space": {"name": "Engineering", "key": "ENG"},
        }

    def listing(self, ids, params):
        start, limit = int(params.get("start", 0)), min(int(params.get("limit", 100)), self.page_size)
        chunk = ids[start : start + limit]
        result = {"results": [self.item(page_id) for page_id in chunk], "_links": {}}
        if start + limit < len(ids):
            result["_links"]["next"] = f"/rest/api/x?start={start + limit}&limit={limit}&expand=version"
        return result

    async def get(self, settings, path, params=None):
        from app.bookmarks import confluence

        params = params or {}
        self.calls.append((path, dict(params)))
        if "body" in str(params.get("expand", "")):
            raise AssertionError("The tracker must not download page content.")
        if path in self.unsupported or (path == "content/search" and "search" in self.unsupported):
            raise confluence.ConfluenceRequestError(501, "not supported")
        if path == "content/search":
            root, day = re.search(r"id = (\d+) or ancestor = (?:\d+)\) and type = page and lastmodified >= \"([\d-]+)\"", params["cql"]).groups()
            ids = [
                page_id for page_id, page in self.pages.items()
                if (page_id == root or root in page.get("ancestors", [])) and str(page.get("updated") or "")[:10] >= day
            ]
            return self.listing(ids, params)
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "content":
            if parts[1] not in self.pages:
                raise confluence.ConfluenceRequestError(404, "missing")
            return self.item(parts[1])
        if len(parts) == 4 and parts[2] == "descendant":
            ids = [page_id for page_id, page in self.pages.items() if parts[1] in page.get("ancestors", [])]
            return self.listing(ids, params)
        if len(parts) == 4 and parts[2] == "child":
            if parts[1] in self.denied:
                raise confluence.ConfluenceRequestError(403, "denied")
            ids = [page_id for page_id, page in self.pages.items() if (page.get("ancestors") or [None])[-1] == parts[1]]
            return self.listing(ids, params)
        if path == "space/ENG":
            return {"key": "ENG", "homepage": {"id": next(iter(self.pages))}}
        if path == "content" and params.get("title"):
            matches = [page_id for page_id, page in self.pages.items() if page["title"] == params["title"]]
            return {"results": [{"id": page_id} for page_id in matches]}
        return {"results": []}
