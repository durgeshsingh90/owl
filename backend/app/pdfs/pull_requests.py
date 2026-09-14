"""Read commit associations and PR review activity without changing Bitbucket."""

from urllib.parse import quote

from app.pdfs.client import BitbucketError


def person(user):
    return user.get("displayName") or user.get("name") or "Unknown"


async def trace(client, project, repo, commits):
    prefix = client.repo_path(project, repo)
    mappings, requests, errors = {}, {}, []
    for commit in commits:
        identity = commit["id"]
        try:
            related = [
                pr
                async for pr in client.pages(
                    prefix + "/commits/" + quote(identity, safe="") + "/pull-requests"
                )
            ]
            mappings[identity] = []
            for pr in related:
                pr_id = pr.get("id")
                if not isinstance(pr_id, int):
                    continue
                target = (pr.get("toRef") or {}).get("repository") or {}
                target_project = (target.get("project") or {}).get("key") or project
                target_repo = target.get("slug") or repo
                key = f"{target_project}/{target_repo}/{pr_id}"
                if key not in mappings[identity]:
                    mappings[identity].append(key)
                requests[key] = (target_project, target_repo, pr_id)
        except BitbucketError as exc:
            mappings[identity] = None
            errors.append(f"Commit {identity}: {exc}")
    details = []
    for key, (target_project, target_repo, pr_id) in requests.items():
        pr_path = (
            client.repo_path(target_project, target_repo) + f"/pull-requests/{pr_id}"
        )
        url = client.base + pr_path + "/overview"
        try:
            pr = await client.request(pr_path)
        except BitbucketError as exc:
            details.append({"key": key, "id": pr_id, "url": url, "error": str(exc)})
            continue
        activities = []
        activity_error = None
        try:
            activities = [a async for a in client.pages(pr_path + "/activities")]
        except BitbucketError as exc:
            activity_error = str(exc)
        # Keep events as history, distinct from the current reviewer state.
        events = [
            {
                "action": a.get("action") or "UNKNOWN",
                "user": person(a.get("user") or {}),
                "timestamp": a.get("createdDate"),
                "message": (a.get("comment") or {}).get("text") or "",
            }
            for a in activities
        ]
        events.sort(key=lambda a: a["timestamp"] or 0)
        reviewers = []
        for reviewer in pr.get("reviewers") or []:
            status = reviewer.get("status") or (
                "APPROVED" if reviewer.get("approved") else "UNAPPROVED"
            )
            reviewers.append(
                {"name": person(reviewer.get("user") or {}), "status": status}
            )
        details.append(
            {
                "key": key,
                "id": pr_id,
                "url": url,
                "title": pr.get("title") or "",
                "description": pr.get("description") or "",
                "state": pr.get("state") or "Unknown",
                "author": person((pr.get("author") or {}).get("user") or {}),
                "created": pr.get("createdDate"),
                "updated": pr.get("updatedDate"),
                "merged": next(
                    (
                        a["timestamp"]
                        for a in reversed(events)
                        if a["action"] == "MERGED"
                    ),
                    None,
                ),
                "reviewers": reviewers,
                "activities": events,
                "activity_error": activity_error,
            }
        )
    return {"commits": mappings, "pull_requests": details, "errors": errors}
