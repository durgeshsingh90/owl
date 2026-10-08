"""Compare two texts. The backend aligns, colours, filters and copies blocks; the UI renders."""

import json
import secrets
import time

from app.compare import diff, formatting
from app.core.database import connection
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/compare")

SHARE_SECONDS = 24 * 60 * 60
VIEW_PATTERN = "^(all|differences|similarities)$"
View = Field("all", pattern=VIEW_PATTERN)


class CompareInput(BaseModel):
    originalText: str = ""
    modifiedText: str = ""
    ignoreWhitespace: bool = True
    ignoreBlankLines: bool = True
    view: str = View


@router.post("/layout")
def layout(value: CompareInput):
    # A plain function runs in FastAPI's thread pool, so large diffs never block requests.
    try:
        return diff.layout(
            value.originalText,
            value.modifiedText,
            value.ignoreWhitespace,
            value.view,
            value.ignoreBlankLines,
        )
    except ValueError as error:
        raise HTTPException(413, str(error)) from None


@router.get("/layout/{key}")
def layout_by_key(key: str, view: str = Query("all", pattern=VIEW_PATTERN)):
    result = diff.layout_by_key(key, view)
    if result is None:
        raise HTTPException(404, "Comparison expired. Send the texts again.")
    return result


class CopyInput(BaseModel):
    key: str
    block: int = Field(ge=0)
    direction: str = Field(pattern="^(left|right)$")


@router.post("/copy")
def copy_block(value: CopyInput):
    try:
        result = diff.copy_block(value.key, value.block, value.direction)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if result is None:
        raise HTTPException(404, "Comparison expired. Send the texts again.")
    return result


class ShareInput(BaseModel):
    originalTitle: str = Field("Original", max_length=200)
    modifiedTitle: str = Field("Modified", max_length=200)
    originalText: str = Field("", max_length=diff.MAX_TEXT_CHARS)
    modifiedText: str = Field("", max_length=diff.MAX_TEXT_CHARS)
    ignoreWhitespace: bool = True
    ignoreBlankLines: bool = True
    view: str = View


@router.post("/share", status_code=201)
def create_share(value: ShareInput):
    now = time.time()
    token = secrets.token_urlsafe(9)
    with connection() as db:
        db.execute("DELETE FROM compare_shares WHERE expires_at<=?", (now,))
        db.execute(
            "INSERT INTO compare_shares(token,payload,created_at,expires_at) VALUES(?,?,?,?)",
            (token, value.model_dump_json(), now, now + SHARE_SECONDS),
        )
    return {"token": token, "expiresAt": now + SHARE_SECONDS}


@router.get("/share/{token}")
def share(token: str):
    with connection() as db:
        row = db.execute(
            "SELECT * FROM compare_shares WHERE token=?", (token,)
        ).fetchone()
        recent = db.execute(
            "SELECT * FROM compare_history WHERE token=?", (token,)
        ).fetchone()
    # A recent comparison's link works for as long as it stays among the recent ones.
    if recent is not None:
        return {
            "token": token,
            "payload": json.loads(recent["payload"]),
            "createdAt": recent["created_at"],
            "expiresAt": None,
        }
    if row is None:
        raise HTTPException(404, "Share link not found. Only the 20 most recent comparisons are kept.")
    if row["expires_at"] <= time.time():
        raise HTTPException(410, "This share link has expired.")
    return {
        "token": token,
        "payload": json.loads(row["payload"]),
        "createdAt": row["created_at"],
        "expiresAt": row["expires_at"],
    }


class FormatInput(BaseModel):
    text: str = Field("", max_length=diff.MAX_TEXT_CHARS)


@router.post("/format")
def format_text(value: FormatInput):
    """Pretty-print JSON, JSON Lines or JSON5."""
    try:
        text, kind = formatting.prettify(value.text)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"text": text, "kind": kind}


# The most recent comparisons are kept so they can be opened again or shared; older
# ones are deleted as new ones are saved.
RECENT = 20


class HistoryInput(ShareInput):
    token: str = Field("", max_length=64)


@router.post("/history")
def save_history(value: HistoryInput):
    """Save the current comparison: update it if it is already recent, else add it."""
    now = time.time()
    payload = value.model_dump_json(exclude={"token"})
    with connection() as db:
        updated = value.token and db.execute(
            "UPDATE compare_history SET payload=?,updated_at=? WHERE token=?",
            (payload, now, value.token),
        ).rowcount
        token = value.token if updated else secrets.token_urlsafe(9)
        if not updated:
            db.execute(
                "INSERT INTO compare_history(token,payload,created_at,updated_at) VALUES(?,?,?,?)",
                (token, payload, now, now),
            )
        db.execute(
            "DELETE FROM compare_history WHERE token NOT IN (SELECT token FROM compare_history ORDER BY updated_at DESC LIMIT ?)",
            (RECENT,),
        )
    return {"token": token, "updatedAt": now}


@router.get("/history")
def history():
    with connection() as db:
        rows = db.execute(
            "SELECT token,created_at,updated_at,json_extract(payload,'$.originalTitle') AS original_title,"
            "json_extract(payload,'$.modifiedTitle') AS modified_title,"
            "length(json_extract(payload,'$.originalText')) AS original_chars,"
            "length(json_extract(payload,'$.modifiedText')) AS modified_chars "
            "FROM compare_history ORDER BY updated_at DESC"
        ).fetchall()
    return {"items": [dict(row) for row in rows], "limit": RECENT}


@router.delete("/history/{token}")
def delete_history(token: str):
    with connection() as db:
        db.execute("DELETE FROM compare_history WHERE token=?", (token,))
    return {"ok": True}
