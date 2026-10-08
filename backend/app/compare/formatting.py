"""Pretty-print JSON, JSON Lines and JSON5 text for comparing.

JSON5 (comments, trailing commas, unquoted keys, single quotes, hex numbers, +/-
Infinity and NaN) is read by a small parser here, so no extra package is needed.
"""

import json
import math
import re

INDENT = 2


class Json5Error(ValueError):
    pass


class Json5Reader:
    IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
    NUMBER = re.compile(
        r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|Infinity|NaN)"
    )

    def __init__(self, text):
        self.text, self.at = text, 0

    def fail(self, message):
        line = self.text.count("\n", 0, self.at) + 1
        raise Json5Error(f"{message} on line {line}.")

    def space(self):
        text = self.text
        while self.at < len(text):
            if text[self.at] in " \t\r\n﻿   ":
                self.at += 1
            elif text.startswith("//", self.at):
                end = text.find("\n", self.at)
                self.at = len(text) if end < 0 else end
            elif text.startswith("/*", self.at):
                end = text.find("*/", self.at + 2)
                if end < 0:
                    self.fail("Unclosed comment")
                self.at = end + 2
            else:
                return

    def value(self):
        self.space()
        if self.at >= len(self.text):
            self.fail("Unexpected end of text")
        char = self.text[self.at]
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char in "\"'":
            return self.string()
        for word, result in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(word, self.at):
                self.at += len(word)
                return result
        match = self.NUMBER.match(self.text, self.at)
        if match:
            self.at = match.end()
            number = match.group(0)
            sign = -1 if number.startswith("-") else 1
            body = number.lstrip("+-")
            if body == "Infinity":
                return sign * math.inf
            if body == "NaN":
                return math.nan
            if body[:2].lower() == "0x":
                return sign * int(body, 16)
            return float(number) if any(c in body for c in ".eE") else int(number)
        self.fail(f"Unexpected character {char!r}")

    def string(self):
        quote = self.text[self.at]
        self.at += 1
        parts = []
        escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
        while self.at < len(self.text):
            char = self.text[self.at]
            self.at += 1
            if char == quote:
                return "".join(parts)
            if char == "\\":
                if self.at >= len(self.text):
                    break
                escape = self.text[self.at]
                self.at += 1
                if escape == "u":
                    parts.append(chr(int(self.text[self.at : self.at + 4], 16)))
                    self.at += 4
                elif escape == "x":
                    parts.append(chr(int(self.text[self.at : self.at + 2], 16)))
                    self.at += 2
                elif escape in "\r\n":
                    # A backslash before a line break continues the string.
                    if escape == "\r" and self.text.startswith("\n", self.at):
                        self.at += 1
                else:
                    parts.append(escapes.get(escape, escape))
            elif char == "\n":
                self.fail("Line break inside a string")
            else:
                parts.append(char)
        self.fail("Unclosed string")

    def key(self):
        self.space()
        if self.text[self.at : self.at + 1] in ("'", '"'):
            return self.string()
        match = self.IDENT.match(self.text, self.at)
        if not match:
            self.fail("Expected a property name")
        self.at = match.end()
        return match.group(0)

    def items(self, close, read):
        self.at += 1
        while True:
            self.space()
            if self.text.startswith(close, self.at):
                self.at += 1
                return
            read()
            self.space()
            if self.text.startswith(",", self.at):
                self.at += 1
            elif not self.text.startswith(close, self.at):
                self.fail(f"Expected ',' or '{close}'")

    def object(self):
        result = {}

        def read():
            name = self.key()
            self.space()
            if not self.text.startswith(":", self.at):
                self.fail("Expected ':'")
            self.at += 1
            result[name] = self.value()

        self.items("}", read)
        return result

    def array(self):
        result = []
        self.items("]", lambda: result.append(self.value()))
        return result

    def document(self):
        result = self.value()
        self.space()
        if self.at != len(self.text):
            self.fail("Unexpected text after the value")
        return result


def dump(value):
    return json.dumps(value, indent=INDENT, ensure_ascii=False)


def prettify(text):
    """Pretty JSON text and what it was: json, jsonl or json5. Raises ValueError."""
    if not text.strip():
        raise ValueError("There is no text to format.")
    try:
        return dump(json.loads(text)), "json"
    except ValueError:
        pass
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        try:
            # JSON Lines: one record per line; each is formatted, one after another.
            return "\n".join(dump(json.loads(line)) for line in lines), "jsonl"
        except ValueError:
            pass
    try:
        return dump(Json5Reader(text).document()), "json5"
    except Json5Error as error:
        raise ValueError(f"Not JSON, JSON Lines or JSON5: {error}") from None
    except (ValueError, IndexError):
        raise ValueError("Not JSON, JSON Lines or JSON5.") from None
