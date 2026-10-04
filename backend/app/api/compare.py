"""Compare two texts. The backend aligns, highlights, filters and pages; the UI renders."""

import json
import secrets
import time

from app.compare import diff
from app.core.database import connection
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/compare")

SHARE_SECONDS = 24 * 60 * 60
View = Field("all", pattern="^(all|differences|similarities)$")


class CompareInput(BaseModel):
    originalText: str = ""
    modifiedText: str = ""
    ignoreWhitespace: bool = True
    view: str = View
    offset: int = Field(0, ge=0)
    limit: int = Field(diff.PAGE_LIMIT, ge=1, le=diff.PAGE_LIMIT)


@router.post("/analyze")
def analyze(value: CompareInput):
    # A plain function runs in FastAPI's thread pool, so large diffs never block requests.
    try:
        return diff.compare(
            value.originalText,
            value.modifiedText,
            value.ignoreWhitespace,
            value.view,
            value.offset,
            value.limit,
        )
    except ValueError as error:
        raise HTTPException(413, str(error)) from None


@router.get("/results/{key}")
def results(
    key: str,
    view: str = Query("all", pattern="^(all|differences|similarities)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(diff.PAGE_LIMIT, ge=1, le=diff.PAGE_LIMIT),
):
    result = diff.page(key, view, offset, limit)
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
