"""JSON Visualizer: snapshots, the command runner, opening URLs and server mode.

Uploads send the file as the raw request body (no multipart), streamed to disk.
"""

import os
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

import httpx
from app.jsonviz import runner, serverdoc, snapshots, storage
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/json-visualizer")

FETCH_LIMIT = 2 * 1024 * 1024 * 1024


def _bad(error, status=400):
    raise HTTPException(status, str(error)) from None


# Snapshots


@router.post("/snapshots", status_code=201)
async def create_snapshot(
    request: Request, name: str = Query(..., max_length=1000), labels: str = "",
    format: str = "", source: str = "upload", command: str = Query("", max_length=2000),
):
    try:
        labels = snapshots.clean_labels(labels)
        snapshots.clean_name(name)
    except ValueError as error:
        _bad(error)
    path, size = await storage.receive(request, storage.subfolder("snapshots"))
    if not size:
        os.unlink(path)
        _bad("The file is empty.")
    try:
        return await run_in_threadpool(
            snapshots.store, path, name, labels=labels, format=format, origin=source, command=command
        )
    except ValueError as error:
        _bad(error)


@router.get("/snapshots")
def list_snapshots():
    return snapshots.listing()


class DeleteAll(BaseModel):
    confirm: str = ""


@router.delete("/snapshots")
def delete_snapshots(value: DeleteAll):
    if value.confirm != "delete all":
        _bad('Send {"confirm": "delete all"} to delete every snapshot.')
    deleted = snapshots.delete_all()
    serverdoc.drop_all()
    return {"deleted": deleted}


def _snapshot(snapshot_id):
    record = snapshots.get(snapshot_id)
    if record is None:
        raise HTTPException(404, "Snapshot not found.")
    return record


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str):
    return _snapshot(snapshot_id)


def content_disposition(name):
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/snapshots/{snapshot_id}/content")
def snapshot_content(snapshot_id: str):
    record = _snapshot(snapshot_id)
    try:
        chunks = snapshots.content(snapshot_id)
    except FileNotFoundError:
        raise HTTPException(404, "The snapshot's file is missing.") from None
    return StreamingResponse(
        chunks, media_type="application/json",
        headers={"Content-Disposition": content_disposition(record["name"]), "Content-Length": str(record["size"])},
    )


class SnapshotChange(BaseModel):
    name: Optional[str] = Field(None, max_length=1000)
    labels: Optional[dict] = None


@router.patch("/snapshots/{snapshot_id}")
def update_snapshot(snapshot_id: str, value: SnapshotChange):
    _snapshot(snapshot_id)
    try:
        return snapshots.update(snapshot_id, value.name, value.labels)
    except ValueError as error:
        _bad(error)


