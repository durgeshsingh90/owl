"""OWL FastAPI entry point: API, managed crawl lifecycle, and static UI."""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from app.api.routes import router
from app.bookmarks.refresh import run_scheduler
from app.core.database import initialize
from app.core.library import TEXT_LIBRARIES, LibraryMiddleware, library
from app.core.logging import configure_logging, error_details, event, request_id
from app.pdfs.jobs import Jobs
from app.pdfs.schedule import run_scheduler as run_bitbucket_scheduler
from app.tracker.service import run_scheduler as run_tracker_scheduler
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
    for name, sub_app in text_apps.items():
        token = library.set(name)
        try:
            initialize(recover_jobs=True)
            sub_app.state.jobs = Jobs()
        finally:
            library.reset(token)
    refresh_task = asyncio.create_task(run_scheduler())
    tracker_task = asyncio.create_task(run_tracker_scheduler())
    bitbucket_task = asyncio.create_task(run_bitbucket_scheduler(app.state.jobs))
    # NAAS and Network Automation refresh on the same daily schedule, each in its own
    # library: a task copies the library selection active when it is created.
    library_tasks = []
    for name, sub_app in text_apps.items():
        token = library.set(name)
        try:
            library_tasks.append(
                asyncio.create_task(run_bitbucket_scheduler(sub_app.state.jobs))
            )
        finally:
            library.reset(token)
    yield
    for task in (bitbucket_task, *library_tasks):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    refresh_task.cancel()
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass
    tracker_task.cancel()
    try:
        await tracker_task
    except asyncio.CancelledError:
        pass
    await app.state.jobs.shutdown()
    app.state.jobs.extractor.shutdown(wait=False, cancel_futures=True)
    for name, sub_app in text_apps.items():
        token = library.set(name)
        try:
            await sub_app.state.jobs.shutdown()
            sub_app.state.jobs.extractor.shutdown(wait=False, cancel_futures=True)
        finally:
            library.reset(token)
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


from app.api.aws_accounts import router as aws_accounts_router
from app.api.bookmarks import router as bookmarks_router
from app.api.compare import router as compare_router
from app.api.confluence_library import router as confluence_library_router
from app.api.compat import router as compat_router
from app.api.tracker import router as tracker_router
from app.api.workspace import router as workspace_router

app.include_router(aws_accounts_router)
app.include_router(bookmarks_router)
app.include_router(compare_router)
app.include_router(workspace_router)
app.include_router(tracker_router)
app.include_router(confluence_library_router)
app.include_router(compat_router)
app.include_router(router)
app.include_router(router, prefix="/api")


@app.get("/")
def home():
    return RedirectResponse("/home/")


frontend = Path(__file__).resolve().parent.parent / "frontend"
for name in (
    "home",
    "bitbucket",
    "bookmarks",
    "confluence-tracker",
    "aws-accounts",
    "compare",
):
    app.mount("/" + name, StaticFiles(directory=frontend / name, html=True), name=name)


from app.api.workspace import workspace as library_workspace
from app.api.workspace import workspace_revision as library_workspace_revision

text_apps = {}
for name, title in (("naas", "NAAS Update"), ("network", "Network Automation")):
    sub_app = FastAPI(title=title)
    sub_app.add_middleware(LibraryMiddleware, name=name)
    sub_app.add_exception_handler(ValueError, invalid_value)
    sub_app.add_exception_handler(ValidationError, validation_error)
    sub_app.add_exception_handler(RequestValidationError, validation_error)
    sub_app.include_router(router, prefix="/api")
    sub_app.include_router(compat_router)
    sub_app.add_api_route("/api/workspace", library_workspace, methods=["GET"])
    sub_app.add_api_route(
        "/api/workspace/revision", library_workspace_revision, methods=["GET"]
    )
    prefix = TEXT_LIBRARIES[name]["prefix"]
    sub_app.mount(
        "/",
        StaticFiles(directory=frontend / prefix.strip("/"), html=True),
        name=name + "-ui",
    )
    app.mount(prefix, sub_app)
    text_apps[name] = sub_app
