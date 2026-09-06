"""Plan PDF work from a repository's saved snapshot and current head."""

from app.pdfs.client import BitbucketError
from app.pdfs.crawler import entry_path


async def plan_changes(client, project, repo, previous):
    prefix = client.repo_path(project, repo)
    result = await client.request(prefix + "/commits", {"limit": 1})
    values = result.get("values")
    if not isinstance(values, list):
        raise BitbucketError("Repository head response is missing commits.")
    head = values[0].get("id") if values else None
    if values and not isinstance(head, str):
        raise BitbucketError("Repository head response is missing a commit ID.")
    client.snapshot_refs[(project, repo)] = head
    if not previous:
        return head, None, set()
    if not head:
        raise BitbucketError("Repository head unavailable; saved index was preserved.")
    if head == previous:
        return head, set(), set()
    paths, deleted = set(), set()
    async for change in client.pages(
        prefix + "/compare/changes",
        {
            "from": head,
            "to": previous,
        },
    ):
        if change.get("type") not in {"ADD", "MODIFY", "DELETE", "MOVE", "COPY"}:
            raise BitbucketError(
                "Unsupported change type; saved checkpoint was preserved."
            )
        path = entry_path("", {"path": change.get("path", {})})
        if change["type"] == "DELETE":
            if path.lower().endswith(".pdf"):
                deleted.add(path)
        else:
            if path.lower().endswith(".pdf"):
                paths.add(path)
            if change["type"] == "MOVE":
                source = entry_path("", {"path": change.get("srcPath", {})})
                if source.lower().endswith(".pdf"):
                    deleted.add(source)
    return head, paths, deleted - paths
