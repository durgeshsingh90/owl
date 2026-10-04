"""Compare two texts. The backend aligns, colours, filters and copies blocks; the UI renders."""

import json
import secrets
import time

from app.compare import diff
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
    view: str = View


@router.post("/layout")
def layout(value: CompareInput):
    # A plain function runs in FastAPI's thread pool, so large diffs never block requests.
    try:
        return diff.layout(
            value.originalText, value.modifiedText, value.ignoreWhitespace, value.view
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
    if row is None:
        raise HTTPException(404, "Share link not found.")
    if row["expires_at"] <= time.time():
        raise HTTPException(410, "This share link has expired.")
    return {
        "token": token,
        "payload": json.loads(row["payload"]),
        "createdAt": row["created_at"],
        "expiresAt": row["expires_at"],
    }
