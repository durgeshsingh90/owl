"""Home: refresh every app in the background, and how much each app stores."""

import os
import threading
import time

from app.core.database import connection, database_path
from app.core.library import library
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/home")

APPS = {
    "bitbucket": "Bitbucket",
    "naas": "NAAS Update",
    "network": "Network Automation",
    "bookmarks": "Bookmarks",
    "tracker": "Confluence Tracker",
    "compare": "Compare",
    "aws": "AWS Accounts",
}
# Tables in the main database, by app (full-text indexes keep their shadow tables
# under the same prefix).
PREFIXES = (
    ("bookmark_", "bookmarks"),
    ("confluence_tracker_", "tracker"),
    ("tracker_", "tracker"),
    ("compare_", "compare"),
    ("aws_", "aws"),
)
BITBUCKET_TABLES = {
    "documents", "repositories", "tracked_projects", "failed_documents", "jobs",
    "pull_activity", "sync_metadata", "repository_sync_timings", "excluded_repositories",
    "bitbucket_sync_schedule", "workspace_revision",
}
CACHE_SECONDS = 600
_cache = {"at": 0, "value": None}
_cache_lock = threading.Lock()


def owner(table):
    for prefix, app in PREFIXES:
        if table.startswith(prefix):
            return app
    if table in BITBUCKET_TABLES or table.startswith("documents_fts"):
        return "bitbucket"
    return None


def file_bytes(path):
    """The database file with its write-ahead log, as they take space on disk."""
    return sum(
        os.path.getsize(name)
        for name in (str(path), f"{path}-wal", f"{path}-shm")
        if os.path.exists(name)
    )


def stored_bytes(db, table):
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        return 0
    total = " + ".join(f'COALESCE(length(CAST("{column}" AS BLOB)),0)' for column in columns)
    return db.execute(f'SELECT COALESCE(SUM({total}),0) FROM "{table}"').fetchone()[0]


def measure():
    """Bytes per app and in total. The main database holds several apps: its size is
    split between them by how much data each one's tables hold."""
    sizes = dict.fromkeys(APPS, 0)
    files, free = [], 0
    main = database_path()
    for name in ("pdf", "naas", "network"):
        token = library.set(name)
        try:
            path = database_path()
            if not path.exists():
                continue
            size = file_bytes(path)
            files.append({"name": path.name, "bytes": size})
            with connection() as db:
                page = db.execute("PRAGMA page_size").fetchone()[0]
                unused = db.execute("PRAGMA freelist_count").fetchone()[0] * page
                free += unused
                if name != "pdf":
                    sizes[name] += size - unused
                    continue
                tables = [
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                data = {}
                for table in tables:
                    app = owner(table)
                    if app:
                        data[app] = data.get(app, 0) + stored_bytes(db, table)
            used = size - unused
            stored = sum(data.values()) or 1
            # Whole bytes that add up exactly to the file's used size.
            shares = {app: used * amount // stored for app, amount in data.items()}
            leftover = used - sum(shares.values()) if data else 0
            for app in sorted(data, key=lambda app: -(used * data[app] % stored))[:leftover]:
                shares[app] += 1
            for app, share in shares.items():
                sizes[app] += share
        finally:
            library.reset(token)
    return {
        "apps": [{"app": app, "name": APPS[app], "bytes": sizes[app]} for app in APPS],
        "total_bytes": sum(item["bytes"] for item in files),
        "free_bytes": free,
        "files": files,
        "folder": str(main.parent),
        "measured_at": time.time(),
    }


@router.get("/storage")
def storage(fresh: bool = False):
    # Measuring reads every table, so a recent result is reused.
    with _cache_lock:
        if not fresh and _cache["value"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
    value = measure()
    with _cache_lock:
        _cache.update(at=time.time(), value=value)
    return value


@router.post("/refresh-all")
def refresh_all():
    """Make every app's automatic refresh due now; each runs in the background."""
    now = time.time()
    started = {}
    for name, app in (("pdf", "bitbucket"), ("naas", "naas"), ("network", "network")):
        token = library.set(name)
        try:
            with connection() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS bitbucket_sync_schedule (server TEXT PRIMARY KEY, next_attempt TEXT NOT NULL, job_id TEXT, last_success TEXT, error TEXT NOT NULL DEFAULT '')"
                )
                started[app] = db.execute(
                    "UPDATE bitbucket_sync_schedule SET next_attempt=? WHERE job_id IS NULL",
                    (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 1)),),
                ).rowcount
        finally:
            library.reset(token)
    with connection() as db:
        started["bookmarks"] = db.execute(
            "UPDATE bookmark_refresh_schedule SET next_run=0 WHERE id=1 AND lease_until<=?",
            (now,),
        ).rowcount
        started["tracker"] = db.execute(
            "UPDATE confluence_tracker_roots SET next_run=0 WHERE lease_until<=?", (now,)
        ).rowcount
    return {"started": {app: bool(count) for app, count in started.items()}}


class Opened(BaseModel):
    app: str = Field(pattern="^(" + "|".join(APPS) + ")$")


@router.post("/opened")
def opened(value: Opened):
    """Count one visit to an app (its page was opened)."""
    with connection() as db:
        db.execute(
            "INSERT INTO app_opens(app,count,last_opened) VALUES(?,1,?) "
            "ON CONFLICT(app) DO UPDATE SET count=count+1,last_opened=excluded.last_opened",
            (value.app, time.time()),
        )
        count = db.execute("SELECT count FROM app_opens WHERE app=?", (value.app,)).fetchone()[0]
    return {"app": value.app, "count": count}


@router.get("/opens")
def opens():
    with connection() as db:
        rows = db.execute("SELECT app,count,last_opened FROM app_opens").fetchall()
    return {"opens": {row["app"]: {"count": row["count"], "last_opened": row["last_opened"]} for row in rows}}
