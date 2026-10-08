"""Server mode: index a big JSON or JSON Lines file on disk and serve parts of it.

The browser cannot parse files over about 50 MB. Here the file stays on disk and only
the byte offset and length of each element of its big arrays are kept: a top-level
array's elements, the elements of arrays under top-level keys, or JSON Lines' lines.
Any element is read again by seeking to it and parsing just that slice.

Scanning decodes the bytes as Latin-1: every byte becomes one character, so a position
in the text is the byte offset in the file, and JSON's structure is all ASCII, so the
UTF-8 inside strings passes through unharmed. Real text is decoded per element.

Nothing is persisted: after a restart the files are deleted and must be opened again.
"""

import functools
import json
import os
import re
import threading
import time
import uuid
from array import array
from collections import OrderedDict

from app.jsonviz import storage
from app.jsonviz.filters import (
    MISSING, SortValue, cell_text, column_keys, compare_sort_values, field_of, filter_test,
    js_string, matcher, preview, type_of,
)

WINDOW = 4 * 1024 * 1024  # Bytes read at a time while indexing.
VALUE_LIMIT = 50 * 1024 * 1024  # A non-array value decoded whole, and /value.
ELEMENT_LIMIT = 512 * 1024 * 1024  # One array element or line.
NODE_VALUE_LIMIT = 2 * 1024 * 1024  # /node includes the value up to this size.
DETECT_BYTES = 16 * 1024 * 1024  # Looking for JSON Lines' first line.
IDLE_SECONDS = 6 * 60 * 60
MAX_MATCHES = 100_000
BAD_LINES = 500
VIEWS = 8
SEARCHES = 4
WHITESPACE = " \t\r\n"
_decoder = json.JSONDecoder()
_scanstring = json.decoder.scanstring
_documents = {}
_lock = threading.Lock()
_cleaned = set()


class NotFound(LookupError):
    """A path, document or search that does not exist (HTTP 404)."""


class TooLarge(ValueError):
    """A value too big to send (HTTP 413)."""


class Region:
    """The elements of one indexed array: byte offsets and lengths."""

    def __init__(self):
        self.offsets = array("q")
        self.lengths = array("I" if array("I").itemsize >= 4 else "L")

    def __len__(self):
        return len(self.offsets)

    def add(self, offset, length):
        self.offsets.append(offset)
        self.lengths.append(length)

    def bytes(self):
        return sum(self.lengths) + len(self) + 1


class Entry:
    """One top-level key of an object root: an indexed array, or a value read whole."""

    def __init__(self, key, offset, length, region=None, value=MISSING):
        self.key, self.offset, self.length, self.region = key, offset, length, region
        if region is not None:
            self.type, self.count, self.preview = "array", len(region), f"[{len(region)} item{'' if len(region) == 1 else 's'}]"
        else:
            self.type, self.count, self.preview = type_of(value), _count(value), preview(value)

    def summary(self):
        item = {"key": self.key, "type": self.type, "preview": self.preview}
        if self.count is not None:
            item["count"] = self.count
        return item


def _count(value):
    return len(value) if isinstance(value, (dict, list)) else None


class Document:
    def __init__(self, document_id, name, path, size):
        self.id, self.name, self.path, self.size = document_id, name, path, size
        self.state, self.error, self.format = "indexing", "", None
        self.progress = 0
        self.region = None  # Array root or JSON Lines.
        self.entries = None  # Object root: key → Entry, in document order.
        self.value_span = None  # Any other root: (offset, length).
        self.bad_lines, self.error_count = [], 0
        self.used = time.time()
        self.cancelled = threading.Event()
        self.views = OrderedDict()
        self.searches = OrderedDict()
        self.lock = threading.Lock()

    def status(self):
        result = {
            "id": self.id, "name": self.name, "size": self.size, "state": self.state,
            "progress": {"bytes": self.progress, "total": self.size},
            "error": self.error, "format": self.format,
        }
        if self.state == "ready":
            result["root"] = self.root()
        if self.format == "jsonl":
            result["errors"] = self.bad_lines
            result["error_count"] = self.error_count
        return result

    def root(self):
        if self.region is not None:
            return {"type": "array", "count": len(self.region)}
        if self.entries is not None:
            return {"type": "object", "count": len(self.entries), "keys": [entry.summary() for entry in self.entries.values()]}
        value = self.load(*self.value_span)
        return {"type": type_of(value), "preview": preview(value)}

    def read(self, offset, length, file=None):
        if file is None:
            with open(self.path, "rb") as file:
                file.seek(offset)
                return file.read(length)
        file.seek(offset)
        return file.read(length)

    def load(self, offset, length, file=None):
        # Invalid UTF-8 shows as U+FFFD, as it would in the browser.
        return json.loads(self.read(offset, length, file).decode("utf-8", "replace"))


