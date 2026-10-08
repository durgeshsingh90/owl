"use strict";
// JMESPath (https://jmespath.org/specification.html), the --query language of the AWS CLI.
// search(data, expression) evaluates; compile(expression) returns a function(data).
// Errors carry a human readable message and, for syntax errors, error.position.
(function (root) {
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);

  function fail(message, position, name = "SyntaxError") {
    const error = new Error(position === undefined ? message : `${message} at position ${position}`);
    error.name = name;
    if (position !== undefined) error.position = position;
    return error;
  }

  // ---- Lexer ----

  const SIMPLE = {".": "dot", "*": "star", ",": "comma", ":": "colon", "{": "lbrace", "}": "rbrace", "]": "rbracket", "(": "lparen", ")": "rparen", "@": "current"};
  const WHITESPACE = new Set([" ", "\t", "\n", "\r"]);

  function tokenize(text) {
    const tokens = [];
    let at = 0;
    const push = (type, start, end, value) => tokens.push({type, start, end, value});
    while (at < text.length) {
      const char = text[at], start = at;
      if (WHITESPACE.has(char)) { at++; continue; }
      if (/[A-Za-z_]/.test(char)) {
        while (at < text.length && /[A-Za-z0-9_]/.test(text[at])) at++;
        push("identifier", start, at, text.slice(start, at));
      } else if (SIMPLE[char]) {
        push(SIMPLE[char], start, ++at);
      } else if (char === "[") {
        const next = text[at + 1];
        if (next === "?") { at += 2; push("filter", start, at); }
        else if (next === "]") { at += 2; push("flatten", start, at); }
        else push("lbracket", start, ++at);
      } else if (/[0-9-]/.test(char)) {
        at++;
        while (at < text.length && /[0-9]/.test(text[at])) at++;
        const raw = text.slice(start, at);
        if (raw === "-") throw fail("Expected a number after '-'", start);
        push("number", start, at, Number(raw));
      } else if (char === "\"") {
        at++;
        while (at < text.length && text[at] !== "\"") at += text[at] === "\\" ? 2 : 1;
        if (at >= text.length) throw fail("Unterminated quoted identifier", start);
        at++;
        let value;
        try { value = JSON.parse(text.slice(start, at)); } catch { throw fail("Invalid quoted identifier", start); }
        push("quoted", start, at, value);
      } else if (char === "'") {
        at++;
        let value = "";
        // Only \' is an escape (as in the AWS CLI); other backslashes stay as typed.
        while (at < text.length && text[at] !== "'") {
          if (text[at] === "\\" && text[at + 1] === "'") { value += "'"; at += 2; } else value += text[at++];
        }
        if (at >= text.length) throw fail("Unterminated raw string literal", start);
        at++;
        push("literal", start, at, value);
      } else if (char === "`") {
        at++;
        let raw = "";
        while (at < text.length && text[at] !== "`") {
          if (text[at] === "\\" && text[at + 1] === "`") { raw += "`"; at += 2; } else raw += text[at++];
        }
        if (at >= text.length) throw fail("Unterminated JSON literal", start);
        at++;
        push("literal", start, at, parseLiteral(raw.trim(), start));
      } else if (char === "|") {
        if (text[at + 1] === "|") { at += 2; push("or", start, at); } else push("pipe", start, ++at);
      } else if (char === "&") {
        if (text[at + 1] === "&") { at += 2; push("and", start, at); } else push("expref", start, ++at);
      } else if (char === "!") {
        if (text[at + 1] === "=") { at += 2; push("ne", start, at); } else push("not", start, ++at);
      } else if (char === "=") {
        if (text[at + 1] !== "=") throw fail("Unexpected '=' (use '==' to compare)", start);
        at += 2; push("eq", start, at);
      } else if (char === "<" || char === ">") {
        const equal = text[at + 1] === "=";
        at += equal ? 2 : 1;
        push((char === "<" ? "lt" : "gt") + (equal ? "e" : ""), start, at);
      } else {
        throw fail(`Unexpected character '${char}'`, start);
      }
    }
    tokens.push({type: "eof", start: text.length, end: text.length});
    return tokens;
  }

  // `foo` (legacy unquoted string) is accepted the way the reference implementations do.
  function parseLiteral(raw, position) {
    try { return JSON.parse(raw); } catch { /* try the legacy form */ }
    try { return JSON.parse(`"${raw}"`); } catch { throw fail("Invalid JSON literal", position); }
  }

  // ---- Parser (top down operator precedence) ----

  const BP = {eof: 0, identifier: 0, quoted: 0, rbracket: 0, rparen: 0, comma: 0, rbrace: 0, number: 0, current: 0, expref: 0, colon: 0, literal: 0,
    pipe: 1, or: 2, and: 3, eq: 5, ne: 5, lt: 5, lte: 5, gt: 5, gte: 5, flatten: 9, star: 20, filter: 21, dot: 40, not: 45, lbrace: 50, lbracket: 55, lparen: 60};
  const COMPARATORS = {eq: "==", ne: "!=", lt: "<", lte: "<=", gt: ">", gte: ">="};
  const IDENTITY = {type: "Identity"};

  function parse(text) {
    const tokens = tokenize(text);
    let index = 0;
    const peek = (offset = 0) => tokens[Math.min(index + offset, tokens.length - 1)];
    const describe = token => token.type === "eof" ? "end of expression" : `'${text.slice(token.start, token.end)}'`;
    const unexpected = token => fail(`Unexpected token ${describe(token)}`, token.start);
    const expect = type => {
      const token = peek();
      if (token.type !== type) {
        const wanted = {rbracket: "']'", rparen: "')'", rbrace: "'}'", colon: "':'", star: "'*'"}[type] || type;
        throw fail(`Expected ${wanted} but found ${describe(token)}`, token.start);
      }
      index++;
      return token;
    };

    function expression(rbp) {
      const token = tokens[index++];
      let left = nud(token);
      while (rbp < BP[peek().type]) left = led(tokens[index++], left);
      return left;
    }

    function nud(token) {
      switch (token.type) {
        case "literal": return {type: "Literal", value: token.value};
        case "identifier": return {type: "Field", name: token.value, start: token.start};
        case "quoted":
          if (peek().type === "lparen") throw fail("Quoted identifier is not allowed as a function name", token.start);
          return {type: "Field", name: token.value};
        case "not": return {type: "Not", child: expression(BP.not)};
        case "star": return {type: "ValueProjection", left: IDENTITY, right: peek().type === "rbracket" ? IDENTITY : projectionRhs(BP.star)};
        case "filter": return led(token, IDENTITY);
        case "lbrace": return multiselectHash();
        case "flatten": return {type: "Projection", left: {type: "Flatten", child: IDENTITY}, right: projectionRhs(BP.flatten)};
        case "lbracket": {
          const next = peek().type;
          if (next === "number" || next === "colon") return projectIfSlice(IDENTITY, indexExpression());
          if (next === "star" && peek(1).type === "rbracket") {
            index += 2;
            return {type: "Projection", left: IDENTITY, right: projectionRhs(BP.star)};
          }
          return multiselectList();
        }
        case "current": return {type: "Current"};
        case "expref": return {type: "ExpRef", child: expression(BP.expref)};
        case "lparen": {
          const inner = expression(0);
          expect("rparen");
          return inner;
        }
        default: throw unexpected(token);
      }
    }

    function led(token, left) {
      switch (token.type) {
        case "dot":
          if (peek().type !== "star") return {type: "Subexpression", left, right: dotRhs(BP.dot)};
          index++;
          return {type: "ValueProjection", left, right: projectionRhs(BP.dot)};
        case "pipe": return {type: "Pipe", left, right: expression(BP.pipe)};
        case "or": return {type: "Or", left, right: expression(BP.or)};
        case "and": return {type: "And", left, right: expression(BP.and)};
        case "lparen": {
          if (left.type !== "Field" || left.start === undefined) throw fail("Only a plain name can be called as a function", token.start);
          const args = [];
          while (peek().type !== "rparen") {
            args.push(expression(0));
            if (peek().type === "comma") {
              index++;
              if (peek().type === "rparen") throw unexpected(peek());
            } else if (peek().type !== "rparen") throw unexpected(peek());
          }
          expect("rparen");
          checkFunction(left.name, args.length, left.start);
          return {type: "Function", name: left.name, args};
        }
        case "filter": {
          const condition = expression(0);
          expect("rbracket");
          const right = peek().type === "flatten" ? IDENTITY : projectionRhs(BP.filter);
          return {type: "FilterProjection", left, right, condition};
        }
        case "flatten": return {type: "Projection", left: {type: "Flatten", child: left}, right: projectionRhs(BP.flatten)};
        case "lbracket": {
          const next = peek().type;
          if (next === "number" || next === "colon") return projectIfSlice(left, indexExpression());
          expect("star");
          expect("rbracket");
          return {type: "Projection", left, right: projectionRhs(BP.star)};
        }
        default:
          if (COMPARATORS[token.type]) return {type: "Comparator", op: COMPARATORS[token.type], left, right: expression(BP[token.type])};
          throw unexpected(token);
      }
    }

    function indexExpression() {
      if (peek().type === "colon" || peek(1).type === "colon") return sliceExpression();
      const token = expect("number");
      expect("rbracket");
      return {type: "Index", value: token.value};
    }

    function sliceExpression() {
      const parts = [null, null, null];
      let part = 0;
      while (peek().type !== "rbracket") {
        const token = peek();
        if (token.type === "colon") {
          part++;
          if (part > 2) throw fail("Too many colons in slice expression", token.start);
        } else if (token.type === "number") {
          parts[part] = token.value;
        } else throw unexpected(token);
        index++;
        if (token.type === "number" && peek().type === "number") throw unexpected(peek());
      }
      expect("rbracket");
      return {type: "Slice", start: parts[0], stop: parts[1], step: parts[2]};
    }

    function projectIfSlice(left, right) {
      const indexed = {type: "IndexExpression", left, right};
      if (right.type === "Slice") return {type: "Projection", left: indexed, right: projectionRhs(BP.star)};
      return indexed;
    }

    function projectionRhs(rbp) {
      const next = peek();
      if (BP[next.type] < 10) return IDENTITY;
      if (next.type === "lbracket" || next.type === "filter") return expression(rbp);
      if (next.type === "dot") {
        index++;
        return dotRhs(rbp);
      }
      throw unexpected(next);
    }

    function dotRhs(rbp) {
      const next = peek();
      if (next.type === "identifier" || next.type === "quoted" || next.type === "star") return expression(rbp);
      if (next.type === "lbracket") { index++; return multiselectList(); }
      if (next.type === "lbrace") { index++; return multiselectHash(); }
      throw fail(`Expected a name, '*', '[' or '{' after '.' but found ${describe(next)}`, next.start);
    }

    function multiselectList() {
      const items = [];
      if (peek().type === "rbracket") throw fail("Empty multiselect list", peek().start);
      for (;;) {
        items.push(expression(0));
        if (peek().type === "comma") { index++; continue; }
        expect("rbracket");
        return {type: "MultiSelectList", items};
      }
    }

    function multiselectHash() {
      const pairs = [];
      if (peek().type === "rbrace") throw fail("Empty multiselect hash", peek().start);
      for (;;) {
        const key = peek();
        if (key.type !== "identifier" && key.type !== "quoted") throw fail(`Expected a key name but found ${describe(key)}`, key.start);
        index++;
        expect("colon");
        pairs.push({key: key.value, value: expression(0)});
        if (peek().type === "comma") { index++; continue; }
        expect("rbrace");
        return {type: "MultiSelectHash", pairs};
      }
    }

    const ast = expression(0);
    if (peek().type !== "eof") throw unexpected(peek());
    return ast;
  }

  // ---- Interpreter ----

  function truthy(value) {
    if (value === null || value === false || value === "") return false;
    if (Array.isArray(value)) return value.length > 0;
    if (isObject(value)) {
      for (const key in value) if (own(value, key)) return true;
      return false;
    }
    return true;
  }

  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((item, i) => equal(item, b[i]));
    if (isObject(a) && isObject(b)) {
      const keys = Object.keys(a);
      return keys.length === Object.keys(b).length && keys.every(key => own(b, key) && equal(a[key], b[key]));
    }
    return false;
  }

  class ExpRef {
    constructor(node) { this.node = node; }
  }

  function typeName(value) {
    if (value === null || value === undefined) return "null";
    if (Array.isArray(value)) return "array";
    if (value instanceof ExpRef) return "expref";
    return typeof value === "object" ? "object" : typeof value;
  }

  function visit(node, value) {
    switch (node.type) {
      case "Field":
        return isObject(value) && own(value, node.name) ? value[node.name] : null;
      case "Subexpression":
      case "IndexExpression":
        return visit(node.right, visit(node.left, value));
      case "Index": {
        if (!Array.isArray(value)) return null;
        const at = node.value < 0 ? value.length + node.value : node.value;
        return at >= 0 && at < value.length ? value[at] : null;
      }
      case "Slice":
        return Array.isArray(value) ? slice(value, node) : null;
      case "Projection": {
        const base = visit(node.left, value);
        if (!Array.isArray(base)) return null;
        const out = [];
        for (const item of base) {
          const result = visit(node.right, item);
          if (result !== null) out.push(result);
        }
        return out;
      }
      case "ValueProjection": {
        const base = visit(node.left, value);
        if (!isObject(base)) return null;
        const out = [];
        for (const key of Object.keys(base)) {
          const result = visit(node.right, base[key]);
          if (result !== null) out.push(result);
        }
        return out;
      }
      case "FilterProjection": {
        const base = visit(node.left, value);
        if (!Array.isArray(base)) return null;
        const out = [];
        for (const item of base) {
          if (!truthy(visit(node.condition, item))) continue;
          const result = visit(node.right, item);
          if (result !== null) out.push(result);
        }
        return out;
      }
      case "Flatten": {
        const base = visit(node.child, value);
        if (!Array.isArray(base)) return null;
        const out = [];
        for (const item of base) {
          if (Array.isArray(item)) for (const inner of item) out.push(inner);
          else out.push(item);
        }
        return out;
      }
      case "Comparator": {
        const left = visit(node.left, value), right = visit(node.right, value);
        if (node.op === "==") return equal(left, right);
        if (node.op === "!=") return !equal(left, right);
        if (typeof left !== "number" || typeof right !== "number") return null;
        return node.op === "<" ? left < right : node.op === "<=" ? left <= right : node.op === ">" ? left > right : left >= right;
      }
      case "Identity":
      case "Current":
        return value;
      case "Literal":
        return node.value;
      case "MultiSelectList":
        return value === null ? null : node.items.map(item => visit(item, value));
      case "MultiSelectHash": {
        if (value === null) return null;
        const out = {};
        for (const pair of node.pairs) Object.defineProperty(out, pair.key, {value: visit(pair.value, value), enumerable: true, writable: true, configurable: true});
        return out;
      }
      case "Or": {
        const left = visit(node.left, value);
        return truthy(left) ? left : visit(node.right, value);
      }
      case "And": {
        const left = visit(node.left, value);
        return truthy(left) ? visit(node.right, value) : left;
      }
      case "Not":
        return !truthy(visit(node.child, value));
      case "Pipe":
        return visit(node.right, visit(node.left, value));
      case "ExpRef":
        return new ExpRef(node.child);
      case "Function":
        return callFunction(node.name, node.args.map(arg => visit(arg, value)));
      default:
        throw new Error(`Unknown node ${node.type}`);
    }
  }

  function slice(list, {start, stop, step}) {
    step = step === null ? 1 : step;
    if (step === 0) throw fail("Invalid slice: step cannot be 0", undefined, "ValueError");
    const length = list.length;
    const clamp = (at, fallback) => {
      if (at === null) return fallback;
      if (at < 0) return Math.max(at + length, step < 0 ? -1 : 0);
      return Math.min(at, step < 0 ? length - 1 : length);
    };
    const from = clamp(start, step < 0 ? length - 1 : 0), to = clamp(stop, step < 0 ? -1 : length);
    const out = [];
    if (step > 0) for (let i = from; i < to; i += step) out.push(list[i]);
    else for (let i = from; i > to; i += step) out.push(list[i]);
    return out;
  }

  // ---- Functions ----

  // Argument types: any, number, string, boolean, array, object, null, expref,
  // array-number, array-string. "a|b" allows either; a trailing "..." makes it variadic.
  const SIGNATURES = {
    abs: ["number"], avg: ["array-number"], ceil: ["number"], contains: ["array|string", "any"], ends_with: ["string", "string"],
    floor: ["number"], join: ["string", "array-string"], keys: ["object"], length: ["string|array|object"], map: ["expref", "array"],
    max: ["array-number|array-string"], max_by: ["array", "expref"], merge: ["object..."], min: ["array-number|array-string"],
    min_by: ["array", "expref"], not_null: ["any..."], reverse: ["string|array"], sort: ["array-number|array-string"],
    sort_by: ["array", "expref"], starts_with: ["string", "string"], sum: ["array-number"], to_array: ["any"], to_number: ["any"],
    to_string: ["any"], type: ["any"], values: ["object"]
  };

  function checkFunction(name, count, position) {
    const signature = SIGNATURES[name];
    if (!signature) throw fail(`Unknown function: ${name}()`, position, "UnknownFunctionError");
    const variadic = signature[signature.length - 1].endsWith("...");
    if (variadic ? count < signature.length : count !== signature.length) {
      const wanted = variadic ? `at least ${signature.length}` : String(signature.length);
      throw fail(`${name}() takes ${wanted} argument${wanted === "1" ? "" : "s"} but received ${count}`, position, "ArgumentError");
    }
  }

  function matches(type, value) {
    const actual = typeName(value);
    if (type === "any") return true;
    if (type === "array-number" || type === "array-string") {
      const inner = type.slice(6);
      return actual === "array" && value.every(item => typeName(item) === inner);
    }
    return actual === type;
  }

  function typeError(name, position, type, value) {
    return fail(`${name}() expected argument ${position} to be type ${type.replace(/\|/g, " or ").replace(/array-(\w+)/g, "array of $1")} but received type ${typeName(value)}`, undefined, "TypeError");
  }

  function callFunction(name, args) {
    const signature = SIGNATURES[name];
    args.forEach((arg, i) => {
      const type = signature[Math.min(i, signature.length - 1)].replace("...", "");
      if (!type.split("|").some(option => matches(option, arg))) throw typeError(name, i + 1, type, arg);
    });
    return FUNCTIONS[name](...args);
  }

  function keyed(name, list, ref, allowed) {
    return list.map((item, index) => {
      const key = visit(ref.node, item);
      if (!allowed.includes(typeName(key))) throw fail(`${name}() expected the expression to return ${allowed.join(" or ")} but received type ${typeName(key)}`, undefined, "TypeError");
      return {item, key, index};
    });
  }

  function byKey(name, list, ref, pickMax) {
    if (!list.length) return null;
    let best = null;
    for (const entry of keyed(name, list, ref, ["number", "string"])) {
      if (best && typeof best.key !== typeof entry.key) throw fail(`${name}() expression results must all be the same type`, undefined, "TypeError");
      if (!best || (pickMax ? entry.key > best.key : entry.key < best.key)) best = entry;
    }
    return best.item;
  }

  function extreme(list, pickMax) {
    if (!list.length) return null;
    return list.reduce((best, item) => pickMax ? (item > best ? item : best) : (item < best ? item : best));
  }

  const NUMBER = /^\s*-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\s*$/;

  const FUNCTIONS = {
    abs: n => Math.abs(n),
    avg: list => list.length ? list.reduce((sum, n) => sum + n, 0) / list.length : null,
    ceil: n => Math.ceil(n),
    contains: (subject, search) => typeof subject === "string" ? typeof search === "string" && subject.includes(search) : subject.some(item => equal(item, search)),
    ends_with: (subject, suffix) => subject.endsWith(suffix),
    floor: n => Math.floor(n),
    join: (glue, list) => list.join(glue),
    keys: object => Object.keys(object),
    length: subject => typeof subject === "string" ? [...subject].length : Array.isArray(subject) ? subject.length : Object.keys(subject).length,
    map: (ref, list) => list.map(item => visit(ref.node, item)),
    max: list => extreme(list, true),
    max_by: (list, ref) => byKey("max_by", list, ref, true),
    merge: (...objects) => {
      const out = {};
      for (const object of objects) for (const key of Object.keys(object)) Object.defineProperty(out, key, {value: object[key], enumerable: true, writable: true, configurable: true});
      return out;
    },
    min: list => extreme(list, false),
    min_by: (list, ref) => byKey("min_by", list, ref, false),
    not_null: (...values) => values.find(value => value !== null) ?? null,
    reverse: subject => typeof subject === "string" ? [...subject].reverse().join("") : [...subject].reverse(),
    sort: list => [...list].sort((a, b) => a < b ? -1 : a > b ? 1 : 0),
    sort_by: (list, ref) => {
      const entries = keyed("sort_by", list, ref, ["number", "string"]);
      if (entries.length && entries.some(entry => typeof entry.key !== typeof entries[0].key)) throw fail("sort_by() expression results must all be the same type", undefined, "TypeError");
      return entries.sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : a.index - b.index).map(entry => entry.item);
    },
    starts_with: (subject, prefix) => subject.startsWith(prefix),
    sum: list => list.reduce((sum, n) => sum + n, 0),
    to_array: value => Array.isArray(value) ? value : [value],
    to_number: value => {
      if (typeof value === "number") return value;
      if (typeof value === "string" && NUMBER.test(value)) return Number(value);
      return null;
    },
    to_string: value => typeof value === "string" ? value : JSON.stringify(value),
    type: value => typeName(value),
    values: object => Object.values(object)
  };

  // ---- API ----

  const cache = new Map();
  function compile(expression) {
    const text = String(expression);
    let ast = cache.get(text);
    if (!ast) {
      ast = parse(text);
      if (cache.size > 200) cache.clear();
      cache.set(text, ast);
    }
    return data => {
      const result = visit(ast, data === undefined ? null : data);
      return result === undefined ? null : result;
    };
  }

  const search = (data, expression) => compile(expression)(data);

  const api = {search, compile, parse, tokenize, functions: Object.keys(SIGNATURES)};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVJmes = api;
})(typeof self !== "undefined" ? self : this);
