"""SQLite persistence; each operation owns its connection and transaction."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote


def database_path():
    return Path(
        os.environ.get(
            "OWL_DB_PATH", Path(__file__).resolve().parents[2] / "data/owl.db"
        )
    )


@contextmanager
def connection():
    conn = sqlite3.connect(database_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(*, recover_jobs=False):
    database_path().parent.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS pull_activity (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracked_projects (
            id INTEGER PRIMARY KEY, project_url TEXT NOT NULL UNIQUE,
            server TEXT NOT NULL, project TEXT NOT NULL,
            added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(server, project)
        );
        CREATE TABLE IF NOT EXISTS excluded_repositories (url TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES tracked_projects(id) ON DELETE CASCADE,
            repo TEXT NOT NULL, name TEXT NOT NULL, last_scanned TEXT,
            UNIQUE(project_id,repo)
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            project TEXT NOT NULL, repo TEXT NOT NULL, pdf_name TEXT NOT NULL,
            path TEXT NOT NULL, url TEXT NOT NULL, file_size INTEGER NOT NULL,
            page_count INTEGER NOT NULL, commit_id TEXT, commit_message TEXT,
            author TEXT, commit_date TEXT, pdf_hash TEXT NOT NULL, pdf_text TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', open_count INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_scanned TEXT NOT NULL,
            UNIQUE(repository_id,path)
        );
        CREATE TABLE IF NOT EXISTS failed_documents (
            id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            path TEXT NOT NULL, error TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
            last_attempt TEXT NOT NULL, UNIQUE(repository_id,path)
        );
        CREATE TABLE IF NOT EXISTS bookmark_workspace (
            id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO bookmark_workspace(id,payload) VALUES(1,'{"bookmarks":[],"groups":[],"notes":{}}');
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, progress TEXT NOT NULL
        );
        """)
        repository_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(repositories)")
        }
        if "last_pull_at" not in repository_columns:
            db.execute("ALTER TABLE repositories ADD COLUMN last_pull_at TEXT")
            db.execute(
                "UPDATE repositories SET last_pull_at=last_scanned WHERE last_scanned IS NOT NULL"
            )
            db.commit()
        if "last_indexed_commit" not in repository_columns:
            db.execute("ALTER TABLE repositories ADD COLUMN last_indexed_commit TEXT")
            db.commit()
        # FTS5 cannot add columns in place. Rebuild only its derived index,
        # preserving all documents and saved notes, including older databases.
        fts_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(documents_fts)")
        }
        if "notes" not in fts_columns:
            db.executescript("""BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS document_insert;
                DROP TRIGGER IF EXISTS document_delete;
                DROP TRIGGER IF EXISTS document_update;
                DROP TABLE IF EXISTS documents_fts;
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            pdf_name, repo, path, pdf_text, notes, content='documents', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS document_insert AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid,pdf_name,repo,path,pdf_text,notes)
            VALUES(new.id,new.pdf_name,new.repo,new.path,new.pdf_text,new.notes);
        END;
        CREATE TRIGGER IF NOT EXISTS document_delete AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts,rowid,pdf_name,repo,path,pdf_text,notes)
            VALUES('delete',old.id,old.pdf_name,old.repo,old.path,old.pdf_text,old.notes);
        END;
        CREATE TRIGGER IF NOT EXISTS document_update AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts,rowid,pdf_name,repo,path,pdf_text,notes)
            VALUES('delete',old.id,old.pdf_name,old.repo,old.path,old.pdf_text,old.notes);
            INSERT INTO documents_fts(rowid,pdf_name,repo,path,pdf_text,notes)
            VALUES(new.id,new.pdf_name,new.repo,new.path,new.pdf_text,new.notes);
        END;
                INSERT INTO documents_fts(documents_fts) VALUES('rebuild');
                COMMIT;
            """)
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(failed_documents)")
        }
        for column in ("pdf_name", "url", "request_url"):
            if column not in columns:
                db.execute(
                    f"ALTER TABLE failed_documents ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        # Fill identifiers for failures saved by older versions, without network access.
        for row in db.execute(
            "SELECT f.id,f.path,r.repo,p.project,p.server FROM failed_documents f "
            "JOIN repositories r ON r.id=f.repository_id "
            "JOIN tracked_projects p ON p.id=r.project_id WHERE f.pdf_name='' OR f.url=''"
        ).fetchall():
            url = (
                row["server"].rstrip("/")
                + "/projects/"
                + quote(row["project"], safe="")
                + "/repos/"
                + quote(row["repo"], safe="")
                + "/browse/"
                + quote(row["path"], safe="/")
            )
            db.execute(
                "UPDATE failed_documents SET pdf_name=?,url=? WHERE id=?",
                (PurePosixPath(row["path"]).name, url, row["id"]),
            )
        if recover_jobs:
            db.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')"
            )


def repository_url(server, project, repo):
    return (
        server.rstrip("/")
        + "/projects/"
        + quote(project, safe="")
        + "/repos/"
        + quote(repo, safe="")
    )


def exclude_repositories(db, rows):
    for row in rows:
        db.execute(
            "INSERT OR IGNORE INTO excluded_repositories(url) VALUES(?)",
            (repository_url(row["server"], row["project"], row["repo"]),),
        )
        # Historical progress includes file paths and errors; discard affected jobs too.
        for job in db.execute("SELECT id,progress FROM jobs").fetchall():
            import json

            progress = json.loads(job["progress"])
            if str(row["id"]) in progress.get("repository_statuses", {}):
                db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        db.execute("DELETE FROM repositories WHERE id=?", (row["id"],))
