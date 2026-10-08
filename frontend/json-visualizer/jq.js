"use strict";
// A practical jq (https://jqlang.github.io/jq/manual/) interpreter. run(data, program)
// returns the output stream as an array. Filters are generators, so limit/first stop early.
(function (root) {
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const MAX_OUTPUTS = 100000;
  const MAX_STEPS = 50000000;

  // An error raised by the program (error/1 or a runtime type error); `value` is what catch sees.
  class JqError extends Error {
    constructor(value) {
      super(typeof value === "string" ? value : `${toJson(value)} (not a string)`);
      this.name = "JqError";
      this.value = value;
    }
  }
  // Limits are not catchable by try.
  class LimitError extends Error {}

  function syntaxError(message, position) {
    const error = new Error(`${message} at position ${position}`);
    error.name = "SyntaxError";
    error.position = position;
    return error;
  }

  function put(object, key, value) {
    if (key === "__proto__") Object.defineProperty(object, key, {value, enumerable: true, writable: true, configurable: true});
    else object[key] = value;
    return object;
  }

  // ---- Values ----

  function typeOf(value) {
    if (value === null || value === undefined) return "null";
    if (Array.isArray(value)) return "array";
    return typeof value === "object" ? "object" : typeof value;
  }

  function toJson(value) {
    return JSON.stringify(value, (key, item) => typeof item === "number" && !Number.isFinite(item)
      ? (Number.isNaN(item) ? null : item > 0 ? 1.7976931348623157e+308 : -1.7976931348623157e+308) : item) ?? "null";
  }

  const toText = value => typeof value === "string" ? value : toJson(value);
  const truthy = value => value !== null && value !== false && value !== undefined;

  // "number (5)" style descriptions used in error messages.
  function describe(value) {
    const text = toJson(value);
    return `${typeOf(value)} (${text.length > 11 ? text.slice(0, 10) + "..." : text})`;
  }

  const ORDER = {null: 0, boolean: 1, number: 3, string: 4, array: 5, object: 6};
  function compare(a, b) {
    const ta = typeOf(a), tb = typeOf(b);
    if (ta !== tb) return (ORDER[ta] + (a === true ? 1 : 0)) - (ORDER[tb] + (b === true ? 1 : 0));
    switch (ta) {
      case "null": return 0;
      case "boolean": return (a ? 1 : 0) - (b ? 1 : 0);
      case "number": return a < b ? -1 : a > b ? 1 : 0;
      case "string": return a < b ? -1 : a > b ? 1 : 0;
      case "array": {
        for (let i = 0; i < Math.min(a.length, b.length); i++) {
          const result = compare(a[i], b[i]);
          if (result) return result;
        }
        return a.length - b.length;
      }
      default: {
        const ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
        const keys = compare(ka, kb);
        if (keys) return keys;
        for (const key of ka) {
          const result = compare(a[key], b[key]);
          if (result) return result;
        }
        return 0;
      }
    }
  }
  const equal = (a, b) => compare(a, b) === 0;

  function deepMerge(a, b) {
    const out = Object.assign({}, a);
    for (const key of Object.keys(b)) put(out, key, isObject(out[key]) && isObject(b[key]) && own(out, key) ? deepMerge(out[key], b[key]) : b[key]);
    return out;
  }

  function binary(op, a, b) {
    switch (op) {
      case "+":
        if (a === null) return b;
        if (b === null) return a;
        if (typeof a === "number" && typeof b === "number") return a + b;
        if (typeof a === "string" && typeof b === "string") return a + b;
        if (Array.isArray(a) && Array.isArray(b)) return a.concat(b);
        if (isObject(a) && isObject(b)) {
          const out = Object.assign({}, a);
          for (const key of Object.keys(b)) put(out, key, b[key]);
          return out;
        }
        throw new JqError(`${describe(a)} and ${describe(b)} cannot be added`);
      case "-":
        if (typeof a === "number" && typeof b === "number") return a - b;
        if (Array.isArray(a) && Array.isArray(b)) return a.filter(item => !b.some(other => equal(item, other)));
        throw new JqError(`${describe(a)} and ${describe(b)} cannot be subtracted`);
      case "*":
        if (typeof a === "number" && typeof b === "number") return a * b;
        if (typeof a === "string" && typeof b === "number" || typeof a === "number" && typeof b === "string") {
          const [text, count] = typeof a === "string" ? [a, b] : [b, a];
          return count <= 0 || Number.isNaN(count) ? null : text.repeat(Math.max(1, Math.ceil(count - 1e-12)) > 1e7 ? 1e7 : Math.max(1, Math.ceil(count - 1e-12)));
        }
        if (isObject(a) && isObject(b)) return deepMerge(a, b);
        throw new JqError(`${describe(a)} and ${describe(b)} cannot be multiplied`);
      case "/":
        if (typeof a === "number" && typeof b === "number") {
          if (b === 0) throw new JqError(`${describe(a)} and ${describe(b)} cannot be divided because the divisor is zero`);
          return a / b;
        }
        if (typeof a === "string" && typeof b === "string") return splitString(a, b);
        throw new JqError(`${describe(a)} and ${describe(b)} cannot be divided`);
      case "%": {
        if (typeof a === "number" && typeof b === "number") {
          const x = Math.trunc(a), y = Math.trunc(b);
          if (y === 0) throw new JqError(`${describe(a)} and ${describe(b)} cannot be divided because the divisor is zero`);
          const result = x % y;
          return result === 0 ? 0 : result;
        }
        throw new JqError(`${describe(a)} and ${describe(b)} cannot be divided`);
      }
      case "==": return equal(a, b);
      case "!=": return !equal(a, b);
      case "<": return compare(a, b) < 0;
      case "<=": return compare(a, b) <= 0;
      case ">": return compare(a, b) > 0;
      case ">=": return compare(a, b) >= 0;
    }
    throw new JqError(`Unknown operator ${op}`);
  }

  const splitString = (text, separator) => text === "" ? [] : text.split(separator);

  function index(value, key) {
    if (key !== null && typeof key === "object" && !Array.isArray(key) && ("start" in key || "end" in key)) return sliceOf(value, key.start ?? null, key.end ?? null);
    if (typeof key === "string") {
      if (value === null) return null;
      if (isObject(value)) return own(value, key) ? value[key] : null;
      throw new JqError(`Cannot index ${typeOf(value)} with "${key}"`);
    }
    if (typeof key === "number") {
      if (value === null) return null;
      if (Array.isArray(value)) {
        if (Number.isNaN(key)) return null;
        let at = Math.floor(key);
        if (at < 0) at += value.length;
        return at >= 0 && at < value.length ? value[at] : null;
      }
      throw new JqError(`Cannot index ${typeOf(value)} with number`);
    }
    if (Array.isArray(key) && Array.isArray(value)) return indicesOf(value, key);
    if (key === null && isObject(value)) throw new JqError("Cannot index object with null");
    if (value === null) return null;
    throw new JqError(`Cannot index ${typeOf(value)} with ${typeOf(key)}`);
  }

  function sliceBounds(length, from, to) {
    const clamp = (at, fallback) => {
      if (at === null) return fallback;
      if (typeof at !== "number") throw new JqError("Start and end indices of an array slice must be numbers");
      at = at < 0 ? length + at : at;
      return Math.min(Math.max(at, 0), length);
    };
    const start = Math.floor(clamp(from, 0));
    return [start, Math.max(start, Math.ceil(clamp(to, length)))];
  }

  function sliceOf(value, from, to) {
    if (value === null) return null;
    if (typeof value === "string") {
      const chars = [...value];
      const [start, end] = sliceBounds(chars.length, from, to);
      return chars.slice(start, end).join("");
    }
    if (!Array.isArray(value)) throw new JqError(`Cannot index ${typeOf(value)} with object`);
    const [start, end] = sliceBounds(value.length, from, to);
    return value.slice(start, end);
  }

  function indicesOf(value, target) {
    if (value === null) return null;
    const out = [];
    if (typeof value === "string" && typeof target === "string") {
      if (!target) return null;
      for (let at = value.indexOf(target); at !== -1; at = value.indexOf(target, at + 1)) out.push(at);
      return out;
    }
    if (Array.isArray(value)) {
      const needle = Array.isArray(target) ? target : [target];
      if (!needle.length) return null;
      for (let i = 0; i + needle.length <= value.length; i++) if (needle.every((item, j) => equal(value[i + j], item))) out.push(i);
      return out;
    }
    throw new JqError(`Cannot determine indices of ${target} in ${describe(value)}`);
  }

  function iterate(value) {
    if (Array.isArray(value)) return value;
    if (isObject(value)) return Object.values(value);
    throw new JqError(`Cannot iterate over ${value === null ? "null" : describe(value)}`);
  }

  function keysOf(value, sorted = true) {
    if (isObject(value)) {
      const keys = Object.keys(value);
      return sorted ? keys.sort((a, b) => a < b ? -1 : a > b ? 1 : 0) : keys;
    }
    if (Array.isArray(value)) return value.map((item, i) => i);
    throw new JqError(`${describe(value)} has no keys`);
  }

  function length(value) {
    if (value === null) return 0;
    if (typeof value === "boolean") throw new JqError(`${describe(value)} has no length`);
    if (typeof value === "number") return Math.abs(value);
    if (typeof value === "string") return [...value].length;
    if (Array.isArray(value)) return value.length;
    return Object.keys(value).length;
  }

  function contains(a, b) {
    if (isObject(a) && isObject(b)) return Object.keys(b).every(key => own(a, key) && contains(a[key], b[key]));
    if (Array.isArray(a) && Array.isArray(b)) return b.every(item => a.some(other => contains(other, item)));
    if (typeof a === "string" && typeof b === "string") return a.includes(b);
    if (typeOf(a) === typeOf(b)) return equal(a, b);
    throw new JqError(`${describe(a)} and ${describe(b)} cannot have their containment checked`);
  }

  // ---- Paths ----

  function getPath(value, path) {
    for (const key of path) {
      if (value === null) return null;
      value = index(value, key);
    }
    return value;
  }

  function setPath(value, path, replacement, at = 0) {
    if (at === path.length) return replacement;
    const key = path[at];
    if (typeof key === "string") {
      if (value !== null && !isObject(value)) throw new JqError(`Cannot index ${typeOf(value)} with "${key}"`);
      const out = Object.assign({}, value);
      return put(out, key, setPath(value && own(value, key) ? value[key] : null, path, replacement, at + 1));
    }
    if (typeof key === "number") {
      if (value !== null && !Array.isArray(value)) throw new JqError(`Cannot index ${typeOf(value)} with number`);
      const out = value ? value.slice() : [];
      let position = Math.floor(key);
      if (position < 0) {
        position += out.length;
        if (position < 0) throw new JqError("Out of bounds negative array index");
      }
      if (position > 10000000) throw new JqError("Array index too large");
      while (out.length < position) out.push(null);
      out[position] = setPath(position < (value || []).length ? value[position] : null, path, replacement, at + 1);
      return out;
    }
    if (isObject(key)) {
      if (value !== null && !Array.isArray(value)) throw new JqError(`Cannot update field at object index of ${typeOf(value)}`);
      const list = value || [];
      const [start, end] = sliceBounds(list.length, key.start ?? null, key.end ?? null);
      const inner = setPath(list.slice(start, end), path, replacement, at + 1);
      if (!Array.isArray(inner)) throw new JqError("A slice of an array can only be assigned another array");
      return [...list.slice(0, start), ...inner, ...list.slice(end)];
    }
    throw new JqError(`Invalid path component ${toJson(key)}`);
  }

  function deletePath(value, path, at = 0) {
    if (value === null) return null;
    const key = path[at];
    if (at === path.length - 1) {
      if (typeof key === "string") {
        if (!isObject(value)) throw new JqError(`Cannot delete field at object index of ${typeOf(value)}`);
        if (!own(value, key)) return value;
        const out = Object.assign({}, value);
        delete out[key];
        return out;
      }
      if (typeof key === "number") {
        if (!Array.isArray(value)) throw new JqError(`Cannot delete field at index of ${typeOf(value)}`);
        let position = Math.floor(key);
        if (position < 0) position += value.length;
        if (position < 0 || position >= value.length) return value;
        return [...value.slice(0, position), ...value.slice(position + 1)];
      }
      if (isObject(key)) {
        if (!Array.isArray(value)) throw new JqError(`Cannot delete slice of ${typeOf(value)}`);
        const [start, end] = sliceBounds(value.length, key.start ?? null, key.end ?? null);
        return [...value.slice(0, start), ...value.slice(end)];
      }
      throw new JqError(`Invalid path component ${toJson(key)}`);
    }
    const child = index(value, key);
    if (child === null) return value;
    return setPath(value, [key], deletePath(child, path, at + 1));
  }

  function deletePaths(value, paths) {
    const sorted = paths.slice().sort(compare).reverse();
    for (const path of sorted) {
      if (!Array.isArray(path)) throw new JqError("Path must be specified as an array");
      value = path.length ? deletePath(value, path) : null;
    }
    return value;
  }

  // Every [path, value] below `value` in pre-order, without recursion.
  function* walkPaths(value, base = []) {
    const stack = [[base, value]];
    while (stack.length) {
      const [path, item] = stack.pop();
      tick();
      yield [path, item];
      if (Array.isArray(item)) for (let i = item.length - 1; i >= 0; i--) stack.push([path.concat(i), item[i]]);
      else if (isObject(item)) {
        const keys = Object.keys(item);
        for (let i = keys.length - 1; i >= 0; i--) stack.push([path.concat(keys[i]), item[keys[i]]]);
      }
    }
  }

  // ---- Lexer ----

  const KEYWORDS = new Set(["def", "if", "then", "elif", "else", "end", "as", "reduce", "foreach", "try", "catch", "label", "import", "include", "and", "or", "__loc__"]);
  const OPERATORS = ["?//=", "|=", "+=", "-=", "*=", "/=", "%=", "//=", "==", "!=", "<=", ">=", "//", "..", "?//",
    ".", "[", "]", "{", "}", "(", ")", "|", ",", ":", ";", "=", "<", ">", "+", "-", "*", "/", "%", "?"];

  // Tokens up to the end, or (nested) up to the ")" closing a string interpolation.
  function tokenize(text, start = 0, nested = false) {
    const tokens = [];
    let at = start, depth = 0;
    while (at < text.length) {
      const char = text[at];
      if (/\s/.test(char)) { at++; continue; }
      if (char === "#") {
        while (at < text.length && text[at] !== "\n") at++;
        continue;
      }
      const begin = at;
      if (char === "\"") {
        const result = lexString(text, at);
        tokens.push({type: "string", parts: result.parts, start: begin});
        at = result.end;
        continue;
      }
      if (/[0-9]/.test(char) || char === "." && /[0-9]/.test(text[at + 1] || "")) {
        const match = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(text.slice(at));
        tokens.push({type: "number", value: Number(match[0]), start: begin});
        at += match[0].length;
        continue;
      }
      if (char === "." && /[A-Za-z_]/.test(text[at + 1] || "")) {
        at++;
        while (at < text.length && /[A-Za-z0-9_]/.test(text[at])) at++;
        tokens.push({type: "field", value: text.slice(begin + 1, at), start: begin});
        continue;
      }
      if (/[A-Za-z_]/.test(char)) {
        while (at < text.length && /[A-Za-z0-9_]/.test(text[at])) at++;
        tokens.push({type: "ident", value: text.slice(begin, at), start: begin});
        continue;
      }
      if (char === "$" || char === "@") {
        at++;
        while (at < text.length && /[A-Za-z0-9_]/.test(text[at])) at++;
        if (at === begin + 1) throw syntaxError(`Expected a name after '${char}'`, begin);
        tokens.push({type: char === "$" ? "var" : "format", value: text.slice(begin + 1, at), start: begin});
        continue;
      }
      const op = OPERATORS.find(candidate => text.startsWith(candidate, at));
      if (!op) throw syntaxError(`Unexpected character '${char}'`, at);
      if (nested) {
        if (op === "(") depth++;
        if (op === ")" && depth-- === 0) return {tokens: [...tokens, {type: "eof", start: at}], end: at + 1};
      }
      tokens.push({type: "op", value: op, start: begin});
      at += op.length;
    }
    if (nested) throw syntaxError("Unterminated string interpolation", start - 2);
    tokens.push({type: "eof", start: at});
    return {tokens, end: at};
  }

  const ESCAPES = {"\"": "\"", "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t"};
  function lexString(text, start) {
    const parts = [];
    let at = start + 1, current = "";
    while (at < text.length && text[at] !== "\"") {
      if (text[at] !== "\\") { current += text[at++]; continue; }
      const next = text[at + 1];
      if (next === "(") {
        if (current) parts.push(current);
        current = "";
        const inner = tokenize(text, at + 2, true);
        parts.push({tokens: inner.tokens, start: at + 2});
        at = inner.end;
      } else if (next === "u") {
        const hex = text.slice(at + 2, at + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw syntaxError("Invalid \\u escape", at);
        current += String.fromCharCode(parseInt(hex, 16));
        at += 6;
      } else if (ESCAPES[next] !== undefined) {
        current += ESCAPES[next];
        at += 2;
      } else throw syntaxError(`Invalid escape '\\${next || ""}'`, at);
    }
    if (at >= text.length) throw syntaxError("Unterminated string", start);
    if (current || !parts.length) parts.push(current);
    return {parts, end: at + 1};
  }

  // ---- Parser ----

  const IDENTITY = {type: "Identity"};

  function parseTokens(tokens, source) {
    let index = 0;
    const peek = (offset = 0) => tokens[Math.min(index + offset, tokens.length - 1)];
    const show = token => token.type === "eof" ? "end of program" : token.type === "string" ? "string" : `'${token.type === "field" ? "." : token.type === "var" ? "$" : token.type === "format" ? "@" : ""}${token.value}'`;
    const unexpected = token => syntaxError(`Unexpected ${show(token)}`, token.start);
    const isOp = (value, offset = 0) => peek(offset).type === "op" && peek(offset).value === value;
    const isWord = (value, offset = 0) => peek(offset).type === "ident" && peek(offset).value === value;
    const expectOp = value => {
      if (!isOp(value)) throw syntaxError(`Expected '${value}' but found ${show(peek())}`, peek().start);
      return tokens[index++];
    };
    const expectWord = value => {
      if (!isWord(value)) throw syntaxError(`Expected '${value}' but found ${show(peek())}`, peek().start);
      return tokens[index++];
    };

    function pipe(noComma = false) {
      if (isWord("def")) {
        const definition = parseDef();
        return {type: "Def", ...definition, rest: peek().type === "eof" || isOp(")") || isOp("]") || isOp("}") ? IDENTITY : pipe(noComma)};
      }
      const left = noComma ? alternative() : comma();
      if (isOp("|")) {
        index++;
        return {type: "Pipe", left, right: pipe(noComma)};
      }
      return left;
    }

    function parseDef() {
      const start = expectWord("def").start;
      const name = peek();
      if (name.type !== "ident" || KEYWORDS.has(name.value)) throw syntaxError("Expected a function name after 'def'", name.start);
      index++;
      const params = [];
      if (isOp("(")) {
        index++;
        for (;;) {
          const param = tokens[index++];
          if (param.type === "var") params.push("$" + param.value);
          else if (param.type === "ident" && !KEYWORDS.has(param.value)) params.push(param.value);
          else throw syntaxError("Expected a parameter name", param.start);
          if (isOp(";")) { index++; continue; }
          expectOp(")");
          break;
        }
      }
      expectOp(":");
      const body = pipe();
      expectOp(";");
      return {name: name.value, params, body, start};
    }

    function comma() {
      let left = alternative();
      while (isOp(",")) {
        index++;
        left = {type: "Comma", left, right: alternative()};
      }
      return left;
    }

    function alternative() {
      const left = assignment();
      if (isOp("//")) {
        index++;
        return {type: "Alt", left, right: alternative()};
      }
      return left;
    }

    const ASSIGN = new Set(["=", "|=", "+=", "-=", "*=", "/=", "%=", "//="]);
    function assignment() {
      const left = or();
      if (peek().type === "op" && ASSIGN.has(peek().value)) {
        const op = tokens[index++].value;
        return {type: "Assign", op, left, right: alternative()};
      }
      return left;
    }

    function or() {
      let left = and();
      while (isWord("or")) {
        index++;
        left = {type: "Or", left, right: and()};
      }
      return left;
    }

    function and() {
      let left = comparison();
      while (isWord("and")) {
        index++;
        left = {type: "And", left, right: comparison()};
      }
      return left;
    }

    const COMPARE = new Set(["==", "!=", "<", "<=", ">", ">="]);
    function comparison() {
      const left = additive();
      if (peek().type === "op" && COMPARE.has(peek().value)) {
        const op = tokens[index++].value;
        const right = additive();
        if (peek().type === "op" && COMPARE.has(peek().value)) throw unexpected(peek());
        return {type: "Binary", op, left, right};
      }
      return left;
    }

    function additive() {
      let left = multiplicative();
      while (isOp("+") || isOp("-")) {
        const op = tokens[index++].value;
        left = {type: "Binary", op, left, right: multiplicative()};
      }
      return left;
    }

    function multiplicative() {
      let left = unary();
      while (isOp("*") || isOp("/") || isOp("%")) {
        const op = tokens[index++].value;
        left = {type: "Binary", op, left, right: unary()};
      }
      return left;
    }

    function unary() {
      if (isOp("-")) {
        index++;
        const body = unary();
        return body.type === "Literal" && typeof body.value === "number" ? {type: "Literal", value: -body.value} : {type: "Neg", body};
      }
      return postfix();
    }

    function postfix(allowAs = true) {
      let term = primary();
      for (;;) {
        const token = peek();
        if (token.type === "field") {
          index++;
          term = {type: "Index", target: term, key: {type: "Literal", value: token.value}};
        } else if (isOp(".") && peek(1).type === "string") {
          index++;
          term = {type: "Index", target: term, key: stringTerm(tokens[index++])};
        } else if (isOp(".") && isOp("[", 1)) {
          index++;
        } else if (isOp("[")) {
          term = bracketSuffix(term);
        } else if (isOp("?")) {
          index++;
          term = {type: "Try", body: term, handler: null};
        } else if (allowAs && isWord("as")) {
          index++;
          const patterns = [pattern()];
          while (isOp("?//")) { index++; patterns.push(pattern()); }
          expectOp("|");
          return {type: "Bind", source: term, patterns, body: pipe()};
        } else return term;
      }
    }

    function bracketSuffix(target) {
      expectOp("[");
      if (isOp("]")) {
        index++;
        return {type: "Iterate", target};
      }
      if (isOp(":")) {
        index++;
        const to = pipe();
        expectOp("]");
        return {type: "Slice", target, from: null, to};
      }
      const key = pipe();
      if (isOp(":")) {
        index++;
        const to = isOp("]") ? null : pipe();
        expectOp("]");
        return {type: "Slice", target, from: key, to};
      }
      expectOp("]");
      return {type: "Index", target, key};
    }

    function pattern() {
      const token = peek();
      if (token.type === "var") {
        index++;
        return {type: "var", name: token.value};
      }
      if (isOp("[")) {
        index++;
        const elements = [];
        for (;;) {
          elements.push(pattern());
          if (isOp(",")) { index++; continue; }
          expectOp("]");
          return {type: "array", elements};
        }
      }
      if (isOp("{")) {
        index++;
        const entries = [];
        for (;;) {
          const key = peek();
          if (key.type === "var") {
            index++;
            const entry = {key: {type: "Literal", value: key.value}, bind: key.value};
            if (isOp(":")) { index++; entry.pattern = pattern(); }
            entries.push(entry);
          } else {
            let keyNode;
            if (key.type === "ident") { index++; keyNode = {type: "Literal", value: key.value}; }
            else if (key.type === "string") { index++; keyNode = stringTerm(key); }
            else if (isOp("(")) { index++; keyNode = pipe(); expectOp(")"); }
            else throw syntaxError("Expected an object pattern key", key.start);
            expectOp(":");
            entries.push({key: keyNode, pattern: pattern()});
          }
          if (isOp(",")) { index++; continue; }
          expectOp("}");
          return {type: "object", entries};
        }
      }
      throw syntaxError(`Expected a $variable or destructuring pattern but found ${show(token)}`, token.start);
    }

    function stringTerm(token, format = null) {
      const parts = token.parts.map(part => typeof part === "string" ? part : parseTokens(part.tokens, source));
      if (parts.length === 1 && typeof parts[0] === "string" && !format) return {type: "Literal", value: parts[0]};
      return {type: "String", parts, format};
    }

    function primary() {
      const token = tokens[index++];
      switch (token.type) {
        case "number": return {type: "Literal", value: token.value};
        case "string": return stringTerm(token);
        case "format":
          if (peek().type === "string") return stringTerm(tokens[index++], token.value);
          return {type: "Format", name: token.value, start: token.start};
        case "field": return {type: "Index", target: IDENTITY, key: {type: "Literal", value: token.value}};
        case "var":
          if (token.value === "__loc__") return {type: "Literal", value: {file: "<stdin>", line: 1}};
          return {type: "Var", name: token.value, start: token.start};
        case "ident": return word(token);
        case "op":
          switch (token.value) {
            case ".":
              if (peek().type === "string") return {type: "Index", target: IDENTITY, key: stringTerm(tokens[index++])};
              return IDENTITY;
            case "..": return {type: "Recurse"};
            case "(": {
              const inner = pipe();
              expectOp(")");
              return {type: "Paren", body: inner};
            }
            case "[": {
              if (isOp("]")) { index++; return {type: "Array", body: null}; }
              const body = pipe();
              expectOp("]");
              return {type: "Array", body};
            }
            case "{": return objectTerm();
            case "-": {
              const body = postfix();
              return {type: "Neg", body};
            }
          }
      }
      throw unexpected(token);
    }

    function word(token) {
      switch (token.value) {
        case "true": return {type: "Literal", value: true};
        case "false": return {type: "Literal", value: false};
        case "null": return {type: "Literal", value: null};
        case "if": {
          const cond = pipe();
          expectWord("then");
          const then = pipe();
          let otherwise = null;
          if (isWord("elif")) {
            index++;
            otherwise = word({value: "if"});
            return {type: "If", cond, then, otherwise};
          }
          if (isWord("else")) { index++; otherwise = pipe(); }
          expectWord("end");
          return {type: "If", cond, then, otherwise};
        }
        case "try": {
          const body = postfix();
          let handler = null;
          if (isWord("catch")) { index++; handler = postfix(); }
          return {type: "Try", body, handler};
        }
        case "reduce": {
          const source = postfix(false);
          expectWord("as");
          const pat = pattern();
          expectOp("(");
          const init = pipe();
          expectOp(";");
          const update = pipe();
          expectOp(")");
          return {type: "Reduce", source, pattern: pat, init, update};
        }
        case "foreach": {
          const source = postfix(false);
          expectWord("as");
          const pat = pattern();
          expectOp("(");
          const init = pipe();
          expectOp(";");
          const update = pipe();
          let extract = null;
          if (isOp(";")) { index++; extract = pipe(); }
          expectOp(")");
          return {type: "Foreach", source, pattern: pat, init, update, extract};
        }
        case "def": {
          index--;
          const definition = parseDef();
          return {type: "Def", ...definition, rest: pipe()};
        }
        case "label": throw syntaxError("label/break is not supported", token.start);
      }
      if (KEYWORDS.has(token.value)) throw unexpected(token);
      const args = [];
      if (isOp("(")) {
        index++;
        for (;;) {
          args.push(pipe());
          if (isOp(";")) { index++; continue; }
          expectOp(")");
          break;
        }
      }
      return {type: "Call", name: token.value, args, start: token.start};
    }

    function objectTerm() {
      const entries = [];
      if (isOp("}")) { index++; return {type: "Object", entries}; }
      for (;;) {
        const token = peek();
        let key, value = null;
        if (token.type === "var") {
          index++;
          key = {type: "Literal", value: token.value};
          value = token.value === "__loc__" ? {type: "Literal", value: {file: "<stdin>", line: 1}} : {type: "Var", name: token.value, start: token.start};
        } else if (token.type === "ident") {
          index++;
          key = {type: "Literal", value: token.value};
        } else if (token.type === "number") {
          throw syntaxError("Object keys must be strings; use (expr) or \"text\"", token.start);
        } else if (token.type === "string") {
          index++;
          key = stringTerm(token);
        } else if (token.type === "format" && peek(1).type === "string") {
          index += 2;
          key = stringTerm(tokens[index - 1], token.value);
        } else if (isOp("(")) {
          index++;
          key = pipe();
          expectOp(")");
          if (!isOp(":")) throw syntaxError("Expected ':' after a computed object key", peek().start);
        } else throw syntaxError(`Expected an object key but found ${show(token)}`, token.start);
        if (isOp(":")) {
          index++;
          value = objectValue();
        } else if (!value) {
          value = {type: "Index", target: IDENTITY, key};
        }
        entries.push({key, value});
        if (isOp(",")) { index++; continue; }
        expectOp("}");
        return {type: "Object", entries};
      }
    }

    // An object value: pipes allowed, commas end the entry.
    function objectValue() {
      let left = alternative();
      while (isOp("|")) {
        index++;
        left = {type: "Pipe", left, right: alternative()};
      }
      return left;
    }

    const ast = pipe();
    if (peek().type !== "eof") throw unexpected(peek());
    return ast;
  }

  // ---- Static check: undefined functions and variables ----

  function check(node, funcs, vars) {
    if (!node || typeof node !== "object") return;
    const recurse = (child, f = funcs, v = vars) => check(child, f, v);
    switch (node.type) {
      case "Call": {
        const key = `${node.name}/${node.args.length}`;
        if (!funcs.has(key) && !BUILTINS[key]) throw syntaxError(`${key} is not defined`, node.start);
        node.builtin = !funcs.has(key);
        node.args.forEach(arg => recurse(arg));
        return;
      }
      case "Var":
        if (!vars.has(node.name) && node.name !== "ENV") throw syntaxError(`$${node.name} is not defined`, node.start);
        return;
      case "Format":
        if (!FORMATS[node.name]) throw syntaxError(`@${node.name} is not a valid format`, node.start);
        return;
      case "String":
        if (node.format && !FORMATS[node.format]) throw syntaxError(`@${node.format} is not a valid format`, 0);
        node.parts.forEach(part => typeof part !== "string" && recurse(part));
        return;
      case "Def": {
        const inner = new Set(funcs).add(`${node.name}/${node.params.length}`);
        const bodyFuncs = new Set(inner), bodyVars = new Set(vars);
        for (const param of node.params) {
          if (param.startsWith("$")) { bodyVars.add(param.slice(1)); bodyFuncs.add(`${param.slice(1)}/0`); } else bodyFuncs.add(`${param}/0`);
        }
        recurse(node.body, bodyFuncs, bodyVars);
        recurse(node.rest, inner, vars);
        return;
      }
      case "Bind": {
        recurse(node.source);
        const inner = new Set(vars);
        node.patterns.forEach(pat => patternVars(pat, inner, funcs, vars));
        recurse(node.body, funcs, inner);
        return;
      }
      case "Reduce":
      case "Foreach": {
        recurse(node.source);
        recurse(node.init);
        const inner = new Set(vars);
        patternVars(node.pattern, inner, funcs, vars);
        recurse(node.update, funcs, inner);
        if (node.extract) recurse(node.extract, funcs, inner);
        return;
      }
      case "Object":
        node.entries.forEach(entry => { recurse(entry.key); recurse(entry.value); });
        return;
    }
    for (const key of ["target", "key", "from", "to", "body", "left", "right", "cond", "then", "otherwise", "handler"]) if (node[key]) recurse(node[key]);
  }

  function patternVars(pat, out, funcs, vars) {
    if (pat.type === "var") out.add(pat.name);
    else if (pat.type === "array") pat.elements.forEach(element => patternVars(element, out, funcs, vars));
    else for (const entry of pat.entries) {
      check(entry.key, funcs, vars);
      if (entry.bind) out.add(entry.bind);
      if (entry.pattern) patternVars(entry.pattern, out, funcs, vars);
    }
  }

  // ---- Evaluator ----

  let steps = 0;
  function tick() {
    if (++steps > MAX_STEPS) throw new LimitError("The program took too long (step limit reached)");
  }

  const bindVar = (env, name, value) => {
    const vars = Object.create(env.vars);
    vars[name] = value;
    return {vars, funcs: env.funcs};
  };

  function* cartesian(nodes, input, env, at = 0, acc = []) {
    if (at === nodes.length) { yield acc.slice(); return; }
    for (const value of ev(nodes[at], input, env)) {
      acc[at] = value;
      yield* cartesian(nodes, input, env, at + 1, acc);
    }
  }

  // Outputs of `node` with the right operand as the outer loop, like jq.
  function* pairs(left, right, input, env) {
    for (const b of ev(right, input, env)) for (const a of ev(left, input, env)) yield [a, b];
  }

  function first(iterable) {
    for (const value of iterable) return {value};
    return null;
  }

  function* destructure(pat, value, env, input) {
    if (pat.type === "var") { yield bindVar(env, pat.name, value); return; }
    if (pat.type === "array") {
      if (value !== null && !Array.isArray(value)) throw new JqError(`Cannot index ${typeOf(value)} with number`);
      yield* destructureList(pat.elements, 0, value, env, input);
      return;
    }
    yield* destructureEntries(pat.entries, 0, value, env, input);
  }

  function* destructureList(elements, at, value, env, input) {
    if (at === elements.length) { yield env; return; }
    for (const next of destructure(elements[at], index(value, at), env, input)) yield* destructureList(elements, at + 1, value, next, input);
  }

  function* destructureEntries(entries, at, value, env, input) {
    if (at === entries.length) { yield env; return; }
    const entry = entries[at];
    for (const key of ev(entry.key, input, env)) {
      if (typeof key !== "string") throw new JqError(`Cannot index ${typeOf(value)} with ${typeOf(key)}`);
      const item = index(value, key);
      let next = entry.bind ? bindVar(env, entry.bind, item) : env;
      if (entry.pattern) {
        for (const inner of destructure(entry.pattern, item, next, input)) yield* destructureEntries(entries, at + 1, value, inner, input);
      } else yield* destructureEntries(entries, at + 1, value, next, input);
    }
  }

  // Nodes that always produce exactly one output (or raise) are evaluated directly by
  // evOne, without generators. That keeps common programs fast and keeps recursion cheap.
  function isSingle(node) {
    if (node.single !== undefined) return node.single;
    let single = false;
    switch (node.type) {
      case "Identity": case "Literal": case "Var": case "Format": case "Array": single = true; break;
      case "Index": single = isSingle(node.target) && isSingle(node.key); break;
      case "Slice": single = isSingle(node.target) && (!node.from || isSingle(node.from)) && (!node.to || isSingle(node.to)); break;
      case "Binary": case "And": case "Or": single = isSingle(node.left) && isSingle(node.right); break;
      case "Neg": case "Paren": single = isSingle(node.body); break;
      case "Object": single = node.entries.every(entry => isSingle(entry.key) && isSingle(entry.value)); break;
      case "String": single = node.parts.every(part => typeof part === "string" || isSingle(part)); break;
      case "If": single = isSingle(node.cond) && isSingle(node.then) && (!node.otherwise || isSingle(node.otherwise)); break;
      case "Reduce": single = isSingle(node.init); break;
      case "Call": {
        const key = `${node.name}/${node.args.length}`;
        single = node.builtin === true && (SINGLE.has(key) || SIMPLE[key] !== undefined && node.args.every(isSingle));
        break;
      }
    }
    node.single = single;
    return single;
  }

  function evOne(node, input, env) {
    switch (node.type) {
      case "Identity": return input;
      case "Literal": return node.value;
      case "Var": return node.name === "ENV" && !(node.name in env.vars) ? {} : env.vars[node.name];
      case "Format": return format(node.name, input);
      case "Paren": return evOne(node.body, input, env);
      case "Array": {
        if (!node.body) return [];
        const out = [];
        for (const value of ev(node.body, input, env)) out.push(value);
        return out;
      }
      case "Index": {
        const key = evOne(node.key, input, env);
        return index(evOne(node.target, input, env), key);
      }
      case "Slice": {
        const to = node.to ? evOne(node.to, input, env) : null, from = node.from ? evOne(node.from, input, env) : null;
        return sliceOf(evOne(node.target, input, env), from, to);
      }
      case "Binary": {
        const b = evOne(node.right, input, env);
        return binary(node.op, evOne(node.left, input, env), b);
      }
      case "And": return truthy(evOne(node.left, input, env)) && truthy(evOne(node.right, input, env));
      case "Or": return truthy(evOne(node.left, input, env)) || truthy(evOne(node.right, input, env));
      case "Neg": {
        const value = evOne(node.body, input, env);
        if (typeof value !== "number") throw new JqError(`${describe(value)} cannot be negated`);
        return -value;
      }
      case "Object": {
        const out = {};
        for (const entry of node.entries) {
          const key = evOne(entry.key, input, env);
          if (typeof key !== "string") throw new JqError(`Object keys must be strings, not ${describe(key)}`);
          put(out, key, evOne(entry.value, input, env));
        }
        return out;
      }
      case "String": {
        let out = "";
        for (const part of node.parts) out += typeof part === "string" ? part : format(node.format || "text", evOne(part, input, env));
        return out;
      }
      case "If":
        if (truthy(evOne(node.cond, input, env))) return evOne(node.then, input, env);
        return node.otherwise ? evOne(node.otherwise, input, env) : input;
      case "Reduce": return reduce(node, evOne(node.init, input, env), input, env);
      case "Call": {
        const key = `${node.name}/${node.args.length}`;
        if (SIMPLE[key]) return SIMPLE[key](input, ...node.args.map(arg => evOne(arg, input, env)));
        const result = first(BUILTINS[key](input, node.args, env));
        if (!result) throw new JqError(`${key} produced no output`);
        return result.value;
      }
    }
    throw new JqError(`Unknown node ${node.type}`);
  }

  function reduce(node, acc, input, env) {
    for (const item of ev(node.source, input, env)) {
      for (const scope of destructure(node.pattern, item, env, input)) {
        let last = null;
        for (const value of ev(node.update, acc, scope)) last = {value};
        acc = last ? last.value : null;
      }
    }
    return acc;
  }

  // The outputs of `node` as an iterable. Pipes, conditionals, bindings and calls in tail
  // position loop here instead of recursing, so tail recursion runs in constant stack.
  function ev(node, input, env) {
    for (;;) {
      tick();
      if (isSingle(node)) return [evOne(node, input, env)];
      switch (node.type) {
        case "Paren":
          node = node.body;
          continue;
        case "Pipe":
          if (!isSingle(node.left)) break;
          input = evOne(node.left, input, env);
          node = node.right;
          continue;
        case "If":
          if (!isSingle(node.cond)) break;
          if (truthy(evOne(node.cond, input, env))) node = node.then;
          else if (node.otherwise) node = node.otherwise;
          else return [input];
          continue;
        case "Bind":
          if (!isSingle(node.source) || node.patterns[0].type !== "var") break;
          env = bindVar(env, node.patterns[0].name, evOne(node.source, input, env));
          node = node.body;
          continue;
        case "Def":
          env = defineFunction(node, env);
          node = node.rest;
          continue;
        case "Call": {
          const fn = lookup(env, `${node.name}/${node.args.length}`);
          if (fn && fn.closure) {
            env = fn.env;
            node = fn.node;
            continue;
          }
          const scope = fn && !fn.value && userScope(fn, node.args, input, env);
          if (scope) {
            env = scope;
            node = fn.body;
            continue;
          }
          return call(node, input, env, false);
        }
      }
      return evStream(node, input, env);
    }
  }

  function defineFunction(node, env) {
    const funcs = Object.create(env.funcs);
    const scope = {vars: env.vars, funcs};
    funcs[`${node.name}/${node.params.length}`] = {params: node.params, body: node.body, env: scope};
    return scope;
  }

  function* evStream(node, input, env) {
    switch (node.type) {
      case "Recurse":
        for (const [, value] of walkPaths(input)) yield value;
        return;
      case "Index":
        for (const key of ev(node.key, input, env)) for (const target of ev(node.target, input, env)) yield index(target, key);
        return;
      case "Slice":
        for (const to of node.to ? ev(node.to, input, env) : [null]) {
          for (const from of node.from ? ev(node.from, input, env) : [null]) {
            for (const target of ev(node.target, input, env)) yield sliceOf(target, from, to);
          }
        }
        return;
      case "Iterate":
        for (const target of ev(node.target, input, env)) yield* iterate(target);
        return;
      case "Pipe":
        for (const value of ev(node.left, input, env)) yield* ev(node.right, value, env);
        return;
      case "Comma":
        yield* ev(node.left, input, env);
        yield* ev(node.right, input, env);
        return;
      case "Neg":
        for (const value of ev(node.body, input, env)) {
          if (typeof value !== "number") throw new JqError(`${describe(value)} cannot be negated`);
          yield -value;
        }
        return;
      case "Binary":
        for (const [a, b] of pairs(node.left, node.right, input, env)) yield binary(node.op, a, b);
        return;
      case "And":
        for (const a of ev(node.left, input, env)) {
          if (!truthy(a)) { yield false; continue; }
          for (const b of ev(node.right, input, env)) yield truthy(b);
        }
        return;
      case "Or":
        for (const a of ev(node.left, input, env)) {
          if (truthy(a)) { yield true; continue; }
          for (const b of ev(node.right, input, env)) yield truthy(b);
        }
        return;
      case "Alt": {
        let any = false;
        try {
          for (const value of ev(node.left, input, env)) {
            if (truthy(value)) { any = true; yield value; }
          }
        } catch (error) {
          if (!(error instanceof JqError)) throw error;
        }
        if (!any) yield* ev(node.right, input, env);
        return;
      }
      case "Object":
        yield* objectProduct(node.entries, 0, input, env, {});
        return;
      case "String":
        yield* stringProduct(node, node.parts.length - 1, input, env, "");
        return;
      case "If":
        for (const cond of ev(node.cond, input, env)) {
          if (truthy(cond)) yield* ev(node.then, input, env);
          else if (node.otherwise) yield* ev(node.otherwise, input, env);
          else yield input;
        }
        return;
      case "Try":
        try {
          for (const value of ev(node.body, input, env)) yield value;
        } catch (error) {
          if (!(error instanceof JqError)) throw error;
          if (node.handler) yield* ev(node.handler, error.value, env);
        }
        return;
      case "Reduce":
        for (const init of ev(node.init, input, env)) yield reduce(node, init, input, env);
        return;
      case "Foreach":
        for (const init of ev(node.init, input, env)) {
          let state = init;
          for (const item of ev(node.source, input, env)) {
            for (const scope of destructure(node.pattern, item, env, input)) {
              for (const value of ev(node.update, state, scope)) {
                state = value;
                if (node.extract) yield* ev(node.extract, value, scope);
                else yield value;
              }
            }
          }
        }
        return;
      case "Bind":
        for (const value of ev(node.source, input, env)) {
          for (const scope of destructure(node.patterns[0], value, env, input)) yield* ev(node.body, input, scope);
        }
        return;
      case "Assign":
        yield* assign(node, input, env);
        return;
    }
    throw new JqError(`Unknown node ${node.type}`);
  }

  function* objectProduct(entries, at, input, env, acc) {
    if (at === entries.length) { yield Object.assign({}, acc); return; }
    const entry = entries[at];
    for (const key of ev(entry.key, input, env)) {
      if (typeof key !== "string") throw new JqError(`Object keys must be strings, not ${describe(key)}`);
      for (const value of ev(entry.value, input, env)) {
        const next = Object.assign({}, acc);
        put(next, key, value);
        yield* objectProduct(entries, at + 1, input, env, next);
      }
    }
  }

  // The last interpolation is the outer loop, as in jq.
  function* stringProduct(node, at, input, env, suffix) {
    if (at < 0) { yield suffix; return; }
    const part = node.parts[at];
    if (typeof part === "string") { yield* stringProduct(node, at - 1, input, env, part + suffix); return; }
    for (const value of ev(part, input, env)) yield* stringProduct(node, at - 1, input, env, format(node.format || "text", value) + suffix);
  }

  function lookup(env, key) {
    const fn = env.funcs[key];
    return fn === Object.prototype[key] ? undefined : fn;
  }

  // Returns an iterable of outputs (or of [path, value] pairs in path mode).
  function call(node, input, env, pathMode) {
    const key = `${node.name}/${node.args.length}`;
    const fn = lookup(env, key);
    if (fn) {
      if (fn.closure) return (pathMode ? paths : ev)(fn.node, input, fn.env);
      if (fn.value) return pathMode ? invalidPath(fn.value.value) : [fn.value.value];
      return callUser(fn, node.args, input, env, pathMode);
    }
    const builtin = pathMode ? PATH_BUILTINS[key] : BUILTINS[key];
    if (builtin) return builtin(input, node.args, env);
    if (pathMode && BUILTINS[key]) return invalidPaths(BUILTINS[key](input, node.args, env));
    throw new JqError(`${key} is not defined`);
  }

  function bindValue(current, name, value) {
    const next = bindVar(current, name, value);
    next.funcs = Object.create(current.funcs);
    next.funcs[`${name}/0`] = {value: {value}};
    return next;
  }

  // The scope for a call of a user function, or null when a $param has several values.
  function userScope(fn, args, input, callerEnv) {
    const funcs = Object.create(fn.env.funcs);
    let scope = {vars: fn.env.vars, funcs};
    for (let i = 0; i < fn.params.length; i++) {
      const param = fn.params[i];
      if (!param.startsWith("$")) funcs[`${param}/0`] = {closure: true, node: args[i], env: callerEnv};
      else if (!isSingle(args[i])) return null;
    }
    for (let i = 0; i < fn.params.length; i++) {
      if (fn.params[i].startsWith("$")) scope = bindValue(scope, fn.params[i].slice(1), evOne(args[i], input, callerEnv));
    }
    return scope;
  }

  function callUser(fn, args, input, callerEnv, pathMode) {
    const run = pathMode ? paths : ev;
    const ready = userScope(fn, args, input, callerEnv);
    if (ready) return run(fn.body, input, ready);
    const funcs = Object.create(fn.env.funcs);
    const scope = {vars: fn.env.vars, funcs};
    const valueParams = [];
    fn.params.forEach((param, i) => {
      if (param.startsWith("$")) valueParams.push([param.slice(1), args[i]]);
      else funcs[`${param}/0`] = {closure: true, node: args[i], env: callerEnv};
    });
    const bindOne = bindValue;
    return (function* bindValues(at, current) {
      if (at === valueParams.length) { yield* run(fn.body, input, current); return; }
      const [name, arg] = valueParams[at];
      for (const value of ev(arg, input, callerEnv)) yield* bindValues(at + 1, bindOne(current, name, value));
    })(0, scope);
  }

  // ---- Path expressions (path(f), del, |=, paths) ----

  function* invalidPath(value) {
    throw new JqError(`Invalid path expression with result ${toJson(value).slice(0, 40)}`);
  }
  function* invalidPaths(values) {
    for (const value of values) yield* invalidPath(value);
  }

  function* paths(node, input, env, base = []) {
    tick();
    switch (node.type) {
      case "Identity": yield [[], input]; return;
      case "Paren": yield* paths(node.body, input, env); return;
      case "Recurse": yield* walkPaths(input); return;
      case "Index":
        for (const key of ev(node.key, input, env)) {
          for (const [path, value] of paths(node.target, input, env)) yield [path.concat([key]), value === null ? null : index(value, key)];
        }
        return;
      case "Slice":
        for (const to of node.to ? ev(node.to, input, env) : [null]) {
          for (const from of node.from ? ev(node.from, input, env) : [null]) {
            for (const [path, value] of paths(node.target, input, env)) yield [path.concat([{start: from, end: to}]), value === null ? null : sliceOf(value, from, to)];
          }
        }
        return;
      case "Iterate":
        for (const [path, value] of paths(node.target, input, env)) {
          if (value === null) continue;
          if (Array.isArray(value)) for (let i = 0; i < value.length; i++) yield [path.concat(i), value[i]];
          else if (isObject(value)) for (const key of Object.keys(value)) yield [path.concat(key), value[key]];
          else iterate(value);
        }
        return;
      case "Pipe":
        for (const [left, value] of paths(node.left, input, env)) {
          for (const [right, inner] of paths(node.right, value, env)) yield [left.concat(right), inner];
        }
        return;
      case "Comma":
        yield* paths(node.left, input, env);
        yield* paths(node.right, input, env);
        return;
      case "If":
        for (const cond of ev(node.cond, input, env)) {
          if (truthy(cond)) yield* paths(node.then, input, env);
          else if (node.otherwise) yield* paths(node.otherwise, input, env);
          else yield [[], input];
        }
        return;
      case "Alt": {
        let any = false;
        try {
          for (const item of paths(node.left, input, env)) if (truthy(item[1])) { any = true; yield item; }
        } catch (error) {
          if (!(error instanceof JqError)) throw error;
        }
        if (!any) yield* paths(node.right, input, env);
        return;
      }
      case "Try":
        try {
          for (const item of paths(node.body, input, env)) yield item;
        } catch (error) {
          if (!(error instanceof JqError)) throw error;
          if (node.handler) for (const value of ev(node.handler, error.value, env)) yield* invalidPath(value);
        }
        return;
      case "Bind":
        for (const value of ev(node.source, input, env)) {
          for (const scope of destructure(node.patterns[0], value, env, input)) yield* paths(node.body, input, scope);
        }
        return;
      case "Def":
        yield* paths(node.rest, input, defineFunction(node, env));
        return;
      case "Reduce": {
        for (const init of paths(node.init, input, env)) {
          let acc = init;
          for (const item of ev(node.source, input, env)) {
            for (const scope of destructure(node.pattern, item, env, input)) {
              let last = null;
              for (const [path, value] of paths(node.update, acc[1], scope)) last = [acc[0].concat(path), value];
              acc = last || [acc[0], null];
            }
          }
          yield acc;
        }
        return;
      }
      case "Literal":
        if (node.value === null) { yield [[], null]; return; }
        break;
      case "Call":
        yield* call(node, input, env, true);
        return;
    }
    for (const value of ev(node, input, env)) yield* invalidPath(value);
  }

  function pathList(node, input, env) {
    const out = [];
    for (const [path] of paths(node, input, env)) out.push(path);
    return out;
  }

  function* assign(node, input, env) {
    const {op, left, right} = node;
    if (op === "|=") {
      let out = input;
      const removed = [];
      for (const path of pathList(left, input, env)) {
        const result = first(ev(right, getPath(out, path), env));
        if (result) out = setPath(out, path, result.value);
        else removed.push(path);
      }
      yield removed.length ? deletePaths(out, removed) : out;
      return;
    }
    const targets = pathList(left, input, env);
    for (const value of ev(right, input, env)) {
      let out = input;
      for (const path of targets) {
        const old = getPath(out, path);
        let next;
        if (op === "=") next = value;
        else if (op === "//=") next = truthy(old) ? old : value;
        else next = binary(op.slice(0, -1), old, value);
        out = setPath(out, path, next);
      }
      yield out;
    }
  }

  // ---- Formats ----

  function utf8(text) { return new TextEncoder().encode(text); }

  const BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  function toBase64(bytes) {
    let out = "";
    for (let i = 0; i < bytes.length; i += 3) {
      const n = bytes[i] << 16 | (bytes[i + 1] ?? 0) << 8 | (bytes[i + 2] ?? 0);
      out += BASE64[n >> 18 & 63] + BASE64[n >> 12 & 63] + (i + 1 < bytes.length ? BASE64[n >> 6 & 63] : "=") + (i + 2 < bytes.length ? BASE64[n & 63] : "=");
    }
    return out;
  }

  function fromBase64(text) {
    const clean = text.replace(/[\s=]+/g, "").replace(/-/g, "+").replace(/_/g, "/");
    const bytes = [];
    let buffer = 0, bits = 0;
    for (const char of clean) {
      const value = BASE64.indexOf(char);
      if (value < 0) throw new JqError(`${describe(text)} is not valid base64 data`);
      buffer = buffer << 6 | value;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        bytes.push(buffer >> bits & 255);
      }
    }
    return new TextDecoder().decode(new Uint8Array(bytes));
  }

  function rowCells(name, value, cell) {
    if (!Array.isArray(value)) throw new JqError(`${describe(value)} cannot be ${name}-formatted, only an array can be`);
    return value.map(item => {
      if (item === null) return "";
      if (typeof item === "boolean" || typeof item === "number") return toJson(item);
      if (typeof item === "string") return cell(item);
      throw new JqError(`${describe(item)} is not valid in a ${name} row`);
    });
  }

  const FORMATS = {
    text: toText,
    json: toJson,
    csv: value => rowCells("csv", value, text => `"${text.replace(/"/g, "\"\"")}"`).join(","),
    tsv: value => rowCells("tsv", value, text => text.replace(/\\/g, "\\\\").replace(/\t/g, "\\t").replace(/\n/g, "\\n").replace(/\r/g, "\\r")).join("\t"),
    html: value => toText(value).replace(/[<>&'"]/g, char => ({"<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&#39;", "\"": "&quot;"})[char]),
    uri: value => Array.from(utf8(toText(value)), byte => /[A-Za-z0-9\-_.~]/.test(String.fromCharCode(byte)) ? String.fromCharCode(byte) : "%" + byte.toString(16).toUpperCase().padStart(2, "0")).join(""),
    sh: value => (Array.isArray(value) ? value : [value]).map(item => {
      if (typeof item === "string") return `'${item.replace(/'/g, "'\\''")}'`;
      if (isObject(item) || Array.isArray(item)) throw new JqError(`${describe(item)} can not be escaped for shell`);
      return toJson(item);
    }).join(" "),
    base64: value => toBase64(utf8(toText(value))),
    base64d: value => fromBase64(toText(value))
  };

  function format(name, value) {
    const fn = FORMATS[name];
    if (!fn) throw new JqError(`${name} is not a valid format`);
    return fn(value);
  }

  // ---- Regular expressions (JavaScript RegExp syntax; jq flags g i x n s l p) ----

  const regexCache = new Map();
  function regex(pattern, flags) {
    if (typeof pattern !== "string") throw new JqError(`${describe(pattern)} cannot be matched, as it is not a string`);
    if (flags !== null && typeof flags !== "string") throw new JqError(`${describe(flags)} is not a string`);
    const key = pattern + "\u0000" + (flags || "");
    let entry = regexCache.get(key);
    if (entry) return entry;
    let source = pattern, js = "";
    const options = {global: false, skipEmpty: false};
    for (const flag of flags || "") {
      if (flag === "g") options.global = true;
      else if (flag === "i") js += "i";
      else if (flag === "x") source = source.replace(/\\#/g, "\u0000").replace(/#.*$/gm, "").replace(/\s+/g, "").replace(/\u0000/g, "\\#");
      else if (flag === "n") options.skipEmpty = true;
      else if (flag === "s") js += "s";
      else if (flag === "p") js += "sm";
      else if (flag === "l") { /* longest match: not available */ }
      else throw new JqError(`${flags} is not a valid modifier string`);
    }
    try {
      entry = {re: new RegExp(source, [...new Set(js + "gdu")].join("")), ...options};
    } catch {
      try {
        entry = {re: new RegExp(source, [...new Set(js + "gd")].join("")), ...options};
      } catch (error) {
        throw new JqError(`${pattern} (at offset 0) is not a valid regex: ${error.message}`);
      }
    }
    if (regexCache.size > 100) regexCache.clear();
    regexCache.set(key, entry);
    return entry;
  }

  function matches(text, pattern, flags, forceGlobal = false) {
    if (typeof text !== "string") throw new JqError(`${describe(text)} cannot be matched, as it is not a string`);
    const {re, global, skipEmpty} = regex(pattern, flags);
    const out = [];
    re.lastIndex = 0;
    let match;
    const codepoint = at => [...text.slice(0, at)].length;
    while ((match = re.exec(text))) {
      if (match[0] === "") re.lastIndex++;
      if (!(skipEmpty && match[0] === "")) {
        // Group names by position: the first group whose span matches each named group.
        const names = [];
        for (const [name, span] of Object.entries(match.indices.groups || {})) {
          for (let i = 1; i < match.length; i++) {
            const at = match.indices[i];
            if (names[i] === undefined && (at === span || at && span && at[0] === span[0] && at[1] === span[1])) { names[i] = name; break; }
          }
        }
        const captures = [];
        for (let i = 1; i < match.length; i++) {
          const string = match[i] ?? null;
          captures.push({offset: string === null ? -1 : codepoint(match.indices[i][0]), length: string === null ? 0 : [...string].length, string, name: names[i] ?? null});
        }
        out.push({offset: codepoint(match.index), length: [...match[0]].length, string: match[0], captures, _index: match.index});
      }
      if (!(global || forceGlobal)) break;
      if (re.lastIndex > text.length) break;
    }
    return out;
  }

  function captureObject(match) {
    const out = {};
    for (const capture of match.captures) if (capture.name) put(out, capture.name, capture.string);
    return out;
  }

  function* regexArgs(args, input, env) {
    if (args.length === 1) {
      for (const value of ev(args[0], input, env)) {
        if (Array.isArray(value)) yield [value[0], value[1] ?? null];
        else yield [value, null];
      }
      return;
    }
    for (const flags of ev(args[1], input, env)) for (const pattern of ev(args[0], input, env)) yield [pattern, flags];
  }

  function* substitute(input, args, env, global) {
    if (typeof input !== "string") throw new JqError(`${describe(input)} cannot be matched, as it is not a string`);
    const flagValues = args.length > 2 ? [...ev(args[2], input, env)] : [null];
    for (const flags of flagValues) {
      for (const pattern of ev(args[0], input, env)) {
        const found = matches(input, pattern, (flags || "") + (global ? "g" : ""));
        let out = "", last = 0;
        for (const match of found) {
          const result = first(ev(args[1], captureObject(match), env));
          const replacement = result ? result.value : "";
          if (typeof replacement !== "string") throw new JqError(`${describe(replacement)} cannot be added to a string`);
          out += input.slice(last, match._index) + replacement;
          last = match._index + match.string.length;
        }
        yield out + input.slice(last);
      }
    }
  }

  // ---- Dates (UTC) ----

  function toDateString(seconds) {
    if (typeof seconds !== "number") throw new JqError("strftime/1 requires parsed datetime inputs");
    return new Date(Math.floor(seconds) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function fromDateString(text) {
    if (typeof text !== "string") throw new JqError(`${describe(text)} cannot be parsed as a date`);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/.test(text)) throw new JqError(`date "${text}" does not match format "%Y-%m-%dT%H:%M:%SZ"`);
    return Math.floor(Date.parse(text) / 1000);
  }

  function brokenDown(seconds) {
    const date = new Date(seconds * 1000);
    const start = Date.UTC(date.getUTCFullYear(), 0, 1);
    return [date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), date.getUTCHours(), date.getUTCMinutes(),
      date.getUTCSeconds() + (seconds % 1 + 1) % 1, date.getUTCDay(), Math.floor((date - start) / 86400000)];
  }

  function fromBrokenDown(parts) {
    if (!Array.isArray(parts) || parts.length < 6 || parts.slice(0, 6).some(part => typeof part !== "number")) throw new JqError("mktime requires array of 6 numbers");
    return Math.floor(Date.UTC(parts[0], parts[1], parts[2], parts[3], parts[4], Math.floor(parts[5])) / 1000);
  }

  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function strftime(value, pattern) {
    if (typeof pattern !== "string") throw new JqError("strftime/1 requires a string format");
    const seconds = Array.isArray(value) ? fromBrokenDown(value) : value;
    if (typeof seconds !== "number") throw new JqError("strftime/1 requires parsed datetime inputs");
    const [year, month, day, hour, minute, second, weekday, yearDay] = brokenDown(seconds);
    const pad = (n, size = 2) => String(Math.floor(n)).padStart(size, "0");
    const map = {Y: year, m: pad(month + 1), d: pad(day), e: String(day).padStart(2, " "), H: pad(hour), M: pad(minute), S: pad(second),
      j: pad(yearDay + 1, 3), a: DAYS[weekday].slice(0, 3), A: DAYS[weekday], b: MONTHS[month].slice(0, 3), h: MONTHS[month].slice(0, 3), B: MONTHS[month],
      y: pad(year % 100), Z: "UTC", z: "+0000", s: Math.floor(seconds), u: weekday || 7, w: weekday, "%": "%",
      I: pad(hour % 12 || 12), p: hour < 12 ? "AM" : "PM", T: `${pad(hour)}:${pad(minute)}:${pad(second)}`, D: `${pad(month + 1)}/${pad(day)}/${pad(year % 100)}`,
      F: `${year}-${pad(month + 1)}-${pad(day)}`, c: `${DAYS[weekday].slice(0, 3)} ${MONTHS[month].slice(0, 3)} ${String(day).padStart(2, " ")} ${pad(hour)}:${pad(minute)}:${pad(second)} ${year}`};
    return pattern.replace(/%([A-Za-z%])/g, (all, code) => map[code] !== undefined ? String(map[code]) : all);
  }

  // ---- Builtins ----
  // Each builtin is a generator (input, argNodes, env). `simple` wraps a function of the input
  // and the argument values (cartesian product over the argument streams).

  const BUILTINS = {};
  const PATH_BUILTINS = {};
  const SIMPLE = {};
  // Builtins defined as generators that still always produce exactly one output.
  const SINGLE = new Set(["map/1", "map_values/1", "sort_by/1", "group_by/1", "unique_by/1", "min_by/1", "max_by/1", "with_entries/1",
    "del/1", "pick/1", "add/1", "any/1", "all/1", "any/2", "all/2", "isempty/1"]);
  function simple(name, arity, fn) {
    SIMPLE[`${name}/${arity}`] = fn;
    BUILTINS[`${name}/${arity}`] = function* (input, args, env) {
      if (!arity) { yield fn(input); return; }
      for (const values of cartesian(args, input, env)) yield fn(input, ...values);
    };
  }
  function define(name, arity, fn) { BUILTINS[`${name}/${arity}`] = fn; }

  const requireString = (name, value) => {
    if (typeof value !== "string") throw new JqError(`${name} input must be a string`);
    return value;
  };
  const requireNumber = (name, value) => {
    if (typeof value !== "number") throw new JqError(`${describe(value)} number required`);
    return value;
  };
  const requireArray = (name, value) => {
    if (!Array.isArray(value)) throw new JqError(`Cannot ${name} ${describe(value)}${Array.isArray(value) ? "" : ", an array is required"}`);
    return value;
  };

  function keyed(input, node, env) {
    if (!Array.isArray(input)) throw new JqError(`Cannot index ${typeOf(input)} with number`);
    return input.map((item, i) => ({item, key: [...ev(node, item, env)], i})).sort((a, b) => compare(a.key, b.key) || a.i - b.i);
  }
  function groups(sorted) {
    const out = [];
    for (const entry of sorted) {
      const last = out[out.length - 1];
      if (last && equal(last.key, entry.key)) last.items.push(entry.item);
      else out.push({key: entry.key, items: [entry.item]});
    }
    return out;
  }
  function extremeBy(input, node, env, max) {
    const list = keyed(input, node, env);
    if (!list.length) return null;
    if (!max) return list[0].item;
    return list[list.length - 1].item;
  }

  function flatten(list, levels) {
    const out = [];
    for (const item of list) {
      if (Array.isArray(item) && levels > 0) out.push(...flatten(item, levels - 1));
      else out.push(item);
    }
    return out;
  }

  function* range(from, upto, by) {
    if ([from, upto, by].some(n => typeof n !== "number")) throw new JqError("Range bounds must be numeric");
    if (by > 0) for (let n = from; n < upto; n += by) { tick(); yield n; }
    else if (by < 0) for (let n = from; n > upto; n += by) { tick(); yield n; }
  }

  function toNumber(value) {
    if (typeof value === "number") return value;
    if (typeof value === "string" && /^\s*-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\s*$/.test(value)) return Number(value);
    if (typeof value === "string" && /^\s*(nan|-?infinity)\s*$/i.test(value)) return value.trim().toLowerCase() === "nan" ? NaN : value.includes("-") ? -Infinity : Infinity;
    throw new JqError(`Cannot parse '${typeof value === "string" ? value : toJson(value)}' as JSON`);
  }

  function fromEntries(list) {
    if (!Array.isArray(list)) throw new JqError(`Cannot iterate over ${describe(list)}`);
    const out = {};
    for (const entry of list) {
      if (!isObject(entry)) throw new JqError(`Cannot index ${typeOf(entry)} with "key"`);
      const pick = names => names.find(name => own(entry, name) && entry[name] !== null && entry[name] !== false);
      const keyName = pick(["key", "k", "name", "Name", "Key", "K"]);
      let key = keyName ? entry[keyName] : null;
      if (typeof key !== "string") key = key === null ? "null" : toJson(key);
      const valueName = ["value", "v", "Value", "V"].find(name => own(entry, name));
      put(out, key, valueName ? entry[valueName] : null);
    }
    return out;
  }

  // Type selectors work as path expressions too (paths(numbers), del(.. | nulls)).
  const SELECTORS = {
    values: value => value !== null, nulls: value => value === null, booleans: value => typeof value === "boolean",
    numbers: value => typeof value === "number", strings: value => typeof value === "string", arrays: Array.isArray,
    objects: isObject, iterables: value => Array.isArray(value) || isObject(value), scalars: value => !Array.isArray(value) && !isObject(value)
  };
  for (const [name, test] of Object.entries(SELECTORS)) {
    define(name, 0, function* (input) { if (test(input)) yield input; });
    PATH_BUILTINS[`${name}/0`] = function* (input) { if (test(input)) yield [[], input]; };
  }

  define("empty", 0, function* () {});
  PATH_BUILTINS["empty/0"] = function* () {};
  define("error", 0, function* (input) { throw new JqError(input); });
  define("error", 1, function* (input, args, env) { for (const value of ev(args[0], input, env)) throw new JqError(value); });
  PATH_BUILTINS["error/0"] = BUILTINS["error/0"];
  PATH_BUILTINS["error/1"] = BUILTINS["error/1"];
  simple("not", 0, value => !truthy(value));
  simple("length", 0, length);
  simple("utf8bytelength", 0, value => utf8(requireString("utf8bytelength", value)).length);
  simple("keys", 0, value => keysOf(value, true));
  simple("keys_unsorted", 0, value => keysOf(value, false));
  simple("has", 1, (value, key) => {
    if (isObject(value) && typeof key === "string") return own(value, key);
    if (Array.isArray(value) && typeof key === "number") return key >= 0 && key < value.length;
    throw new JqError(`Cannot check whether ${typeOf(value)} has a ${typeOf(key)} key`);
  });
  define("in", 1, function* (input, args, env) {
    for (const object of ev(args[0], input, env)) yield* BUILTINS["has/1"](object, [{type: "Literal", value: input}], env);
  });
  simple("contains", 1, (value, other) => contains(value, other));
  simple("inside", 1, (value, other) => contains(other, value));
  define("map", 1, function* (input, args, env) {
    const out = [];
    for (const item of iterate(input)) for (const value of ev(args[0], item, env)) out.push(value);
    yield out;
  });
  define("map_values", 1, function* (input, args, env) {
    if (Array.isArray(input)) {
      const out = [];
      for (const item of input) { const result = first(ev(args[0], item, env)); if (result) out.push(result.value); }
      yield out;
    } else if (isObject(input)) {
      const out = {};
      for (const key of Object.keys(input)) { const result = first(ev(args[0], input[key], env)); if (result) put(out, key, result.value); }
      yield out;
    } else iterate(input);
  });
  define("select", 1, function* (input, args, env) { for (const value of ev(args[0], input, env)) if (truthy(value)) yield input; });
  PATH_BUILTINS["select/1"] = function* (input, args, env) { for (const value of ev(args[0], input, env)) if (truthy(value)) yield [[], input]; };

  // recurse(f): pre-order, depth first, without deep JS recursion.
  function* recurseWith(input, node, env, cond, pathMode) {
    const stack = [[pathMode ? [[], input] : input]];
    while (stack.length) {
      const top = stack[stack.length - 1];
      if (!top.length) { stack.pop(); continue; }
      const item = top.shift();
      yield item;
      const value = pathMode ? item[1] : item;
      const next = [];
      const source = pathMode ? paths(node, value, env) : ev(node, value, env);
      for (const child of source) {
        const childValue = pathMode ? child[1] : child;
        if (cond && !truthy(first(ev(cond, childValue, env))?.value)) continue;
        next.push(pathMode ? [item[0].concat(child[0]), child[1]] : child);
        if (stack.length > 100000) throw new LimitError("recurse is too deep");
      }
      stack.push(next);
    }
  }
  const OPTIONAL_ITERATE = {type: "Try", body: {type: "Iterate", target: IDENTITY}, handler: null};
  define("recurse", 0, function* (input) { for (const [, value] of walkPaths(input)) yield value; });
  define("recurse", 1, (input, args, env) => recurseWith(input, args[0], env, null, false));
  define("recurse", 2, (input, args, env) => recurseWith(input, args[0], env, args[1], false));
  PATH_BUILTINS["recurse/0"] = input => walkPaths(input);
  PATH_BUILTINS["recurse/1"] = (input, args, env) => recurseWith(input, args[0], env, null, true);
  PATH_BUILTINS["recurse/2"] = (input, args, env) => recurseWith(input, args[0], env, args[1], true);

  define("path", 1, function* (input, args, env) { for (const [path] of paths(args[0], input, env)) yield path; });
  define("paths", 0, function* (input) { for (const [path] of walkPaths(input)) if (path.length) yield path; });
  define("paths", 1, function* (input, args, env) {
    for (const [path, value] of walkPaths(input)) if (path.length && truthy(first(ev(args[0], value, env))?.value)) yield path;
  });
  define("leaf_paths", 0, function* (input) { for (const [path, value] of walkPaths(input)) if (path.length && SELECTORS.scalars(value)) yield path; });
  simple("getpath", 1, (value, path) => {
    if (!Array.isArray(path)) throw new JqError("Path must be specified as an array");
    try { return getPath(value, path); } catch (error) { if (error instanceof JqError) return null; throw error; }
  });
  PATH_BUILTINS["getpath/1"] = function* (input, args, env) {
    for (const path of ev(args[0], input, env)) {
      if (!Array.isArray(path)) throw new JqError("Path must be specified as an array");
      yield [path, getPath(input, path)];
    }
  };
  simple("setpath", 2, (value, path, replacement) => {
    if (!Array.isArray(path)) throw new JqError("Path must be specified as an array");
    return setPath(value, path, replacement);
  });
  simple("delpaths", 1, (value, list) => {
    if (!Array.isArray(list)) throw new JqError("Paths must be specified as an array");
    return deletePaths(value, list);
  });
  define("del", 1, function* (input, args, env) { yield deletePaths(input, pathList(args[0], input, env)); });
  define("pick", 1, function* (input, args, env) {
    let out = null;
    for (const path of pathList(args[0], input, env)) out = setPath(out, path, getPath(input, path));
    yield out;
  });
  simple("to_entries", 0, value => keysOf(value, false).map(key => ({key, value: value[key]})));
  simple("from_entries", 0, fromEntries);
  define("with_entries", 1, function* (input, args, env) {
    const out = [];
    for (const key of keysOf(input, false)) for (const value of ev(args[0], {key, value: input[key]}, env)) out.push(value);
    yield fromEntries(out);
  });
  simple("add", 0, value => {
    let acc = null;
    for (const item of iterate(value)) acc = binary("+", acc, item);
    return acc;
  });
  define("add", 1, function* (input, args, env) {
    let acc = null;
    for (const item of ev(args[0], input, env)) acc = binary("+", acc, item);
    yield acc;
  });
  simple("any", 0, value => iterate(value).some(truthy));
  simple("all", 0, value => iterate(value).every(truthy));
  define("any", 1, function* (input, args, env) {
    for (const item of iterate(input)) for (const value of ev(args[0], item, env)) if (truthy(value)) { yield true; return; }
    yield false;
  });
  define("all", 1, function* (input, args, env) {
    for (const item of iterate(input)) for (const value of ev(args[0], item, env)) if (!truthy(value)) { yield false; return; }
    yield true;
  });
  define("any", 2, function* (input, args, env) {
    for (const item of ev(args[0], input, env)) for (const value of ev(args[1], item, env)) if (truthy(value)) { yield true; return; }
    yield false;
  });
  define("all", 2, function* (input, args, env) {
    for (const item of ev(args[0], input, env)) for (const value of ev(args[1], item, env)) if (!truthy(value)) { yield false; return; }
    yield true;
  });
  define("isempty", 1, function* (input, args, env) { yield !first(ev(args[0], input, env)); });
  simple("flatten", 0, value => flatten(requireArray("flatten", value), Infinity));
  simple("flatten", 1, (value, levels) => {
    if (levels < 0) throw new JqError("flatten depth must not be negative");
    return flatten(requireArray("flatten", value), levels);
  });
  define("range", 1, function* (input, args, env) { for (const upto of ev(args[0], input, env)) yield* range(0, upto, 1); });
  define("range", 2, function* (input, args, env) {
    for (const from of ev(args[0], input, env)) for (const upto of ev(args[1], input, env)) yield* range(from, upto, 1);
  });
  define("range", 3, function* (input, args, env) {
    for (const from of ev(args[0], input, env)) for (const upto of ev(args[1], input, env)) for (const by of ev(args[2], input, env)) yield* range(from, upto, by);
  });
  for (const name of ["floor", "ceil", "sqrt", "exp", "log", "log2", "log10", "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "cbrt", "trunc"]) {
    simple(name, 0, value => Math[name](requireNumber(name, value)));
  }
  simple("round", 0, value => { const n = requireNumber("round", value); return Math.sign(n) * Math.round(Math.abs(n)); });
  simple("fabs", 0, value => Math.abs(requireNumber("fabs", value)));
  simple("exp10", 0, value => 10 ** requireNumber("exp10", value));
  simple("exp2", 0, value => 2 ** requireNumber("exp2", value));
  simple("abs", 0, value => {
    if (typeof value !== "number") throw new JqError(`${describe(value)} has no absolute value`);
    return value < 0 ? -value : value;
  });
  simple("pow", 2, (input, a, b) => requireNumber("pow", a) ** requireNumber("pow", b));
  simple("log", 0, value => Math.log(requireNumber("log", value)));
  simple("infinite", 0, () => Infinity);
  simple("nan", 0, () => NaN);
  simple("isinfinite", 0, value => requireNumber("isinfinite", value) === Infinity || value === -Infinity);
  simple("isnan", 0, value => Number.isNaN(requireNumber("isnan", value)));
  simple("isnormal", 0, value => Number.isFinite(requireNumber("isnormal", value)) && value !== 0);
  simple("tostring", 0, toText);
  simple("tonumber", 0, toNumber);
  simple("type", 0, typeOf);
  simple("toarray", 0, value => Array.isArray(value) ? value : [value]);
  simple("sort", 0, value => requireArray("sort", value).slice().sort(compare));
  define("sort_by", 1, function* (input, args, env) { yield keyed(input, args[0], env).map(entry => entry.item); });
  define("group_by", 1, function* (input, args, env) { yield groups(keyed(input, args[0], env)).map(group => group.items); });
  simple("unique", 0, value => groups(requireArray("unique", value).map((item, i) => ({item, key: item, i})).sort((a, b) => compare(a.key, b.key))).map(group => group.items[0]));
  define("unique_by", 1, function* (input, args, env) { yield groups(keyed(input, args[0], env)).map(group => group.items[0]); });
  simple("min", 0, value => { const list = requireArray("min", value); return list.length ? list.reduce((best, item) => compare(item, best) < 0 ? item : best) : null; });
  simple("max", 0, value => { const list = requireArray("max", value); return list.length ? list.reduce((best, item) => compare(item, best) >= 0 ? item : best) : null; });
  define("min_by", 1, function* (input, args, env) { yield extremeBy(input, args[0], env, false); });
  define("max_by", 1, function* (input, args, env) { yield extremeBy(input, args[0], env, true); });
  simple("reverse", 0, value => {
    if (value === null) return [];
    if (typeof value === "string") return [...value].reverse().join("");
    return requireArray("reverse", value).slice().reverse();
  });
  simple("startswith", 1, (value, prefix) => {
    if (typeof value !== "string" || typeof prefix !== "string") throw new JqError("startswith() requires string inputs");
    return value.startsWith(prefix);
  });
  simple("endswith", 1, (value, suffix) => {
    if (typeof value !== "string" || typeof suffix !== "string") throw new JqError("endswith() requires string inputs");
    return value.endsWith(suffix);
  });
  simple("ltrimstr", 1, (value, prefix) => typeof value === "string" && typeof prefix === "string" && value.startsWith(prefix) ? value.slice(prefix.length) : value);
  simple("rtrimstr", 1, (value, suffix) => typeof value === "string" && typeof suffix === "string" && suffix && value.endsWith(suffix) ? value.slice(0, -suffix.length) : value);
  simple("trim", 0, value => requireString("trim", value).trim());
  simple("ltrim", 0, value => requireString("ltrim", value).trimStart());
  simple("rtrim", 0, value => requireString("rtrim", value).trimEnd());
  simple("split", 1, (value, separator) => {
    if (typeof value !== "string" || typeof separator !== "string") throw new JqError("split input and separator must be strings");
    return splitString(value, separator);
  });
  define("split", 2, function* (input, args, env) {
    for (const [pattern, flags] of regexArgs(args, input, env)) {
      const found = matches(input, pattern, flags, true);
      const out = [];
      let last = 0;
      for (const match of found) { out.push(input.slice(last, match._index)); last = match._index + match.string.length; }
      out.push(input.slice(last));
      yield out;
    }
  });
  define("splits", 1, function* (input, args, env) { for (const list of BUILTINS["split/2"](input, [args[0], {type: "Literal", value: null}], env)) yield* list; });
  define("splits", 2, function* (input, args, env) { for (const list of BUILTINS["split/2"](input, args, env)) yield* list; });
  simple("join", 1, (value, separator) => {
    if (typeof separator !== "string") throw new JqError(`${describe(separator)} is not a valid separator`);
    return iterate(value).map(item => {
      if (item === null) return "";
      if (typeof item === "string") return item;
      if (typeof item === "number" || typeof item === "boolean") return toJson(item);
      throw new JqError(`Cannot join with ${typeOf(item)}`);
    }).join(separator);
  });
  simple("ascii_downcase", 0, value => requireString("ascii_downcase", value).replace(/[A-Z]/g, char => char.toLowerCase()));
  simple("ascii_upcase", 0, value => requireString("ascii_upcase", value).replace(/[a-z]/g, char => char.toUpperCase()));
  simple("explode", 0, value => Array.from(requireString("explode", value), char => char.codePointAt(0)));
  simple("implode", 0, value => String.fromCodePoint(...requireArray("implode", value)));
  simple("ascii", 0, value => String.fromCharCode(value));
  simple("tojson", 0, toJson);
  simple("fromjson", 0, value => {
    try { return JSON.parse(requireString("fromjson", value)); } catch (error) {
      if (error instanceof JqError) throw error;
      throw new JqError(`${value} (while parsing '${value}')`);
    }
  });
  define("test", 1, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield matches(input, pattern, flags).length > 0; });
  define("test", 2, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield matches(input, pattern, flags).length > 0; });
  const publicMatch = match => ({offset: match.offset, length: match.length, string: match.string, captures: match.captures});
  define("match", 1, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags).map(publicMatch); });
  define("match", 2, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags).map(publicMatch); });
  define("capture", 1, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags).map(captureObject); });
  define("capture", 2, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags).map(captureObject); });
  const scanResult = match => match.captures.length ? match.captures.map(capture => capture.string) : match.string;
  define("scan", 1, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags, true).map(scanResult); });
  define("scan", 2, function* (input, args, env) { for (const [pattern, flags] of regexArgs(args, input, env)) yield* matches(input, pattern, flags, true).map(scanResult); });
  define("sub", 2, (input, args, env) => substitute(input, args, env, false));
  define("sub", 3, (input, args, env) => substitute(input, args, env, false));
  define("gsub", 2, (input, args, env) => substitute(input, args, env, true));
  define("gsub", 3, (input, args, env) => substitute(input, args, env, true));
  simple("first", 0, value => index(value, 0));
  simple("last", 0, value => index(value, -1));
  simple("nth", 1, (value, n) => index(value, n));
  PATH_BUILTINS["first/0"] = function* (input) { yield [[0], index(input, 0)]; };
  PATH_BUILTINS["last/0"] = function* (input) { yield [[-1], index(input, -1)]; };
  define("first", 1, function* (input, args, env) { const result = first(ev(args[0], input, env)); if (result) yield result.value; });
  PATH_BUILTINS["first/1"] = function* (input, args, env) { const result = first(paths(args[0], input, env)); if (result) yield result.value; };
  define("last", 1, function* (input, args, env) { let last = null; for (const value of ev(args[0], input, env)) last = {value}; if (last) yield last.value; });
  PATH_BUILTINS["last/1"] = function* (input, args, env) { let last = null; for (const value of paths(args[0], input, env)) last = {value}; if (last) yield last.value; };
  function* limited(source, n) {
    if (n <= 0) return;
    let count = 0;
    for (const value of source) {
      yield value;
      if (++count >= n) return;
    }
  }
  define("limit", 2, function* (input, args, env) { for (const n of ev(args[0], input, env)) yield* limited(ev(args[1], input, env), n); });
  PATH_BUILTINS["limit/2"] = function* (input, args, env) { for (const n of ev(args[0], input, env)) yield* limited(paths(args[1], input, env), n); };
  define("nth", 2, function* (input, args, env) {
    for (const n of ev(args[0], input, env)) {
      if (n < 0) throw new JqError("Out of bounds negative array index");
      let count = 0;
      for (const value of ev(args[1], input, env)) if (count++ === n) { yield value; break; }
    }
  });
  define("until", 2, function* (input, args, env) {
    let value = input;
    for (;;) {
      tick();
      const cond = first(ev(args[0], value, env));
      if (!cond || truthy(cond.value)) { yield value; return; }
      const next = first(ev(args[1], value, env));
      if (!next) return;
      value = next.value;
    }
  });
  define("while", 2, function* (input, args, env) {
    let value = input;
    for (;;) {
      tick();
      const cond = first(ev(args[0], value, env));
      if (!cond || !truthy(cond.value)) return;
      yield value;
      const next = first(ev(args[1], value, env));
      if (!next) return;
      value = next.value;
    }
  });
  define("repeat", 1, function* (input, args, env) {
    let value = input;
    for (;;) {
      tick();
      yield value;
      const next = first(ev(args[0], value, env));
      if (!next) return;
      value = next.value;
    }
  });
  simple("indices", 1, (value, target) => indicesOf(value, target));
  simple("index", 1, (value, target) => { const found = indicesOf(value, target); return found && found.length ? found[0] : null; });
  simple("rindex", 1, (value, target) => { const found = indicesOf(value, target); return found && found.length ? found[found.length - 1] : null; });
  simple("env", 0, () => ({}));
  simple("builtins", 0, () => Object.keys(BUILTINS).filter(key => !key.startsWith("_")));
  simple("input_filename", 0, () => null);
  define("input", 0, function* () { throw new JqError("No more inputs"); });
  define("inputs", 0, function* () {});
  simple("debug", 0, value => value);
  define("debug", 1, function* (input) { yield input; });
  simple("stderr", 0, value => value);
  simple("now", 0, () => Date.now() / 1000);
  simple("todate", 0, toDateString);
  simple("todateiso8601", 0, toDateString);
  simple("fromdate", 0, fromDateString);
  simple("fromdateiso8601", 0, fromDateString);
  simple("dateadd", 2, (input, unit, n) => input + n);
  simple("gmtime", 0, value => brokenDown(requireNumber("gmtime", value)));
  simple("localtime", 0, value => brokenDown(requireNumber("localtime", value)));
  simple("mktime", 0, fromBrokenDown);
  simple("strftime", 1, strftime);
  simple("strflocaltime", 1, strftime);
  simple("date", 0, toDateString);
  simple("transpose", 0, value => {
    const rows = requireArray("transpose", value);
    const width = Math.max(0, ...rows.map(row => requireArray("transpose", row).length));
    return Array.from({length: width}, (_, i) => rows.map(row => i < row.length ? row[i] : null));
  });
  define("walk", 1, function* (input, args, env) {
    function* walk(value) {
      let rebuilt = value;
      if (Array.isArray(value)) {
        rebuilt = [];
        for (const item of value) for (const out of walk(item)) rebuilt.push(out);
      } else if (isObject(value)) {
        rebuilt = {};
        for (const key of Object.keys(value)) { const result = first(walk(value[key])); if (result) put(rebuilt, key, result.value); }
      }
      yield* ev(args[0], rebuilt, env);
    }
    yield* walk(input);
  });
  define("combinations", 0, function* (input) {
    const lists = requireArray("combinations", input).map(list => iterate(list));
    function* product(at, acc) {
      if (at === lists.length) { yield acc.slice(); return; }
      for (const item of lists[at]) { acc[at] = item; yield* product(at + 1, acc); }
    }
    yield* product(0, []);
  });
  define("combinations", 1, function* (input, args, env) {
    for (const n of ev(args[0], input, env)) yield* BUILTINS["combinations/0"](Array.from({length: n}, () => input), [], env);
  });
  simple("have_decnum", 0, () => false);

  // ---- API ----

  const cache = new Map();
  function parse(program) {
    const text = String(program);
    let ast = cache.get(text);
    if (ast) return ast;
    const {tokens} = tokenize(text);
    ast = parseTokens(tokens, text);
    check(ast, new Set(), new Set());
    if (cache.size > 100) cache.clear();
    cache.set(text, ast);
    return ast;
  }

  function execute(ast, data, limit = MAX_OUTPUTS) {
    steps = 0;
    const out = [];
    try {
      for (const value of ev(ast, data === undefined ? null : data, {vars: Object.create(null), funcs: Object.create(null)})) {
        out.push(value);
        if (out.length >= limit) {
          out.truncated = true;
          break;
        }
      }
    } catch (error) {
      if (error instanceof RangeError) throw new Error("The program recursed too deeply");
      if (error instanceof LimitError) throw new Error(error.message);
      throw error;
    }
    return out;
  }

  const run = (data, program, options = {}) => execute(parse(program), data, options.limit);
  const compile = program => {
    const ast = parse(program);
    return (data, options = {}) => execute(ast, data, options.limit);
  };
  const builtins = [...new Set(Object.keys(BUILTINS).map(key => key.split("/")[0]))].sort();

  const api = {run, compile, parse, builtins, formats: Object.keys(FORMATS).map(name => "@" + name), JqError};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVJq = api;
})(typeof self !== "undefined" ? self : this);