# Indexing


class Scanner:
    """A sliding window over the file, as Latin-1 text; positions are byte offsets."""

    def __init__(self, file, start, size, window, report):
        self.file, self.size, self.window, self.report = file, size, window, report
        file.seek(start)
        self.base, self.buffer, self.pos, self.eof = start, "", 0, False

    def offset(self):
        return self.base + self.pos

    def fill(self, at_least=0):
        """Read more, dropping the text before pos. Returns False at the end of the file."""
        if self.eof:
            return False
        wanted = max(self.window, at_least)
        chunk = self.file.read(wanted)
        self.buffer = self.buffer[self.pos:] + chunk.decode("latin-1")
        self.base += self.pos
        self.pos = 0
        if len(chunk) < wanted:
            self.eof = True
        self.report(self.base)
        return bool(chunk)

    def peek(self):
        """The next character that is not whitespace, or "" at the end."""
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos] in WHITESPACE:
                self.pos += 1
            if self.pos < len(self.buffer) or not self.fill():
                return self.buffer[self.pos] if self.pos < len(self.buffer) else ""

    def expect(self, characters, what):
        char = self.peek()
        if not char or char not in characters:
            found = f"'{char}'" if char else "the end of the file"
            raise ValueError(f"Expecting {what} at byte {self.offset()}, found {found}.")
        self.pos += 1
        return char

    def _grow(self, limit, what):
        held = len(self.buffer) - self.pos
        if held >= limit:
            raise ValueError(f"{what} at byte {self.offset()} is larger than {limit // (1024 * 1024)} MB.")
        return self.fill(min(held, limit))

    def value(self, limit, what="A value"):
        """Decode the value at pos: (value, offset, length). The window grows until the
        whole value is inside it; a number at the window's edge may continue past it."""
        while True:
            try:
                value, end = _decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError as error:
                truncated = (
                    error.pos >= len(self.buffer) - 32
                    or error.msg.startswith(("Unterminated string", "Invalid \\uXXXX"))
                )
                if not self.eof and truncated:
                    # Reading more moves the window: decode again from the new pos.
                    self._grow(limit, what)
                    continue
                at = self.base + error.pos
                raise ValueError(f"Invalid JSON at byte {at}: {error.msg}.") from None
            except RecursionError:
                raise ValueError(f"The JSON at byte {self.offset()} is nested too deeply.") from None
            if end >= len(self.buffer) - 32 and not self.eof:
                self._grow(limit, what)
                continue
            start = self.offset()
            self.pos = end
            return value, start, end - (start - self.base)

    def string(self):
        """A key at pos (its opening quote): (offset, length) of the quoted text."""
        while True:
            try:
                _, end = _scanstring(self.buffer, self.pos + 1)
            except json.JSONDecodeError as error:
                if not self.eof:
                    self._grow(VALUE_LIMIT, "A key")
                    continue
                raise ValueError(f"Invalid JSON at byte {self.base + error.pos}: {error.msg}.") from None
            start = self.offset()
            self.pos = end
            return start, end - (start - self.base)

    def text(self, offset, length):
        """Bytes of a span that is still in the window."""
        start = offset - self.base
        return self.buffer[start:start + length].encode("latin-1")


def _array(scanner, region, document):
    scanner.expect("[", "'['")
    if scanner.peek() == "]":
        scanner.pos += 1
        return
    while True:
        if document.cancelled.is_set():
            raise InterruptedError
        scanner.peek()
        _, offset, length = scanner.value(ELEMENT_LIMIT, "An array element")
        region.add(offset, length)
        if scanner.expect(",]", "',' or ']'") == "]":
            return


