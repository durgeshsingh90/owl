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


# Filler words never decide "words close together"; they still count in the exact phrase.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "the", "to", "with", "via", "vs",
}
# bm25 column weights in documents_fts order: pdf_name, repo, path, pdf_text, notes.
BM25_WEIGHTS = (10.0, 2.0, 3.0, 1.0, 4.0)
TIER_PHRASE, TIER_NEAR, TIER_ALL = 3, 2, 1
NEAR_DISTANCE = 10


def matching_document_ids(query, fields, mode):
    """Matching document IDs, most relevant first."""
    return [row[0] for row in ranked_matches(query, fields, mode)]


def ranked_matches(query, fields, mode):
    """Require every term across selected fields of one document, or one phrase.

    Returns (id, tier) pairs, best first: the exact phrase, then every word within a
    few words of each other in one field, then every word anywhere. Each tier is
    ordered by bm25 with file names weighted highest.
    """
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
    words = positive
    if mode == "together" and positive:
        positive = [" ".join(positive)]
    else:
        # "AWS for IDE" requires AWS and IDE; filler words are optional unless that is all there is.
        positive = [word for word in positive if word.lower() not in STOPWORDS] or positive

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
    def quoted(term):
        return '"' + term.replace('"', '""') + '"'

    scope = "{" + " ".join(selected) + "}"
    with connection() as db:
        if not positive:
            return [
                (row[0], TIER_ALL)
                for row in db.execute(
                    "SELECT id FROM documents WHERE " + " AND ".join(conditions)
                    + " ORDER BY commit_date DESC,id DESC",
                    params,
                )
            ]
        ids = [
            row[0]
            for row in db.execute(
                "SELECT id FROM documents WHERE " + " AND ".join(conditions), params
            )
        ]
        if not ids:
            return []

        def matching(expression):
            return {
                row[0]
                for row in db.execute(
                    "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?",
                    (expression,),
                )
            }

        # Relevance of the positive terms; lower bm25 is better.
        scores = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT rowid,bm25(documents_fts,?,?,?,?,?) FROM documents_fts WHERE documents_fts MATCH ?",
                (*BM25_WEIGHTS, params[0]),
            )
        }
        phrase = set(ids) if mode == "together" or len(words) < 2 else matching(
            scope + " : " + quoted(" ".join(words))
        )
        key = [word for word in words if word.lower() not in STOPWORDS] or words
        near = set()
        if mode != "together" and len(key) > 1:
            near = matching(
                scope + " : NEAR(" + " ".join(quoted(word) for word in key) + f", {NEAR_DISTANCE})"
            )
    tier = {
        id: TIER_PHRASE if id in phrase else TIER_NEAR if id in near else TIER_ALL
        for id in ids
    }
    ids.sort(key=lambda id: (-tier[id], scores.get(id, 0.0), -id))
    return [(id, tier[id]) for id in ids]
