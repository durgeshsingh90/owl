"""Side-by-side text comparison: alignment, word highlights, filtering and paging.

The browser only renders what this returns. Rows are aligned so a line removed from
one side faces a blank on the other; changed lines carry word-level segments.
"""

import hashlib
import re
import threading
from collections import OrderedDict
from difflib import SequenceMatcher

INLINE_DIFF_MAX_LINE_LENGTH = 2000
INLINE_DIFF_MAX_TOTAL_CHARS = 2_000_000
INLINE_DIFF_MAX_CHANGED_LINES = 5000
MAX_TEXT_CHARS = 5_000_000
PAGE_LIMIT = 1000
VIEWS = ("all", "differences", "similarities")


def normalize(line, ignore_whitespace):
    return line.strip() if ignore_whitespace else line


def tokenize(value):
    # Keep whitespace tokens so spacing is preserved when segments are rendered.
    return re.findall(r"\s+|[^\s]+", value or "")


def clear_edge_whitespace(tokens, changed):
    content = [index for index, token in enumerate(tokens) if not token.isspace()]
    if not content:
        changed[:] = [False] * len(changed)
        return
    for index, token in enumerate(tokens):
        if (index < content[0] or index > content[-1]) and token.isspace():
            changed[index] = False


def merge(tokens, changed):
    """Join neighbouring tokens with the same state to keep responses small."""
    segments = []
    for token, flag in zip(tokens, changed):
        if segments and segments[-1][1] == flag:
            segments[-1][0] += token
        else:
            segments.append([token, flag])
    return [{"text": text, "changed": flag} for text, flag in segments]


def word_segments(original, modified, ignore_whitespace):
    a, b = tokenize(original), tokenize(modified)
    a_changed, b_changed = [False] * len(a), [False] * len(b)
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "equal":
            a_changed[i1:i2] = [True] * (i2 - i1)
            b_changed[j1:j2] = [True] * (j2 - j1)
    if ignore_whitespace:
        clear_edge_whitespace(a, a_changed)
        clear_edge_whitespace(b, b_changed)
    return merge(a, a_changed), merge(b, b_changed)


def make_row(original, modified, ignore_whitespace, inline):
    """original/modified are (line_number, text) or None for the blank facing side."""
    left = original[1] if original else ""
    right = modified[1] if modified else ""
    if original and modified:
        same = normalize(left, ignore_whitespace) == normalize(right, ignore_whitespace)
        kind = "same" if same else "changed"
    else:
        kind = "removed" if original else "added"
    row = {
        "kind": kind,
        "originalLine": original[0] if original else None,
        "modifiedLine": modified[0] if modified else None,
        "original": left,
        "modified": right,
    }
    if kind == "changed":
        if inline:
            row["originalSegments"], row["modifiedSegments"] = word_segments(
                left, right, ignore_whitespace
            )
        else:
            row["originalSegments"] = [{"text": left, "changed": True}]
            row["modifiedSegments"] = [{"text": right, "changed": True}]
    return row


