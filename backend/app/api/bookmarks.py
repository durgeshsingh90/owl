"""Bookmark metadata and independent Confluence settings."""

from urllib.parse import parse_qs

from app.bookmarks import confluence
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

router = APIRouter()


@router.get("/bookmarks/settings/workspace/")
def settings_workspace():
    try:
        settings = confluence.load()
        config = {
            "baseUrl": settings.base_url,
            "verifySsl": settings.verify_ssl,
            "hasToken": True,
        }
    except ValueError:
        config = {"baseUrl": "", "verifySsl": True, "hasToken": False}
    return {
        "configuration": config,
        "urls": {
            "confluenceTest": "/bookmarks/settings/test/",
            "confluenceSave": "/bookmarks/settings/save/",
        },
    }


async def settings_form(request):
    fields = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    token = fields.get("personal_access_token", "")
    if not token:
        previous = confluence.load()
        if (
            confluence.ConfluenceSettings.valid_base(fields.get("base_url", ""))
            != previous.base_url
        ):
            raise ValueError("Enter a new PAT when changing the server.")
        token = previous.token.get_secret_value()
    return confluence.ConfluenceSettings(
        base_url=fields.get("base_url", ""),
        token=token,
        verify_ssl=fields.get("verify_ssl") == "on",
    )


@router.post("/bookmarks/settings/test/")
async def test_settings(request: Request):
    return await confluence.test(await settings_form(request))


@router.post("/bookmarks/settings/save/")
async def save_settings(request: Request):
    settings = await settings_form(request)
    await confluence.test(settings)
    confluence.save(settings)
    return {"state": "success"}


@router.post("/bookmarks/connection/test/")
async def test_connection():
    return await confluence.test(confluence.load())


class BookmarkURL(BaseModel):
    url: str


@router.post("/api/bookmarks/resolve")
async def resolve(value: BookmarkURL):
    return await confluence.metadata(value.url)


class FolderDownload(BaseModel):
    folder_key: str
    space_key: str = ""
    root_title: str = ""
    base_url: str = ""
    root_ids: list[str] = []


@router.get("/api/bookmarks/downloads")
def folder_downloads():
    from app.core.database import connection

    with connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM bookmark_downloads")]


@router.post("/api/bookmarks/downloads", status_code=202)
async def start_folder_download(
    value: FolderDownload, background_tasks: BackgroundTasks
):
    from app.bookmarks.downloads import download, stamp
    from app.core.database import connection
    from fastapi import HTTPException

    settings = confluence.load()
    if value.base_url and value.base_url.rstrip("/") != settings.base_url.rstrip("/"):
        raise HTTPException(
            422, "Configure the Confluence connection for this folder first."
        )
    if (
        not value.folder_key
        or (not value.space_key and not value.root_ids)
        or any(not root.isdecimal() for root in value.root_ids)
    ):
        raise HTTPException(
            422, "This folder has no resolvable Confluence page or space."
        )
    with connection() as db:
        row = db.execute(
            "SELECT status FROM bookmark_downloads WHERE folder_key=?",
            (value.folder_key,),
        ).fetchone()
        if row and row[0] == "running":
            return {"status": "running"}
        db.execute(
            "INSERT OR REPLACE INTO bookmark_downloads (folder_key,status,count,error,updated_at,total,phase) VALUES(?,'running',0,NULL,?,0,'discovering')",
            (value.folder_key, stamp()),
        )
    background_tasks.add_task(
        download,
        settings,
        value.folder_key,
        value.space_key,
        value.root_ids,
        value.root_title,
    )
    return {"status": "running"}


@router.get("/api/bookmarks/downloaded-search")
def downloaded_search(
    q: str = "",
    fields: str = "title,url,content,page_id",
    mode: str = "separate",
    include_all: bool = False,
):
    from app.core.database import connection

    selected = [
        field
        for field in fields.split(",")
        if field in {"title", "url", "content", "page_id"}
    ]
    terms = ([q.strip()] if mode == "together" else q.split()) if q.strip() else []
    if (not terms and not include_all) or (terms and not selected):
        return {"total": 0, "items": []}
    clauses, params = [], []
    for field in selected:
        for term in terms:
            clauses.append(f"instr(lower({field}), lower(?)) > 0")
            params.append(term)
    condition = " OR ".join(clauses) or "1=1"
    with connection() as db:
        rows = db.execute(
            f"SELECT page_id,title,url,folder_path,folder_key FROM bookmark_downloaded_pages WHERE {condition} ORDER BY title",
            params,
        ).fetchall()
    import json

    unique = {}
    for row in rows:
        item = dict(row)
        item["folderPath"] = json.loads(item.pop("folder_path"))
        if not item["folderPath"]:
            try:
                item["folderPath"] = json.loads(item["folder_key"])[1:]
            except (ValueError, TypeError):
                item["folderPath"] = ["Downloaded pages"]
        unique.setdefault(item["url"], item)
    return {"total": len(unique), "items": list(unique.values())}
