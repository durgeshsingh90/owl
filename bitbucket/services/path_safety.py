"""Shared path-character rules for Git catalog, display, and native actions."""

from __future__ import annotations

import unicodedata

_DISALLOWED_PATH_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})


def has_disallowed_path_characters(value: str) -> bool:
    """Reject controls, surrogates, and Unicode line/paragraph separators."""

    return any(
        unicodedata.category(character) in _DISALLOWED_PATH_CATEGORIES for character in value
    )