def _index_json(document, file, start, window):
    scanner = Scanner(file, start, document.size, window, lambda at: setattr(document, "progress", at))
    first = scanner.peek()
    if not first:
        raise ValueError("The file is empty.")
    if first == "[":
        region = Region()
        _array(scanner, region, document)
        document.region = region
    elif first == "{":
        entries = {}
        scanner.pos += 1
        if scanner.peek() == "}":
            scanner.pos += 1
        else:
            while True:
                if scanner.peek() != '"':
                    scanner.expect('"', "a key")
                key_offset, key_length = scanner.string()
                key = json.loads(scanner.text(key_offset, key_length).decode("utf-8", "replace"))
                scanner.expect(":", "':'")
                if scanner.peek() == "[":
                    offset, region = scanner.offset(), Region()
                    _array(scanner, region, document)
                    entry = Entry(key, offset, scanner.offset() - offset, region=region)
                else:
                    _, offset, length = scanner.value(VALUE_LIMIT, f"The value of {key}")
                    value = json.loads(scanner.text(offset, length).decode("utf-8", "replace"))
                    entry = Entry(key, offset, length, value=value)
                # A repeated key keeps its first place and its last value, like JSON.parse.
                entries[key] = entry
                if scanner.expect(",}", "',' or '}'") == "}":
                    break
        document.entries = entries
    else:
        _, offset, length = scanner.value(VALUE_LIMIT, "The value")
        document.value_span = (offset, length)
    if scanner.peek():
        raise ValueError(f"Unexpected text after the JSON value at byte {scanner.offset()}.")


def _index_lines(document, file, start):
    region = Region()
    number, offset, carry = 0, start, b""
    file.seek(start)

    def line(data, at):
        nonlocal number
        number += 1
        stripped = data.strip()
        if not stripped:
            return
        lead = len(data) - len(data.lstrip())
        try:
            json.loads(stripped)
        except (ValueError, UnicodeDecodeError) as error:
            document.error_count += 1
            if len(document.bad_lines) < BAD_LINES:
                message = f"{error.msg} at column {error.colno}" if isinstance(error, json.JSONDecodeError) else "Not UTF-8 text"
                document.bad_lines.append({
                    "line": number, "error": message,
                    "text": stripped[:120].decode("utf-8", "replace"),
                })
            return
        region.add(at + lead, len(stripped))

    while True:
        if document.cancelled.is_set():
            raise InterruptedError
        chunk = file.read(WINDOW)
        if not chunk:
            break
        data = carry + chunk
        position = 0
        while (end := data.find(b"\n", position)) >= 0:
            line(data[position:end], offset + position)
            position = end + 1
        offset += position
        carry = data[position:]
        if len(carry) > ELEMENT_LIMIT:
            raise ValueError(f"Line {number + 1} is longer than {ELEMENT_LIMIT // (1024 * 1024)} MB.")
        document.progress = offset
    if carry:
        line(carry, offset)
    document.region = region


def detect(name, file, start):
    """"jsonl" when the name says so, or the first non-empty line is a whole JSON value
    and another line follows; else "json"."""
    if name.lower().endswith((".jsonl", ".ndjson")):
        return "jsonl"
    file.seek(start)
    head = file.read(DETECT_BYTES)
    lines = head.split(b"\n")
    # The last piece may be cut off when the head did not reach the end of the file.
    whole = len(lines) - 1 if len(head) == DETECT_BYTES else len(lines)
    for number, line in enumerate(lines):
        if not line.strip():
            continue
        if number >= whole:
            return "json"
        try:
            json.loads(line)
        except ValueError:
            return "json"
        return "jsonl" if any(rest.strip() for rest in lines[number + 1:]) else "json"
    return "json"


def index(document, window=WINDOW):
    """Index the document's file; sets state ready or error. Runs in its own thread."""
    try:
        with open(document.path, "rb") as file:
            start = 3 if file.read(3) == b"\xef\xbb\xbf" else 0
            document.format = detect(document.name, file, start)
            if document.format == "jsonl":
                _index_lines(document, file, start)
            else:
                _index_json(document, file, start, window)
        document.progress = document.size
        document.state = "ready"
    except InterruptedError:
        document.state, document.error = "error", "Closed."
    except (ValueError, OSError, MemoryError) as error:
        document.state, document.error = "error", str(error) or type(error).__name__
    except Exception as error:  # noqa: BLE001 - the status endpoint must show something
        document.state, document.error = "error", f"Indexing failed: {type(error).__name__}."


# Registry


def _folder():
    folder = storage.subfolder("documents")
    key = str(folder)
    if key not in _cleaned:
        # Files from before a restart: their indexes are gone.
        _cleaned.add(key)
        with _lock:
            known = {str(document.path) for document in _documents.values()}
        for path in folder.iterdir():
            if str(path) not in known:
                path.unlink(missing_ok=True)
    return folder