@router.delete("/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str):
    if not snapshots.delete(snapshot_id):
        raise HTTPException(404, "Snapshot not found.")
    return {"deleted": snapshot_id}


class SettingsInput(BaseModel):
    retention_days: Optional[int] = None
    max_snapshots: Optional[int] = None


@router.get("/settings")
def get_settings():
    return snapshots.settings()


@router.put("/settings")
def put_settings(value: SettingsInput):
    try:
        return snapshots.save_settings(value.model_dump(exclude_none=True))
    except ValueError as error:
        _bad(error)


# Command runner


@router.get("/runner")
def runner_overview():
    return runner.overview()


class RunInput(BaseModel):
    tool: str
    args: list[str] = Field(default_factory=list, max_length=100)
    profile: str = ""
    region: str = ""
    subscription: str = ""
    context: str = ""
    project: str = ""
    timeout: int = Field(120, ge=5, le=900)
    name: str = Field("", max_length=1000)
    save_as: str = Field("", max_length=200)
    command_id: Optional[int] = None


def _fields(value):
    return runner.options(value.tool, value.model_dump(include={"profile", "region", "subscription", "context", "project"}))


@router.post("/run", status_code=202)
def run(value: RunInput):
    try:
        fields = _fields(value)
        command_id = value.command_id
        if value.save_as.strip():
            command_id = runner.save_command(value.save_as, value.tool, value.args, fields)["id"]
        job = runner.start(value.tool, value.args, fields, timeout=value.timeout, name=value.name or None, command_id=command_id)
    except ValueError as error:
        _bad(error)
    return {"job_id": job.id, "command": job.command}


def _job(job_id):
    job = runner.job(job_id)
    if job is None:
        raise HTTPException(404, "This run is no longer known. Run the command again.")
    return job


@router.get("/run/{job_id}")
def run_status(job_id: str):
    return _job(job_id).view()


@router.post("/run/{job_id}/cancel")
def run_cancel(job_id: str):
    _job(job_id)
    return runner.cancel(job_id).view()


class CommandInput(BaseModel):
    name: str
    tool: str
    args: list[str] = Field(default_factory=list, max_length=100)
    profile: str = ""
    region: str = ""
    subscription: str = ""
    context: str = ""
    project: str = ""


@router.get("/commands")
def list_commands():
    return {"commands": runner.list_commands()}


@router.post("/commands", status_code=201)
def save_command(value: CommandInput):
    try:
        return runner.save_command(value.name, value.tool, value.args, _fields(value))
    except ValueError as error:
        _bad(error)


@router.delete("/commands/{command_id}")
def delete_command(command_id: int):
    if not runner.delete_command(command_id):
        raise HTTPException(404, "Saved command not found.")
    return {"deleted": command_id}


# Opening a URL


class FetchInput(BaseModel):
    url: str = Field(max_length=8000)


def file_name(url):
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return name.strip() or "download.json"


@router.post("/fetch")
async def fetch(value: FetchInput):
    """Download a URL for the page (which cannot, across origins) and stream it back."""
    url = value.url.strip()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        _bad("This URL is not valid.")
    if parsed.scheme.lower() not in ("http", "https") or not hostname:
        _bad("Use an http:// or https:// URL.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        _bad("Remove the user name and password from the URL.")
    # Like the rest of OWL, certificates are not verified: work networks re-sign TLS.
    client = httpx.AsyncClient(
        follow_redirects=True, verify=False, timeout=httpx.Timeout(30, connect=30, read=300)
    )
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        _bad(f"Could not download the file: {type(error).__name__}.", 502)
    if response.status_code >= 400:
        await response.aclose()
        await client.aclose()
        _bad(f"The server answered {response.status_code} {response.reason_phrase}.".strip(), 502)
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > FETCH_LIMIT:
        await response.aclose()
        await client.aclose()
        _bad("The file is larger than 2 GB.")

    async def body():
        received = 0
        try:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > FETCH_LIMIT:
                    raise ValueError("The file is larger than 2 GB.")
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    # The name is percent-encoded: header values must be ASCII.
    headers = {"X-File-Name": quote(file_name(url), safe=" ._-()[]")}
    if length.isdigit() and not response.headers.get("content-encoding"):
        headers["X-File-Size"] = length
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


# Server mode


def _document(document_id, ready=True):
    try:
        return serverdoc.get(document_id, ready)
    except serverdoc.NotFound as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def _serve(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except serverdoc.NotFound as error:
        raise HTTPException(404, str(error)) from None
    except serverdoc.TooLarge as error:
        raise HTTPException(413, str(error)) from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


def _path(text):
    return _serve(serverdoc.parse_path, text)


@router.post("/documents", status_code=201)
async def upload_document(request: Request, name: str = Query(..., max_length=1000)):
    try:
        name = snapshots.clean_name(name)
    except ValueError as error:
        _bad(error)
    try:
        path, size = await storage.receive(request, serverdoc.upload_folder(), gunzip=True)
    except ValueError as error:
        _bad(error)
    if not size:
        os.unlink(path)
        _bad("The file is empty.")
    document = serverdoc.add(name, path, size)
    return {"id": document.id, "name": document.name, "size": document.size}


@router.get("/documents/{document_id}/status")
def document_status(document_id: str):
    return _document(document_id, ready=False).status()


@router.delete("/documents/{document_id}")
def delete_document(document_id: str):
    if not serverdoc.drop(document_id):
        raise HTTPException(404, "This document is no longer open on the server.")
    return {"deleted": document_id}


@router.get("/documents/{document_id}/node")
def document_node(document_id: str, path: str = ""):
    return _serve(serverdoc.node, _document(document_id), _path(path))


@router.get("/documents/{document_id}/children")
def document_children(document_id: str, path: str = "", offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)):
    return _serve(serverdoc.children, _document(document_id), _path(path), offset, limit)


@router.get("/documents/{document_id}/value")
def document_value(document_id: str, path: str = ""):
    return Response(_serve(serverdoc.value, _document(document_id), _path(path)), media_type="application/json")


@router.get("/documents/{document_id}/rows")
def document_rows(
    document_id: str, path: str = "", flatten: str = "", offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000), filters: str = "", sort: str = "", columns: bool = False,
):
    document = _document(document_id)
    return _serve(
        serverdoc.rows, document, _path(path), flatten or None, offset, limit,
        _serve(serverdoc.parse_filters, filters), _serve(serverdoc.parse_sort, sort), columns,
    )


@router.get("/documents/{document_id}/summary")
def document_summary(document_id: str, fields: str, path: str = "", flatten: str = "", filters: str = ""):
    names = [field.strip() for field in fields.split(",") if field.strip()]
    if not names or len(names) > 20:
        _bad("Give 1 to 20 fields, separated by commas.")
    document = _document(document_id)
    return _serve(serverdoc.summary, document, _path(path), names, flatten or None, _serve(serverdoc.parse_filters, filters))


class SearchInput(BaseModel):
    query: str = Field(max_length=1000)
    caseSensitive: bool = False
    wholeWord: bool = False
    regex: bool = False
    scope: str = "both"


@router.post("/documents/{document_id}/search", status_code=202)
def document_search(document_id: str, value: SearchInput):
    search = _serve(
        serverdoc.start_search, _document(document_id), value.query, value.caseSensitive,
        value.wholeWord, value.regex, value.scope,
    )
    return {"search_id": search.id}


@router.get("/documents/{document_id}/search/{search_id}")
def document_search_results(document_id: str, search_id: str, after: int = Query(0, ge=0)):
    return _serve(serverdoc.get_search, _document(document_id), search_id).page(after)


@router.delete("/documents/{document_id}/search/{search_id}")
def document_search_cancel(document_id: str, search_id: str):
    if not serverdoc.cancel_search(_document(document_id), search_id):
        raise HTTPException(404, "This search has finished or was replaced.")
    return {"cancelled": search_id}
