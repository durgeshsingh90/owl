"use strict";
// JSON Visualizer core: documents and tabs, reading files in a worker per document,
// the outline, breadcrumb and search, and the page's global events. The views live
// in views.js, the side panels and dialogs in panels.js, big files in server.js.
(() => {
  const JV = window.JV, M = JV.M, C = JV.C, $ = JV.$, esc = JV.esc;
  const PROGRESS_FROM = 5 * 1024 * 1024, RECENT_LIMIT = 20, RECENT_KEEP_BYTES = 50 * 1024 * 1024;
  JV.docs = [];
  JV.active = null;
  JV.screen = "home"; // home | doc | diff
  let sequence = 0, pasteCount = 0;

  // ---------- Documents ----------
  JV.newDoc = fields => ({
    id: ++sequence, kind: "client", name: "", size: 0, blob: null, text: undefined, format: "auto", worker: null,
    status: "loading", progress: null, value: undefined, detected: null, lines: null, errors: [], notices: [],
    source: "unknown", unwrap: null, focus: [], sub: null, view: "auto", expanded: new Set(), pages: new Map(),
    selected: null, tables: new Map(), scroll: new Map(), rows: null, error: null, revealed: new Set(), revealAll: false,
    labels: {}, snapshotId: null, query: {lang: "jmes", text: "", result: undefined, error: "", count: null},
    search: {query: "", options: {caseSensitive: false, wholeWord: false, regex: false, scope: "both"}, id: 0, matches: [], matchIds: new Set(), done: true, index: -1, scanned: 0, error: ""},
    ...fields,
  });

  JV.openFiles = (files, replace = null) => {
    let first = true;
    for (const file of files) {
      if (replace && first) { first = false; JV.load(replace, {blob: file, name: file.name, size: file.size, text: undefined, format: "auto", kind: "client"}); continue; }
      const doc = JV.newDoc({name: file.name, size: file.size, blob: file});
      JV.docs.push(doc);
      JV.activate(doc);
      JV.load(doc);
    }
  };
  JV.openText = (text, name, fields = {}) => {
    const doc = JV.newDoc({name, size: new Blob([text]).size, text, ...fields});
    JV.docs.push(doc);
    JV.activate(doc);
    JV.load(doc);
    return doc;
  };
  // A parsed value that did not come from a file (query results, merges, embedded JSON).
  JV.openValue = (value, name, fields = {}) => {
    const text = JSON.stringify(value, null, 2) ?? "null";
    return JV.openText(text, name, {format: "json", derived: true, ...fields});
  };

  JV.load = (doc, changes = {}, force = false) => {
    Object.assign(doc, changes);
    doc.worker?.terminate();
    doc.worker = null;
    if (doc === JV.active) JV.disposeRaw?.();
    Object.assign(doc, {status: "loading", progress: null, value: undefined, error: null, rows: null, unwrap: null, focus: [], sub: null, selected: null, expanded: new Set(), pages: new Map(), tables: new Map(), scroll: new Map()});
    doc.search = {...doc.search, matches: [], matchIds: new Set(), done: true, index: -1, id: 0};
    if (doc.kind === "server") return JV.server.open(doc);
    if (doc.size > JV.settings.limitMb * 1024 * 1024 && !force) {
      doc.status = "large";
      return JV.render();
    }
    const worker = doc.worker = new Worker("worker.js?v=3");
    const started = performance.now();
    worker.onmessage = ({data}) => {
      if (doc.worker !== worker) return;
      if (data.type === "progress") {
        doc.progress = data;
        if (doc === JV.active && JV.screen === "doc") renderProgress(doc);
      } else if (data.type === "opened") opened(doc, data, performance.now() - started);
      else if (data.type === "matches") searchResults(doc, data);
      else if (data.type === "error") {
        doc.status = "error";
        doc.error = data;
        JV.render();
      }
    };
    worker.onerror = event => {
      event.preventDefault();
      doc.status = "error";
      doc.error = {message: `The parser stopped: ${event.message || "out of memory?"}. Other tabs are not affected.`};
      JV.render();
    };
    worker.postMessage({type: "open", blob: doc.blob, text: doc.text, name: doc.name, format: doc.format});
    JV.render();
  };

  function opened(doc, data, took) {
    Object.assign(doc, {status: "ready", value: data.value, detected: data.format, lines: data.lines, errors: data.errors || [], notices: data.notices || [], took});
    doc.source = C.detectSource(doc.value);
    doc.unwrap = C.unwrap(doc.value, doc.source);
    doc.template = doc.unwrap ? C.template(doc.unwrap.rows.map(row => row.value)) : null;
    doc.secretCount = window.JVSecrets && JV.settings.maskSecrets ? window.JVSecrets.scan(doc.value, JV.settings.rules || [], 10000).length : 0;
    doc.expanded.add("");
    if (M.isContainer(doc.value) && !doc.unwrap) {
      const keys = Object.keys(doc.value);
      if (keys.length <= 40) for (const key of keys) if (M.isContainer(doc.value[key]) && Object.keys(doc.value[key]).length <= 40) doc.expanded.add(JV.childId("", Array.isArray(doc.value) ? Number(key) : key));
    }
    JV.remember(doc);
    JV.render();
    if (doc.search.query) JV.runSearch(doc);
  }

  JV.closeDoc = doc => {
    doc.worker?.terminate();
    if (doc.kind === "server") JV.server.close(doc);
    const index = JV.docs.indexOf(doc);
    JV.docs.splice(index, 1);
    if (JV.active === doc) JV.active = JV.docs[Math.min(index, JV.docs.length - 1)] || null;
    if (!JV.active && JV.screen === "doc") JV.screen = "home";
    JV.disposeRaw?.();
    JV.render();
  };
  JV.activate = doc => {
    if (JV.active && JV.active !== doc) JV.saveScroll(JV.active);
    JV.active = doc;
    JV.screen = "doc";
    JV.disposeRaw?.();
    JV.render();
  };
  JV.showHome = () => {
    if (JV.active) JV.saveScroll(JV.active);
    JV.screen = "home";
    JV.disposeRaw?.();
    JV.render();
  };

  // ---------- Focus and views ----------
  // The focus is the node the main area shows; `sub` lists one field of every resource
  // (all Tags of all instances) as its own table.
  JV.focusValue = doc => doc.kind === "server" ? JV.server.focusValue(doc) : M.getAt(doc.value, doc.focus);
  JV.useUnwrap = doc => Boolean(doc.unwrap) && doc.focus.length === 0;
  JV.autoView = doc => {
    if (doc.kind === "server") return JV.server.autoView(doc);
    if (doc.sub || JV.useUnwrap(doc)) return "table";
    const value = JV.focusValue(doc);
    if (Array.isArray(value) && value.length && value.filter(item => M.isContainer(item) && !Array.isArray(item)).length >= value.length / 2) return "table";
    if (M.isContainer(value) && !Array.isArray(value) && doc.focus.length) return "card";
    return "tree";
  };
  JV.viewOf = doc => doc.view === "auto" ? JV.autoView(doc) : doc.view;
  JV.focus = (doc, path, view = "auto", sub = null) => {
    JV.saveScroll(doc);
    doc.focus = path;
    doc.sub = sub;
    doc.view = view;
    doc.expanded.add(JV.pathId(path));
    JV.disposeRaw?.();
    JV.render();
  };
  JV.saveScroll = doc => {
    const viewport = $("#view .scroller");
    if (viewport && doc.status === "ready") doc.scroll.set(`${JV.viewOf(doc)}:${JV.pathId(doc.focus)}:${doc.sub || ""}`, viewport.scrollTop);
  };

  // ---------- Render ----------
  JV.render = () => {
    const doc = JV.active;
    if (JV.screen === "doc" && !doc) JV.screen = "home";
    document.body.dataset.screen = JV.screen;
    renderTabs();
    renderHeader(doc);
    $("#home").hidden = JV.screen !== "home";
    $("#workspace").hidden = JV.screen !== "doc";
    $("#diff").hidden = JV.screen !== "diff";
    if (JV.screen === "home") { document.title = "OWL · JSON Visualizer"; return JV.renderHome(); }
    if (JV.screen === "diff") { document.title = "Compare · OWL JSON Visualizer"; return JV.renderDiff(); }
    document.title = `${doc.name} · OWL JSON Visualizer`;
    JV.setHighlight(doc);
    renderOutline(doc);
    renderStatus(doc);
    renderSearchCount(doc);
    if (doc.status === "loading") return renderProgress(doc);
    if (doc.status === "large") return renderLarge(doc);
    if (doc.status === "error") return renderError(doc);
    renderBreadcrumb(doc);
    const view = JV.viewOf(doc);
    if (view === "tree") JV.renderTree(doc);
    else if (view === "table") JV.renderTable(doc);
    else if (view === "card") JV.renderCard(doc);
    else if (view === "graph") JV.renderGraph(doc);
    else JV.renderRaw(doc);
    JV.renderQueryBar(doc);
    JV.renderDetail(doc);
  };

  function renderHeader(doc) {
    const ready = JV.screen === "doc" && doc?.status === "ready";
    $("#view-switch").hidden = JV.screen !== "doc";
    $("#header-search").hidden = JV.screen !== "doc";
    $("#export-button").hidden = JV.screen !== "doc";
    $("#export-button").disabled = !ready;
    $("#diff-export").hidden = JV.screen !== "diff";
    $("#diff-close").hidden = JV.screen !== "diff";
    $("#compare-button").hidden = JV.screen === "diff";
    const view = ready ? JV.viewOf(doc) : null;
    document.querySelectorAll("[data-view]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.view === view));
      button.disabled = !ready;
    });
  }

  function renderTabs() {
    const tabs = JV.docs.map(doc => {
      const meta = doc.status === "error" ? "Error" : doc.status === "large" ? "Too large" : doc.status === "loading" ? "…" : M.formatBytes(doc.size);
      const current = doc === JV.active && JV.screen === "doc";
      return `<div class="tab${current ? " active" : ""}" role="tab" aria-selected="${current}" data-tab="${doc.id}" title="${esc(doc.name)}${doc.kind === "server" ? " · server mode" : ""}">
        <span class="tab-name">${esc(doc.name)}</span>${current ? `<span class="tab-meta">${esc(meta)}${doc.kind === "server" ? " · server" : ""}</span>` : ""}
        <button type="button" class="tab-close" data-close="${doc.id}" aria-label="Close ${esc(doc.name)}" title="Close">×</button></div>`;
    }).join("");
    $("#tabs").innerHTML = `<button type="button" class="tab home-tab${JV.screen === "home" ? " active" : ""}" data-home title="Open files and recent history">${JV.icon.logo}<span class="sr-only">Home</span></button>${tabs}<button type="button" class="tab-add" id="tab-add" title="Open more files" aria-label="Open files">＋</button>`;
  }

  function clearCenter() {
    for (const id of ["#breadcrumb", "#view-tools", "#dashboard", "#chips", "#detail", "#query-bar"]) $(id).innerHTML = "";
    $("#query-bar").hidden = true;
  }
  function renderProgress(doc) {
    clearCenter();
    const progress = doc.progress;
    const big = doc.size >= PROGRESS_FROM || doc.kind === "server";
    const percent = progress?.total ? Math.min(100, Math.round(progress.loaded / progress.total * 100)) : 0;
    $("#view").innerHTML = `<div class="state-card">
      <h2>${esc(progress?.phase || "Opening")} ${esc(doc.name)}…</h2>
      ${big ? `<div class="progress"><span style="width:${progress?.phase === "Parsing" ? 100 : percent}%"></span></div>
      <p>${progress ? `${esc(M.formatBytes(progress.loaded))} of ${esc(M.formatBytes(progress.total))}` : "Starting…"}</p>` : "<p>Reading…</p>"}
      <button type="button" data-action="cancel-load">Cancel</button></div>`;
  }
  JV.renderProgress = renderProgress;
  function renderLarge(doc) {
    clearCenter();
    $("#view").innerHTML = `<div class="state-card">
      <h2>${esc(doc.name)} is ${esc(M.formatBytes(doc.size))}</h2>
      <p>Files up to <strong>${JV.settings.limitMb} MB</strong> are parsed in the browser. Bigger files can use 5–10 times their size in memory, so they open through the local OWL server instead: it indexes the file and sends only what is on screen.</p>
      <div class="state-actions"><button type="button" class="primary" data-action="open-server">Open with the local server</button><button type="button" data-action="open-anyway">Parse in the browser anyway</button>
      <label>Browser limit <input id="limit-input" type="number" min="1" max="4000" value="${JV.settings.limitMb}"> MB</label><button type="button" data-action="save-limit">Save</button></div></div>`;
  }
  function renderError(doc) {
    clearCenter();
    const error = doc.error || {};
    const caret = error.snippet && error.caret !== null && error.caret !== undefined ? `<pre class="snippet">${esc(error.snippet)}\n${" ".repeat(Math.max(0, error.caret))}^</pre>` : error.snippet ? `<pre class="snippet">${esc(error.snippet)}</pre>` : "";
    $("#view").innerHTML = `<div class="state-card error">
      <h2>Could not read ${esc(doc.name)}</h2>
      <p class="error-message">${esc(error.message || "Unknown error")}${error.line ? ` <span class="where">line ${error.line}${error.column ? `, column ${error.column}` : ""}</span>` : ""}</p>
      ${caret}
      <p>Choose a format to read it again:</p>
      <div class="state-actions">${["auto", "json", "jsonl", "json5"].map(format => `<button type="button" data-reread="${format}"${doc.format === format ? ' class="primary"' : ""}>${format === "auto" ? "Detect" : JV.FORMAT_LABEL[format]}</button>`).join("")}</div></div>`;
  }

  function renderStatus(doc) {
    const parts = [];
    if (doc.detected) parts.push(`<span class="pill">${esc(JV.FORMAT_LABEL[doc.detected])}</span>`);
    parts.push(`<span>${esc(M.formatBytes(doc.size))}${doc.kind === "server" ? " · server mode" : ""}</span>`);
    if (doc.status === "ready" && doc.took) parts.push(`<span title="Time to read and parse">${Math.round(doc.took).toLocaleString()} ms</span>`);
    if (doc.secretCount) parts.push(`<button type="button" class="link" data-action="reveal-all">${doc.revealAll ? "Hide" : "Reveal"} ${doc.secretCount} secret${doc.secretCount === 1 ? "" : "s"}</button>`);
    if (doc.errors.length) parts.push(`<button type="button" class="link warn" data-action="show-errors">${doc.errors.length} bad line${doc.errors.length === 1 ? "" : "s"}</button>`);
    for (const notice of doc.notices) parts.push(`<span class="notice" title="${esc(notice)}">${esc(notice)}</span>`);
    $("#status").innerHTML = parts.join("");
  }
  JV.renderStatus = renderStatus;

  // root › Reservations[*] › Instances[*] for unwrapped lists, else the focus path.
  function renderBreadcrumb(doc) {
    const unwrapTable = (JV.useUnwrap(doc) || doc.sub) && JV.viewOf(doc) === "table";
    const crumbs = [`<button type="button" data-crumb="0" class="${doc.focus.length || unwrapTable ? "" : "current"}">root</button>`];
    if (unwrapTable && doc.unwrap) {
      const parts = doc.unwrap.kind.replace(/\[\]/g, "[*]").split(".");
      parts.forEach((part, index) => crumbs.push(`<button type="button" data-crumb-unwrap="${index}" class="${index === parts.length - 1 && !doc.sub ? "current" : ""}">${esc(part)}</button>`));
      if (doc.sub) crumbs.push(`<button type="button" class="current">${esc(doc.sub)}[*]</button>`);
    } else {
      doc.focus.forEach((part, index) => crumbs.push(`<button type="button" data-crumb="${index + 1}" class="${index === doc.focus.length - 1 ? "current" : ""}">${esc(JV.partText(part))}</button>`));
      const selected = doc.selected && JV.startsWith(doc.selected, doc.focus) && JV.viewOf(doc) === "tree" ? doc.selected.slice(doc.focus.length) : [];
      selected.forEach((part, index) => crumbs.push(`<button type="button" class="beyond${index === selected.length - 1 ? " current" : ""}" data-crumb="${doc.focus.length + index + 1}" data-crumb-selected>${esc(JV.partText(part))}</button>`));
    }
    $("#breadcrumb").innerHTML = crumbs.join(`<span class="sep">›</span>`);
  }
  JV.renderBreadcrumb = renderBreadcrumb;

  // ---------- Outline ----------
  // Fields of the resources that hold lists (Tags, SecurityGroups…), with their total counts.
  JV.subLists = rows => {
    const totals = new Map();
    for (const row of rows.slice(0, 20000)) {
      const record = row.value;
      if (!record || typeof record !== "object" || Array.isArray(record)) continue;
      for (const [key, value] of Object.entries(record)) {
        if (Array.isArray(value) && value.length && value.some(M.isContainer)) totals.set(key, (totals.get(key) || 0) + value.length);
      }
    }
    return [...totals].sort((a, b) => b[1] - a[1]).slice(0, 12);
  };
  function renderOutline(doc) {
    const parts = [];
    if (doc.status === "ready") {
      const detail = doc.template?.name || (doc.unwrap ? doc.unwrap.label : "");
      if (JV.SOURCE_LABEL[doc.source]) parts.push(`<div class="side-block"><span class="side-title">Detected</span>${JV.sourceBadge(doc.source, detail)}</div>`);
      const items = [];
      const tableOn = JV.viewOf(doc) === "table";
      const item = (attrs, name, count, depth, current, meta = "") => `<button type="button" class="outline-item${current ? " current" : ""}" ${attrs} style="--depth:${depth}"><span class="o-name">${esc(name)}</span>${count === null ? `<span class="o-meta">${esc(meta)}</span>` : `<span class="o-count">${count.toLocaleString()}</span>`}</button>`;
      if (doc.kind === "server") items.push(...JV.server.outline(doc));
      else if (doc.unwrap && doc.unwrap.path.length) {
        const nested = doc.unwrap.kind.includes(".");
        const top = doc.unwrap.path[0];
        if (nested) items.push(item(`data-outline-key="${esc(top)}"`, top, M.getAt(doc.value, [top])?.length || 0, 0, JV.samePath(doc.focus, [top])));
        items.push(item(`data-outline="unwrap"`, doc.unwrap.label, doc.unwrap.rows.length, nested ? 1 : 0, JV.useUnwrap(doc) && tableOn && !doc.sub));
        for (const [key, count] of JV.subLists(doc.unwrap.rows)) items.push(item(`data-outline-sub="${esc(key)}"`, key, count, nested ? 2 : 1, doc.sub === key && tableOn));
        for (const key of Object.keys(doc.value).filter(key => key !== top).slice(0, 50)) items.push(outlineKey(doc, key, 0, item));
      } else if (M.isContainer(doc.value)) {
        if (Array.isArray(doc.value)) {
          items.push(item(`data-outline="root"`, doc.detected === "jsonl" ? "Records" : "Items", doc.value.length, 0, !doc.focus.length && !doc.sub));
          if (doc.unwrap) for (const [key, count] of JV.subLists(doc.unwrap.rows)) items.push(item(`data-outline-sub="${esc(key)}"`, key, count, 1, doc.sub === key));
        } else {
          const keys = Object.keys(doc.value);
          for (const key of keys.slice(0, 300)) items.push(outlineKey(doc, key, 0, item));
          if (keys.length > 300) items.push(`<p class="side-note">${(keys.length - 300).toLocaleString()} more keys</p>`);
        }
      }
      parts.push(`<div class="side-block"><span class="side-title">Outline</span>${items.join("") || '<p class="side-note">A single value.</p>'}</div>`);
      parts.push(JV.savedViewsHtml(doc));
      parts.push(JV.bookmarksHtml(doc));
      if (doc.source === "aws" && doc.kind === "client") parts.push(`<div class="side-block"><span class="side-title">Relationships</span><button type="button" class="outline-item${JV.viewOf(doc) === "graph" ? " current" : ""}" data-action="graph" style="--depth:0"><span class="o-name">VPC → subnet → instance graph</span></button></div>`);
      if (doc.errors.length) parts.push(`<div class="side-block"><span class="side-title warn">Bad lines (${doc.errors.length})</span><ul class="bad-lines" id="bad-lines">${doc.errors.slice(0, 200).map(error => `<li><b>Line ${error.line}</b> ${esc(error.message)}<code>${esc(error.snippet.slice(0, 60))}</code></li>`).join("")}</ul></div>`);
    } else parts.push(`<div class="side-block"><span class="side-title">Outline</span><p class="side-note">${doc.status === "loading" ? "Reading…" : "Nothing to show."}</p></div>`);
    $("#outline").innerHTML = parts.join("");
  }
  JV.renderOutline = renderOutline;
  function outlineKey(doc, key, depth, item) {
    const child = doc.value[key], type = M.typeOf(child);
    const count = type === "array" ? child.length : type === "object" ? Object.keys(child).length : null;
    return item(`data-outline-key="${esc(key)}"`, key, count, depth, JV.samePath(doc.focus, [key]), M.preview(child, 18));
  }

  // ---------- Search ----------
  let searchTimer;
  JV.runSearch = doc => {
    clearTimeout(searchTimer);
    const search = doc.search;
    search.id++;
    Object.assign(search, {matches: [], matchIds: new Set(), done: !search.query, index: -1, scanned: 0, error: ""});
    if (!search.query || doc.status !== "ready") { JV.render(); return; }
    if (doc.kind === "server") JV.server.search(doc);
    else doc.worker.postMessage({type: "search", id: search.id, query: search.query, options: search.options});
    renderSearchCount(doc);
  };
  const searchResults = JV.searchResults = (doc, data) => {
    const search = doc.search;
    if (data.id !== search.id) return;
    if (data.error) search.error = data.error;
    const first = !search.matches.length && data.matches.length;
    for (const match of data.matches) {
      search.matches.push(match);
      search.matchIds.add(JV.pathId(match.path));
    }
    search.done = data.done;
    search.scanned = data.scanned;
    if (doc !== JV.active) return;
    renderSearchCount(doc);
    JV.setHighlight(doc);
    if (first) JV.goToMatch(doc, 0);
    else if (JV.viewOf(doc) === "tree") JV.paintTree(doc);
  };
  function renderSearchCount(doc) {
    const search = doc.search;
    if ($("#search-input") !== document.activeElement || !$("#search-input").value) $("#search-input").value = search.query;
    $("#search-cancel").hidden = search.done;
    const count = search.matches.length;
    $("#search-count").textContent = search.error ? "Bad pattern" : !search.query ? "" : count ? `${(search.index + 1).toLocaleString()} of ${count.toLocaleString()}${search.done ? "" : "+"}` : search.done ? "No matches" : "Searching…";
    $("#search-count").title = search.error || (search.done ? "" : `Searching… ${search.scanned.toLocaleString()} nodes so far`);
    $("#search-count").classList.toggle("none", Boolean(search.query && search.done && !count));
  }
  JV.renderSearchCount = renderSearchCount;
  JV.goToMatch = (doc, index) => {
    const search = doc.search;
    if (!search.matches.length) return;
    search.index = (index + search.matches.length) % search.matches.length;
    JV.reveal(doc, search.matches[search.index].path);
    renderSearchCount(doc);
  };

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
  JV.recentList = async () => {
    try {
      const objects = (await db()).transaction("recent").objectStore("recent");
      const list = await new Promise((resolve, reject) => { const request = objects.getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
      return list.sort((a, b) => b.openedAt - a.openedAt);
    } catch { return []; }
  };
  JV.remember = async doc => {
    if (doc.derived) return;
    try {
      const key = `${doc.name}|${doc.size}`;
      const blob = doc.blob || (doc.text !== undefined ? new Blob([doc.text], {type: "text/plain"}) : null);
      const entry = {key, name: doc.name, size: doc.size, format: doc.detected, source: doc.source, detail: doc.template?.name || "", openedAt: Date.now(), blob: blob && doc.size <= RECENT_KEEP_BYTES ? blob : null, server: doc.kind === "server"};
      const list = await JV.recentList();
      const transaction = (await db()).transaction("recent", "readwrite");
      const objects = transaction.objectStore("recent");
      objects.put(entry);
      for (const old of list.filter(item => item.key !== key).slice(RECENT_LIMIT - 1)) objects.delete(old.key);
      transaction.oncomplete = () => { if (JV.screen === "home") JV.renderHome(); };
    } catch { /* recent files are a convenience */ }
  };
  JV.openRecent = async key => {
    const entry = (await JV.recentList()).find(item => item.key === key);
    if (!entry?.blob) return JV.toast("That file was too large to keep in the browser; open it again.", {tone: "warn"});
    JV.openFiles([new File([entry.blob], entry.name)]);
  };
  JV.forgetRecent = async () => {
    try {
      const transaction = (await db()).transaction("recent", "readwrite");
      transaction.objectStore("recent").clear();
      await new Promise(resolve => { transaction.oncomplete = resolve; });
    } catch { /* nothing kept */ }
  };

  // ---------- Paste and open ----------
  JV.pickFiles = () => $("#file-input").click();
  JV.pasteText = text => JV.openText(text, `Pasted ${++pasteCount}`);
  JV.pasteFromClipboard = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text.trim()) return JV.pasteText(text);
    } catch { /* permission denied: use the dialog */ }
    JV.dialog(`<form method="dialog" class="dialog-form" id="paste-form"><h2>Paste JSON</h2>
      <textarea id="paste-text" spellcheck="false" placeholder="Paste JSON, JSON Lines or JSON5 here" aria-label="JSON to open"></textarea>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Open</button></div></form>`, {wide: true});
    $("#paste-text").focus();
  };

  // ---------- Events ----------
  $("#file-input").addEventListener("change", event => { JV.openFiles([...event.target.files]); event.target.value = ""; });
  $("#open-button").addEventListener("click", JV.pickFiles);
  $("#paste-button").addEventListener("click", JV.pasteFromClipboard);
  $("#run-button").addEventListener("click", () => JV.openRunner());
  $("#palette-button").addEventListener("click", () => JV.openPalette());
  $("#compare-button").addEventListener("click", () => JV.openDiff());
  $("#diff-close").addEventListener("click", () => { JV.screen = JV.active ? "doc" : "home"; JV.render(); });
  $("#diff-export").addEventListener("click", event => JV.diffExportMenu(event.currentTarget));
  $("#export-button").addEventListener("click", () => JV.openExport());
  $("#app-dialog").addEventListener("submit", event => {
    if (event.target.id !== "paste-form") return;
    const text = $("#paste-text").value;
    if (text.trim()) JV.pasteText(text);
  });
  document.addEventListener("click", event => { if (event.target.closest("[data-close-dialog]")) JV.closeDialog(); });
  document.addEventListener("paste", event => {
    if (event.target.closest?.("input,textarea,[contenteditable],.raw-host,dialog")) return;
    const text = event.clipboardData?.getData("text");
    if (text?.trim()) { event.preventDefault(); JV.pasteText(text); }
  });

  // Drag and drop: anywhere opens new tabs; onto a tab replaces it; onto the diff view compares.
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
    const under = document.elementsFromPoint(event.clientX, event.clientY);
    const slot = under.find(element => element.dataset?.diffSlot);
    if (JV.screen === "diff") return JV.diffDrop(slot?.dataset.diffSlot || "after", files);
    const tab = under.find(element => element.dataset?.tab);
    const target = tab ? JV.docs.find(doc => doc.id === Number(tab.dataset.tab)) : null;
    JV.openFiles(files, target);
    if (target) JV.activate(target);
  });

  $("#tabs").addEventListener("click", event => {
    const close = event.target.closest("[data-close]");
    if (close) return JV.closeDoc(JV.docs.find(doc => doc.id === Number(close.dataset.close)));
    if (event.target.closest("#tab-add")) return JV.pickFiles();
    if (event.target.closest("[data-home]")) return JV.showHome();
    const tab = event.target.closest("[data-tab]");
    if (tab) JV.activate(JV.docs.find(doc => doc.id === Number(tab.dataset.tab)));
  });
  $("#tabs").addEventListener("auxclick", event => {
    const tab = event.target.closest("[data-tab]");
    if (tab && event.button === 1) JV.closeDoc(JV.docs.find(doc => doc.id === Number(tab.dataset.tab)));
  });
  $("#tabs").addEventListener("contextmenu", event => {
    const tab = event.target.closest("[data-tab]");
    if (!tab) return;
    event.preventDefault();
    const doc = JV.docs.find(item => item.id === Number(tab.dataset.tab));
    JV.openMenu(event.clientX, event.clientY, [
      {label: "Save as snapshot…", disabled: doc.status !== "ready" || doc.kind === "server", action: () => JV.saveSnapshot(doc)},
      {label: "Compare with…", disabled: doc.status !== "ready" || doc.kind === "server", action: () => JV.openDiff(doc)},
      {label: "Merge with other tabs…", disabled: doc.status !== "ready" || doc.kind === "server", action: () => JV.openMerge(doc)},
      "-",
      {label: "Close tab", action: () => JV.closeDoc(doc)},
      {label: "Close other tabs", action: () => { for (const other of [...JV.docs]) if (other !== doc) JV.closeDoc(other); }},
    ]);
  });

  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready") return;
    JV.saveScroll(doc);
    doc.view = button.dataset.view;
    JV.disposeRaw?.();
    JV.render();
  }));

  // Actions shared by several areas.
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const doc = JV.active;
    const action = button.dataset.action;
    const actions = {
      "cancel-load": () => { doc.worker?.terminate(); doc.worker = null; if (doc.kind === "server") JV.server.close(doc); doc.status = "error"; doc.error = {message: "Cancelled."}; JV.render(); },
      "open-anyway": () => JV.load(doc, {}, true),
      "open-server": () => JV.load(doc, {kind: "server"}),
      "save-limit": () => { JV.settings.limitMb = Math.max(1, Number($("#limit-input").value) || 50); JV.saveSettings(); JV.load(doc); },
      "reveal-all": () => { doc.revealAll = !doc.revealAll; JV.render(); },
      "show-errors": () => $("#bad-lines")?.scrollIntoView({block: "start"}),
      graph: () => { doc.view = "graph"; JV.render(); },
    };
    if (actions[action] && doc) { event.preventDefault(); actions[action](); }
  });
  document.addEventListener("click", event => {
    const reread = event.target.closest("[data-reread]");
    if (reread && JV.active) JV.load(JV.active, {format: reread.dataset.reread}, true);
    const reveal = event.target.closest("[data-reveal]");
    if (reveal && JV.active) {
      event.stopPropagation();
      event.preventDefault();
      if (reveal.dataset.reveal) JV.active.revealed.add(reveal.dataset.reveal); else JV.active.revealAll = true;
      JV.render();
    }
  }, true);

  $("#breadcrumb").addEventListener("click", event => {
    const doc = JV.active;
    if (!doc) return;
    const unwrap = event.target.closest("[data-crumb-unwrap]");
    if (unwrap) {
      if (Number(unwrap.dataset.crumbUnwrap) === 0 && doc.unwrap.kind.includes(".")) return JV.focus(doc, [doc.unwrap.path[0]], "table");
      return JV.focus(doc, [], "table");
    }
    const crumb = event.target.closest("[data-crumb]");
    if (!crumb) return;
    const depth = Number(crumb.dataset.crumb);
    const path = crumb.hasAttribute("data-crumb-selected") ? doc.selected.slice(0, depth) : doc.focus.slice(0, depth);
    JV.focus(doc, path, depth === 0 ? "tree" : "auto");
  });
  $("#outline").addEventListener("click", event => {
    const doc = JV.active;
    const item = event.target.closest("[data-outline],[data-outline-key],[data-outline-sub]");
    if (!item || !doc) return;
    if (doc.kind === "server") return JV.server.outlineClick(doc, item);
    if (item.dataset.outline === "unwrap") return JV.focus(doc, [], "table");
    if (item.dataset.outline === "root") return JV.focus(doc, []);
    if (item.dataset.outlineSub) return JV.focus(doc, [], "table", item.dataset.outlineSub);
    JV.focus(doc, [item.dataset.outlineKey]);
  });
  $("#goto-form").addEventListener("submit", event => {
    event.preventDefault();
    const doc = JV.active;
    if (!doc || doc.status !== "ready") return;
    const path = M.parsePath($("#goto-input").value);
    if (!path) return JV.toast("Not a path. Try Reservations[0].Instances[2] or .items[0].metadata", {tone: "warn"});
    if (doc.kind === "client" && path.length && M.getAt(doc.value, path) === undefined) return JV.toast("Nothing at that path", {tone: "warn"});
    JV.reveal(doc, path);
  });
  window.addEventListener("hashchange", () => {
    const doc = JV.active;
    const path = M.parsePath(decodeURIComponent(location.hash.slice(1)));
    if (doc?.status === "ready" && doc.kind === "client" && path && !JV.samePath(path, doc.selected) && M.getAt(doc.value, path) !== undefined) JV.reveal(doc, path);
  });

  // The search box in the header, with its options.
  $("#search-form").addEventListener("submit", event => {
    event.preventDefault();
    const doc = JV.active;
    if (!doc) return;
    const query = $("#search-input").value;
    if (query !== doc.search.query) { doc.search.query = query; return JV.runSearch(doc); }
    JV.goToMatch(doc, doc.search.index + 1);
  });
  $("#search-input").addEventListener("input", () => {
    const doc = JV.active;
    if (!doc) return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { doc.search.query = $("#search-input").value; JV.runSearch(doc); }, 250);
  });
  $("#search-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); if (JV.active) JV.goToMatch(JV.active, JV.active.search.index - 1); }
    if (event.key === "Escape") { $("#search-input").value = ""; if (JV.active) { JV.active.search.query = ""; JV.runSearch(JV.active); } }
  });
  $("#search-prev").addEventListener("click", () => JV.active && JV.goToMatch(JV.active, JV.active.search.index - 1));
  $("#search-next").addEventListener("click", () => JV.active && JV.goToMatch(JV.active, JV.active.search.index + 1));
  $("#search-cancel").addEventListener("click", () => {
    const doc = JV.active;
    if (!doc) return;
    if (doc.kind === "server") JV.server.cancelSearch(doc); else doc.worker?.postMessage({type: "cancel-search"});
    doc.search.done = true;
    renderSearchCount(doc);
  });
  $("#search-options").addEventListener("click", event => {
    const options = JV.active?.search.options || {};
    JV.openPopover(event.currentTarget, `<div class="search-pop" role="group" aria-label="Search options">
      <label><input type="checkbox" data-search-option="caseSensitive"${options.caseSensitive ? " checked" : ""}> Match case</label>
      <label><input type="checkbox" data-search-option="wholeWord"${options.wholeWord ? " checked" : ""}> Whole word</label>
      <label><input type="checkbox" data-search-option="regex"${options.regex ? " checked" : ""}> Regular expression</label>
      <label class="stack">Search in <select id="search-scope"><option value="both">Keys and values</option><option value="keys">Keys only</option><option value="values">Values only</option></select></label></div>`);
    $("#search-scope").value = options.scope || "both";
  });
  $("#popover").addEventListener("change", event => {
    const doc = JV.active;
    if (!doc) return;
    const option = event.target.closest("[data-search-option]");
    if (option) { doc.search.options[option.dataset.searchOption] = option.checked; return JV.runSearch(doc); }
    if (event.target.id === "search-scope") { doc.search.options.scope = event.target.value; JV.runSearch(doc); }
  });

  document.addEventListener("click", event => {
    if (!event.target.closest("#menu")) JV.closeMenu();
    if (!event.target.closest("#popover,[data-popover-anchor],#search-options")) JV.closePopover();
  });
  $("#menu").addEventListener("click", event => {
    const button = event.target.closest("[data-menu]");
    if (!button) return;
    const item = $("#menu").items[Number(button.dataset.menu)];
    JV.closeMenu();
    item.action();
  });
  $("#menu").addEventListener("keydown", event => {
    const buttons = [...$("#menu").querySelectorAll("button:not(:disabled)")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); buttons[(index + 1) % buttons.length]?.focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length]?.focus(); }
  });

  // Keyboard: global shortcuts; tree and table keys are in views.js.
  document.addEventListener("keydown", event => {
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (mod && key === "k") { event.preventDefault(); return JV.openPalette(); }
    if (mod && key === "f" && JV.screen === "doc") { event.preventDefault(); $("#search-input").focus(); $("#search-input").select(); return; }
    if (mod && key === "o") { event.preventDefault(); return JV.pickFiles(); }
    if (event.key === "Escape") { JV.closeMenu(); JV.closePopover(); }
    if (event.target.closest?.("input,textarea,select,.raw-host,dialog")) return;
    const doc = JV.active;
    // ⌘C copies the selected value, ⇧⌘C its JMESPath path (when no text is selected).
    if (mod && key === "c" && JV.screen === "doc" && doc?.selected && !String(getSelection())) {
      event.preventDefault();
      if (event.shiftKey) return JV.copy(M.formatPath(doc.selected, "jmes"), {label: "Copied path of", key: JV.lastKey(doc.selected) ?? "root"});
      return JV.copySelected(doc);
    }
    if (!mod && !event.altKey && JV.screen === "doc" && doc?.status === "ready") JV.viewKeys?.(doc, event);
  });
  window.addEventListener("resize", () => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready" || JV.screen !== "doc") return;
    const view = JV.viewOf(doc);
    if (view === "tree") JV.paintTree(doc);
    if (view === "table") JV.paintTable(doc);
  });

  // Start once every module has loaded.
  window.addEventListener("DOMContentLoaded", () => JV.render());
})();
