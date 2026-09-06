"""Literal FTS terms: ordinary terms use OR; all +terms use AND."""

import re

from app.core.database import connection


def build_query(query):
    terms = re.findall(r'\+?"[^"]+"|\S+', query.strip())
    if not terms:
        return None
    use_and = all(term.startswith("+") for term in terms)
    quoted = []
    for term in terms:
        term = term.lstrip("+").strip('"')
        if term and any(c.isalnum() for c in term):
            quoted.append('"' + term.replace('"', '""') + '"')
    return (" AND " if use_and else " OR ").join(quoted) or None


def search_documents(q="", project=None, repo=None, author=None, limit=100, offset=0):
    conditions, params = [], []
    expression = build_query(q)
    if q.strip() and not expression:
        return []
    if expression:
        conditions.append(
            "d.id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)"
        )
        params.append(expression)
    for column, value in [("project", project), ("repo", repo), ("author", author)]:
        if value is not None:
            conditions.append(f"d.{column}=?")
            params.append(value)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT d.id,d.repository_id,d.project,d.repo,d.pdf_name,d.path,d.url,d.author,d.commit_date,"
                "d.file_size,d.page_count,d.added_at,d.updated_at,d.open_count FROM documents d"
                + where
                + " ORDER BY d.commit_date DESC,d.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]


def matching_document_ids(query, fields, mode):
    """Search selected FTS columns without truncating the workspace result set."""
    columns = {
        "name": "pdf_name",
        "path": "path",
        "content": "pdf_text",
        "notes": "notes",
    }
    selected = [columns[field] for field in fields if field in columns]
    if not selected or not query.strip():
        return []
    terms = query.split() if mode == "separate" else [query.strip()]
    terms = [term for term in terms if any(c.isalnum() for c in term)]
    if not terms:
        return []
    expression = (
        "{"
        + " ".join(selected)
        + "} : ("
        + " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        + ")"
    )
    with connection() as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?",
                (expression,),
            )
        ]
