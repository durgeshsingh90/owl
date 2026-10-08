"use strict";
// Values, paths, table columns, filters and export text. No DOM: used by the page and tests.
(function (root) {
  const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
  const JMES_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;

  const typeOf = value => value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
  const isContainer = value => value !== null && typeof value === "object";

  // A path is a list of keys (strings) and indexes (numbers) from the document root.
  function formatPath(path, syntax = "js") {
    if (syntax === "jq") {
      if (!path.length) return ".";
      return path.map(part => typeof part === "number" ? `[${part}]`
        : IDENTIFIER.test(part) && !part.includes("$") ? `.${part}` : `.[${JSON.stringify(part)}]`).join("").replace(/^\[/, ".[");
    }
    if (syntax === "jmes") {
      if (!path.length) return "@";
      return path.map((part, index) => typeof part === "number" ? `[${part}]`
        : `${index ? "." : ""}${JMES_IDENTIFIER.test(part) ? part : JSON.stringify(part)}`).join("");
    }
    return path.map((part, index) => typeof part === "number" ? `[${part}]`
      : IDENTIFIER.test(part) ? `${index ? "." : ""}${part}` : `[${JSON.stringify(part)}]`).join("");
  }

  // "Reservations[0].Instances[2]", ".a.b[1]", 'a["x-y"]' or a.b.0 → a path, or null.
  function parsePath(text) {
    const path = [];
    let rest = String(text).trim().replace(/^[$@.]?(?=\.|\[|$)/, "");
    if (rest === "" || rest === ".") return path;
    const pattern = /^\s*(?:\.?([A-Za-z_$][\w$-]*)|\[\s*(\d+)\s*\]|\.?\[\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*\]|\.?"((?:[^"\\]|\\.)*)"|\.(\d+))/;
    while (rest.length) {
      const match = pattern.exec(rest);
      if (!match) return null;
      if (match[1] !== undefined) path.push(match[1]);
      else if (match[2] !== undefined) path.push(Number(match[2]));
      else if (match[3] !== undefined) path.push(match[3][0] === "'" ? match[3].slice(1, -1).replace(/\\'/g, "'") : JSON.parse(match[3]));
      else if (match[4] !== undefined) path.push(JSON.parse(`"${match[4]}"`));
      else path.push(Number(match[5]));
      rest = rest.slice(match[0].length);
    }
    return path;
  }

  function getAt(value, path) {
    for (const part of path) {
      if (!isContainer(value)) return undefined;
      value = value[part];
    }
    return value;
  }

  // "State.Name" inside one record; "@name" is the resource display name.
  function getField(record, key, displayName) {
    if (key === "@name") return displayName ? displayName(record) : undefined;
    if (!isContainer(record)) return key === "@value" ? record : undefined;
    if (key in record) return record[key];
    let value = record;
    for (const part of key.split(".")) {
      if (!isContainer(value)) return undefined;
      value = value[part];
    }
    return value;
  }

  // Strings copy without quotes; objects and arrays as pretty JSON.
  function copyText(value, minified = false) {
    if (typeof value === "string") return value;
    if (value === undefined) return "";
    return JSON.stringify(value, null, minified ? 0 : 2);
  }

  const DATE = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
  const URL_PATTERN = /^https?:\/\/[^\s]+$/;
  const IPV4 = /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\/\d{1,2})?$/;
  const IPV6 = /^(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?:\/\d{1,3})?$/i;
  const ARN = /^arn:aws[\w-]*:[\w-]+:[\w-]*:\d{0,12}:.+$/;
  const RESOURCE = /^(?:(?:i|ami|vol|snap|sg|subnet|vpc|eni|igw|nat|rtb|acl|eipalloc|lt|tgw|vpce|pcx|dopt|key|r|eigw|cvpn|vgw|cgw|elb|fs|db|lc|asg)-[0-9a-f]{6,17}|\/subscriptions\/[0-9a-f-]{36}(?:\/.*)?)$/i;
  // What kind of string this is, for styling: date, url, ip, arn, resource or string.
  function stringKind(text) {
    if (text.length > 2048) return "string";
    if (DATE.test(text) && !Number.isNaN(Date.parse(text))) return "date";
    if (URL_PATTERN.test(text)) return "url";
    if (ARN.test(text)) return "arn";
    if (RESOURCE.test(text)) return "resource";
    if (IPV4.test(text) || (text.includes(":") && IPV6.test(text) && /[0-9a-f]/i.test(text) && text.length > 2)) return "ip";
    return "string";
  }

  // A short one-line preview of a value.
  function preview(value, max = 120) {
    if (Array.isArray(value)) return `[${value.length} item${value.length === 1 ? "" : "s"}]`;
    if (isContainer(value)) {
      const keys = Object.keys(value);
      return `{${keys.length} key${keys.length === 1 ? "" : "s"}}`;
    }
    const text = typeof value === "string" ? value : String(value);
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // Nested objects become dotted columns (State.Name) up to `depth`; arrays stay whole.
  function flattenKeys(record, depth = 3, prefix = "", out = new Map()) {
    if (!isContainer(record) || Array.isArray(record)) return out;
    for (const key of Object.keys(record)) {
      const value = record[key], name = prefix + key;
      if (isContainer(value) && !Array.isArray(value) && depth > 1 && Object.keys(value).length && Object.keys(value).length <= 40) {
        flattenKeys(value, depth - 1, name + ".", out);
      } else out.set(name, (out.get(name) || 0) + 1);
    }
    return out;
  }

  // The union of every record's (flattened) keys, most common first, ties in first-seen order.
  function columnKeys(records, depth = 3, sample = 5000) {
    const counts = new Map();
    const step = Math.max(1, Math.floor(records.length / sample));
    for (let index = 0; index < records.length; index += step) {
      const record = records[index];
      if (isContainer(record) && !Array.isArray(record)) {
        for (const [key, count] of flattenKeys(record, depth)) counts.set(key, (counts.get(key) || 0) + count);
      }
    }
    const order = [...counts.keys()];
    return order.map((key, index) => ({key, count: counts.get(key), index}))
      .sort((a, b) => b.count - a.count || a.index - b.index).map(item => item.key);
  }

  // The text a cell shows and sorts, filters and exports by.
  function cellText(value) {
    if (value === undefined) return "";
    if (value === null) return "null";
    if (Array.isArray(value)) {
      if (value.every(item => !isContainer(item))) return value.join(", ");
      return preview(value);
    }
    if (isContainer(value)) return JSON.stringify(value);
    return String(value);
  }

  // A column filter: text (contains), =x, !=x, >n, >=n, <n, <=n, a..b, empty, !empty.
  function filterTest(expression) {
    const text = String(expression || "").trim();
    if (!text) return () => true;
    const lower = text.toLowerCase();
    if (lower === "empty") return value => cellText(value) === "";
    if (lower === "!empty" || lower === "not empty") return value => cellText(value) !== "";
    const range = /^(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)$/.exec(text);
    if (range) return value => { const number = Number(cellText(value)); return cellText(value) !== "" && number >= Number(range[1]) && number <= Number(range[2]); };
    const compare = /^(>=|<=|>|<)\s*(.+)$/.exec(text);
    if (compare) {
      const target = compare[2].trim(), numeric = !Number.isNaN(Number(target));
      return value => {
        const raw = cellText(value);
        if (raw === "") return false;
        const left = numeric ? Number(raw) : raw, right = numeric ? Number(target) : target;
        if (numeric && Number.isNaN(left)) return false;
        return compare[1] === ">" ? left > right : compare[1] === ">=" ? left >= right : compare[1] === "<" ? left < right : left <= right;
      };
    }
    const equals = /^(!=|=)\s*(.*)$/.exec(text);
    if (equals) {
      const target = equals[2].trim().toLowerCase();
      return equals[1] === "=" ? value => cellText(value).toLowerCase() === target : value => cellText(value).toLowerCase() !== target;
    }
    return value => cellText(value).toLowerCase().includes(lower);
  }

  // Sort comparator by several columns: numbers numerically, text naturally, empties last.
  function compareCells(a, b) {
    const left = cellText(a), right = cellText(b);
    if (left === "" || right === "") return left === right ? 0 : left === "" ? 1 : -1;
    const x = Number(left), y = Number(right);
    if (!Number.isNaN(x) && !Number.isNaN(y)) return x - y;
    return left.localeCompare(right, undefined, {numeric: true, sensitivity: "base"});
  }

  function toCsv(header, rows) {
    const cell = text => /[",\n\r]/.test(text) || /^\s|\s$/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    return [header, ...rows].map(row => row.map(value => cell(String(value))).join(",")).join("\r\n") + "\r\n";
  }

  function relativeTime(time, now = Date.now()) {
    const seconds = Math.round((time - now) / 1000), abs = Math.abs(seconds);
    const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
    for (const [unit, size] of units) {
      if (abs >= size || unit === "second") {
        const amount = Math.round(abs / size);
        if (unit === "second" && amount < 30) return "just now";
        const text = `${amount} ${unit}${amount === 1 ? "" : "s"}`;
        return seconds < 0 ? `${text} ago` : `in ${text}`;
      }
    }
    return "";
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
  }

  const api = {typeOf, isContainer, formatPath, parsePath, getAt, getField, copyText, stringKind, preview, flattenKeys, columnKeys, cellText, filterTest, compareCells, toCsv, relativeTime, formatBytes};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVModel = api;
})(typeof self !== "undefined" ? self : this);
