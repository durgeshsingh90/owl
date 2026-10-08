"""Where the JSON Visualizer keeps files, and receiving uploads without loading them."""

import os
import tempfile
import zlib

from app.core.database import database_path

GZIP_MAGIC = b"\x1f\x8b"
# Decompressed output per step: a small, highly compressed upload must not expand into
# memory all at once.
INFLATE_STEP = 16 * 1024 * 1024


def folder():
    return database_path().parent / "json-visualizer"


def subfolder(name):
    path = folder() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def folder_bytes():
    """Bytes in the folder (snapshots, server documents, command output being written)."""
    total = 0
    for directory, _, names in os.walk(folder()):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass  # Removed while counting.
    return total


class Gunzip:
    """Streaming gzip decompression, including files of several gzip members."""

    def __init__(self):
        self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.done = False

    def feed(self, data):
        while data:
            if self.done:
                # A further member, or padding after the last one (ignored, like gzip -d).
                if not data.startswith(GZIP_MAGIC):
                    return
                self.inflater, self.done = zlib.decompressobj(16 + zlib.MAX_WBITS), False
            try:
                chunk = self.inflater.decompress(data, INFLATE_STEP)
                while chunk:
                    yield chunk
                    if not self.inflater.unconsumed_tail:
                        break
                    chunk = self.inflater.decompress(self.inflater.unconsumed_tail, INFLATE_STEP)
            except zlib.error:
                raise ValueError("The gzip file is damaged.") from None
            if self.inflater.eof:
                self.done = True
                data = self.inflater.unused_data
            else:
                data = b""

    def finish(self):
        if not self.done:
            raise ValueError("The gzip file is incomplete.")


async def receive(request, directory, *, gunzip=False):
    """Stream the request body to a new file in directory: (path, bytes written).

    With gunzip, a gzip body (detected by its magic bytes) is decompressed on the way.
    """
    handle, path = tempfile.mkstemp(dir=directory, prefix=".upload-")
    size = 0
    inflate = None
    first = True
    try:
        with os.fdopen(handle, "wb") as file:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if first:
                    first = False
                    if gunzip and chunk[:2] == GZIP_MAGIC:
                        inflate = Gunzip()
                for data in inflate.feed(chunk) if inflate else (chunk,):
                    file.write(data)
                    size += len(data)
            if inflate:
                inflate.finish()
    except BaseException:
        os.unlink(path)
        raise
    return path, size