def upload_folder():
    return _folder()


def add(name, temp_path, size, *, window=WINDOW, background=True):
    """Register an uploaded file and start indexing it."""
    sweep()
    document_id = uuid.uuid4().hex
    path = _folder() / f"{document_id}.data"
    os.replace(temp_path, path)
    document = Document(document_id, name, path, size)
    with _lock:
        _documents[document_id] = document
    if background:
        threading.Thread(target=index, args=(document, window), daemon=True, name=f"jsonviz-index-{document_id[:8]}").start()
    else:
        index(document, window)
    return document


def get(document_id, ready=True):
    sweep()
    with _lock:
        document = _documents.get(document_id)
    if document is None:
        raise NotFound("This document is no longer open on the server. Open the file again.")
    document.used = time.time()
    if ready and document.state != "ready":
        raise ValueError(document.error or "The document is still being indexed.")
    return document


def drop(document_id):
    with _lock:
        document = _documents.pop(document_id, None)
    if document is None:
        return False
    document.cancelled.set()
    for search in list(document.searches.values()):
        search.cancelled.set()
    try:
        os.unlink(document.path)
    except FileNotFoundError:
        pass
    except OSError:
        pass  # Windows: still open by the indexer; the next start removes it.
    return True


def drop_all():
    with _lock:
        ids = list(_documents)
    for document_id in ids:
        drop(document_id)
    for path in storage.subfolder("documents").iterdir():
        try:
            path.unlink()
        except OSError:
            pass


def sweep(now=None):
    """Forget documents nobody looked at for six hours."""
    now = time.time() if now is None else now
    with _lock:
        idle = [key for key, document in _documents.items() if now - document.used > IDLE_SECONDS]
    for document_id in idle:
        drop(document_id)


# Paths


class Indexed:
    """An indexed array node."""

    def __init__(self, region):
        self.region = region


class RootObject:
    pass


def parse_path(text):
    if text is None or text == "":
        return []
    try:
        path = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        raise ValueError("path must be a JSON array of keys and indexes.") from None
    if not isinstance(path, list) or not all(
        isinstance(part, str) or (isinstance(part, int) and not isinstance(part, bool)) for part in path
    ):
        raise ValueError("path must be a JSON array of keys and indexes.")
    return path


def _element(document, region, index, file=None):
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(region):
        raise NotFound(f"No element {index} here.")
    return document.load(region.offsets[index], region.lengths[index], file)


def locate(document, path):
    """What path names: Indexed, RootObject, or the value itself (a Python value)."""
    rest = list(path)
    if document.region is not None:
        if not rest:
            return Indexed(document.region)
        value = _element(document, document.region, rest.pop(0))
    elif document.entries is not None:
        if not rest:
            return RootObject()
        key = rest.pop(0)
        entry = document.entries.get(key) if isinstance(key, str) else None
        if entry is None:
            raise NotFound(f"No key {key} here.")
        if entry.region is not None:
            if not rest:
                return Indexed(entry.region)
            value = _element(document, entry.region, rest.pop(0))
        else:
            value = document.load(entry.offset, entry.length)
    else:
        value = document.load(*document.value_span)
    for part in rest:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            raise NotFound(f"Nothing at {part!r} in this path.")
    return value


def _describe(value, key=MISSING):
    item = {} if key is MISSING else {"key": key}
    item.update(type=type_of(value), preview=preview(value))
    if isinstance(value, (dict, list)):
        item["count"] = len(value)
    return item


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _whole(document, node):
    """The bytes of an indexed array or the object root as JSON, within VALUE_LIMIT."""
    if isinstance(node, Indexed):
        if node.region.bytes() > VALUE_LIMIT:
            raise TooLarge("This array is larger than 50 MB. Open its elements one at a time.")
        with open(document.path, "rb") as file:
            parts = [document.read(offset, length, file) for offset, length in zip(node.region.offsets, node.region.lengths)]
        return b"[" + b",".join(parts) + b"]"
    if document.size > VALUE_LIMIT:
        raise TooLarge("This document is larger than 50 MB. Open its parts one at a time.")
    with open(document.path, "rb") as file:
        data = file.read()
    return data[3:] if data.startswith(b"\xef\xbb\xbf") else data


