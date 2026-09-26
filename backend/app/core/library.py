"""Request/task-local library selection; PDF remains the default."""

from contextvars import ContextVar
from pathlib import PurePosixPath

library = ContextVar("owl_library", default="pdf")


def is_naas():
    return library.get() == "naas"


def supported_file(path):
    name = PurePosixPath(path).name.lower()
    if not is_naas():
        return name.endswith(".pdf")
    return name.endswith((".yaml", ".yml")) or name in {
        "readme",
        "readme.md",
        "readme.markdown",
        "readme.rst",
        "readme.txt",
        "readme.adoc",
    }


def extract_text(content):
    from app.pdfs.client import BitbucketError

    try:
        encoding = (
            "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        )
        text = content.decode(encoding)
        if "\x00" in text:
            raise ValueError("binary data")
        return len(text.splitlines()), text
    except (UnicodeError, ValueError):
        raise BitbucketError("File must contain UTF-8 or UTF-16 text.") from None


class LibraryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = library.set("naas")
        try:
            await self.app(scope, receive, send)
        finally:
            library.reset(token)
