"""Validated local document open and reveal actions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

from bitbucket.models import Document, DocumentKind
from bitbucket.services.git_sync import repository_path


class DocumentActionError(RuntimeError):
    pass


def document_path(document: Document) -> Path:
    relative = PurePosixPath(document.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise DocumentActionError("The stored document path is unsafe.")
    root = repository_path(document.repository)
    candidate = (root / Path(*relative.parts)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise DocumentActionError("The stored document path leaves its repository.")
    if not candidate.is_file():
        raise DocumentActionError("The document is no longer available locally.")
    if document.kind != DocumentKind.PDF or candidate.suffix.casefold() != ".pdf":
        raise DocumentActionError("Only catalogued PDF files can be opened.")
    return candidate


def _launch(arguments: tuple[str, ...]) -> None:
    subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )


def open_pdf(document: Document) -> Path:
    path = document_path(document)
    if sys.platform == "darwin":
        _launch(("open", str(path)))
    elif sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        _launch(("xdg-open", str(path)))
    return path


def reveal_pdf(document: Document) -> Path:
    path = document_path(document)
    if sys.platform == "darwin":
        _launch(("open", "-R", str(path)))
    elif sys.platform == "win32":
        _launch(("explorer", f"/select,{path}"))
    else:
        _launch(("xdg-open", str(path.parent)))
    return path.parent
