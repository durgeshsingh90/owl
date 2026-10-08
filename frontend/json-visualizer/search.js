"use strict";
// Search across keys and values of a parsed document, in slices, so the worker can
// report the first matches early and stop when a newer search starts.
(function (root) {
  function escapeRegex(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // The test for one string. Options: caseSensitive, wholeWord, regex.
  function matcher(query, {caseSensitive = false, wholeWord = false, regex = false} = {}) {
    let source = regex ? query : escapeRegex(query);
    if (wholeWord) source = `(?:^|\\b|(?<=\\W))(?:${source})(?:\\b|$|(?=\\W))`;
    const pattern = new RegExp(source, caseSensitive ? "" : "i");
    return text => pattern.test(text);
  }

  // A walker over every key and value; next(limit) visits up to `limit` nodes and
  // returns the matches found ({path, on: "key" | "value"}) and whether it is done.
  function searcher(document, query, options = {}) {
    const test = matcher(query, options);
    const keys = options.scope !== "values", values = options.scope !== "keys";
    // Each entry: the value, its key and the entry it sits in (paths are built only for matches).
    const stack = [{value: document, key: undefined, parent: null}];
    let scanned = 0;
    const pathOf = entry => {
      const path = [];
      for (let at = entry; at && at.parent; at = at.parent) path.push(at.key);
      return path.reverse();
    };
    function next(limit = 20000) {
      const matches = [];
      let visited = 0;
      while (stack.length && visited < limit) {
        const entry = stack.pop();
        visited++;
        const {value} = entry;
        if (keys && typeof entry.key === "string" && test(entry.key)) matches.push({path: pathOf(entry), on: "key"});
        if (value !== null && typeof value === "object") {
          if (Array.isArray(value)) {
            for (let index = value.length - 1; index >= 0; index--) stack.push({value: value[index], key: index, parent: entry});
          } else {
            const names = Object.keys(value);
            for (let index = names.length - 1; index >= 0; index--) stack.push({value: value[names[index]], key: names[index], parent: entry});
          }
        } else if (values && entry.parent && test(value === null ? "null" : String(value))) {
          matches.push({path: pathOf(entry), on: "value"});
        }
      }
      scanned += visited;
      return {matches, done: !stack.length, scanned};
    }
    return {next};
  }

  const api = {matcher, searcher, escapeRegex};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVSearch = api;
})(typeof self !== "undefined" ? self : this);