def node(document, path):
    target = locate(document, path)
    if isinstance(target, Indexed):
        count = len(target.region)
        result = {"path": path, "type": "array", "count": count, "preview": f"[{count} item{'' if count == 1 else 's'}]"}
        small = target.region.bytes() <= NODE_VALUE_LIMIT
    elif isinstance(target, RootObject):
        count = len(document.entries)
        result = {"path": path, "type": "object", "count": count, "preview": f"{{{count} key{'' if count == 1 else 's'}}}"}
        small = document.size <= NODE_VALUE_LIMIT
    else:
        result = {"path": path, **_describe(target)}
        text = _serialize(target)
        if len(text.encode("utf-8")) <= NODE_VALUE_LIMIT:
            result["value"] = target
        else:
            result["truncated"] = True
        return result
    if small:
        result["value"] = json.loads(_whole(document, target).decode("utf-8", "replace"))
    else:
        result["truncated"] = True
    return result


def children(document, path, offset=0, limit=500):
    target = locate(document, path)
    if isinstance(target, Indexed):
        region = target.region
        indexes = range(offset, min(len(region), offset + limit))
        with open(document.path, "rb") as file:
            items = [(index, _element(document, region, index, file)) for index in indexes]
        total = len(region)
    elif isinstance(target, RootObject):
        entries = list(document.entries.values())
        return {"total": len(entries), "children": [_child_entry(document, entry) for entry in entries[offset:offset + limit]]}
    elif isinstance(target, list):
        total, items = len(target), list(enumerate(target))[offset:offset + limit]
    elif isinstance(target, dict):
        total, items = len(target), list(target.items())[offset:offset + limit]
    else:
        raise ValueError("This value has no children.")
    return {"total": total, "children": [_child(key, value) for key, value in items]}


def _child(key, value):
    item = _describe(value, key)
    if not isinstance(value, (dict, list)):
        item["value"] = value
    return item


def _child_entry(document, entry):
    item = entry.summary()
    if entry.region is None and entry.type not in ("object", "array"):
        item["value"] = document.load(entry.offset, entry.length)
    return item


def value(document, path):
    """The full JSON value as bytes; TooLarge above 50 MB."""
    target = locate(document, path)
    if isinstance(target, (Indexed, RootObject)):
        return _whole(document, target)
    data = _serialize(target).encode("utf-8")
    if len(data) > VALUE_LIMIT:
        raise TooLarge("This value is larger than 50 MB. Open its parts one at a time.")
    return data


# Tables


class Rows:
    """The elements of an array node, read one at a time when they are on disk."""

    def __init__(self, document, path):
        target = locate(document, path)
        if isinstance(target, Indexed):
            self.region, self.items = target.region, None
        elif isinstance(target, list):
            self.region, self.items = None, target
        else:
            raise ValueError("Rows need an array. Choose an array in the document.")
        self.document, self.path = document, list(path)

    def __len__(self):
        return len(self.region) if self.region is not None else len(self.items)

    def get(self, index, file=None):
        if self.items is not None:
            return self.items[index]
        return self.document.load(self.region.offsets[index], self.region.lengths[index], file)

    def scan(self, flatten=None):
        """(path, value) of every row; with flatten, rows are value[flatten][j]."""
        with open(self.document.path, "rb") as file:
            for index in range(len(self)):
                if self.document.cancelled.is_set():
                    raise ValueError("The document was closed.")
                item = self.get(index, file)
                if flatten is None:
                    yield index, -1, item
                elif isinstance(item, dict) and isinstance(item.get(flatten), list):
                    for inner, sub in enumerate(item[flatten]):
                        yield index, inner, sub

    def row(self, index, inner, flatten, file=None):
        item = self.get(index, file)
        if inner < 0:
            return self.path + [index], item
        return self.path + [index, flatten, inner], item[flatten][inner]


def parse_filters(text):
    if not text:
        return {}
    try:
        filters = json.loads(text)
    except ValueError:
        raise ValueError("filters must be a JSON object of column → filter text.") from None
    if not isinstance(filters, dict) or not all(isinstance(value, str) for value in filters.values()):
        raise ValueError("filters must be a JSON object of column → filter text.")
    return {key: value for key, value in filters.items() if value.strip()}


