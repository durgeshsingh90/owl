"""Saved JSON documents (snapshots): gzip files on disk, metadata in the database."""

import gzip
import hashlib
import json
import os
import re
import time
import uuid

from app.core.database import connection
from app.jsonviz import storage

CHUNK = 1024 * 1024
DEFAULTS = {"retention_days": 0, "max_snapshots": 200}
LIMITS = {"retention_days": (0, 3650), "max_snapshots": (1, 10000)}
FORMAT = re.compile(r"^[a-z0-9]{1,20}$")
SOURCE = re.compile(r"^[a-z0-9_-]{1,50}$")


def clean_name(name):
    # The last path part only: a name from a browser or a URL may carry folders.
    name = re.split(r"[\\/]", str(name or ""))[-1].strip()
    name = "".join(char for char in name if char >= " ")
    if not name:
        raise ValueError("A file name is required.")
    return name[:255]


def clean_labels(labels):
    """Labels: up to 20 string keys and string values of up to 200 characters."""
    if labels is None:
        return {}
    if isinstance(labels, str):
        try:
            labels = json.loads(labels) if labels.strip() else {}
        except ValueError:
            raise ValueError("Labels must be a JSON object.") from None
    if not isinstance(labels, dict):
        raise ValueError("Labels must be a JSON object.")
    if len(labels) > 20:
        raise ValueError("Use at most 20 labels.")
    for key, value in labels.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("Label names must be text of 1 to 200 characters.")
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"Label {key} must be text of at most 200 characters.")
    return dict(labels)


def _record(row):
    record = dict(row)
    record["labels"] = json.loads(record["labels"] or "{}")
    return record


def _path(snapshot_id):
    return storage.subfolder("snapshots") / f"{snapshot_id}.json.gz"


def _digest(file):
    hasher, size = hashlib.sha256(), 0
    while chunk := file.read(CHUNK):
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def store(source, name, *, labels=None, format="", origin="upload", command=""):
    """Save the file at source (JSON, or gzip of it) as a snapshot; source is moved or removed.

    A gzip file is kept as it is; anything else is compressed. Either way the file is
    written next to its final name first, then renamed, so a crash leaves no half file.
    """
    ready = None
    try:
        name = clean_name(name)
        labels = clean_labels(labels)
        format = (format or "").strip().lower() or ("jsonl" if name.lower().endswith((".jsonl", ".ndjson")) else "json")
        if not FORMAT.match(format):
            raise ValueError("Format must be a short word such as json or jsonl.")
        origin = (origin or "upload").strip().lower()
        if not SOURCE.match(origin):
            raise ValueError("Source must be a short word such as upload or runner.")
        command = str(command or "")[:2000]
        snapshot_id = uuid.uuid4().hex
        final = _path(snapshot_id)
        with open(source, "rb") as file:
            compressed = file.read(2) == storage.GZIP_MAGIC
        if compressed:
            try:
                with gzip.open(source, "rb") as file:
                    sha256, size = _digest(file)
            except (OSError, EOFError, ValueError):
                raise ValueError("The gzip file is damaged.") from None
            ready = source
        else:
            ready = final.with_name(f".{snapshot_id}.tmp")
            hasher, size = hashlib.sha256(), 0
            with open(source, "rb") as file, gzip.open(ready, "wb", compresslevel=6) as out:
                while chunk := file.read(CHUNK):
                    hasher.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            sha256 = hasher.hexdigest()
        if not size:
            raise ValueError("The file is empty.")
        os.replace(ready, final)
    finally:
        # Whatever was not renamed into place: the upload, or a half-written gzip.
        for leftover in {str(source), str(ready or source)}:
            if os.path.exists(leftover):
                os.unlink(leftover)
    with connection() as db:
        db.execute(
            "INSERT INTO json_viz_snapshots(id,name,size,stored,sha256,format,source,labels,command,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, name, size, os.path.getsize(final), sha256, format, origin,
             json.dumps(labels), command, time.time()),
        )
    apply_retention()
    return get(snapshot_id)


def get(snapshot_id):
    with connection() as db:
        row = db.execute("SELECT * FROM json_viz_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    return _record(row) if row else None


def listing():
    apply_retention()
    with connection() as db:
        rows = db.execute("SELECT * FROM json_viz_snapshots ORDER BY created_at DESC, rowid DESC").fetchall()
    snapshots = [_record(row) for row in rows]
    return {
        "snapshots": snapshots,
        "total_bytes": sum(item["stored"] for item in snapshots),
        "settings": settings(),
    }


def content(snapshot_id):
    """The decompressed bytes, in chunks."""
    path = _path(snapshot_id)
    file = gzip.open(path, "rb")

    def chunks():
        with file:
            while chunk := file.read(CHUNK):
                yield chunk
    return chunks()


def update(snapshot_id, name=None, labels=None):
    fields = {}
    if name is not None:
        fields["name"] = clean_name(name)
    if labels is not None:
        fields["labels"] = json.dumps(clean_labels(labels))
    if fields:
        with connection() as db:
            db.execute(
                "UPDATE json_viz_snapshots SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                (*fields.values(), snapshot_id),
            )
    return get(snapshot_id)


def _remove(ids):
    if not ids:
        return
    with connection() as db:
        db.executemany("DELETE FROM json_viz_snapshots WHERE id=?", [(item,) for item in ids])
    for item in ids:
        _path(item).unlink(missing_ok=True)


def delete(snapshot_id):
    if not get(snapshot_id):
        return False
    _remove([snapshot_id])
    return True


def delete_all():
    with connection() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM json_viz_snapshots")]
    _remove(ids)
    # Files without a row (for example from an interrupted save).
    for path in storage.subfolder("snapshots").iterdir():
        path.unlink(missing_ok=True)
    return len(ids)


def settings():
    with connection() as db:
        rows = dict(db.execute("SELECT key,value FROM json_viz_settings").fetchall())
    result = {}
    for key, default in DEFAULTS.items():
        try:
            result[key] = int(rows.get(key, default))
        except ValueError:
            result[key] = default
    return result


def save_settings(values):
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting {key}.")
        low, high = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}.")
    with connection() as db:
        db.executemany(
            "INSERT INTO json_viz_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, str(value)) for key, value in values.items()],
        )
    return settings()


def apply_retention(now=None):
    """Delete snapshots older than retention_days (0 keeps them), then the oldest beyond
    max_snapshots."""
    limits = settings()
    now = time.time() if now is None else now
    with connection() as db:
        old = []
        if limits["retention_days"]:
            old = [row[0] for row in db.execute(
                "SELECT id FROM json_viz_snapshots WHERE created_at<?",
                (now - limits["retention_days"] * 86400,),
            )]
        extra = [row[0] for row in db.execute(
            "SELECT id FROM json_viz_snapshots ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (limits["max_snapshots"],),
        )]
    _remove(list(dict.fromkeys(old + extra)))
