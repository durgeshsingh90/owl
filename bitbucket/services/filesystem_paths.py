"""Windows long-path I/O adapters, not substitutes for checkout/path validation."""

from __future__ import annotations

import ntpath
import os
from pathlib import Path, PureWindowsPath


def _extended_windows_path(value: str) -> str:
    """Adapt an absolute drive/UNC path without enabling Windows device names."""

    value = value.replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = _plain_windows_path(value)
    if value.startswith("\\\\.\\") or not PureWindowsPath(value).is_absolute():
        raise ValueError("An absolute filesystem path is required.")
    normalized = ntpath.normpath(value)
    drive = PureWindowsPath(normalized).drive
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        return "\\\\?\\" + normalized
    raise ValueError("Only drive-letter and UNC filesystem paths are supported.")


def _plain_windows_path(value: str) -> str:
    if value[:8].casefold() == "\\\\?\\unc\\":
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def filesystem_path(path: Path) -> Path:
    """Use only after validating the normal path's boundary and relative components.

    Python and Git must both be able to access a deep checkout. Keep this private
    I/O spelling out of persisted paths and clipboard/native-shell arguments.
    """

    return Path(_extended_windows_path(str(path))) if os.name == "nt" else path


def display_path(path: Path) -> Path:
    """Return the ordinary path spelling after long-path resolution/validation."""

    return Path(_plain_windows_path(str(path))) if os.name == "nt" else path