def parse_sort(text):
    if not text:
        return []
    try:
        sort = json.loads(text)
    except ValueError:
        raise ValueError('sort must be a JSON list such as [{"key": "Name", "dir": 1}].') from None
    if not isinstance(sort, list) or not all(
        isinstance(item, dict) and isinstance(item.get("key"), str) and item.get("dir", 1) in (1, -1)
        and not isinstance(item.get("dir", 1), bool)
        for item in sort
    ):
        raise ValueError('sort must be a JSON list such as [{"key": "Name", "dir": 1}].')
    return [(item["key"], item.get("dir", 1)) for item in sort]


def _view(document, source, flatten, filters, sort):
    """Row references (element, inner index) after filters and sort, cached per query."""
    key = json.dumps([source.path, flatten, filters, sort], sort_keys=True)
    with document.lock:
        if key in document.views:
            document.views.move_to_end(key)
            return document.views[key]
    tests = [(name, filter_test(text)) for name, text in filters.items()]
    outer, inner, sort_values = array("q"), array("q"), []
    unfiltered = 0
    for index, sub, item in source.scan(flatten):
        unfiltered += 1
        path = source.path + ([index] if sub < 0 else [index, flatten, sub])
        if all(test(field_of(path, item, name)) for name, test in tests):
            outer.append(index)
            inner.append(sub)
            if sort:
                sort_values.append([SortValue(field_of(path, item, name)) for name, _ in sort])
    order = list(range(len(outer)))
    if sort:
        directions = [direction for _, direction in sort]

        def compare(a, b):
            for position, direction in enumerate(directions):
                result = compare_sort_values(sort_values[a][position], sort_values[b][position])
                if result:
                    return direction * result
            return a - b
        order.sort(key=functools.cmp_to_key(compare))
    view = {"outer": array("q", (outer[i] for i in order)), "inner": array("q", (inner[i] for i in order)), "unfiltered": unfiltered}
    with document.lock:
        document.views[key] = view
        while len(document.views) > VIEWS:
            document.views.popitem(last=False)
    return view


def rows(document, path, flatten=None, offset=0, limit=500, filters=None, sort=None, columns=False):
    source = Rows(document, path)
    filters, sort = filters or {}, sort or []
    with open(document.path, "rb") as file:
        if flatten or filters or sort:
            view = _view(document, source, flatten or None, filters, sort)
            total, unfiltered = len(view["outer"]), view["unfiltered"]

            def page(start, count):
                return [source.row(view["outer"][i], view["inner"][i], flatten, file)
                        for i in range(start, min(total, start + count))]
        else:
            total = unfiltered = len(source)

            def page(start, count):
                return [source.row(i, -1, None, file) for i in range(start, min(total, start + count))]
        result = {"total": total, "unfiltered": unfiltered,
                  "rows": [{"path": row_path, "value": item} for row_path, item in page(offset, limit)]}
        if columns:
            result["columns"] = column_keys([item for _, item in page(0, 500)])
    return result


def summary(document, path, fields, flatten=None, filters=None):
    """Top 20 values (by cell text) of each field over every row."""
    source = Rows(document, path)
    tests = [(name, filter_test(text)) for name, text in (filters or {}).items()]
    counts = {field: {} for field in fields}
    total = 0
    for index, sub, item in source.scan(flatten or None):
        row_path = source.path + ([index] if sub < 0 else [index, flatten, sub])
        if not all(test(field_of(row_path, item, name)) for name, test in tests):
            continue
        total += 1
        for field in fields:
            text = cell_text(field_of(row_path, item, field))
            counts[field][text] = counts[field].get(text, 0) + 1
    breakdowns = {}
    for field, values in counts.items():
        ranked = sorted(enumerate(values.items()), key=lambda item: (-item[1][1], item[0]))
        breakdowns[field] = [[text, count] for _, (text, count) in ranked[:20]]
    return {"total": total, "breakdowns": breakdowns}


# Search


class Search:
    def __init__(self, search_id, document):
        self.id, self.total = search_id, document.size
        self.matches, self.done, self.scanned, self.error, self.capped = [], False, 0, "", False
        self.cancelled = threading.Event()

    def page(self, after=0):
        return {
            "id": self.id, "matches": self.matches[after:after + 5000], "done": self.done,
            "scanned_bytes": self.scanned, "total_bytes": self.total, "count": len(self.matches),
            "capped": self.capped, "error": self.error,
        }


class _Stop(Exception):
    pass


