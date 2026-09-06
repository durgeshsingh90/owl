"""OWL FastAPI entry point: API, managed crawl lifecycle, and static UI."""

from contextlib import asynccontextmanager
from pathlib import Path

from app.api.routes import router
from app.core.database import initialize
from app.pdfs.jobs import Jobs
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError


@asynccontextmanager
async def lifespan(app):
    initialize(recover_jobs=True)
    app.state.jobs = Jobs()
    yield
    await app.state.jobs.shutdown()


app = FastAPI(title="OWL API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def local_writes(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse(
                {"detail": "Cross-origin writes are not allowed."}, status_code=403
            )
    return await call_next(request)


@app.exception_handler(ValueError)
async def invalid_value(request, error):
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.exception_handler(ValidationError)
@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    # Pydantic's default response includes input values, potentially a token.
    return JSONResponse(
        {
            "detail": [
                {"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                for e in error.errors()
            ]
        },
        status_code=422,
    )


from app.api.compat import router as compat_router
from app.api.workspace import router as workspace_router

app.include_router(workspace_router)
app.include_router(compat_router)
app.include_router(router)
app.include_router(router, prefix="/api")


@app.get("/")
def home():
    return RedirectResponse("/home/")


frontend = Path(__file__).resolve().parent.parent / "frontend"
for name in ("home", "bitbucket", "bookmarks"):
    app.mount("/" + name, StaticFiles(directory=frontend / name, html=True), name=name)
