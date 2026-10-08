"""The page's table and search rules, ported from frontend/json-visualizer (model.js,
cloud.js and search.js), so server mode filters, sorts, names and finds exactly like the
browser does for small files. JavaScript's `undefined` is MISSING here.
"""

import json
import math
import re
import unicodedata
from decimal import Decimal

MISSING = type("Missing", (), {"__repr__": lambda self: "MISSING", "__bool__": lambda self: False})()


def is_container(value):
    return isinstance(value, (dict, list))


def type_of(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def js_number(value):
    """String(number) in JavaScript: 1.0 → "1", 1e-7 → "1e-7", 1e21 → "1e+21"."""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    if 1e-6 <= abs(value) < 1e21:
        return format(Decimal(repr(value)), "f")
    # Both languages print the shortest round-trip digits; only the exponent differs.
    mantissa, exponent = repr(value).split("e")
    exponent = int(exponent)
    return f"{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"


def js_string(value):
    """String(value) for any JSON value."""
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None or item is MISSING else js_string(item) for item in value)
    return "[object Object]"


def js_json(value):
    """JSON.stringify(value) without spaces, numbers written the JavaScript way."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{js_json(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(js_json(item) for item in value) + "]"
    if isinstance(value, float) and not math.isfinite(value):
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return js_number(value)
    return json.dumps(value, ensure_ascii=False)


def truthy(value):
    if value is MISSING or value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return True


def _index(text):
    return text.isascii() and text.isdigit() and (text == "0" or not text.startswith("0"))


def _has(container, key):
    if isinstance(container, dict):
        return key in container
    return key == "length" or (_index(key) and int(key) < len(container))


def _item(container, key):
    """container[key] in JavaScript, for a string key."""
    if isinstance(container, dict):
        return container.get(key, MISSING)
    if key == "length":
        return len(container)
    return container[int(key)] if _index(key) and int(key) < len(container) else MISSING


def _prop(value, key):
    return value.get(key, MISSING) if isinstance(value, dict) else MISSING


def _nullish(value):
    return value is None or value is MISSING


def tags(record):
    """Tags as [(key, value)]: AWS [{Key, Value}], Azure {k: v}, kubectl metadata.labels."""
    if not isinstance(record, dict):
        return []
    found = MISSING
    for candidate in (_prop(record, "Tags"), _prop(record, "TagSet"), _prop(record, "tags"),
                      _prop(_prop(record, "metadata"), "labels"), _prop(record, "labels")):
        found = candidate
        if truthy(candidate):
            break
    if isinstance(found, list):
        result = []
        for tag in found:
            if isinstance(tag, dict) and ("Key" in tag or "key" in tag):
                key = tag.get("Key", MISSING)
                key = tag.get("key", MISSING) if _nullish(key) else key
                value = tag.get("Value", MISSING)
                value = tag.get("value", MISSING) if _nullish(value) else value
                result.append((js_string(key), "" if _nullish(value) else value))
        return result
    if isinstance(found, dict):
        return [(key, "" if value is None else value) for key, value in found.items() if not isinstance(value, dict)]
    return []


ID_KEYS = ["InstanceId", "VpcId", "SubnetId", "GroupId", "VolumeId", "ImageId", "NetworkInterfaceId",
           "DBInstanceIdentifier", "FunctionName", "BucketName", "Arn", "ARN", "arn", "id", "Id", "uid"]
ANY_ID = re.compile(r"(Id|Arn|Name)$")


def display_name(record):
    """Name tag, then name, then the resource ID (cloud.js displayName)."""
    if not isinstance(record, dict):
        return MISSING
    for key, value in tags(record):
        if key in ("Name", "name"):
            if value != "":
                return js_string(value)
            break
    for key in ("Name", "name", "displayName", "DisplayName"):
        if isinstance(record.get(key), str) and record[key]:
            return record[key]
    name = _prop(_prop(record, "metadata"), "name")
    if isinstance(name, str):
        return name
    for key in ID_KEYS:
        value = record.get(key, MISSING)
        if value is not MISSING and not isinstance(value, dict):
            return js_string(value)
    for key, value in record.items():
        if ANY_ID.search(key) and isinstance(value, str):
            return value
    return MISSING


def get_field(record, key, display=display_name):
    """"State.Name" inside one record; "@name" is the resource display name."""
    if key == "@name":
        return display(record) if display else MISSING
    if not is_container(record):
        return record if key == "@value" else MISSING
    if _has(record, key):
        return _item(record, key)
    value = record
    for part in key.split("."):
        if not is_container(value):
            return MISSING
        value = _item(value, part)
    return value


def field_of(path, value, key):
    """A table cell: "@key" is the row's key, "@value" the row itself."""
    if key == "@key":
        return path[-1] if path else MISSING
    if key == "@value":
        return value
    return get_field(value, key)


def preview(value, limit=120):
    """A short one-line preview of a value."""
    if isinstance(value, list):
        return f"[{len(value)} item{'' if len(value) == 1 else 's'}]"
    if isinstance(value, dict):
        return f"{{{len(value)} key{'' if len(value) == 1 else 's'}}}"
    text = js_string(value)
    return text[: limit - 1] + "…" if len(text) > limit else text


