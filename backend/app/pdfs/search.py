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
    """Require every term across selected fields of one document, or one phrase."""
    columns = {
        "name": "pdf_name",
        "path": "path",
        "content": "pdf_text",
        "notes": "notes",
    }
    selected = [columns[field] for field in fields if field in columns]
    if not selected or not query.strip():
        return []
    positive, negative = [], []
    for token in re.findall(r'-?"[^"\n]*"|\S+', query.strip()):
        excluded = token.startswith("-") and len(token) > 1
        term = (token[1:] if excluded else token).strip('"')
        if any(c.isalnum() for c in term):
            (negative if excluded else positive).append(term)
    if not positive and not negative:
        return []
    if mode == "together" and positive:
        positive = [" ".join(positive)]

    def expression(terms, operator):
        return (
            "{"
            + " ".join(selected)
            + "} : ("
            + operator.join('"' + term.replace('"', '""') + '"' for term in terms)
            + ")"
        )

    conditions, params = [], []
    if positive:
        conditions.append(
            "id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)"
        )
        params.append(expression(positive, " AND "))
    if negative:
        conditions.append(
            "id NOT IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)"
        )
        params.append(expression(negative, " OR "))
    with connection() as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT id FROM documents WHERE " + " AND ".join(conditions), params
            )
        ]
