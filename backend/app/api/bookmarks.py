"""Bookmark metadata and independent Confluence settings."""

from urllib.parse import parse_qs

from app.bookmarks import confluence
from fastapi import APIRouter, Request
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