def flatten_keys(record, depth=3, prefix="", out=None):
    """Nested objects become dotted columns (State.Name) up to depth; arrays stay whole."""
    out = {} if out is None else out
    if not isinstance(record, dict):
        return out
    for key, value in record.items():
        name = prefix + key
        if isinstance(value, dict) and depth > 1 and 0 < len(value) <= 40:
            flatten_keys(value, depth - 1, name + ".", out)
        else:
            out[name] = out.get(name, 0) + 1
    return out


def column_keys(records, depth=3, sample=5000):
    """Every record's (flattened) keys, most common first, ties in first-seen order."""
    counts = {}
    step = max(1, len(records) // sample)
    for index in range(0, len(records), step):
        for key, count in flatten_keys(records[index], depth).items():
            counts[key] = counts.get(key, 0) + count
    return [key for _, key in sorted(enumerate(counts), key=lambda item: (-counts[item[1]], item[0]))]


def cell_text(value):
    """The text a cell shows and sorts, filters and exports by."""
    if value is MISSING:
        return ""
    if value is None:
        return "null"
    if isinstance(value, list):
        if not any(is_container(item) for item in value):
            return ", ".join("" if item is None else js_string(item) for item in value)
        return preview(value)
    if isinstance(value, dict):
        return js_json(value)
    return js_string(value)


NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
PREFIXED = re.compile(r"0([xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)", re.ASCII)


def js_to_number(text):
    """Number(text) in JavaScript; NaN when it is not a number."""
    text = text.strip()
    if not text:
        return 0.0
    if NUMBER.fullmatch(text):
        return float(text)
    if text in ("Infinity", "+Infinity"):
        return math.inf
    if text == "-Infinity":
        return -math.inf
    match = PREFIXED.fullmatch(text)
    if match:
        return float(int(match.group(0), 0))
    return math.nan


RANGE = re.compile(r"^(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)$", re.ASCII)
COMPARE = re.compile(r"^(>=|<=|>|<)\s*(.+)$", re.DOTALL)
EQUALS = re.compile(r"^(!=|=)\s*(.*)$", re.DOTALL)


def filter_test(expression):
    """A column filter: text (contains), =x, !=x, >n, >=n, <n, <=n, a..b, empty, !empty."""
    text = str(expression or "").strip()
    if not text:
        return lambda value: True
    lower = text.lower()
    if lower == "empty":
        return lambda value: cell_text(value) == ""
    if lower in ("!empty", "not empty"):
        return lambda value: cell_text(value) != ""
    match = RANGE.match(text)
    if match:
        low, high = js_to_number(match.group(1)), js_to_number(match.group(2))

        def in_range(value):
            raw = cell_text(value)
            number = js_to_number(raw)
            return raw != "" and number >= low and number <= high
        return in_range
    match = COMPARE.match(text)
    if match:
        operator, target = match.group(1), match.group(2).strip()
        numeric = not math.isnan(js_to_number(target))
        right = js_to_number(target) if numeric else target

        def compare(value):
            raw = cell_text(value)
            if raw == "":
                return False
            left = js_to_number(raw) if numeric else raw
            if numeric and math.isnan(left):
                return False
            if operator == ">":
                return left > right
            if operator == ">=":
                return left >= right
            return left < right if operator == "<" else left <= right
        return compare
    match = EQUALS.match(text)
    if match:
        target = match.group(2).strip().lower()
        if match.group(1) == "=":
            return lambda value: cell_text(value).lower() == target
        return lambda value: cell_text(value).lower() != target
    return lambda value: lower in cell_text(value).lower()


CHUNKS = re.compile(r"\d+|\D+", re.ASCII)


def _base(text):
    # localeCompare with sensitivity "base": case and accents do not count.
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)).casefold()


def natural_key(text):
    """localeCompare(…, {numeric: true, sensitivity: "base"}), closely: digit runs compare
    as numbers, before text; text without case or accents."""
    return tuple((0, int(chunk), "") if chunk.isdigit() else (1, 0, _base(chunk)) for chunk in CHUNKS.findall(text))


class SortValue:
    """A cell prepared once for compare_texts: its text, number and natural key."""

    __slots__ = ("text", "number", "key")

    def __init__(self, value):
        self.text = cell_text(value)
        number = js_to_number(self.text) if self.text else math.nan
        self.number = None if math.isnan(number) else number
        self.key = None


def compare_sort_values(a, b):
    """compareCells: numbers numerically, text naturally, empties last."""
    if a.text == "" or b.text == "":
        return 0 if a.text == b.text else 1 if a.text == "" else -1
    if a.number is not None and b.number is not None:
        difference = a.number - b.number
        return 0 if math.isnan(difference) else (difference > 0) - (difference < 0)
    if a.key is None:
        a.key = natural_key(a.text)
    if b.key is None:
        b.key = natural_key(b.text)
    return (a.key > b.key) - (a.key < b.key)


def compare_cells(a, b):
    return compare_sort_values(SortValue(a), SortValue(b))


def escape_regex(text):
    return re.sub(r"[.*+?^${}()|[\]\\]", lambda match: "\\" + match.group(0), text)


def matcher(query, case_sensitive=False, whole_word=False, regex=False):
    """The compiled test for one string (search.js matcher). Raises ValueError."""
    source = query if regex else escape_regex(query)
    if whole_word:
        source = rf"(?:^|\b|(?<=\W))(?:{source})(?:\b|$|(?=\W))"
    try:
        return re.compile(source, 0 if case_sensitive else re.IGNORECASE)
    except re.error as error:
        raise ValueError(f"Invalid regular expression: {error}.") from None