# Numbers whose text in the file is exactly how JavaScript prints them: only then can an
# element whose raw text does not match be skipped without parsing it.
_TOKENS = re.compile(rb'"[^"]*"|(-?[0-9][0-9.eE+-]*)')
_PLAIN_NUMBER = re.compile(rb"-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{0,5}[1-9])?")


def _raw_text_is_exact(raw):
    if b"\\" in raw:
        return False  # Escapes: the text in the file differs from the decoded text.
    for match in _TOKENS.finditer(raw):
        number = match.group(1)
        if number is not None and (
            not _PLAIN_NUMBER.fullmatch(number) or number == b"-0" or len(number.strip(b"-.")) > 16
        ):
            return False
    return True


def _walk(value, path, key, has_parent, test, keys, values, add):
    """search.js searcher over one value: depth first, keys before their values."""
    stack = [(value, key, has_parent, None)]
    while stack:
        entry = stack.pop()
        item, name, parented, _ = entry
        if keys and isinstance(name, str) and test(name):
            add(_entry_path(entry, path), "key")
        if isinstance(item, dict):
            for child in reversed(list(item.items())):
                stack.append((child[1], child[0], True, entry))
        elif isinstance(item, list):
            for index in range(len(item) - 1, -1, -1):
                stack.append((item[index], index, True, entry))
        elif values and parented and test(js_string(item)):
            add(_entry_path(entry, path), "value")


def _entry_path(entry, base):
    parts = []
    while entry[3] is not None:
        parts.append(entry[1])
        entry = entry[3]
    return base + parts[::-1]


def _run_search(document, search, pattern, keys, values, quick):
    test = lambda text: pattern.search(text) is not None  # noqa: E731

    def add(path, on):
        if len(search.matches) >= MAX_MATCHES:
            search.capped = True
            raise _Stop
        search.matches.append({"path": path, "on": on})

    def elements(region, base, file):
        for index in range(len(region)):
            if search.cancelled.is_set() or document.cancelled.is_set():
                raise _Stop
            offset, length = region.offsets[index], region.lengths[index]
            raw = document.read(offset, length, file)
            search.scanned = offset + length
            if quick and not test(raw.decode("utf-8", "replace")) and _raw_text_is_exact(raw):
                continue
            item = json.loads(raw.decode("utf-8", "replace"))
            _walk(item, base + [index], index, True, test, keys, values, add)

    try:
        with open(document.path, "rb") as file:
            if document.region is not None:
                elements(document.region, [], file)
            elif document.entries is not None:
                for entry in document.entries.values():
                    if entry.region is not None:
                        if keys and test(entry.key):
                            add([entry.key], "key")
                        elements(entry.region, [entry.key], file)
                    else:
                        item = document.load(entry.offset, entry.length, file)
                        _walk(item, [entry.key], entry.key, True, test, keys, values, add)
                        search.scanned = entry.offset + entry.length
            else:
                _walk(document.load(*document.value_span, file), [], MISSING, False, test, keys, values, add)
        search.scanned = search.total
    except _Stop:
        pass
    except Exception as error:  # noqa: BLE001 - shown in the search status
        search.error = f"Search failed: {type(error).__name__}."
    search.done = True


def start_search(document, query, case_sensitive=False, whole_word=False, regex=False, scope="both", background=True):
    if not isinstance(query, str) or not query:
        raise ValueError("Enter something to search for.")
    if scope not in ("both", "keys", "values"):
        raise ValueError("scope must be both, keys or values.")
    pattern = matcher(query, case_sensitive, whole_word, regex)
    search = Search(uuid.uuid4().hex, document)
    with document.lock:
        document.searches[search.id] = search
        while len(document.searches) > SEARCHES:
            _, old = document.searches.popitem(last=False)
            old.cancelled.set()
    # A regular expression can match text the file writes differently (anchors, for
    # example), so only plain text searches skip elements by their raw text.
    arguments = (document, search, pattern, scope != "values", scope != "keys", not regex)
    if background:
        threading.Thread(target=_run_search, args=arguments, daemon=True, name=f"jsonviz-search-{search.id[:8]}").start()
    else:
        _run_search(*arguments)
    return search


def get_search(document, search_id):
    search = document.searches.get(search_id)
    if search is None:
        raise NotFound("This search has finished or was replaced. Search again.")
    return search


def cancel_search(document, search_id):
    with document.lock:
        search = document.searches.pop(search_id, None)
    if search is None:
        return False
    search.cancelled.set()
    return True