def align(rows, original, modified, ignore_whitespace, inline):
    """Pair a replaced block line by line, matching any lines the two sides share."""
    matcher = SequenceMatcher(
        a=[normalize(line, ignore_whitespace) for _, line in original],
        b=[normalize(line, ignore_whitespace) for _, line in modified],
        autojunk=False,
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left, right = original[i1:i2], modified[j1:j2]
        if tag == "delete":
            rows.extend(make_row(line, None, ignore_whitespace, inline) for line in left)
        elif tag == "insert":
            rows.extend(make_row(None, line, ignore_whitespace, inline) for line in right)
        else:
            for index in range(max(len(left), len(right))):
                rows.append(
                    make_row(
                        left[index] if index < len(left) else None,
                        right[index] if index < len(right) else None,
                        ignore_whitespace,
                        inline,
                    )
                )


def build_rows(original_text, modified_text, ignore_whitespace):
    original = list(enumerate(original_text.split("\n"), 1))
    modified = list(enumerate(modified_text.split("\n"), 1))
    matcher = SequenceMatcher(
        a=[normalize(line, ignore_whitespace) for _, line in original],
        b=[normalize(line, ignore_whitespace) for _, line in modified],
        autojunk=False,
    )
    total_chars = len(original_text) + len(modified_text)
    changed_budget = 0
    rows = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left, right = original[i1:i2], modified[j1:j2]
        if tag == "equal":
            rows.extend(
                make_row(a, b, ignore_whitespace, False) for a, b in zip(left, right)
            )
        elif tag == "delete":
            rows.extend(make_row(line, None, ignore_whitespace, False) for line in left)
        elif tag == "insert":
            rows.extend(make_row(None, line, ignore_whitespace, False) for line in right)
        else:
            changed_budget += max(len(left), len(right))
            longest = max(len(text) for _, text in left + right)
            # Word-level highlights are skipped for very large changes to stay responsive.
            inline = (
                total_chars <= INLINE_DIFF_MAX_TOTAL_CHARS
                and changed_budget <= INLINE_DIFF_MAX_CHANGED_LINES
                and longest <= INLINE_DIFF_MAX_LINE_LENGTH
            )
            align(rows, left, right, ignore_whitespace, inline)
    return rows


def side_stats(text):
    return {"lines": text.count("\n") + 1, "chars": len(text)}


def summarize(rows, original_text, modified_text):
    counts = {"same": 0, "changed": 0, "removed": 0, "added": 0}
    for row in rows:
        counts[row["kind"]] += 1
    return {
        "rows": len(rows),
        "similarities": counts["same"],
        "differences": counts["changed"] + counts["removed"] + counts["added"],
        "changed": counts["changed"],
        "removed": counts["removed"],
        "added": counts["added"],
        "identical": counts["same"] == len(rows),
        "original": side_stats(original_text),
        "modified": side_stats(modified_text),
    }


class Comparison:
    def __init__(self, original_text, modified_text, ignore_whitespace):
        self.rows = build_rows(original_text, modified_text, ignore_whitespace)
        self.summary = summarize(self.rows, original_text, modified_text)
        self.views = {}

    def view(self, name):
        """Row indexes shown in a view, and where each block of differences starts."""
        if name not in self.views:
            if name == "differences":
                indexes = [i for i, row in enumerate(self.rows) if row["kind"] != "same"]
            elif name == "similarities":
                indexes = [i for i, row in enumerate(self.rows) if row["kind"] == "same"]
            else:
                indexes = list(range(len(self.rows)))
            hunks = []
            previous = None
            for position, index in enumerate(indexes):
                changed = self.rows[index]["kind"] != "same"
                # A new block starts after an unchanged row or a gap in the source rows.
                if changed and (
                    previous is None
                    or self.rows[previous]["kind"] == "same"
                    or index != previous + 1
                ):
                    hunks.append(position)
                previous = index
            self.views[name] = (indexes, hunks)
        return self.views[name]

    def page(self, name, offset, limit):
        indexes, hunks = self.view(name)
        offset = max(0, min(offset, max(0, len(indexes) - 1)))
        rows = [
            {"position": position, **self.rows[index]}
            for position, index in enumerate(
                indexes[offset : offset + limit], start=offset
            )
        ]
        return {
            "view": name,
            "total": len(indexes),
            "offset": offset,
            "limit": limit,
            "rows": rows,
            "hunks": hunks,
        }


class Cache:
    """Recent comparisons, so paging and switching views never recompute the diff."""

    def __init__(self, size=8):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, original_text, modified_text, ignore_whitespace):
        key = hashlib.sha256(
            b"\0".join(
                [
                    original_text.encode(),
                    modified_text.encode(),
                    b"1" if ignore_whitespace else b"0",
                ]
            )
        ).hexdigest()
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return key, self.items[key]
        comparison = Comparison(original_text, modified_text, ignore_whitespace)
        with self.lock:
            self.items[key] = comparison
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return key, comparison


    def find(self, key):
        with self.lock:
            comparison = self.items.get(key)
            if comparison is not None:
                self.items.move_to_end(key)
            return comparison


cache = Cache()


def compare(original_text, modified_text, ignore_whitespace=True, view="all", offset=0, limit=PAGE_LIMIT):
    if view not in VIEWS:
        raise ValueError("View must be all, differences or similarities.")
    if max(len(original_text), len(modified_text)) > MAX_TEXT_CHARS:
        raise ValueError("Each text can hold up to 5,000,000 characters.")
    key, comparison = cache.get(original_text, modified_text, ignore_whitespace)
    return {
        "key": key,
        "summary": comparison.summary,
        **comparison.page(view, offset, max(1, min(limit, PAGE_LIMIT))),
    }


def page(key, view="all", offset=0, limit=PAGE_LIMIT):
    """Another page or view of a recent comparison, or None once it has been evicted."""
    if view not in VIEWS:
        raise ValueError("View must be all, differences or similarities.")
    comparison = cache.find(key)
    if comparison is None:
        return None
    return {
        "key": key,
        "summary": comparison.summary,
        **comparison.page(view, offset, max(1, min(limit, PAGE_LIMIT))),
    }
