"""Settings contract used by the existing Bitbucket frontend."""

from urllib.parse import parse_qs

from app.api.routes import test_value
from app.core.config import Settings, load_settings, save_settings
from app.core.database import connection
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/bitbucket/workspace/")
def workspace():
    try:
        settings = load_settings()
        credentials = [
            {
                "baseUrl": settings.base_url,
                "username": settings.username,
                "verifySsl": settings.verify_ssl,
            }
        ]
    except ValueError:
        credentials = []
    with connection() as db:
        repositories = [
            dict(row)
            for row in db.execute(
                "SELECT r.id,r.repo,p.server,p.project FROM repositories r JOIN tracked_projects p ON p.id=r.project_id"
            )
        ]
    return {
        "ok": True,
        "credentials": credentials,
        "repositories": repositories,
        "csrfToken": "",
        "settingsTestUrl": "/bitbucket/settings/test/",
        "settingsSaveUrl": "/bitbucket/settings/save/",
    }


async def form_settings(request):
    data = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    token = data.get("access_token", "")
    if not token:
        previous = load_settings()
        if (
            Settings.normalize(data.get("base_url", "")) != previous.base_url
            or data.get("username") != previous.username
        ):
            raise HTTPException(
                400, "Enter a token when changing the server or username."
            )
        token = previous.token.get_secret_value()
    return Settings(
        base_url=data.get("base_url", ""),
        username=data.get("username", ""),
        token=token,
        verify_ssl=False,
    )


@router.post("/bitbucket/settings/test/")
async def test(request: Request):
    return await test_value(await form_settings(request))


@router.post("/bitbucket/settings/save/")
async def save(request: Request):
    if request.app.state.jobs.active():
        raise HTTPException(409, "Wait for the active crawl before changing settings.")
    save_settings(await form_settings(request))
    return {"ok": True}
