"""Incremental PDF crawling without retaining downloaded PDF files."""

import asyncio
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

import pymupdf
from app.core.database import connection
from app.pdfs.client import BitbucketError


def now():
    return datetime.now(timezone.utc).isoformat()


def extract(content):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            temp_path = temporary.name
            temporary.write(content)
        with pymupdf.open(temp_path) as pdf:
            if pdf.needs_pass:
                raise BitbucketError("PDF requires a password.")
            return len(pdf), "\n".join(page.get_text() for page in pdf)
    except BitbucketError:
        raise
    except Exception:  # noqa: BLE001 - isolate parser/upstream failures without leaking secrets
        raise BitbucketError("PDF text extraction failed.") from None
    finally:
        if temp_path:
            os.remove(temp_path)


async def process_pdf(client, project, repo, repository_id, path):
    prefix = client.repo_path(project, repo)
    data = await client.request(prefix + "/commits", {"path": path, "limit": 1})
    commits = data.get("values", [])
    commit = commits[0] if commits else {}
    commit_id = commit.get("id") or None
    with connection() as db:
        old = db.execute(
            "SELECT * FROM documents WHERE repository_id=? AND path=?",
            (repository_id, path),
        ).fetchone()
        if old and commit_id and old["commit_id"] == commit_id:
            db.execute(
                "UPDATE documents SET last_scanned=? WHERE id=?", (now(), old["id"])
            )
            db.execute(
                "DELETE FROM failed_documents WHERE repository_id=? AND path=?",
                (repository_id, path),
            )
            return "unchanged"
    content = await client.request(
        prefix + "/raw/" + quote(path, safe="/"),
        {"at": commit_id} if commit_id else None,
        raw=True,
    )
    digest = hashlib.sha256(content).hexdigest()
    if old and old["pdf_hash"] == digest:
        page_count, text = old["page_count"], old["pdf_text"]
    else:
        # A separate process keeps status requests responsive during PDF parsing.
        page_count, text = await asyncio.get_running_loop().run_in_executor(
            client.extractor, extract, content
        )
    stamp = now()
    timestamp = commit.get("authorTimestamp")
    commit_date = (
        datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
        if isinstance(timestamp, (int, float))
        else None
    )
    values = {
        "repository_id": repository_id,
        "project": project,
        "repo": repo,
        "pdf_name": PurePosixPath(path).name,
        "path": path,
        "url": client.base + prefix + "/browse/" + quote(path, safe="/"),
        "file_size": len(content),
        "page_count": page_count,
        "commit_id": commit_id,
        "commit_message": commit.get("message"),
        "author": (commit.get("author") or {}).get("displayName")
        or (commit.get("author") or {}).get("name"),
        "commit_date": commit_date,
        "pdf_hash": digest,
        "pdf_text": text,
        "added_at": stamp,
        "updated_at": stamp,
        "last_scanned": stamp,
    }
    columns = list(values)
    updates = ",".join(
        f"{key}=excluded.{key}"
        for key in columns
        if key not in ("repository_id", "path", "added_at")
    )
    with connection() as db:
        db.execute(
            f"INSERT INTO documents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(repository_id,path) DO UPDATE SET {updates}",
            list(values.values()),
        )
        db.execute(
            "DELETE FROM failed_documents WHERE repository_id=? AND path=?",
            (repository_id, path),
        )
    return "updated" if old else "new"


async def discover_pdfs(client, project, repo):
    prefix = client.repo_path(project, repo)
    folders = [""]
    visited = set()
    while folders:
        folder = folders.pop()
        if folder in visited:
            continue
        visited.add(folder)
        async for item in client.pages(
            prefix + "/browse/" + quote(folder, safe="/"), nested="children"
        ):
            name = item.get("path", {}).get("name", "")
            if not name or name in (".", "..") or "/" in name:
                raise BitbucketError("Invalid repository path entry.")
            path = str(PurePosixPath(folder) / name)
            if item.get("type") == "DIRECTORY":
                folders.append(path)
            elif item.get("type") == "FILE" and path.lower().endswith(".pdf"):
                yield path


async def crawl_repository(client, project, repo, repository_id, progress, paths):
    for path in paths:
        try:
            outcome = await process_pdf(client, project, repo, repository_id, path)
            progress[outcome] += 1
        except (BitbucketError, ValueError, OverflowError) as error:
            progress["failed"] += 1
            progress["failure"](path)
            with connection() as db:
                db.execute(
                    """INSERT INTO failed_documents(repository_id,path,error,last_attempt)
                              VALUES(?,?,?,?) ON CONFLICT(repository_id,path) DO UPDATE SET
                              error=excluded.error,last_attempt=excluded.last_attempt,attempts=attempts+1""",
                    (
                        repository_id,
                        path,
                        str(error)
                        if isinstance(error, BitbucketError)
                        else "PDF metadata could not be processed.",
                        now(),
                    ),
                )
        progress["processed"] += 1
        progress["save"]()
    with connection() as db:
        db.execute(
            "UPDATE repositories SET last_scanned=? WHERE id=?", (now(), repository_id)
        )
