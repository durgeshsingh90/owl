"""SQLite persistence; each operation owns its connection and transaction."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


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
        CREATE TABLE IF NOT EXISTS tracked_projects (
            id INTEGER PRIMARY KEY, project_url TEXT NOT NULL UNIQUE,
            server TEXT NOT NULL, project TEXT NOT NULL,
            added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(server, project)
        );
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
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            pdf_name, repo, path, pdf_text, content='documents', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS document_insert AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid,pdf_name,repo,path,pdf_text)
            VALUES(new.id,new.pdf_name,new.repo,new.path,new.pdf_text);
        END;
        CREATE TRIGGER IF NOT EXISTS document_delete AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts,rowid,pdf_name,repo,path,pdf_text)
            VALUES('delete',old.id,old.pdf_name,old.repo,old.path,old.pdf_text);
        END;
        CREATE TRIGGER IF NOT EXISTS document_update AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts,rowid,pdf_name,repo,path,pdf_text)
            VALUES('delete',old.id,old.pdf_name,old.repo,old.path,old.pdf_text);
            INSERT INTO documents_fts(rowid,pdf_name,repo,path,pdf_text)
            VALUES(new.id,new.pdf_name,new.repo,new.path,new.pdf_text);
        END;
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
        if recover_jobs:
            db.execute(
                "UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')"
            )
