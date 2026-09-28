"""Request/task-local library selection; PDF remains the default."""

from contextvars import ContextVar
from pathlib import PurePosixPath

library = ContextVar("owl_library", default="pdf")

# Text libraries reuse the Bitbucket explorer with their own data, URL prefix and files.
TEXT_LIBRARIES = {
    "naas": {"prefix": "/naas", "suffixes": (".yaml", ".yml"), "label": "YAML"},
    "network": {
        "prefix": "/network-automation",
        "suffixes": (".json",),
        "label": "JSON",
    },
}
README_NAMES = {
    "readme",
    "readme.md",
    "readme.markdown",
    "readme.rst",
    "readme.txt",
    "readme.adoc",
}


def is_text_library():
    return library.get() in TEXT_LIBRARIES


def library_prefix():
    return TEXT_LIBRARIES.get(library.get(), {}).get("prefix", "")


def library_label():
    return TEXT_LIBRARIES[library.get()]["label"] + "/README"


def supported_file(path):
    name = PurePosixPath(path).name.lower()
    if not is_text_library():
        return name.endswith(".pdf")
    return name.endswith(TEXT_LIBRARIES[library.get()]["suffixes"]) or name in README_NAMES


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
    def __init__(self, app, name="naas"):
        self.app = app
        self.name = name

    async def __call__(self, scope, receive, send):
        token = library.set(self.name)
        try:
            await self.app(scope, receive, send)
        finally:
            library.reset(token)
