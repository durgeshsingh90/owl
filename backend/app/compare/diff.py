"""Side-by-side text comparison: alignment, colouring, filtering and block copying.

The browser only renders what this returns. Rows are aligned so a line removed from
one side faces an empty filler line on the other; changed lines carry word ranges.
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
VIEWS = ("all", "differences", "similarities")


def normalize(line, ignore_whitespace):
    return line.strip() if ignore_whitespace else line


def tokenize(value):
    # Words, spaces and each punctuation mark are separate tokens, so only what changed
    # is highlighted: the comma added after "bookmarks", or the 2 in 1.4.2 -> 1.5.2.
    # Spacing is kept so segments render exactly.
    return re.findall(r"\s+|\w+|[^\w\s]", value or "")


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
            for a, b in pair_lines(left, right, ignore_whitespace):
                rows.append(make_row(a, b, ignore_whitespace, inline))


# A replaced line pairs with a line on the other side at least this similar, so a line
# where only a few words changed shows those words rather than lining up with whatever
# happens to sit at the same position.
SIMILAR = 0.5
SEARCH_AHEAD = 20


def similarity(left, right):
    matcher = SequenceMatcher(a=left, b=right, autojunk=False)
    return matcher.ratio() if matcher.quick_ratio() >= SIMILAR else 0


def pair_lines(left, right, ignore_whitespace):
    """Pair replaced lines by similarity, keeping their order; the rest stand alone."""
    pairs, start = [], 0
    for line in left:
        text = normalize(line[1], ignore_whitespace)
        best, best_score = None, SIMILAR
        for index in range(start, min(len(right), start + SEARCH_AHEAD)):
            score = similarity(text, normalize(right[index][1], ignore_whitespace))
            if score >= best_score and (best is None or score > best_score):
                best, best_score = index, score
        if best is None:
            pairs.append((line, None))
            continue
        pairs.extend((None, added) for added in right[start:best])
        pairs.append((line, right[best]))
        start = best + 1
    pairs.extend((None, added) for added in right[start:])
    if not any(a and b for a, b in pairs):
        # Nothing alike: pair by position, as two rewritten lines facing each other.
        pairs = [
            (left[index] if index < len(left) else None, right[index] if index < len(right) else None)
            for index in range(max(len(left), len(right)))
        ]
    return pairs


def with_blank_lines(rows, original, modified):
    """Put back the blank lines left out of the comparison, as unchanged rows: blank
    lines face each other where both sides have them, otherwise an empty filler."""
    from collections import deque

    blank = {
        "original": deque(number for number, text in original if not text.strip()),
        "modified": deque(number for number, text in modified if not text.strip()),
    }
    texts = {"original": dict(original), "modified": dict(modified)}
    result = []

    def flush(limits):
        while any(blank[side] and blank[side][0] < limits[side] for side in blank):
            row = {"kind": "same", "originalLine": None, "modifiedLine": None, "original": "", "modified": ""}
            for side in blank:
                if blank[side] and blank[side][0] < limits[side]:
                    number = blank[side].popleft()
                    row[side + "Line"] = number
                    row[side] = texts[side][number]
            result.append(row)

    for row in rows:
        # A blank line goes before the first row that comes after it on its side.
        flush({
            "original": row["originalLine"] or float("-inf"),
            "modified": row["modifiedLine"] or float("-inf"),
        })
        result.append(row)
    flush({"original": float("inf"), "modified": float("inf")})
    return result


def build_rows(original_text, modified_text, ignore_whitespace, ignore_blank=False):
    every_original = list(enumerate(original_text.split("\n"), 1))
    every_modified = list(enumerate(modified_text.split("\n"), 1))
    # Ignoring empty lines: they take no part in lining the texts up.
    original = [line for line in every_original if line[1].strip()] if ignore_blank else every_original
    modified = [line for line in every_modified if line[1].strip()] if ignore_blank else every_modified
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
    if ignore_blank:
        rows = with_blank_lines(rows, every_original, every_modified)
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


def changed_ranges(segments):
    """Column ranges [start, end) of the changed words on one side of a line."""
    ranges, column = [], 0
    for segment in segments:
        end = column + len(segment["text"])
        if segment["changed"] and end > column:
            ranges.append([column, end])
        column = end
    return ranges


class Comparison:
    def __init__(self, original_text, modified_text, ignore_whitespace, ignore_blank=False):
        self.original_text = original_text
        self.modified_text = modified_text
        self.ignore_whitespace = ignore_whitespace
        self.ignore_blank = ignore_blank
        self.rows = build_rows(original_text, modified_text, ignore_whitespace, ignore_blank)
        self.summary = summarize(self.rows, original_text, modified_text)
        self.views = {}
        self.layouts = {}

    def layout(self, name):
        """Both editors' text, already aligned, with everything needed to colour it.

        Line i of each side is row i: a line missing from one side is an empty filler
        line there. kinds has one letter per line: s(ame), c(hanged), r(emoved: only in
        original) or a(dded: only in modified).
        """
        if name not in self.layouts:
            indexes, hunks = self.view(name)
            letters = {"same": "s", "changed": "c", "removed": "r", "added": "a"}
            sides = {
                side: {"lines": [], "numbers": [], "words": {}}
                for side in ("original", "modified")
            }
            kinds = []
            for line, index in enumerate(indexes):
                row = self.rows[index]
                kinds.append(letters[row["kind"]])
                for side in ("original", "modified"):
                    target = sides[side]
                    number = row[side + "Line"]
                    target["lines"].append(row[side] if number else "")
                    target["numbers"].append(number)
                    segments = row.get(side + "Segments")
                    if segments and len(segments) > 1:
                        target["words"][line] = changed_ranges(segments)
            blocks = []
            for start in hunks:
                end = start
                while (
                    end < len(indexes)
                    and kinds[end] != "s"
                    and (end == start or indexes[end] == indexes[end - 1] + 1)
                ):
                    end += 1
                blocks.append([start, end])
            self.layouts[name] = {
                "view": name,
                "lines": len(indexes),
                "kinds": "".join(kinds),
                "blocks": blocks,
                "original": {
                    "text": "\n".join(sides["original"]["lines"]),
                    "numbers": sides["original"]["numbers"],
                    "words": sides["original"]["words"],
                },
                "modified": {
                    "text": "\n".join(sides["modified"]["lines"]),
                    "numbers": sides["modified"]["numbers"],
                    "words": sides["modified"]["words"],
                },
            }
        return self.layouts[name]

    def copy_block(self, block, direction):
        """Texts after copying one block of differences to the other side."""
        blocks = self.layout("all")["blocks"]
        if not 0 <= block < len(blocks):
            raise ValueError("That difference no longer exists. Compare again.")
        start, end = blocks[block]
        source, target = (
            ("original", "modified") if direction == "right" else ("modified", "original")
        )
        lines = []
        for index, row in enumerate(self.rows):
            side = source if start <= index < end else target
            if row[side + "Line"]:
                lines.append(row[side])
        text = "\n".join(lines)
        return (
            (self.original_text, text)
            if direction == "right"
            else (text, self.modified_text)
        )

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


class Cache:
    """Recent comparisons, so switching views or copying blocks never recomputes the diff."""

    def __init__(self, size=8):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, original_text, modified_text, ignore_whitespace, ignore_blank=False):
        key = hashlib.sha256(
            b"\0".join(
                [
                    original_text.encode(),
                    modified_text.encode(),
                    b"1" if ignore_whitespace else b"0",
                    b"1" if ignore_blank else b"0",
                ]
            )
        ).hexdigest()
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return key, self.items[key]
        comparison = Comparison(original_text, modified_text, ignore_whitespace, ignore_blank)
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


def check(original_text, modified_text, view):
    if view not in VIEWS:
        raise ValueError("View must be all, differences or similarities.")
    if max(len(original_text), len(modified_text)) > MAX_TEXT_CHARS:
        raise ValueError("Each text can hold up to 5,000,000 characters.")


def layout(original_text, modified_text, ignore_whitespace=True, view="all", ignore_blank=False):
    check(original_text, modified_text, view)
    key, comparison = cache.get(original_text, modified_text, ignore_whitespace, ignore_blank)
    return {"key": key, "summary": comparison.summary, **comparison.layout(view)}


def layout_by_key(key, view="all"):
    if view not in VIEWS:
        raise ValueError("View must be all, differences or similarities.")
    comparison = cache.find(key)
    if comparison is None:
        return None
    return {"key": key, "summary": comparison.summary, **comparison.layout(view)}


def copy_block(key, block, direction):
    comparison = cache.find(key)
    if comparison is None:
        return None
    original_text, modified_text = comparison.copy_block(block, direction)
    result = layout(
        original_text, modified_text, comparison.ignore_whitespace, "all", comparison.ignore_blank
    )
    return {"originalText": original_text, "modifiedText": modified_text, **result}
