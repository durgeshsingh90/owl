"""OWL FastAPI entry point: API, managed crawl lifecycle, and static UI."""

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from app.api.routes import router
from app.core.database import initialize
from app.core.logging import configure_logging, error_details, event, request_id
from app.pdfs.jobs import Jobs
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    event("backend.starting", verify_ssl=False)
    initialize(recover_jobs=True)
    app.state.jobs = Jobs()
    yield
    await app.state.jobs.shutdown()
    event("backend.stopped")


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


@app.middleware("http")
async def diagnostics(request: Request, call_next):
    identifier = uuid.uuid4().hex[:16]
    context = request_id.set(identifier)
    started = time.monotonic()
    event("request.started", method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = identifier
        if request.url.path.endswith((".js", ".css", ".html", "/")):
            response.headers["Cache-Control"] = "no-store"
        event(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return response
    except Exception as error:  # noqa: BLE001 - log safe diagnostics at the HTTP boundary
        event("request.failed", level=40, **error_details(error))
        return JSONResponse(
            {"detail": "Backend error. Check backend logs.", "request_id": identifier},
            status_code=500,
            headers={"X-Request-ID": identifier},
        )
    finally:
        request_id.reset(context)


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
