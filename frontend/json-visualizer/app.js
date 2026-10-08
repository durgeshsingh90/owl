"use strict";
// JSON Visualizer: open JSON, JSON Lines and JSON5 (also gzip) in tabs and look at it
// as a tree, a table, cards or raw text, with cloud-aware formatting, search and
// one-click copy. Files are parsed in a worker per document; nothing leaves the browser.
(() => {
  const M = window.JVModel, C = window.JVCloud;
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value).replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[char]);
  const store = {
    get(key, fallback) { try { const value = localStorage.getItem(key); return value === null ? fallback : value; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage may be unavailable */ } },
  };

  const ROW = 24, TABLE_ROW = 30, PAGE = 2000, MORE = 5000, EXPAND_CAP = 200000, PROGRESS_FROM = 5 * 1024 * 1024;
  const COPY_CONFIRM = 10 * 1024 * 1024, RECENT_LIMIT = 20, RECENT_KEEP_BYTES = 50 * 1024 * 1024;
  const VIEWS = ["tree", "table", "card", "raw"];
  const FORMAT_LABEL = {json: "JSON", jsonl: "JSON Lines", json5: "JSON5"};
  const SOURCE_LABEL = {aws: "AWS", azure: "Azure", gcp: "GCP", kubectl: "kubectl", terraform: "Terraform", unknown: ""};
  let limitMb = Number(store.get("owl-json-limit-mb", 50)) || 50;
  let timeZone = store.get("owl-json-tz", "local");
  const docs = [];
  let active = null, sequence = 0, pasteCount = 0;

  // ---------- Paths ----------
  // A path id is built from its parts so a child's id is its parent's plus one part.
  const childId = (parentId, part) => parentId + (typeof part === "number" ? "\u0001#" + part : "\u0001s" + part);
  const pathId = path => path.reduce(childId, "");
  const samePath = (a, b) => a && b && a.length === b.length && a.every((part, index) => part === b[index]);
  const startsWith = (path, prefix) => prefix.length <= path.length && prefix.every((part, index) => part === path[index]);
  const lastKey = path => path.length ? path[path.length - 1] : undefined;

  // ---------- Toast and clipboard ----------
  let toastTimer;
  function toast(message, tone = "") {
    const element = $("#toast");
    element.textContent = message;
    element.className = `toast ${tone}`;
    element.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { element.hidden = true; }, 2600);
  }
  async function copy(text, what = "Copied") {
    if (text.length > COPY_CONFIRM && !confirm(`Copy ${M.formatBytes(text.length)} of text to the clipboard?`)) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement("textarea"), {value: text});
      document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    }
    const short = text.length > 80 ? text.slice(0, 79).replace(/\s+/g, " ") + "…" : text.replace(/\s+/g, " ");
    toast(`${what}: ${short || "(empty)"}`);
  }
  const copyValue = value => copy(M.copyText(value), "Copied value");

  // ---------- Dates ----------
  function formatDate(text) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    if (timeZone === "utc") return date.toISOString().replace("T", " ").replace(/\.\d+Z$|Z$/, "") + " UTC";
    const pad = number => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }
  function dateTitle(text) {
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return `${text}\nLocal: ${date.toLocaleString()}\nUTC: ${date.toISOString()}\n${M.relativeTime(date.getTime())}`;
  }

  // ---------- Search highlighting ----------
  let highlight = null;
  function setHighlight(doc) {
    highlight = null;
    const search = doc?.search;
    if (!search?.query || search.error) return;
    try {
      let source = search.options.regex ? search.query : window.JVSearch.escapeRegex(search.query);
      if (search.options.wholeWord) source = `\\b(?:${source})\\b`;
      highlight = new RegExp(source, search.options.caseSensitive ? "g" : "gi");
    } catch { highlight = null; }
  }
  function marked(text) {
    text = String(text);
    if (!highlight || !text) return esc(text);
    let out = "", last = 0;
    highlight.lastIndex = 0;
    for (let match; (match = highlight.exec(text));) {
      if (!match[0]) { highlight.lastIndex++; continue; }
      out += esc(text.slice(last, match.index)) + `<mark>${esc(match[0])}</mark>`;
      last = match.index + match[0].length;
      if (out.length > 20000) break;
    }
    return out + esc(text.slice(last));
  }

  // ---------- Value rendering ----------
  function tagChips(list, max = 6) {
    const chips = list.slice(0, max).map(tag => `<span class="tag"><b>${marked(tag.key)}</b>${tag.value === "" ? "" : `=${marked(M.cellText(tag.value))}`}</span>`).join("");
    return chips + (list.length > max ? `<span class="tag more">+${list.length - max}</span>` : "");
  }
  // One value inline: typed colour, status badge, dates, tag chips, container summary.
  function valueHtml(key, value, {limit = 300} = {}) {
    const type = M.typeOf(value);
    if (type === "string") {
      const tone = C.statusTone(key, value);
      if (tone) return `<span class="badge tone-${tone}">${marked(value)}</span>`;
      const kind = M.stringKind(value);
      if (kind === "date") {
        const date = Date.parse(value);
        return `<span class="v t-date" title="${esc(dateTitle(value))}">${marked(formatDate(value))}<small> · ${esc(M.relativeTime(date))}</small></span>`;
      }
      const text = value.length > limit ? value.slice(0, limit) + "…" : value;
      return `<span class="v t-string k-${kind}"${value.length > limit ? ` title="${esc(M.formatBytes(value.length))} string; select it to see all"` : ""}>${marked(text)}</span>`;
    }
    if (type === "array") {
      if (C.isTagList(value)) return `<span class="tags">${tagChips(C.tags({Tags: value}))}</span>`;
      if (value.length && value.length <= 8 && value.every(item => !M.isContainer(item))) {
        return `<span class="v t-array">[${value.map(item => marked(M.cellText(item))).join(", ")}]</span>`;
      }
      return `<span class="v t-array">[${value.length.toLocaleString()} item${value.length === 1 ? "" : "s"}]</span>`;
    }
    if (type === "object") {
      const count = Object.keys(value).length, name = C.displayName(value), tags = C.tags(value);
      const status = statusOf(value);
      return `<span class="v t-object">{${count.toLocaleString()} key${count === 1 ? "" : "s"}}</span>${name ? ` <span class="v-name">${marked(name)}</span>` : ""}${status ? ` <span class="badge tone-${status.tone}">${esc(status.value)}</span>` : ""}${tags.length && !name ? ` <span class="tags">${tagChips(tags, 3)}</span>` : ""}`;
    }
    if (type === "number") return `<span class="v t-number">${marked(String(value))}</span>`;
    if (type === "boolean") return `<span class="v t-boolean">${value}</span>`;
    return `<span class="v t-null">null</span>`;
  }
  // The status of a resource: State.Name, status, provisioningState…
  function statusOf(record) {
    if (!record || typeof record !== "object" || Array.isArray(record)) return null;
    const candidates = [["State", record.State?.Name ?? record.State], ["status", record.status?.phase ?? record.status], ["Status", record.Status], ["provisioningState", record.provisioningState ?? record.properties?.provisioningState], ["powerState", record.powerState], ["phase", record.phase]];
    for (const [key, value] of candidates) {
      const tone = C.statusTone(key, value);
      if (tone) return {tone, value};
    }
    return null;
  }

  // ---------- Documents ----------
  function newDoc(fields) {
    return {
      id: ++sequence, name: "", size: 0, blob: null, text: undefined, format: "auto", worker: null,
      status: "loading", progress: null, value: undefined, detected: null, lines: null, errors: [], notices: [],
      source: "unknown", unwrap: null, focus: [], view: "auto", expanded: new Set(), pages: new Map(),
      selected: null, tables: new Map(), scroll: new Map(), rows: null, error: null,
      search: {query: "", options: {caseSensitive: false, wholeWord: false, regex: false, scope: "both"}, id: 0, matches: [], done: true, index: -1, scanned: 0, error: ""},
      ...fields,
    };
  }

  function openFiles(files, replace = null) {
    let first = true;
    for (const file of files) {
      if (replace && first) { load(replace, {blob: file, name: file.name, size: file.size, text: undefined, format: "auto"}); first = false; continue; }
      const doc = newDoc({name: file.name, size: file.size, blob: file});
      docs.push(doc);
      activate(doc);
      load(doc);
    }
  }
  function openText(text, name) {
    const doc = newDoc({name, size: new Blob([text]).size, text});
    docs.push(doc);
    activate(doc);
    load(doc);
  }

  function load(doc, changes = {}, force = false) {
    Object.assign(doc, changes);
    doc.worker?.terminate();
    doc.worker = null;
    if (doc === active) disposeRaw();
    Object.assign(doc, {status: "loading", progress: null, value: undefined, error: null, rows: null, unwrap: null, focus: [], selected: null, expanded: new Set(), pages: new Map(), tables: new Map(), scroll: new Map()});
    doc.search = {...doc.search, matches: [], done: true, index: -1, id: 0};
    if (doc.size > limitMb * 1024 * 1024 && !force) {
      doc.status = "large";
      return render();
    }
    const worker = doc.worker = new Worker("worker.js?v=2");
    const started = performance.now();
    worker.onmessage = ({data}) => {
      if (doc.worker !== worker) return;
      if (data.type === "progress") {
        doc.progress = data;
        if (doc === active) renderProgress(doc);
      } else if (data.type === "opened") opened(doc, data, performance.now() - started);
      else if (data.type === "matches") searchResults(doc, data);
      else if (data.type === "error") {
        doc.status = "error";
        doc.error = data;
        render();
      }
    };
    worker.onerror = event => {
      event.preventDefault();
      doc.status = "error";
      doc.error = {message: `The parser stopped: ${event.message || "out of memory?"}. Other tabs are not affected.`};
      render();
    };
    worker.postMessage({type: "open", blob: doc.blob, text: doc.text, name: doc.name, format: doc.format});
    render();
  }

  function opened(doc, data, took) {
    Object.assign(doc, {status: "ready", value: data.value, detected: data.format, lines: data.lines, errors: data.errors || [], notices: data.notices || [], took});
    doc.source = C.detectSource(doc.value);
    doc.unwrap = C.unwrap(doc.value, doc.source);
    // Open the root, and its first level when it is small.
    doc.expanded.add("");
    if (M.isContainer(doc.value) && !doc.unwrap) {
      const keys = Object.keys(doc.value);
      if (keys.length <= 40) for (const key of keys) if (M.isContainer(doc.value[key]) && Object.keys(doc.value[key]).length <= 40) doc.expanded.add(childId("", Array.isArray(doc.value) ? Number(key) : key));
    }
    remember(doc);
    render();
    if (doc.search.query) runSearch(doc);
  }

  function closeDoc(doc) {
    doc.worker?.terminate();
    const index = docs.indexOf(doc);
    docs.splice(index, 1);
    if (active === doc) active = docs[Math.min(index, docs.length - 1)] || null;
    disposeRaw();
    render();
  }

  function activate(doc) {
    if (active && active !== doc) saveScroll(active);
    active = doc;
    disposeRaw();
    render();
  }

  // ---------- Views ----------
  const focusValue = doc => M.getAt(doc.value, doc.focus);
  const useUnwrap = doc => doc.unwrap && doc.focus.length === 0;
  function autoView(doc) {
    const value = focusValue(doc);
    if (useUnwrap(doc)) return "table";
    if (Array.isArray(value) && value.length && value.filter(item => M.isContainer(item) && !Array.isArray(item)).length >= value.length / 2) return "table";
    if (M.isContainer(value) && !Array.isArray(value) && doc.focus.length) return "card";
    return "tree";
  }
  const viewOf = doc => doc.view === "auto" ? autoView(doc) : doc.view;
  function focus(doc, path, view = "auto") {
    saveScroll(doc);
    doc.focus = path;
    doc.view = view;
    doc.expanded.add(pathId(path));
    disposeRaw();
    render();
  }
  function saveScroll(doc) {
    const viewport = $("#view .scroller");
    if (viewport && doc.status === "ready") doc.scroll.set(`${viewOf(doc)}:${pathId(doc.focus)}`, viewport.scrollTop);
  }

  // ---------- Render: frame ----------
  function render() {
    renderTabs();
    const doc = active;
    $("#empty").hidden = Boolean(doc);
    $("#workspace").hidden = !doc;
    $("#bottom").hidden = !doc;
    $("#format-select").disabled = !doc;
    $("#export-button").disabled = !doc || doc.status !== "ready";
    $("#tz-toggle").textContent = timeZone === "utc" ? "UTC" : "Local time";
    if (!doc) { renderRecent(); document.title = "OWL · JSON Visualizer"; return; }
    document.title = `${doc.name} · OWL JSON Visualizer`;
    $("#format-select").value = doc.format;
    setHighlight(doc);
    const view = doc.status === "ready" ? viewOf(doc) : null;
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === doc.view)));
    $("#auto-view").textContent = doc.view === "auto" && view ? `· ${view}` : "";
    renderOutline(doc);
    renderStatus(doc);
    renderSearchCount(doc);
    if (doc.status === "loading") return renderProgress(doc);
    if (doc.status === "large") return renderLarge(doc);
    if (doc.status === "error") return renderError(doc);
    renderBreadcrumb(doc);
    if (view === "tree") renderTree(doc);
    else if (view === "table") renderTable(doc);
    else if (view === "card") renderCard(doc);
    else renderRaw(doc);
    renderDetail(doc);
  }

  function renderTabs() {
    $("#tabs").innerHTML = docs.map(doc => {
      const format = doc.detected ? FORMAT_LABEL[doc.detected] : doc.status === "error" ? "Error" : doc.status === "large" ? "Too large" : "…";
      return `<div class="tab${doc === active ? " active" : ""}" role="tab" aria-selected="${doc === active}" data-tab="${doc.id}" title="${esc(doc.name)}">
        <span class="tab-name">${esc(doc.name)}</span><span class="tab-meta">${esc(format)} · ${esc(M.formatBytes(doc.size))}</span>
        <button type="button" class="tab-close" data-close="${doc.id}" aria-label="Close ${esc(doc.name)}" title="Close">×</button></div>`;
    }).join("") + (docs.length ? `<button type="button" class="tab-add" id="tab-add" title="Open more files">＋</button>` : "");
  }

  function clearCenter() {
    $("#breadcrumb").innerHTML = "";
    $("#view-tools").innerHTML = "";
    $("#detail").innerHTML = "";
  }
  function renderProgress(doc) {
    clearCenter();
    const progress = doc.progress;
    const big = doc.size >= PROGRESS_FROM;
    const percent = progress?.total ? Math.min(100, Math.round(progress.loaded / progress.total * 100)) : 0;
    $("#view").innerHTML = `<div class="state-card">
      <h2>${esc(progress?.phase || "Opening")} ${esc(doc.name)}…</h2>
      ${big ? `<div class="progress"><span style="width:${progress?.phase === "Parsing" ? 100 : percent}%"></span></div>
      <p>${progress ? `${esc(M.formatBytes(progress.loaded))} of ${esc(M.formatBytes(progress.total))}` : "Starting…"}</p>` : "<p>Reading…</p>"}
      <button type="button" data-action="cancel-load">Cancel</button></div>`;
  }
  function renderLarge(doc) {
    clearCenter();
    $("#view").innerHTML = `<div class="state-card">
      <h2>${esc(doc.name)} is ${esc(M.formatBytes(doc.size))}</h2>
      <p>The browser viewer opens files up to <strong>${limitMb} MB</strong>. Bigger files can use 5–10 times their size in memory and may freeze or crash this tab.</p>
      <div class="state-actions"><button type="button" class="primary" data-action="open-anyway">Open anyway</button>
      <label>Limit <input id="limit-input" type="number" min="1" max="4000" value="${limitMb}"> MB</label><button type="button" data-action="save-limit">Save limit</button></div></div>`;
  }
  function renderError(doc) {
    clearCenter();
    const error = doc.error || {};
    const caret = error.snippet && error.caret !== null && error.caret !== undefined ? `<pre class="snippet">${esc(error.snippet)}\n${" ".repeat(Math.max(0, error.caret))}^</pre>` : error.snippet ? `<pre class="snippet">${esc(error.snippet)}</pre>` : "";
    $("#view").innerHTML = `<div class="state-card error">
      <h2>Could not read ${esc(doc.name)}</h2>
      <p class="error-message">${esc(error.message || "Unknown error")}${error.line ? ` <span class="where">line ${error.line}${error.column ? `, column ${error.column}` : ""}</span>` : ""}</p>
      ${caret}
      <p>Pick another format above (JSON, JSON Lines or JSON5) to read it again.</p>
      <button type="button" data-action="retry">Try again</button></div>`;
  }

  function renderStatus(doc) {
    const parts = [];
    if (doc.detected) parts.push(`<span class="pill">${esc(FORMAT_LABEL[doc.detected])}</span>`);
    parts.push(`<span>${esc(M.formatBytes(doc.size))}</span>`);
    if (doc.source && SOURCE_LABEL[doc.source]) parts.push(`<span class="pill source-${doc.source}">${esc(SOURCE_LABEL[doc.source])}</span>`);
    if (doc.status === "ready") {
      const value = doc.value;
      if (Array.isArray(value)) parts.push(`<span>${value.length.toLocaleString()} ${doc.detected === "jsonl" ? "records" : "items"}</span>`);
      if (doc.unwrap && doc.unwrap.path.length) parts.push(`<span>${doc.unwrap.rows.length.toLocaleString()} resource${doc.unwrap.rows.length === 1 ? "" : "s"} in ${esc(doc.unwrap.kind)}</span>`);
      if (doc.took) parts.push(`<span title="Time to read and parse">${Math.round(doc.took).toLocaleString()} ms</span>`);
    }
    if (doc.errors.length) parts.push(`<button type="button" class="link warn" data-action="show-errors">${doc.errors.length} bad line${doc.errors.length === 1 ? "" : "s"}</button>`);
    for (const notice of doc.notices) parts.push(`<span class="notice" title="${esc(notice)}">ⓘ ${esc(notice)}</span>`);
    $("#status").innerHTML = parts.join("");
  }

  function renderBreadcrumb(doc) {
    const crumbs = [`<button type="button" data-crumb="0" class="${doc.focus.length ? "" : "current"}">${esc(doc.name)}</button>`];
    doc.focus.forEach((part, index) => crumbs.push(`<button type="button" data-crumb="${index + 1}" class="${index === doc.focus.length - 1 ? "current" : ""}">${esc(typeof part === "number" ? `[${part}]` : part)}</button>`));
    // In the tree, the selected node's path beyond the focus, fainter.
    const selected = doc.selected && startsWith(doc.selected, doc.focus) ? doc.selected.slice(doc.focus.length) : [];
    selected.forEach((part, index) => crumbs.push(`<button type="button" class="beyond" data-crumb="${doc.focus.length + index + 1}" data-crumb-selected>${esc(typeof part === "number" ? `[${part}]` : part)}</button>`));
    $("#breadcrumb").innerHTML = crumbs.join(`<span class="sep">›</span>`);
  }

  function renderOutline(doc) {
    if (doc.status !== "ready") {
      $("#outline").innerHTML = `<div class="side-title">Outline</div><p class="side-note">${doc.status === "loading" ? "Reading…" : "Nothing to show."}</p>`;
      return renderRecent();
    }
    const value = doc.value, items = [];
    if (doc.unwrap && doc.unwrap.path.length) {
      items.push(`<button type="button" class="outline-item resources${useUnwrap(doc) && viewOf(doc) === "table" ? " current" : ""}" data-outline="unwrap"><span class="o-name">${esc(doc.unwrap.label)}<small class="o-meta">${esc(doc.unwrap.kind)}</small></span><span class="o-count">${doc.unwrap.rows.length.toLocaleString()}</span></button>`);
    }
    if (M.isContainer(value)) {
      const keys = Array.isArray(value) ? [] : Object.keys(value);
      for (const key of keys.slice(0, 300)) {
        const child = value[key], type = M.typeOf(child);
        const count = type === "array" ? child.length : type === "object" ? Object.keys(child).length : null;
        items.push(`<button type="button" class="outline-item${samePath(doc.focus, [key]) ? " current" : ""}" data-outline-key="${esc(key)}"><span class="o-type t-${type}">${type === "array" ? "[ ]" : type === "object" ? "{ }" : "·"}</span><span class="o-name">${esc(key)}</span>${count === null ? `<span class="o-meta">${esc(M.preview(child, 24))}</span>` : `<span class="o-count">${count.toLocaleString()}</span>`}</button>`);
      }
      if (keys.length > 300) items.push(`<p class="side-note">${(keys.length - 300).toLocaleString()} more keys</p>`);
      if (Array.isArray(value)) items.push(`<button type="button" class="outline-item${doc.focus.length ? "" : " current"}" data-outline="root"><span class="o-type t-array">[ ]</span><span class="o-name">${doc.detected === "jsonl" ? "Records" : "Items"}</span><span class="o-count">${value.length.toLocaleString()}</span></button>`);
    }
    const errors = doc.errors.length ? `<div class="side-title warn">Bad lines (${doc.errors.length})</div><ul class="bad-lines" id="bad-lines">${doc.errors.slice(0, 200).map(error => `<li><b>Line ${error.line}</b> ${esc(error.message)}<code>${esc(error.snippet.slice(0, 60))}</code></li>`).join("")}</ul>` : "";
    $("#outline").innerHTML = `<div class="side-title">Outline</div>${items.join("") || '<p class="side-note">A single value.</p>'}${errors}`;
    renderRecent();
  }

  // ---------- Tree ----------
  function childEntries(value) {
    return Array.isArray(value) ? null : Object.keys(value);
  }
  // The visible rows: the focus node, then every expanded node's children (paged).
  function buildRows(doc) {
    const rootValue = focusValue(doc), rootId = pathId(doc.focus);
    const rows = [{id: rootId, key: doc.focus.length ? lastKey(doc.focus) : doc.name, value: rootValue, depth: 0, parent: null, root: true}];
    const stack = [];
    const open = row => {
      if (!M.isContainer(row.value) || !doc.expanded.has(row.id)) return;
      const keys = childEntries(row.value);
      const total = keys ? keys.length : row.value.length;
      stack.push({row, keys, index: 0, total, limit: Math.min(total, doc.pages.get(row.id) || PAGE)});
    };
    open(rows[0]);
    while (stack.length) {
      const frame = stack[stack.length - 1];
      if (frame.index >= frame.limit) {
        stack.pop();
        if (frame.limit < frame.total) rows.push({id: frame.row.id + "\u0002more", more: true, parent: frame.row, depth: frame.row.depth + 1, remaining: frame.total - frame.limit});
        continue;
      }
      const part = frame.keys ? frame.keys[frame.index] : frame.index;
      frame.index++;
      const row = {id: childId(frame.row.id, part), key: part, value: frame.row.value[part], depth: frame.row.depth + 1, parent: frame.row};
      rows.push(row);
      open(row);
    }
    return rows;
  }
  function rowPath(doc, row) {
    const parts = [];
    for (let at = row; at && !at.root; at = at.parent) parts.push(at.key);
    return [...doc.focus, ...parts.reverse()];
  }

  let treeFrame = 0;
  function renderTree(doc) {
    doc.rows = buildRows(doc);
    $("#view-tools").innerHTML = `<button type="button" data-action="expand-all" title="Expand everything (up to ${EXPAND_CAP.toLocaleString()} rows)">Expand all</button>
      <button type="button" data-action="collapse-all">Collapse all</button>
      <label class="depth">Depth <input id="depth-input" type="number" min="1" max="50" value="${doc.depth || 2}"></label><button type="button" data-action="expand-depth" title="Expand to this depth">Apply</button>
      <span class="tool-note">${doc.rows.length.toLocaleString()} rows</span>`;
    let viewport = $("#view .scroller.tree");
    const treeKey = `${doc.id}:${pathId(doc.focus)}`;
    if (!viewport || viewport.dataset.key !== treeKey) {
      $("#view").innerHTML = `<div class="scroller tree" tabindex="0" role="tree" aria-label="JSON tree"><div class="spacer"></div><div class="rows"></div></div>`;
      viewport = $("#view .scroller.tree");
      viewport.dataset.key = treeKey;
      viewport.addEventListener("scroll", () => { cancelAnimationFrame(treeFrame); treeFrame = requestAnimationFrame(() => paintTree(active)); });
      const saved = doc.scroll.get(`tree:${pathId(doc.focus)}`);
      if (saved) requestAnimationFrame(() => { viewport.scrollTop = saved; });
    }
    viewport.querySelector(".spacer").style.height = `${doc.rows.length * ROW}px`;
    paintTree(doc);
  }
  function paintTree(doc) {
    const viewport = $("#view .scroller.tree");
    if (!viewport || !doc?.rows) return;
    const first = Math.max(0, Math.floor(viewport.scrollTop / ROW) - 15);
    const last = Math.min(doc.rows.length, first + Math.ceil(viewport.clientHeight / ROW) + 30);
    const selectedId = doc.selected ? pathId(doc.selected) : null;
    const current = doc.search.matches[doc.search.index];
    const currentId = current ? pathId(current.path) : null;
    const matchIds = doc.search.matchIds || new Set();
    const html = [];
    for (let index = first; index < last; index++) {
      const row = doc.rows[index];
      const indent = row.depth * 16 + 6;
      if (row.more) {
        html.push(`<div class="tr more" style="top:${index * ROW}px;padding-left:${indent + 18}px" data-index="${index}"><button type="button" class="link" data-more="${index}">Show ${Math.min(MORE, row.remaining).toLocaleString()} more</button><span class="tool-note">${row.remaining.toLocaleString()} not shown</span>${row.remaining > MORE ? ` <button type="button" class="link" data-more-all="${index}">Show all</button>` : ""}</div>`);
        continue;
      }
      const container = M.isContainer(row.value);
      const open = container && doc.expanded.has(row.id);
      const isIndex = typeof row.key === "number";
      const keyClass = isIndex ? "k index" : `k${C.isKeyField(row.key) ? " keyfield" : ""}`;
      const keyText = row.root && !doc.focus.length ? esc(row.key) : isIndex ? row.key : marked(row.key);
      const classes = ["tr"];
      if (row.id === selectedId) classes.push("selected");
      if (matchIds.has(row.id)) classes.push("match");
      if (row.id === currentId) classes.push("current");
      html.push(`<div class="${classes.join(" ")}" role="treeitem" aria-level="${row.depth + 1}"${container ? ` aria-expanded="${open}"` : ""} style="top:${index * ROW}px;padding-left:${indent}px" data-index="${index}">
        ${container ? `<button type="button" class="chev" data-toggle="${index}" tabindex="-1" aria-label="${open ? "Collapse" : "Expand"}">${open ? "▾" : "▸"}</button>` : `<span class="chev"></span>`}<span class="${keyClass}" data-copy-row="${index}" title="Click to copy the value · double-click to ${container ? "expand" : "select"}">${keyText}</span><span class="colon">:</span>
        <span class="val" data-copy-row="${index}">${valueHtml(row.key, row.value)}</span>
        <button type="button" class="copy-ico" data-copy-row="${index}" tabindex="-1" title="Copy value" aria-label="Copy value">⧉</button></div>`);
    }
    viewport.querySelector(".rows").innerHTML = html.join("");
  }

  function toggleRow(doc, row) {
    if (!M.isContainer(row.value)) return;
    if (doc.expanded.has(row.id)) doc.expanded.delete(row.id);
    else doc.expanded.add(row.id);
    renderTree(doc);
  }
  function expandAll(doc, maxDepth = Infinity) {
    // Breadth first, so a cap still opens the upper levels everywhere.
    const startId = pathId(doc.focus);
    let level = [{id: startId, value: focusValue(doc)}], depth = 0, rows = 1;
    const expanded = new Set([...doc.expanded].filter(id => !id.startsWith(startId) || id === startId));
    expanded.add(startId);
    while (level.length && depth < maxDepth && rows < EXPAND_CAP) {
      const next = [];
      for (const node of level) {
        if (!M.isContainer(node.value)) continue;
        expanded.add(node.id);
        const keys = Array.isArray(node.value) ? null : Object.keys(node.value);
        const total = keys ? keys.length : node.value.length;
        const limit = Math.min(total, PAGE);
        rows += limit;
        for (let index = 0; index < limit; index++) {
          const part = keys ? keys[index] : index;
          if (M.isContainer(node.value[part])) next.push({id: childId(node.id, part), value: node.value[part]});
        }
        if (rows >= EXPAND_CAP) break;
      }
      level = next;
      depth++;
    }
    doc.expanded = expanded;
    if (rows >= EXPAND_CAP) toast(`Expanded until ${EXPAND_CAP.toLocaleString()} rows; open deeper parts one by one.`);
    renderTree(doc);
  }
  function collapseAll(doc) {
    const startId = pathId(doc.focus);
    doc.expanded = new Set([...doc.expanded].filter(id => !id.startsWith(startId)));
    doc.expanded.add(startId);
    renderTree(doc);
  }

  function scrollToRow(doc, index) {
    const viewport = $("#view .scroller.tree");
    if (!viewport) return;
    const top = index * ROW;
    if (top < viewport.scrollTop + ROW || top > viewport.scrollTop + viewport.clientHeight - ROW * 2) viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 3);
    paintTree(doc);
  }

  // Open every ancestor of a path in the tree (and page far enough), then select it.
  function reveal(doc, path, {select = true} = {}) {
    if (!startsWith(path, doc.focus) || !["tree"].includes(viewOf(doc))) {
      doc.focus = startsWith(path, doc.focus) ? doc.focus : [];
      doc.view = "tree";
      disposeRaw();
    }
    let id = pathId(doc.focus), value = focusValue(doc);
    doc.expanded.add(id);
    for (const part of path.slice(doc.focus.length, -1)) {
      const keys = Array.isArray(value) ? null : Object.keys(value);
      const position = keys ? keys.indexOf(part) : part;
      if (position >= (doc.pages.get(id) || PAGE)) doc.pages.set(id, position + 1);
      id = childId(id, part);
      value = value[part];
      doc.expanded.add(id);
    }
    if (path.length > doc.focus.length) {
      const keys = Array.isArray(value) ? null : Object.keys(value);
      const position = keys ? keys.indexOf(lastKey(path)) : lastKey(path);
      if (position >= (doc.pages.get(id) || PAGE)) doc.pages.set(id, position + 1);
    }
    if (select) { doc.selected = path; setHash(path); }
    render();
    const target = pathId(path);
    const index = doc.rows.findIndex(row => row.id === target);
    if (index >= 0) scrollToRow(doc, index);
  }

  // ---------- Table ----------
  function tableSource(doc) {
    if (useUnwrap(doc)) return {rows: doc.unwrap.rows, key: "unwrap"};
    const value = focusValue(doc);
    if (Array.isArray(value)) return {rows: value.map((item, index) => ({value: item, path: [...doc.focus, index], key: index})), key: pathId(doc.focus)};
    if (M.isContainer(value)) return {rows: Object.keys(value).map(key => ({value: value[key], path: [...doc.focus, key], key})), key: pathId(doc.focus), keyed: true};
    return {rows: [{value, path: doc.focus, key: lastKey(doc.focus)}], key: pathId(doc.focus)};
  }
  function tableState(doc) {
    const source = tableSource(doc);
    let state = doc.tables.get(source.key);
    if (state && state.rowsRef === source.rows.length) return {state, source};
    const records = source.rows.map(row => row.value);
    const objectRows = records.some(record => M.isContainer(record) && !Array.isArray(record));
    const keys = objectRows ? M.columnKeys(records) : [];
    const known = C.template(records);
    const hasNames = objectRows && records.slice(0, 50).some(record => C.tags(record).length || record?.Name || record?.name);
    let visible;
    if (known) visible = known.columns.map(column => Array.isArray(column) ? column[0] : column).filter(key => key === "@name" ? hasNames : keys.includes(key));
    else {
      const keyFields = keys.filter(key => C.isKeyField(key.split(".").pop()));
      visible = [...new Set([...keyFields, ...keys])].slice(0, 12);
    }
    const labels = new Map(known ? known.columns.filter(Array.isArray) : []);
    const all = [...(source.keyed ? ["@key"] : []), ...(hasNames && !keys.includes("@name") ? ["@name"] : []), ...keys, ...(objectRows ? [] : ["@value"])];
    const shown = new Set([...(source.keyed ? ["@key"] : []), ...visible, ...(objectRows ? [] : ["@value"])]);
    const order = [...shown, ...all.filter(key => !shown.has(key))];
    const columns = order.map(key => ({key, label: labels.get(key) || (key === "@name" ? "Name" : key === "@key" ? "Key" : key === "@value" ? "Value" : key), width: key === "@key" ? 160 : 180, visible: shown.has(key), pinned: false}));
    state = {columns, sort: [], filters: {}, rowsRef: source.rows.length, template: known?.name || "", selected: -1, view: null, version: 0};
    doc.tables.set(source.key, state);
    return {state, source};
  }
  const fieldOf = (row, key) => key === "@key" ? row.key : key === "@value" ? row.value : M.getField(row.value, key, C.displayName);
  function orderedColumns(state) {
    const visible = state.columns.filter(column => column.visible);
    return [...visible.filter(column => column.pinned), ...visible.filter(column => !column.pinned)];
  }
  // Filtered and sorted row indexes, cached until a filter or the sort changes.
  function tableView(state, source) {
    const signature = JSON.stringify([state.filters, state.sort]);
    if (state.view && state.viewSignature === signature) return state.view;
    const filters = Object.entries(state.filters).filter(([, text]) => text.trim()).map(([key, text]) => [key, M.filterTest(text)]);
    let indexes = [];
    for (let index = 0; index < source.rows.length; index++) {
      const row = source.rows[index];
      if (filters.every(([key, test]) => test(fieldOf(row, key)))) indexes.push(index);
    }
    if (state.sort.length) {
      const keys = state.sort.map(sort => ({dir: sort.dir, values: new Map(indexes.map(index => [index, fieldOf(source.rows[index], sort.key)]))}));
      indexes.sort((a, b) => {
        for (const key of keys) {
          const result = M.compareCells(key.values.get(a), key.values.get(b));
          if (result) return key.dir * result;
        }
        return a - b;
      });
    }
    state.view = indexes;
    state.viewSignature = signature;
    return indexes;
  }

  let tableFrame = 0;
  function renderTable(doc) {
    const {state, source} = tableState(doc);
    const indexes = tableView(state, source);
    const columns = orderedColumns(state);
    const activeFilters = Object.values(state.filters).filter(text => text.trim()).length;
    $("#view-tools").innerHTML = `<button type="button" data-action="columns" aria-haspopup="dialog">Columns (${columns.length}/${state.columns.length}) ▾</button>
      ${activeFilters ? `<button type="button" data-action="clear-filters">Clear ${activeFilters} filter${activeFilters === 1 ? "" : "s"}</button>` : ""}
      ${state.sort.length ? `<button type="button" data-action="clear-sort">Clear sort</button>` : ""}
      <span class="tool-note">${indexes.length.toLocaleString()}${indexes.length === source.rows.length ? "" : ` of ${source.rows.length.toLocaleString()}`} rows${state.template ? ` · ${esc(state.template)} layout` : ""} · filters: text, =x, !=x, &gt;n, a..b, empty</span>`;
    const widths = `56px ${columns.map(column => `${column.width}px`).join(" ")}`;
    let left = 56;
    const sticky = columns.map(column => {
      if (!column.pinned) return "";
      const style = `position:sticky;left:${left}px;z-index:2`;
      left += column.width;
      return style;
    });
    const sortOf = key => { const index = state.sort.findIndex(sort => sort.key === key); return index < 0 ? "" : `${state.sort[index].dir > 0 ? "▲" : "▼"}${state.sort.length > 1 ? index + 1 : ""}`; };
    const header = `<div class="th-row" style="grid-template-columns:${widths}"><div class="th gutter">#</div>${columns.map((column, index) => `<div class="th${column.pinned ? " pinned" : ""}" style="${sticky[index]}" draggable="true" data-column="${esc(column.key)}" title="${esc(column.key)} · click to sort, Shift+click to add a sort · drag to move">
        <span class="th-label">${esc(column.label)}</span><span class="sort">${sortOf(column.key)}</span>
        <button type="button" class="th-menu" data-column-menu="${esc(column.key)}" title="Column options" aria-label="Options for ${esc(column.label)}">⋮</button><span class="resize" data-resize="${esc(column.key)}"></span></div>`).join("")}</div>
      <div class="filter-row" style="grid-template-columns:${widths}"><div class="th gutter"></div>${columns.map((column, index) => `<div class="tf${column.pinned ? " pinned" : ""}" style="${sticky[index]}"><input data-filter="${esc(column.key)}" value="${esc(state.filters[column.key] || "")}" placeholder="Filter" spellcheck="false" aria-label="Filter ${esc(column.label)}"></div>`).join("")}</div>`;
    const width = 56 + columns.reduce((sum, column) => sum + column.width, 0);
    let viewport = $("#view .scroller.table");
    const key = `${doc.id}:${source.key}:${columns.map(column => column.key + column.width + column.pinned).join("|")}`;
    if (!viewport || viewport.dataset.key !== key) {
      const focused = document.activeElement?.dataset?.filter;
      const caret = document.activeElement?.selectionStart;
      const scroll = viewport ? [viewport.scrollTop, viewport.scrollLeft] : [doc.scroll.get(`table:${pathId(doc.focus)}`) || 0, 0];
      $("#view").innerHTML = `<div class="scroller table" tabindex="0" role="grid" aria-rowcount="${indexes.length}"><div class="table-inner" style="width:${width}px"><div class="thead">${header}</div><div class="tbody" style="position:relative"><div class="rows"></div></div></div></div>`;
      viewport = $("#view .scroller.table");
      viewport.dataset.key = key;
      viewport.addEventListener("scroll", () => { cancelAnimationFrame(tableFrame); tableFrame = requestAnimationFrame(() => paintTable(active)); });
      [viewport.scrollTop, viewport.scrollLeft] = scroll;
      if (focused) { const input = viewport.querySelector(`[data-filter="${CSS.escape(focused)}"]`); input?.focus(); if (caret !== undefined) input?.setSelectionRange(caret, caret); }
    } else {
      const thead = viewport.querySelector(".thead");
      const focused = document.activeElement?.dataset?.filter;
      if (!focused) thead.innerHTML = header;
      else viewport.querySelector(".th-row").outerHTML = header.slice(0, header.indexOf("<div class=\"filter-row\""));
    }
    viewport.querySelector(".tbody").style.height = `${indexes.length * TABLE_ROW}px`;
    doc.tableColumns = columns;
    doc.tableSticky = sticky;
    doc.tableWidths = widths;
    paintTable(doc);
  }
  function paintTable(doc) {
    const viewport = $("#view .scroller.table");
    if (!viewport || !doc) return;
    const {state, source} = tableState(doc);
    const indexes = tableView(state, source);
    const columns = doc.tableColumns, sticky = doc.tableSticky;
    const headerHeight = viewport.querySelector(".thead").offsetHeight;
    const first = Math.max(0, Math.floor((viewport.scrollTop - headerHeight) / TABLE_ROW) - 10);
    const last = Math.min(indexes.length, first + Math.ceil(viewport.clientHeight / TABLE_ROW) + 25);
    const html = [];
    for (let position = first; position < last; position++) {
      const index = indexes[position], row = source.rows[index];
      html.push(`<div class="tr-row${state.selected === index ? " selected" : ""}" role="row" style="top:${position * TABLE_ROW}px;grid-template-columns:${doc.tableWidths}" data-row="${index}">
        <div class="td gutter" title="Select row">${(position + 1).toLocaleString()}</div>${columns.map((column, at) => {
          const value = fieldOf(row, column.key);
          const name = column.key.split(".").pop();
          return `<div class="td${column.pinned ? " pinned" : ""}" role="gridcell" style="${sticky[at]}" data-cell="${esc(column.key)}" title="${esc(M.cellText(value).slice(0, 500))}">${value === undefined ? "" : column.key === "@key" ? `<span class="v k">${marked(value)}</span>` : valueHtml(name, value, {limit: 200})}</div>`;
        }).join("")}</div>`);
    }
    viewport.querySelector(".rows").innerHTML = html.join("");
  }
  function selectTableRow(doc, index, scroll = false) {
    const {state, source} = tableState(doc);
    state.selected = index;
    doc.selected = source.rows[index]?.path || null;
    paintTable(doc);
    renderDetail(doc);
    if (doc.selected) setHash(doc.selected);
    if (scroll) {
      const viewport = $("#view .scroller.table"), position = tableView(state, source).indexOf(index);
      const top = position * TABLE_ROW, header = viewport.querySelector(".thead").offsetHeight;
      if (top < viewport.scrollTop || top > viewport.scrollTop + viewport.clientHeight - header - TABLE_ROW * 2) viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 3);
    }
  }

  // ---------- Card ----------
  const refs = {card: new Map(), detail: new Map()};
  let refSequence = 0;
  function ref(region, path, value) {
    const id = `${region}:${++refSequence}`;
    refs[region].set(id, {path, value});
    return id;
  }
  // Key/value grid of an object; nested objects as sections, lists of objects as links to a table.
  function cardBody(region, value, path, depth = 0) {
    if (!M.isContainer(value)) return `<div class="kv"><span class="cv" data-ref="${ref(region, path, value)}">${valueHtml(lastKey(path), value, {limit: 4000})}</span></div>`;
    if (Array.isArray(value)) {
      if (!value.length) return `<div class="kv empty-note">Empty list</div>`;
      if (C.isTagList(value)) return `<div class="tags block">${tagChips(C.tags({Tags: value}), 100)}</div>`;
      if (value.every(item => !M.isContainer(item))) return `<div class="chips">${value.slice(0, 200).map((item, index) => `<span class="chip" data-ref="${ref(region, [...path, index], item)}">${valueHtml(lastKey(path), item, {limit: 200})}</span>`).join("")}${value.length > 200 ? `<span class="chip more">+${(value.length - 200).toLocaleString()}</span>` : ""}</div>`;
      const names = value.slice(0, 5).map(item => C.displayName(item)).filter(Boolean);
      return `<div class="list-link"><button type="button" class="link" data-focus-ref="${ref(region, path, value)}" data-focus-view="table">View ${value.length.toLocaleString()} item${value.length === 1 ? "" : "s"} as a table →</button>${names.length ? `<span class="tool-note">${names.map(esc).join(", ")}${value.length > names.length ? ", …" : ""}</span>` : ""}</div>`;
    }
    const keys = Object.keys(value);
    // Key fields first, then the rest in their own order.
    const ordered = [...keys.filter(key => C.isKeyField(key) && !M.isContainer(value[key])), ...keys.filter(key => !(C.isKeyField(key) && !M.isContainer(value[key])))];
    const limit = depth ? 300 : 2000;
    const lines = ordered.slice(0, limit).map(key => {
      const child = value[key], childPath = [...path, key], id = ref(region, childPath, child);
      const label = `<span class="ck${C.isKeyField(key) ? " keyfield" : ""}" data-ref="${id}" title="Click to copy the value">${marked(key)}</span>`;
      if (M.isContainer(child) && !(Array.isArray(child) && (C.isTagList(child) || child.every(item => !M.isContainer(item))))) {
        if (depth >= 3) return `<div class="kv">${label}<span class="cv"><button type="button" class="link" data-focus-ref="${id}">${esc(M.preview(child))} open →</button></span></div>`;
        const count = Array.isArray(child) ? child.length : Object.keys(child).length;
        return `<details class="section" ${depth < 1 && count <= 60 ? "open" : ""}><summary>${label}<span class="tool-note">${esc(M.preview(child))}</span><button type="button" class="link small" data-focus-ref="${id}" title="Open this part on its own">open →</button></summary>${cardBody(region, child, childPath, depth + 1)}</details>`;
      }
      return `<div class="kv">${label}<span class="cv" data-ref="${id}">${M.isContainer(child) ? cardBody(region, child, childPath, depth + 1) : valueHtml(key, child, {limit: 2000})}</span></div>`;
    });
    if (keys.length > limit) lines.push(`<div class="kv empty-note">${(keys.length - limit).toLocaleString()} more keys; open the tree to see them</div>`);
    return `<div class="card-grid">${lines.join("") || '<div class="kv empty-note">Empty object</div>'}</div>`;
  }
  function cardHeader(region, value, path) {
    const name = C.displayName(value), status = statusOf(value), tags = C.tags(value);
    const link = path && M.isContainer(value) ? [value.Arn, value.ARN, value.arn, value.id].map(C.consoleLink).find(Boolean) : C.consoleLink(value);
    return `<header class="card-head"><h2>${esc(name || (path.length ? String(lastKey(path)) : "Document"))}</h2>${status ? `<span class="badge tone-${status.tone}">${esc(status.value)}</span>` : ""}${link ? `<a class="console" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>` : ""}</header>${tags.length ? `<div class="tags block">${tagChips(tags, 40)}</div>` : ""}`;
  }
  function renderCard(doc) {
    refs.card.clear();
    const value = focusValue(doc);
    $("#view-tools").innerHTML = `<span class="tool-note">Click a key or value to copy it · right-click for more</span>`;
    let html;
    if (Array.isArray(value) && value.some(M.isContainer)) {
      const shown = Math.min(value.length, doc.cardLimit || 100);
      html = value.slice(0, shown).map((item, index) => `<article class="card">${cardHeader("card", item, [...doc.focus, index])}${cardBody("card", item, [...doc.focus, index], 1)}</article>`).join("") +
        (value.length > shown ? `<button type="button" class="more-cards" data-action="more-cards">Show ${Math.min(100, value.length - shown)} more of ${(value.length - shown).toLocaleString()}</button>` : "");
    } else html = `<article class="card">${cardHeader("card", value, doc.focus)}${cardBody("card", value, doc.focus)}</article>`;
    $("#view").innerHTML = `<div class="scroller cards">${html}</div>`;
    const saved = doc.scroll.get(`card:${pathId(doc.focus)}`);
    if (saved) $("#view .scroller").scrollTop = saved;
  }

  // ---------- Raw (Monaco) ----------
  let monaco = null, rawEditor = null, monacoLoading = null;
  function loadMonaco() {
    if (monaco) return Promise.resolve(monaco);
    if (monacoLoading) return monacoLoading;
    monacoLoading = new Promise((resolve, reject) => {
      const script = Object.assign(document.createElement("script"), {src: "../vendor/monaco/vs/loader.js"});
      script.onload = () => {
        window.require.config({paths: {vs: "../vendor/monaco/vs"}});
        window.require(["vs/editor/editor.main"], () => {
          monaco = window.monaco;
          monaco.languages.json?.jsonDefaults.setDiagnosticsOptions({validate: false});
          new MutationObserver(() => monaco.editor.setTheme(monacoTheme())).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
          resolve(monaco);
        }, reject);
      };
      script.onerror = reject;
      document.head.append(script);
    });
    return monacoLoading;
  }
  const monacoTheme = () => document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs";
  function disposeRaw() {
    rawEditor?.getModel()?.dispose();
    rawEditor?.dispose();
    rawEditor = null;
  }
  async function renderRaw(doc) {
    const value = focusValue(doc);
    $("#view-tools").innerHTML = `<button type="button" data-action="copy-raw">Copy formatted</button><button type="button" data-action="copy-min">Copy minified</button><span class="tool-note">Read-only · fold with the arrows in the gutter</span>`;
    if (rawEditor && rawEditor.owlDoc === doc && samePath(rawEditor.owlFocus, doc.focus)) return;
    disposeRaw();
    const text = JSON.stringify(value, null, 2) ?? String(value);
    if (text.length > 150 * 1024 * 1024) {
      $("#view").innerHTML = `<div class="state-card"><h2>Too large to show as text</h2><p>${esc(M.formatBytes(text.length))} formatted. Open a smaller part from the tree or the outline.</p></div>`;
      return;
    }
    $("#view").innerHTML = `<div class="raw-host"><p class="tool-note pad">Loading editor…</p></div>`;
    try { await loadMonaco(); } catch { $("#view").innerHTML = `<pre class="raw-fallback">${esc(text.slice(0, 2_000_000))}</pre>`; return; }
    if (active !== doc || viewOf(doc) !== "raw") return;
    const host = $("#view .raw-host");
    host.textContent = "";
    rawEditor = monaco.editor.create(host, {
      model: monaco.editor.createModel(text, "json"), theme: monacoTheme(), readOnly: true, automaticLayout: true,
      minimap: {enabled: text.length < 20 * 1024 * 1024}, folding: true, showFoldingControls: "always", lineNumbersMinChars: 5,
      scrollBeyondLastLine: false, fontSize: 13, wordWrap: "off", renderValidationDecorations: "off", largeFileOptimizations: true,
      unicodeHighlight: {ambiguousCharacters: false, invisibleCharacters: false},
    });
    rawEditor.owlDoc = doc;
    rawEditor.owlFocus = doc.focus;
  }

  // ---------- Detail panel ----------
  function renderDetail(doc) {
    refs.detail.clear();
    const path = doc.selected;
    if (!path || doc.status !== "ready") {
      $("#detail").innerHTML = `<div class="detail-empty"><b>Details</b><p>Select a row or a node to see it here, with its path and copy actions.</p>
        <p class="tool-note">Click a key or value to copy it. Right-click for paths in JS, JMESPath or jq syntax.</p></div>`;
      return;
    }
    const value = M.getAt(doc.value, path);
    const type = M.typeOf(value);
    const link = M.isContainer(value) ? [value.Arn, value.ARN, value.arn, value.id].map(C.consoleLink).find(Boolean) : C.consoleLink(value);
    let body;
    if (M.isContainer(value)) body = cardBody("detail", value, path, 1);
    else if (type === "string") {
      const kind = M.stringKind(value);
      const embedded = /^\s*[[{]/.test(value) && value.length < 50 * 1024 * 1024 && (() => { try { JSON.parse(value); return true; } catch { return false; } })();
      body = `<pre class="full-value t-string k-${kind}">${marked(value)}</pre>
        ${kind === "date" ? `<p class="tool-note">${esc(dateTitle(value)).replace(/\n/g, "<br>")}</p>` : ""}
        ${kind === "url" && /^https?:\/\//.test(value) ? `<a href="${esc(value)}" target="_blank" rel="noopener noreferrer">Open link ↗</a>` : ""}
        ${embedded ? `<button type="button" data-action="open-embedded">This string is JSON: open it in a new tab</button>` : ""}`;
    } else body = `<pre class="full-value t-${type}">${esc(String(value))}</pre>`;
    const paths = ["js", "jmes", "jq"].map(syntax => `<div class="path-line"><span class="path-syntax">${syntax === "jmes" ? "JMESPath" : syntax === "js" ? "JS" : "jq"}</span><code>${esc(M.formatPath(path, syntax))}</code><button type="button" class="copy-mini" data-copy-path="${syntax}" title="Copy path">⧉</button></div>`).join("");
    $("#detail").innerHTML = `<div class="detail-inner">
      ${M.isContainer(value) && !Array.isArray(value) ? cardHeader("detail", value, path) : `<header class="card-head"><h2>${esc(path.length ? String(lastKey(path)) : doc.name)}</h2><span class="pill">${type}</span></header>`}
      <div class="paths">${paths}</div>
      <div class="detail-actions"><button type="button" data-action="copy-selected">Copy value</button>${M.isContainer(value) ? `<button type="button" data-action="copy-selected-min">Minified</button><button type="button" data-action="focus-selected">Open here</button>` : ""}<button type="button" data-action="tree-selected" title="Show in the tree">Show in tree</button>${link && !M.isContainer(value) ? `<a class="console" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>` : ""}</div>
      <div class="detail-body">${body}</div></div>`;
  }
  function select(doc, path) {
    doc.selected = path;
    renderDetail(doc);
    renderBreadcrumb(doc);
    setHash(path);
  }
  function setHash(path) {
    try { history.replaceState(null, "", "#" + encodeURIComponent(M.formatPath(path, "jmes"))); } catch { /* sandboxed */ }
  }

  // ---------- Search ----------
  let searchTimer;
  function runSearch(doc) {
    clearTimeout(searchTimer);
    const search = doc.search;
    search.id++;
    Object.assign(search, {matches: [], matchIds: new Set(), done: !search.query, index: -1, scanned: 0, error: ""});
    if (!search.query || doc.status !== "ready") { render(); return; }
    doc.worker.postMessage({type: "search", id: search.id, query: search.query, options: search.options});
    renderSearchCount(doc);
  }
  function searchResults(doc, data) {
    const search = doc.search;
    if (data.id !== search.id) return;
    if (data.error) search.error = data.error;
    const first = !search.matches.length && data.matches.length;
    for (const match of data.matches) {
      search.matches.push(match);
      search.matchIds.add(pathId(match.path));
    }
    search.done = data.done;
    search.scanned = data.scanned;
    if (doc !== active) return;
    renderSearchCount(doc);
    setHighlight(doc);
    if (first) goToMatch(doc, 0);
    else if (viewOf(doc) === "tree") paintTree(doc);
  }
  function renderSearchCount(doc) {
    const search = doc.search;
    $("#search-cancel").hidden = search.done;
    $("#search-input").value === search.query || ($("#search-input").value = search.query);
    document.querySelectorAll("[data-option]").forEach(button => button.setAttribute("aria-pressed", String(Boolean(search.options[button.dataset.option]))));
    $("#search-scope").value = search.options.scope;
    const count = search.matches.length;
    $("#search-count").textContent = search.error ? search.error : !search.query ? "" : count ? `${(search.index + 1).toLocaleString()} of ${count.toLocaleString()}${search.done ? "" : "+ · searching…"}` : search.done ? "No matches" : `Searching… ${search.scanned.toLocaleString()} nodes`;
    $("#search-count").classList.toggle("none", Boolean(search.query && search.done && !count));
  }
  function goToMatch(doc, index) {
    const search = doc.search;
    if (!search.matches.length) return;
    search.index = (index + search.matches.length) % search.matches.length;
    reveal(doc, search.matches[search.index].path);
    renderSearchCount(doc);
  }

  // ---------- Context menu ----------
  function openMenu(x, y, items) {
    const menu = $("#menu");
    menu.innerHTML = items.map((item, index) => item === "-" ? `<hr>` : `<button type="button" role="menuitem" data-menu="${index}"${item.disabled ? " disabled" : ""}>${esc(item.label)}${item.hint ? `<small>${esc(item.hint)}</small>` : ""}</button>`).join("");
    menu.hidden = false;
    menu.items = items;
    const box = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(x, innerWidth - box.width - 8)}px`;
    menu.style.top = `${Math.min(y, innerHeight - box.height - 8)}px`;
    menu.querySelector("button")?.focus();
  }
  const closeMenu = () => { $("#menu").hidden = true; };
  function nodeMenu(doc, path, value, extra = []) {
    const key = lastKey(path);
    const link = C.consoleLink(value);
    return [
      {label: "Copy value", hint: typeof value === "string" ? "no quotes" : M.isContainer(value) ? "pretty JSON" : "", action: () => copyValue(value)},
      {label: "Copy key", disabled: key === undefined, action: () => copy(String(key), "Copied key")},
      {label: "Copy path (JS)", hint: M.formatPath(path, "js").slice(0, 40), action: () => copy(M.formatPath(path, "js"), "Copied path")},
      {label: "Copy path (JMESPath)", hint: "for --query", action: () => copy(M.formatPath(path, "jmes"), "Copied path")},
      {label: "Copy path (jq)", action: () => copy(M.formatPath(path, "jq"), "Copied path")},
      {label: "Copy as minified JSON", action: () => copy(JSON.stringify(value) ?? "", "Copied minified")},
      ...extra,
      "-",
      ...(M.isContainer(value) ? [
        {label: "Open here as table", action: () => focus(doc, path, "table")},
        {label: "Open here as card", action: () => focus(doc, path, "card")},
        {label: "Open here as tree", action: () => focus(doc, path, "tree")},
        {label: "Open here as raw JSON", action: () => focus(doc, path, "raw")},
      ] : [{label: "Show in tree", action: () => reveal(doc, path)}]),
      ...(link ? [{label: link.label, action: () => window.open(link.url, "_blank", "noopener")}] : []),
    ];
  }

  // ---------- Column chooser and column menu ----------
  function openPopover(anchor, html) {
    const popover = $("#popover");
    popover.innerHTML = html;
    popover.hidden = false;
    const box = anchor.getBoundingClientRect();
    popover.style.left = `${Math.max(8, Math.min(box.left, innerWidth - popover.offsetWidth - 8))}px`;
    popover.style.top = `${box.bottom + 4}px`;
  }
  function columnChooser(doc, anchor) {
    const {state} = tableState(doc);
    openPopover(anchor, `<div class="chooser"><div class="chooser-head"><b>Columns</b><input id="column-find" placeholder="Find a column" spellcheck="false"><button type="button" class="link" data-columns="all">All</button><button type="button" class="link" data-columns="none">None</button></div>
      <ol class="chooser-list">${state.columns.map((column, index) => `<li data-column-item="${esc(column.key.toLowerCase())}"><label><input type="checkbox" data-column-show="${index}" ${column.visible ? "checked" : ""}> <span>${esc(column.label)}</span></label>
        <button type="button" class="mini" data-column-pin="${index}" aria-pressed="${column.pinned}" title="Pin to the left">📌</button><button type="button" class="mini" data-column-move="${index}" data-dir="-1" title="Move up">↑</button><button type="button" class="mini" data-column-move="${index}" data-dir="1" title="Move down">↓</button></li>`).join("")}</ol></div>`);
    $("#column-find").focus();
  }
  function invalidate(state) { state.view = null; }

  // ---------- Recent files (IndexedDB, this browser only) ----------
  let database = null;
  function db() {
    if (database) return database;
    database = new Promise((resolve, reject) => {
      try {
        const request = indexedDB.open("owl-json-visualizer", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("recent", {keyPath: "key"});
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      } catch (error) { reject(error); }
    });
    return database;
  }
  async function recentList() {
    try {
      const store = (await db()).transaction("recent").objectStore("recent");
      return await new Promise((resolve, reject) => { const request = store.getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); })
        .then(list => list.sort((a, b) => b.openedAt - a.openedAt));
    } catch { return []; }
  }
  async function remember(doc) {
    try {
      const key = `${doc.name}|${doc.size}`;
      const blob = doc.blob || (doc.text !== undefined ? new Blob([doc.text], {type: "text/plain"}) : null);
      const entry = {key, name: doc.name, size: doc.size, format: doc.detected, source: doc.source, openedAt: Date.now(), blob: blob && doc.size <= RECENT_KEEP_BYTES ? blob : null};
      const list = await recentList();
      const transaction = (await db()).transaction("recent", "readwrite");
      const objects = transaction.objectStore("recent");
      objects.put(entry);
      for (const old of list.filter(item => item.key !== key).slice(RECENT_LIMIT - 1)) objects.delete(old.key);
      transaction.oncomplete = () => renderRecent();
    } catch { /* recent files are a convenience */ }
  }
  async function renderRecent() {
    const list = await recentList();
    const item = entry => `<button type="button" class="recent-item" data-recent="${esc(entry.key)}"${entry.blob ? "" : " disabled title=\"Too large to keep; open the file again\""}><span class="o-name">${esc(entry.name)}</span><span class="o-meta">${esc([entry.format && FORMAT_LABEL[entry.format], SOURCE_LABEL[entry.source], M.formatBytes(entry.size), M.relativeTime(entry.openedAt)].filter(Boolean).join(" · "))}</span></button>`;
    $("#recent-side").innerHTML = list.length ? list.map(item).join("") : `<p class="side-note">Files you open appear here (kept in this browser).</p>`;
    $("#recent-empty").innerHTML = list.length ? `<div class="side-title">Recent files</div>${list.slice(0, 8).map(item).join("")}` : "";
    $("#clear-history").hidden = !list.length;
  }
  async function openRecent(key) {
    const entry = (await recentList()).find(item => item.key === key);
    if (!entry?.blob) return toast("That file was too large to keep; open it again.");
    const file = new File([entry.blob], entry.name);
    openFiles([file]);
  }
  async function clearHistory() {
    if (!confirm("Delete every recent file kept in this browser?")) return;
    try {
      const transaction = (await db()).transaction("recent", "readwrite");
      transaction.objectStore("recent").clear();
      transaction.oncomplete = () => { renderRecent(); toast("Recent files deleted"); };
    } catch { /* nothing kept */ }
  }

  // ---------- Export ----------
  function download(text, name, type) {
    const url = URL.createObjectURL(new Blob([text], {type}));
    const link = Object.assign(document.createElement("a"), {href: url, download: name});
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  function exportAs(kind) {
    const doc = active;
    if (!doc || doc.status !== "ready") return;
    const base = doc.name.replace(/\.(json5?|jsonl|ndjson|txt)(\.gz)?$/i, "") + (doc.focus.length ? "-" + String(lastKey(doc.focus)) : "");
    if (kind === "csv" || kind === "table-json") {
      const {state, source} = tableState(doc);
      const indexes = tableView(state, source), columns = orderedColumns(state);
      if (kind === "csv") download(M.toCsv(columns.map(column => column.label), indexes.map(index => columns.map(column => M.cellText(fieldOf(source.rows[index], column.key))))), `${base}.csv`, "text/csv");
      else download(JSON.stringify(indexes.map(index => source.rows[index].value), null, 2), `${base}-rows.json`, "application/json");
      return toast(`Exported ${indexes.length.toLocaleString()} rows`);
    }
    const value = focusValue(doc);
    download(JSON.stringify(value, null, kind === "min" ? 0 : 2), `${base}${kind === "min" ? ".min" : ""}.json`, "application/json");
  }

  // ---------- Events ----------
  $("#file-input").addEventListener("change", event => { openFiles([...event.target.files]); event.target.value = ""; });
  const pickFiles = () => $("#file-input").click();
  $("#open-button").addEventListener("click", pickFiles);
  $("#empty-open").addEventListener("click", pickFiles);
  async function pasteFromClipboard() {
    try {
      const text = await navigator.clipboard.readText();
      if (text.trim()) return openText(text, `Pasted ${++pasteCount}`);
    } catch { /* permission denied: use the dialog */ }
    $("#paste-text").value = "";
    $("#paste-dialog").showModal();
    $("#paste-text").focus();
  }
  $("#paste-button").addEventListener("click", pasteFromClipboard);
  $("#empty-paste").addEventListener("click", pasteFromClipboard);
  $("#paste-dialog").addEventListener("close", () => {
    if ($("#paste-dialog").returnValue === "open" && $("#paste-text").value.trim()) openText($("#paste-text").value, `Pasted ${++pasteCount}`);
  });
  document.addEventListener("paste", event => {
    const target = event.target;
    if (target.closest?.("input,textarea,[contenteditable],.raw-host")) return;
    const text = event.clipboardData?.getData("text");
    if (text?.trim()) { event.preventDefault(); openText(text, `Pasted ${++pasteCount}`); }
  });

  // Drag and drop: anywhere opens new tabs; onto a tab replaces it.
  let dragDepth = 0;
  const hasFiles = event => [...(event.dataTransfer?.types || [])].includes("Files");
  window.addEventListener("dragenter", event => { if (!hasFiles(event)) return; dragDepth++; $("#drop-overlay").hidden = false; });
  window.addEventListener("dragleave", event => { if (!hasFiles(event)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
  window.addEventListener("dragover", event => { if (hasFiles(event)) event.preventDefault(); });
  window.addEventListener("drop", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    $("#drop-overlay").hidden = true;
    const files = [...event.dataTransfer.files];
    if (!files.length) return;
    const tab = document.elementsFromPoint(event.clientX, event.clientY).find(element => element.dataset?.tab);
    const target = tab ? docs.find(doc => doc.id === Number(tab.dataset.tab)) : null;
    openFiles(files, target);
    if (target) activate(target);
  });

  $("#tabs").addEventListener("click", event => {
    const close = event.target.closest("[data-close]");
    if (close) return closeDoc(docs.find(doc => doc.id === Number(close.dataset.close)));
    if (event.target.closest("#tab-add")) return pickFiles();
    const tab = event.target.closest("[data-tab]");
    if (tab) activate(docs.find(doc => doc.id === Number(tab.dataset.tab)));
  });
  $("#tabs").addEventListener("auxclick", event => {
    const tab = event.target.closest("[data-tab]");
    if (tab && event.button === 1) closeDoc(docs.find(doc => doc.id === Number(tab.dataset.tab)));
  });

  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => {
    if (!active || active.status !== "ready") return;
    saveScroll(active);
    active.view = button.dataset.view;
    disposeRaw();
    render();
  }));
  $("#format-select").addEventListener("change", event => { if (active) load(active, {format: event.target.value}, true); });
  $("#tz-toggle").addEventListener("click", () => {
    timeZone = timeZone === "utc" ? "local" : "utc";
    store.set("owl-json-tz", timeZone);
    render();
  });
  $("#export-button").addEventListener("click", event => {
    const menu = $("#export-menu");
    menu.hidden = !menu.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
    const table = active && viewOf(active) === "table";
    menu.querySelectorAll("[data-export='csv'],[data-export='table-json']").forEach(button => { button.disabled = !table; });
  });
  $("#export-menu").addEventListener("click", event => {
    const button = event.target.closest("[data-export]");
    if (!button) return;
    $("#export-menu").hidden = true;
    exportAs(button.dataset.export);
  });
  $("#clear-history").addEventListener("click", clearHistory);
  document.addEventListener("click", event => {
    const recent = event.target.closest("[data-recent]");
    if (recent) return openRecent(recent.dataset.recent);
    if (!event.target.closest("#menu")) closeMenu();
    if (!event.target.closest("#popover,[data-action='columns'],[data-column-menu]")) $("#popover").hidden = true;
    if (!event.target.closest(".menu-wrap")) $("#export-menu").hidden = true;
  });

  // Actions in the center, detail and status areas.
  document.addEventListener("click", event => {
    const doc = active;
    if (!doc) return;
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    const actions = {
      "cancel-load": () => { doc.worker?.terminate(); doc.worker = null; doc.status = "error"; doc.error = {message: "Cancelled."}; render(); },
      "open-anyway": () => load(doc, {}, true),
      "save-limit": () => { limitMb = Math.max(1, Number($("#limit-input").value) || 50); store.set("owl-json-limit-mb", String(limitMb)); load(doc); },
      retry: () => load(doc, {}, true),
      "expand-all": () => expandAll(doc),
      "collapse-all": () => collapseAll(doc),
      "expand-depth": () => { doc.depth = Math.max(1, Number($("#depth-input").value) || 2); collapseAll(doc); expandAll(doc, doc.depth); },
      columns: () => columnChooser(doc, button),
      "clear-filters": () => { const {state} = tableState(doc); state.filters = {}; invalidate(state); $("#view").innerHTML = ""; renderTable(doc); },
      "clear-sort": () => { const {state} = tableState(doc); state.sort = []; invalidate(state); renderTable(doc); },
      "copy-raw": () => copy(JSON.stringify(focusValue(doc), null, 2) ?? "", "Copied formatted JSON"),
      "copy-min": () => copy(JSON.stringify(focusValue(doc)) ?? "", "Copied minified JSON"),
      "copy-selected": () => copyValue(M.getAt(doc.value, doc.selected)),
      "copy-selected-min": () => copy(JSON.stringify(M.getAt(doc.value, doc.selected)), "Copied minified"),
      "focus-selected": () => focus(doc, doc.selected),
      "tree-selected": () => reveal(doc, doc.selected),
      "open-embedded": () => openText(M.getAt(doc.value, doc.selected), `${String(lastKey(doc.selected))} (embedded)`),
      "more-cards": () => { doc.cardLimit = (doc.cardLimit || 100) + 100; renderCard(doc); },
      "show-errors": () => $("#bad-lines")?.scrollIntoView({block: "start"}),
    };
    actions[action]?.();
  });

  $("#breadcrumb").addEventListener("click", event => {
    const crumb = event.target.closest("[data-crumb]");
    if (!crumb || !active) return;
    const depth = Number(crumb.dataset.crumb);
    const path = crumb.hasAttribute("data-crumb-selected") ? active.selected.slice(0, depth) : active.focus.slice(0, depth);
    focus(active, path);
  });
  $("#outline").addEventListener("click", event => {
    const doc = active;
    const item = event.target.closest("[data-outline],[data-outline-key]");
    if (!item || !doc) return;
    if (item.dataset.outline === "unwrap") return focus(doc, [], "table");
    if (item.dataset.outline === "root") return focus(doc, []);
    focus(doc, [item.dataset.outlineKey]);
  });
  $("#goto-form").addEventListener("submit", event => {
    event.preventDefault();
    const doc = active;
    if (!doc || doc.status !== "ready") return;
    const path = M.parsePath($("#goto-input").value);
    if (!path) return toast("Not a path. Try Reservations[0].Instances[2] or .items[0].metadata", "warn");
    if (M.getAt(doc.value, path) === undefined && path.length) return toast("Nothing at that path", "warn");
    reveal(doc, path);
  });
  window.addEventListener("hashchange", () => {
    const doc = active;
    const path = M.parsePath(decodeURIComponent(location.hash.slice(1)));
    if (doc?.status === "ready" && path && !samePath(path, doc.selected) && M.getAt(doc.value, path) !== undefined) reveal(doc, path);
  });

  // Tree interactions.
  $("#view").addEventListener("click", event => {
    const doc = active;
    if (!doc || doc.status !== "ready") return;
    const view = viewOf(doc);
    if (view === "tree") {
      const more = event.target.closest("[data-more],[data-more-all]");
      if (more) {
        const row = doc.rows[Number(more.dataset.more ?? more.dataset.moreAll)];
        const total = Array.isArray(row.parent.value) ? row.parent.value.length : Object.keys(row.parent.value).length;
        doc.pages.set(row.parent.id, more.dataset.moreAll !== undefined ? total : (doc.pages.get(row.parent.id) || PAGE) + MORE);
        return renderTree(doc);
      }
      const toggle = event.target.closest("[data-toggle]");
      if (toggle) return toggleRow(doc, doc.rows[Number(toggle.dataset.toggle)]);
      const element = event.target.closest(".tr[data-index]");
      if (!element) return;
      const row = doc.rows[Number(element.dataset.index)];
      if (row.more) return;
      const path = rowPath(doc, row);
      select(doc, path);
      paintTree(doc);
      // One click on a key or value copies it; the second click of a double-click does not.
      if (event.target.closest("[data-copy-row]") && event.detail <= 1) copyValue(row.value);
      return;
    }
    if (view === "table") {
      const header = event.target.closest(".th[data-column]");
      const {state, source} = tableState(doc);
      if (event.target.closest("[data-column-menu]")) {
        const key = event.target.closest("[data-column-menu]").dataset.columnMenu;
        const column = state.columns.find(item => item.key === key);
        const rect = event.target.getBoundingClientRect();
        return openMenu(rect.left, rect.bottom, [
          {label: "Sort ascending", action: () => { state.sort = [{key, dir: 1}]; invalidate(state); renderTable(doc); }},
          {label: "Sort descending", action: () => { state.sort = [{key, dir: -1}]; invalidate(state); renderTable(doc); }},
          {label: column.pinned ? "Unpin" : "Pin to the left", action: () => { column.pinned = !column.pinned; renderTable(doc); }},
          {label: "Hide column", action: () => { column.visible = false; renderTable(doc); }},
          {label: "Copy column values", hint: "one per line", action: () => copy(tableView(state, source).map(index => M.cellText(fieldOf(source.rows[index], key))).join("\n"), "Copied column")},
        ]);
      }
      if (header && !event.target.closest(".resize")) {
        const key = header.dataset.column;
        const existing = state.sort.find(sort => sort.key === key);
        if (event.shiftKey) {
          if (existing) existing.dir = -existing.dir; else state.sort.push({key, dir: 1});
        } else state.sort = existing && state.sort.length === 1 ? (existing.dir > 0 ? [{key, dir: -1}] : []) : [{key, dir: 1}];
        invalidate(state);
        return renderTable(doc);
      }
      const rowElement = event.target.closest(".tr-row[data-row]");
      if (!rowElement) return;
      const index = Number(rowElement.dataset.row);
      selectTableRow(doc, index);
      const cell = event.target.closest("[data-cell]");
      if (cell) copyValue(fieldOf(source.rows[index], cell.dataset.cell));
      return;
    }
    if (view === "card") {
      const focusRef = event.target.closest("[data-focus-ref]");
      if (focusRef) {
        event.preventDefault();
        const entry = refs.card.get(focusRef.dataset.focusRef);
        return focus(doc, entry.path, focusRef.dataset.focusView || "auto");
      }
      const element = event.target.closest("[data-ref]");
      if (element && (!event.target.closest("summary") || event.target.closest(".ck"))) {
        if (event.target.closest("summary")) event.preventDefault();
        const entry = refs.card.get(element.dataset.ref);
        select(doc, entry.path);
        copyValue(entry.value);
      }
    }
  });
  $("#view").addEventListener("dblclick", event => {
    const doc = active;
    if (!doc || viewOf(doc) !== "tree") return;
    const key = event.target.closest(".k[data-copy-row]");
    if (key) toggleRow(doc, doc.rows[Number(key.dataset.copyRow)]);
  });
  $("#detail").addEventListener("click", event => {
    const doc = active;
    if (!doc?.selected) return;
    const pathButton = event.target.closest("[data-copy-path]");
    if (pathButton) return copy(M.formatPath(doc.selected, pathButton.dataset.copyPath), "Copied path");
    const focusRef = event.target.closest("[data-focus-ref]");
    if (focusRef) { event.preventDefault(); return focus(doc, refs.detail.get(focusRef.dataset.focusRef).path, focusRef.dataset.focusView || "auto"); }
    const element = event.target.closest("[data-ref]");
    if (element) copyValue(refs.detail.get(element.dataset.ref).value);
  });

  // Right-click menus.
  document.addEventListener("contextmenu", event => {
    const doc = active;
    if (!doc || doc.status !== "ready") return;
    let path, value, extra = [];
    const treeRow = event.target.closest("#view .tr[data-index]");
    const cell = event.target.closest("#view .td[data-cell]");
    const refElement = event.target.closest("[data-ref]");
    if (treeRow) {
      const row = doc.rows[Number(treeRow.dataset.index)];
      if (row.more) return;
      path = rowPath(doc, row); value = row.value;
      select(doc, path); paintTree(doc);
    } else if (cell) {
      const {state, source} = tableState(doc);
      const index = Number(cell.closest("[data-row]").dataset.row), row = source.rows[index], key = cell.dataset.cell;
      selectTableRow(doc, index);
      value = fieldOf(row, key);
      path = key.startsWith("@") ? row.path : [...row.path, ...key.split(".")];
      if (M.getAt(doc.value, path) === undefined) path = row.path;
      const text = M.cellText(value);
      extra = [
        {label: "Copy row as JSON", action: () => copy(JSON.stringify(row.value, null, 2), "Copied row")},
        {label: "Copy column values", hint: "one per line", action: () => copy(tableView(state, source).map(at => M.cellText(fieldOf(source.rows[at], key))).join("\n"), "Copied column")},
        {label: `Filter where = ${text.slice(0, 24)}`, action: () => { state.filters[key] = `=${text}`; invalidate(state); renderTable(doc); }},
        {label: `Filter where ≠ ${text.slice(0, 24)}`, action: () => { state.filters[key] = `!=${text}`; invalidate(state); renderTable(doc); }},
      ];
    } else if (refElement) {
      const entry = refs[refElement.closest("#detail") ? "detail" : "card"].get(refElement.dataset.ref);
      if (!entry) return;
      ({path, value} = entry);
    } else return;
    event.preventDefault();
    openMenu(event.clientX, event.clientY, nodeMenu(doc, path, value, extra));
  });
  $("#menu").addEventListener("click", event => {
    const button = event.target.closest("[data-menu]");
    if (!button) return;
    const item = $("#menu").items[Number(button.dataset.menu)];
    closeMenu();
    item.action();
  });

  // Table: filters, resizing, column dragging, chooser.
  let filterTimer;
  $("#view").addEventListener("input", event => {
    const input = event.target.closest("[data-filter]");
    if (!input || !active) return;
    const {state} = tableState(active);
    state.filters[input.dataset.filter] = input.value;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { invalidate(state); renderTable(active); }, 150);
  });
  $("#view").addEventListener("pointerdown", event => {
    const handle = event.target.closest("[data-resize]");
    if (!handle || !active) return;
    event.preventDefault();
    const {state} = tableState(active);
    const column = state.columns.find(item => item.key === handle.dataset.resize);
    const startX = event.clientX, startWidth = column.width;
    const move = moveEvent => {
      column.width = Math.max(60, Math.min(1200, startWidth + moveEvent.clientX - startX));
      renderTable(active);
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  let draggedColumn = null;
  $("#view").addEventListener("dragstart", event => {
    const header = event.target.closest(".th[data-column]");
    if (!header) return;
    draggedColumn = header.dataset.column;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", draggedColumn);
  });
  $("#view").addEventListener("dragover", event => { if (draggedColumn && event.target.closest(".th[data-column]")) event.preventDefault(); });
  $("#view").addEventListener("drop", event => {
    const header = event.target.closest(".th[data-column]");
    if (!draggedColumn || !header || !active) return;
    event.preventDefault();
    event.stopPropagation();
    const {state} = tableState(active);
    const from = state.columns.findIndex(column => column.key === draggedColumn);
    const [column] = state.columns.splice(from, 1);
    state.columns.splice(state.columns.findIndex(item => item.key === header.dataset.column), 0, column);
    draggedColumn = null;
    renderTable(active);
  });
  $("#view").addEventListener("dragend", () => { draggedColumn = null; });
  $("#popover").addEventListener("input", event => {
    if (event.target.id !== "column-find") return;
    const text = event.target.value.toLowerCase();
    document.querySelectorAll("[data-column-item]").forEach(item => { item.hidden = Boolean(text) && !item.dataset.columnItem.includes(text); });
  });
  $("#popover").addEventListener("change", event => {
    const box = event.target.closest("[data-column-show]");
    if (!box || !active) return;
    tableState(active).state.columns[Number(box.dataset.columnShow)].visible = box.checked;
    renderTable(active);
  });
  $("#popover").addEventListener("click", event => {
    const doc = active;
    if (!doc) return;
    const {state} = tableState(doc);
    const all = event.target.closest("[data-columns]");
    const pin = event.target.closest("[data-column-pin]");
    const move = event.target.closest("[data-column-move]");
    if (all) state.columns.forEach(column => { column.visible = all.dataset.columns === "all"; });
    else if (pin) state.columns[Number(pin.dataset.columnPin)].pinned = !state.columns[Number(pin.dataset.columnPin)].pinned;
    else if (move) {
      const index = Number(move.dataset.columnMove), target = index + Number(move.dataset.dir);
      if (target < 0 || target >= state.columns.length) return;
      [state.columns[index], state.columns[target]] = [state.columns[target], state.columns[index]];
    } else return;
    renderTable(doc);
    columnChooser(doc, document.querySelector("[data-action='columns']"));
  });

  // Search.
  $("#search-form").addEventListener("submit", event => {
    event.preventDefault();
    const doc = active;
    if (!doc) return;
    const query = $("#search-input").value;
    if (query !== doc.search.query) { doc.search.query = query; return runSearch(doc); }
    goToMatch(doc, doc.search.index + (event.submitter?.id === "search-prev" ? -1 : 1));
  });
  $("#search-input").addEventListener("input", () => {
    const doc = active;
    if (!doc) return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { doc.search.query = $("#search-input").value; runSearch(doc); }, 250);
  });
  $("#search-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); if (active) goToMatch(active, active.search.index - 1); }
    if (event.key === "Escape") { $("#search-input").value = ""; if (active) { active.search.query = ""; runSearch(active); } }
  });
  $("#search-prev").addEventListener("click", () => active && goToMatch(active, active.search.index - 1));
  $("#search-next").addEventListener("click", () => active && goToMatch(active, active.search.index + 1));
  $("#search-cancel").addEventListener("click", () => {
    if (!active) return;
    active.worker?.postMessage({type: "cancel-search"});
    active.search.done = true;
    renderSearchCount(active);
  });
  document.querySelectorAll("[data-option]").forEach(button => button.addEventListener("click", () => {
    if (!active) return;
    active.search.options[button.dataset.option] = !active.search.options[button.dataset.option];
    runSearch(active);
  }));
  $("#search-scope").addEventListener("change", event => { if (active) { active.search.options.scope = event.target.value; runSearch(active); } });

  // Keyboard.
  document.addEventListener("keydown", event => {
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === "f" && active) { event.preventDefault(); $("#search-input").focus(); $("#search-input").select(); return; }
    if (mod && event.key.toLowerCase() === "o") { event.preventDefault(); pickFiles(); return; }
    if (event.key === "Escape") { closeMenu(); $("#popover").hidden = true; $("#export-menu").hidden = true; }
    const doc = active;
    if (!doc || doc.status !== "ready" || event.target.closest("input,textarea,select,.raw-host") || mod || event.altKey) return;
    const view = viewOf(doc);
    if (view === "tree" && doc.rows) {
      const selectedId = doc.selected ? pathId(doc.selected) : null;
      let index = doc.rows.findIndex(row => row.id === selectedId);
      const row = doc.rows[index];
      const go = next => {
        next = Math.max(0, Math.min(doc.rows.length - 1, next));
        if (doc.rows[next].more) next += next > index ? -1 : 1;
        const target = doc.rows[next];
        if (!target || target.more) return;
        select(doc, rowPath(doc, target));
        scrollToRow(doc, next);
      };
      const keys = {
        ArrowDown: () => go(index + 1), ArrowUp: () => go(index - 1),
        PageDown: () => go(index + 20), PageUp: () => go(index - 20), Home: () => go(0), End: () => go(doc.rows.length - 1),
        ArrowRight: () => { if (!row) return go(0); if (M.isContainer(row.value) && !doc.expanded.has(row.id)) toggleRow(doc, row); else go(index + 1); },
        ArrowLeft: () => { if (!row) return; if (M.isContainer(row.value) && doc.expanded.has(row.id)) toggleRow(doc, row); else if (row.parent) go(doc.rows.indexOf(row.parent)); },
        Enter: () => row && toggleRow(doc, row),
        c: () => row && copyValue(row.value),
      };
      if (keys[event.key]) { event.preventDefault(); keys[event.key](); }
    } else if (view === "table") {
      const {state, source} = tableState(doc);
      const indexes = tableView(state, source);
      const position = indexes.indexOf(state.selected);
      const go = next => { const target = indexes[Math.max(0, Math.min(indexes.length - 1, next))]; if (target !== undefined) selectTableRow(doc, target, true); };
      const keys = {
        ArrowDown: () => go(position + 1), ArrowUp: () => go(position - 1), PageDown: () => go(position + 20), PageUp: () => go(position - 20),
        Home: () => go(0), End: () => go(indexes.length - 1),
        c: () => position >= 0 && copy(JSON.stringify(source.rows[state.selected].value, null, 2), "Copied row"),
        Enter: () => position >= 0 && reveal(doc, source.rows[state.selected].path),
      };
      if (keys[event.key]) { event.preventDefault(); keys[event.key](); }
    }
  });
  window.addEventListener("resize", () => {
    if (!active || active.status !== "ready") return;
    const view = viewOf(active);
    if (view === "tree") paintTree(active);
    if (view === "table") paintTable(active);
  });

  render();
})();
