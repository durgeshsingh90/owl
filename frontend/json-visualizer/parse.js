"use strict";
// Reading JSON, JSON Lines and JSON5 text: format detection, error locations (line,
// column and the offending text) and tolerance for CLI noise before the JSON.
// Runs in the parse worker and in Node tests; it has no DOM dependencies.
(function (root) {
  class ParseError extends Error {
    constructor(message, location = {}) {
      super(message);
      Object.assign(this, location);
    }
  }

  // Line, column (both from 1) and a one-line snippet around a character index.
  function locate(text, index) {
    index = Math.max(0, Math.min(index, text.length));
    let line = 1, start = 0;
    for (let at = text.indexOf("\n"); at !== -1 && at < index; at = text.indexOf("\n", at + 1)) {
      line++;
      start = at + 1;
    }
    let end = text.indexOf("\n", start);
    if (end === -1) end = text.length;
    const column = index - start + 1;
    // At most 80 characters of the line, keeping the error position in view.
    const from = Math.max(start, index - 40), to = Math.min(end, from + 80);
    const snippet = text.slice(from, to).replace(/\r$/, "");
    return {line, column, snippet, caret: index - from};
  }

  // Bytes to text: UTF-8 with or without BOM, UTF-16 by BOM or by its zero bytes.
  function decodeBytes(bytes) {
    const notices = [];
    let encoding = "utf-8", offset = 0;
    if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) offset = 3;
    else if (bytes[0] === 0xff && bytes[1] === 0xfe) { encoding = "utf-16le"; offset = 2; }
    else if (bytes[0] === 0xfe && bytes[1] === 0xff) { encoding = "utf-16be"; offset = 2; }
    else if (bytes.length > 3 && bytes[0] !== 0 && bytes[1] === 0 && bytes[3] === 0) encoding = "utf-16le";
    else if (bytes.length > 3 && bytes[0] === 0 && bytes[1] !== 0 && bytes[2] === 0) encoding = "utf-16be";
    if (encoding !== "utf-8") notices.push(`Converted from ${encoding.toUpperCase()}.`);
    const text = new TextDecoder(encoding).decode(offset ? bytes.subarray(offset) : bytes);
    return {text, notices};
  }

  // CLI output sometimes starts with warnings. Skip whole lines before the first line
  // that starts with { or [ (only when the text does not already start with JSON).
  function skipNoise(text) {
    const start = text.search(/\S/);
    // JSON, or JSON5 starting with a comment, is left alone.
    if (start === -1 || /[[{"'\-\d]/.test(text[start]) || /^(true|false|null)\b|^\/[/*]/.test(text.slice(start, start + 5))) {
      return {text, skippedLines: 0};
    }
    const match = /^[ \t]*[[{]/m.exec(text);
    if (!match) return {text, skippedLines: 0};
    const skipped = text.slice(0, match.index);
    return {text: text.slice(match.index), skippedLines: (skipped.match(/\n/g) || []).length, skipped};
  }

  // A native JSON.parse error with its location, when the browser reports one.
  function nativeError(error, text) {
    const message = String(error.message || error);
    const lineColumn = /line (\d+) column (\d+)/i.exec(message);
    const position = /position (\d+)/i.exec(message);
    let index = null;
    if (position) index = Number(position[1]);
    else if (lineColumn) {
      let at = 0;
      for (let line = 1; line < Number(lineColumn[1]); line++) {
        const next = text.indexOf("\n", at);
        if (next === -1) break;
        at = next + 1;
      }
      index = at + Number(lineColumn[2]) - 1;
    }
    const clean = message.replace(/^JSON\.parse:\s*/, "").replace(/\s*(in JSON )?at position \d+.*$/i, "").replace(/ at line \d+ column \d+ of the JSON data$/, "");
    return new ParseError(clean || "Invalid JSON", index === null ? {} : locate(text, index));
  }

  // JSON5: comments, trailing commas, unquoted keys, single quotes, hex, +/-Infinity, NaN.
  function parseJson5(text) {
    let at = 0;
    const fail = (message, index = at) => { throw new ParseError(message, locate(text, index)); };
    const IDENT = /[A-Za-z_$][A-Za-z0-9_$]*/y;
    const NUMBER = /[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|Infinity|NaN)/y;
    function space() {
      while (at < text.length) {
        const char = text[at];
        if (char === " " || char === "\t" || char === "\n" || char === "\r" || char === "﻿" || char === " " || char === " " || char === " ") at++;
        else if (text.startsWith("//", at)) { const end = text.indexOf("\n", at); at = end === -1 ? text.length : end; }
        else if (text.startsWith("/*", at)) { const end = text.indexOf("*/", at + 2); if (end === -1) fail("Unclosed comment"); at = end + 2; }
        else return;
      }
    }
    function string() {
      const quote = text[at++];
      let out = "";
      const escapes = {b: "\b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v", 0: "\0"};
      while (at < text.length) {
        const char = text[at++];
        if (char === quote) return out;
        if (char === "\\") {
          const escape = text[at++];
          if (escape === "u") { out += String.fromCharCode(parseInt(text.slice(at, at + 4), 16)); at += 4; }
          else if (escape === "x") { out += String.fromCharCode(parseInt(text.slice(at, at + 2), 16)); at += 2; }
          else if (escape === "\r") { if (text[at] === "\n") at++; }
          else if (escape === "\n" || escape === " " || escape === " ") { /* line continuation */ }
          else out += escapes[escape] ?? escape;
        } else if (char === "\n") fail("Line break inside a string", at - 1);
        else out += char;
      }
      fail("Unclosed string");
    }
    function key() {
      space();
      if (text[at] === "\"" || text[at] === "'") return string();
      IDENT.lastIndex = at;
      const match = IDENT.exec(text);
      if (!match) fail("Expected a property name");
      at = IDENT.lastIndex;
      return match[0];
    }
    function value() {
      space();
      if (at >= text.length) fail("Unexpected end of text");
      const char = text[at];
      if (char === "{") {
        at++;
        const out = {};
        for (;;) {
          space();
          if (text[at] === "}") { at++; return out; }
          const name = key();
          space();
          if (text[at] !== ":") fail("Expected ':'");
          at++;
          out[name] = value();
          space();
          if (text[at] === ",") at++;
          else if (text[at] !== "}") fail("Expected ',' or '}'");
        }
      }
      if (char === "[") {
        at++;
        const out = [];
        for (;;) {
          space();
          if (text[at] === "]") { at++; return out; }
          out.push(value());
          space();
          if (text[at] === ",") at++;
          else if (text[at] !== "]") fail("Expected ',' or ']'");
        }
      }
      if (char === "\"" || char === "'") return string();
      for (const [word, result] of [["true", true], ["false", false], ["null", null]]) {
        if (text.startsWith(word, at)) { at += word.length; return result; }
      }
      NUMBER.lastIndex = at;
      const match = NUMBER.exec(text);
      if (match) {
        at = NUMBER.lastIndex;
        const number = match[0], sign = number[0] === "-" ? -1 : 1, body = number.replace(/^[+-]/, "");
        if (body === "Infinity") return sign * Infinity;
        if (body === "NaN") return NaN;
        if (/^0x/i.test(body)) return sign * parseInt(body, 16);
        return Number(number);
      }
      fail(`Unexpected character ${JSON.stringify(char)}`);
    }
    const result = value();
    space();
    if (at < text.length) fail("Unexpected text after the value");
    return result;
  }

  // JSON Lines: one record per line. A bad line is listed and skipped; the rest load.
  function parseJsonLines(text) {
    const records = [], lines = [], errors = [];
    let start = 0, line = 1;
    while (start <= text.length) {
      let end = text.indexOf("\n", start);
      if (end === -1) end = text.length;
      const raw = text.slice(start, end).replace(/\r$/, "");
      if (raw.trim()) {
        try {
          records.push(JSON.parse(raw));
          lines.push(line);
        } catch (error) {
          const located = nativeError(error, raw);
          errors.push({line, column: located.column || null, message: located.message, snippet: raw.slice(0, 120)});
        }
      }
      if (end === text.length) break;
      start = end + 1;
      line++;
    }
    return {records, lines, errors};
  }

  function looksLikeLines(text) {
    const lines = text.split("\n", 6).map(line => line.trim()).filter(Boolean);
    if (lines.length < 2) return false;
    try { JSON.parse(lines[0]); return true; } catch { return false; }
  }

  // name: the file name (its extension is a hint); format: auto, json, jsonl or json5.
  function parseDocument(text, {name = "", format = "auto"} = {}) {
    const notices = [];
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    const noise = skipNoise(text);
    if (noise.skippedLines) {
      notices.push(`Skipped ${noise.skippedLines} line${noise.skippedLines === 1 ? "" : "s"} of text before the JSON.`);
      text = noise.text;
    }
    const extension = (/\.([a-z0-9]+)(?:\.gz)?$/i.exec(name)?.[1] || "").toLowerCase();
    let chosen = format;
    if (chosen === "auto" && (extension === "jsonl" || extension === "ndjson")) chosen = "jsonl";
    if (chosen === "auto" && extension === "json5") chosen = "json5";
    if (chosen === "jsonl") {
      const {records, lines, errors} = parseJsonLines(text);
      if (!records.length && errors.length) throw new ParseError(`No valid JSON lines: line ${errors[0].line}: ${errors[0].message}`, {line: errors[0].line, column: errors[0].column, snippet: errors[0].snippet});
      return {value: records, format: "jsonl", lines, errors, notices};
    }
    if (chosen === "json5") return {value: parseJson5(text), format: "json5", errors: [], notices};
    try {
      return {value: JSON.parse(text), format: "json", errors: [], notices};
    } catch (error) {
      if (chosen === "json") throw nativeError(error, text);
      if (looksLikeLines(text)) {
        const {records, lines, errors} = parseJsonLines(text);
        return {value: records, format: "jsonl", lines, errors, notices};
      }
      try {
        return {value: parseJson5(text), format: "json5", errors: [], notices};
      } catch (json5Error) {
        const native = nativeError(error, text);
        // The browser's own message and position when it gave one, else the JSON5 reader's.
        throw native.line ? native : json5Error;
      }
    }
  }

  const api = {ParseError, locate, decodeBytes, skipNoise, parseJson5, parseJsonLines, parseDocument};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVParse = api;
})(typeof self !== "undefined" ? self : this);
