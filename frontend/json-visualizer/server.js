"use strict";
// Server mode for files too big to parse in the browser: the local OWL server indexes
// the file and the page asks only for what is on screen — tree children, table pages
// (filtered and sorted on the server), single values and streamed search matches.
(() => {
  const JV = window.JV, M = JV.M, C = JV.C, $ = JV.$, esc = JV.esc;
  const PAGE_ROWS = 500, CHILD_PAGE = 2000;
  const enc = value => encodeURIComponent(JSON.stringify(value));
  const S = JV.server = {};

  // ---------- Opening ----------
  S.open = doc => {
    doc.status = "loading";
    doc.progress = {phase: "Uploading to the local server", loaded: 0, total: doc.size};
    doc.cache = new Map();
    doc.children = new Map();
    JV.render();
    const request = new XMLHttpRequest();
    doc.upload = request;
    request.open("POST", `/api/json-visualizer/documents?name=${encodeURIComponent(doc.name)}`);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = event => {
      doc.progress = {phase: "Uploading to the local server", loaded: event.loaded, total: event.total || doc.size};
      if (doc === JV.active && JV.screen === "doc") JV.renderProgress(doc);
    };
    request.onerror = () => fail(doc, "The local server could not be reached. Is OWL running?");
    request.onload = () => {
      doc.upload = null;
      let data = {};
      try { data = JSON.parse(request.responseText); } catch { /* not JSON */ }
      if (request.status >= 400) return fail(doc, request.status === 404 ? "This needs the latest OWL backend: restart OWL (python dev.py restart)." : data.detail || `Upload failed (${request.status}).`);
      doc.serverId = data.id;
      poll(doc);
    };
    request.send(doc.blob || new Blob([doc.text ?? ""]));
  };
  function fail(doc, message) {
    doc.status = "error";
    doc.error = {message};
    JV.render();
  }
  async function poll(doc) {
    const started = performance.now();
    for (;;) {
      if (!JV.docs.includes(doc) || doc.status !== "loading") return;
      let status;
      try { status = await JV.api(`/documents/${doc.serverId}/status`); } catch (error) { return fail(doc, error.message); }
      if (status.state === "error") return fail(doc, status.error || "Indexing failed.");
      if (status.state === "ready") return ready(doc, status, performance.now() - started);
      doc.progress = {phase: "Indexing", loaded: status.progress?.bytes || 0, total: status.progress?.total || doc.size};
      if (doc === JV.active && JV.screen === "doc") JV.renderProgress(doc);
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  }
  // The resource list to show as a table: Reservations[].Instances[], the root array, or the biggest list.
  function mainList(root) {
    if (root.type === "array") return {path: [], flatten: null, label: "Items", kind: "[]", count: root.count};
    const keys = root.keys || [];
    const reservations = keys.find(item => item.key === "Reservations" && item.type === "array");
    if (reservations) return {path: ["Reservations"], flatten: "Instances", label: "Instances", kind: "Reservations[].Instances[]", count: null, parentCount: reservations.count};
    const lists = keys.filter(item => item.type === "array").sort((a, b) => b.count - a.count);
    return lists[0] ? {path: [lists[0].key], flatten: null, label: lists[0].key, kind: `${lists[0].key}[]`, count: lists[0].count} : null;
  }
  async function ready(doc, status, took) {
    Object.assign(doc, {status: "ready", detected: status.format, took, root: status.root, errors: (status.errors || []).map(error => ({line: error.line, message: error.error || error.message || "", snippet: error.text || error.snippet || ""}))});
    doc.list = mainList(status.root);
    doc.notices = [`Indexed by the local server; only what is on screen is loaded.${status.error_count > (status.errors || []).length ? ` ${status.error_count.toLocaleString()} bad lines in all.` : ""}`];
    if (doc.list) {
      try {
        const page = await rows(doc, {offset: 0, limit: 50, columns: true, sort: [], filters: {}});
        doc.sample = page.rows;
        doc.sampleColumns = page.columns;
        if (doc.list.count === null) doc.list.count = page.total;
        const records = page.rows.map(row => row.value);
        const wrapped = doc.list.flatten ? {Reservations: [{Instances: records}]} : doc.list.path.length ? {[doc.list.path[0]]: records} : records;
        doc.source = C.detectSource(wrapped);
        doc.template = C.template(records);
        doc.unwrap = {path: doc.list.path, rows: [], label: doc.list.label, kind: doc.list.kind};
      } catch (error) { doc.notices.push(error.message); }
    }
    doc.expanded.add("");
    JV.remember(doc);
    JV.render();
  }
  S.close = doc => {
    doc.upload?.abort();
    if (doc.serverId) JV.api(`/documents/${doc.serverId}`, {method: "DELETE"}).catch(() => {});
  };

  // ---------- Values ----------
  S.cachedValue = (doc, path) => doc.cache?.get(JV.pathId(path));
  S.valueAt = async (doc, path) => {
    const id = JV.pathId(path);
    if (doc.cache.has(id)) return doc.cache.get(id);
    const value = await JV.api(`/documents/${doc.serverId}/value?path=${enc(path)}`);
    doc.cache.set(id, value);
    return value;
  };
  S.focusValue = doc => S.cachedValue(doc, doc.focus);
  S.autoView = doc => !doc.focus.length && doc.list ? "table" : "tree";

  // ---------- Outline ----------
  S.outline = doc => {
    const items = [];
    const item = (attrs, name, count, depth, current) => `<button type="button" class="outline-item${current ? " current" : ""}" ${attrs} style="--depth:${depth}"><span class="o-name">${esc(name)}</span>${count === null || count === undefined ? "" : `<span class="o-count">${count.toLocaleString()}</span>`}</button>`;
    const tableOn = JV.viewOf(doc) === "table" && !doc.focus.length;
    if (doc.list?.flatten) {
      items.push(item(`data-outline-key="Reservations"`, "Reservations", doc.list.parentCount, 0, JV.samePath(doc.focus, ["Reservations"])));
      items.push(item(`data-outline="unwrap"`, doc.list.label, doc.list.count, 1, tableOn));
    } else if (doc.list) items.push(item(`data-outline="unwrap"`, doc.list.label, doc.list.count, 0, tableOn));
    for (const key of (doc.root?.keys || []).filter(entry => !doc.list || entry.key !== doc.list.path[0]).slice(0, 100)) {
      items.push(item(`data-outline-key="${esc(key.key)}"`, key.key, key.type === "array" || key.type === "object" ? key.count : undefined, 0, JV.samePath(doc.focus, [key.key])));
    }
    return items;
  };
  S.outlineClick = (doc, element) => {
    if (element.dataset.outline === "unwrap" || element.dataset.outline === "root") return JV.focus(doc, [], "table");
    if (element.dataset.outlineKey) JV.focus(doc, [element.dataset.outlineKey], "tree");
  };

  // ---------- Tree ----------
  // Children are fetched a page at a time and cached per node.
  async function loadChildren(doc, id, path, offset = 0, limit = CHILD_PAGE) {
    const entry = doc.children.get(id) || {total: null, items: [], loading: false};
    if (entry.loading) return;
    entry.loading = true;
    doc.children.set(id, entry);
    try {
      const data = await JV.api(`/documents/${doc.serverId}/children?path=${enc(path)}&offset=${offset}&limit=${limit}`);
      entry.total = data.total;
      entry.items.splice(offset, data.children.length, ...data.children);
    } catch (error) {
      entry.error = error.message;
    } finally { entry.loading = false; }
    if (JV.active === doc && JV.viewOf(doc) === "tree") S.renderTree(doc);
  }
  function previewHtml(child) {
    if (child.type === "object") return `<span class="v t-meta">{ ${(child.count ?? 0).toLocaleString()} key${child.count === 1 ? "" : "s"} }</span>${child.preview ? ` <span class="v-name">${JV.marked(child.preview)}</span>` : ""}`;
    if (child.type === "array") return `<span class="v t-meta">[ ${(child.count ?? 0).toLocaleString()} item${child.count === 1 ? "" : "s"} ]</span>`;
    return null;
  }
  function buildRows(doc) {
    const rootId = JV.pathId(doc.focus);
    const rootValue = S.cachedValue(doc, doc.focus);
    const rows = [{id: rootId, key: doc.focus.length ? JV.lastKey(doc.focus) : null, value: rootValue, container: true, depth: 0, parent: null, root: true, path: doc.focus, preview: doc.focus.length ? "" : `<span class="v t-meta">${doc.root?.type === "array" ? `[ ${(doc.root.count || 0).toLocaleString()} items ]` : `{ ${(doc.root?.keys?.length || 0).toLocaleString()} keys }`}</span>`}];
    const visit = (row, depth) => {
      if (!doc.expanded.has(row.id) || depth > 60) return;
      const entry = doc.children.get(row.id);
      if (!entry) { loadChildren(doc, row.id, row.path); rows.push({id: row.id + "\u0002loading", key: "…", depth: depth + 1, parent: row, loading: true, preview: '<span class="tool-note">Loading…</span>', value: null}); return; }
      if (entry.error) { rows.push({id: row.id + "\u0002error", key: "!", depth: depth + 1, parent: row, preview: `<span class="tool-note">${esc(entry.error)}</span>`, value: null}); return; }
      for (const child of entry.items) {
        if (!child) continue;
        const path = [...row.path, child.key];
        const container = child.type === "object" || child.type === "array";
        const id = JV.childId(row.id, child.key);
        const childRow = {id, key: child.key, value: container ? undefined : child.value, container, depth: depth + 1, parent: row, path, preview: container ? previewHtml(child) : undefined};
        rows.push(childRow);
        if (container) visit(childRow, depth + 1);
      }
      if (entry.total !== null && entry.items.length < entry.total) rows.push({id: row.id + "\u0002more", more: true, parent: row, depth: depth + 1, remaining: entry.total - entry.items.length});
    };
    visit(rows[0], 0);
    return rows;
  }
  S.renderTree = doc => {
    doc.rows = buildRows(doc);
    JV.treeTools(doc, doc.rows.length);
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    JV.mountTree(doc);
  };
  S.more = (doc, row, all) => {
    const entry = doc.children.get(row.parent.id);
    const limit = all ? Math.min(entry.total - entry.items.length, 5000) : CHILD_PAGE;
    loadChildren(doc, row.parent.id, row.parent.path, entry.items.length, limit);
  };
  S.expandAll = async (doc, depth = 2) => {
    // Opens loaded containers level by level (big lists stay paged).
    const open = async (id, path, level) => {
      doc.expanded.add(id);
      if (!doc.children.has(id)) await loadChildren(doc, id, path);
      if (level >= depth) return;
      for (const child of (doc.children.get(id)?.items || []).slice(0, 200)) {
        if (child && (child.type === "object" || child.type === "array")) await open(JV.childId(id, child.key), [...path, child.key], level + 1);
      }
    };
    await open(JV.pathId(doc.focus), doc.focus, 1);
    S.renderTree(doc);
  };
  S.collapseAll = doc => {
    const startId = JV.pathId(doc.focus);
    doc.expanded = new Set([...doc.expanded].filter(id => !id.startsWith(startId)));
    doc.expanded.add(startId);
    S.renderTree(doc);
  };
  // A path is shown from the indexed element that holds it, so huge lists are never paged through.
  S.reveal = async (doc, path) => {
    const root = doc.root?.type === "array" ? 1 : 2;
    const base = path.slice(0, Math.min(path.length, typeof path[1] === "number" || root === 1 ? root : 1));
    doc.focus = base;
    doc.sub = null;
    doc.view = "tree";
    JV.disposeRaw();
    let id = JV.pathId(base);
    doc.expanded.add(id);
    await loadChildren(doc, id, base, 0, 5000);
    for (let index = base.length; index < path.length - 1; index++) {
      id = JV.childId(id, path[index]);
      doc.expanded.add(id);
      if (!doc.children.has(id)) await loadChildren(doc, id, path.slice(0, index + 1), 0, 5000);
    }
    doc.selected = path;
    JV.setHash(path);
    JV.render();
    const target = JV.pathId(path);
    const index = doc.rows.findIndex(row => row.id === target);
    if (index >= 0) JV.scrollToRow(doc, index);
  };

  // ---------- Table ----------
  async function rows(doc, {offset, limit, columns = false, sort, filters}) {
    const list = doc.list;
    const query = `path=${enc(list.path)}${list.flatten ? `&flatten=${encodeURIComponent(list.flatten)}` : ""}&offset=${offset}&limit=${limit}&filters=${enc(filters)}&sort=${enc(sort)}${columns ? "&columns=1" : ""}`;
    return JV.api(`/documents/${doc.serverId}/rows?${query}`);
  }
  S.tableState = doc => {
    let state = doc.tables.get("server");
    const source = {rows: doc.sample || [], key: "server", loading: !state?.total};
    if (state) return {state, source};
    const records = (doc.sample || []).map(row => row.value);
    const keys = doc.sampleColumns || M.columnKeys(records);
    const known = doc.template;
    const hasNames = records.some(record => C.tags(record).length || record?.Name || record?.name);
    let visible = known ? known.columns.map(column => Array.isArray(column) ? column[0] : column).filter(key => key === "@name" ? hasNames : keys.includes(key)) : [...new Set([...keys.filter(key => C.isKeyField(key.split(".").pop())), ...keys])].slice(0, 10);
    const labels = new Map(known ? known.columns.filter(Array.isArray) : []);
    const all = [...(hasNames && !keys.includes("@name") ? ["@name"] : []), ...keys];
    const shown = new Set(visible);
    const order = [...shown, ...all.filter(key => !shown.has(key))];
    state = {columns: order.map(key => ({key, label: labels.get(key) || (key === "@name" ? "Name" : key), width: 170, visible: shown.has(key), pinned: false})), sort: [], filters: {}, groupBy: null, collapsed: new Set(), selection: new Set(), anchor: -1, template: known?.name || "", selected: -1, view: null, dashboard: true, breakdown: false, pages: new Map(), total: null, signature: ""};
    doc.tables.set("server", state);
    return {state, source};
  };
  // Pages of rows for the current filters and sort; a new filter starts over.
  function ensure(doc, state) {
    const signature = JSON.stringify([state.filters, state.sort]);
    if (state.signature !== signature) {
      state.signature = signature;
      state.pages = new Map();
      state.total = null;
      state.view = null;
      state.selection.clear();
    }
    return signature;
  }
  function loadPage(doc, state, page) {
    if (state.pages.has(page)) return;
    const signature = state.signature;
    state.pages.set(page, null);
    rows(doc, {offset: page * PAGE_ROWS, limit: PAGE_ROWS, sort: state.sort, filters: state.filters}).then(data => {
      if (state.signature !== signature) return;
      state.pages.set(page, data.rows);
      const first = state.total === null;
      state.total = data.total;
      state.view = Array.from({length: data.total}, (_, index) => index);
      if (JV.active !== doc || JV.viewOf(doc) !== "table") return;
      if (first) S.renderTable(doc); else S.paintTable(doc);
    }).catch(error => {
      state.pages.delete(page);
      JV.toast(error.message, {tone: "warn"});
    });
  }
  S.rowAt = (doc, index) => {
    const {state} = S.tableState(doc);
    const page = state.pages.get(Math.floor(index / PAGE_ROWS));
    return page ? page[index % PAGE_ROWS] : undefined;
  };
  S.renderTable = doc => {
    if (!doc.list) { $("#view").innerHTML = `<div class="state-card"><p>No list to show as a table here. Use the tree.</p></div>`; return; }
    if (doc.focus.length) { JV.focus(doc, [], "table"); return; }
    const {state} = S.tableState(doc);
    ensure(doc, state);
    if (state.total === null) loadPage(doc, state, 0);
    const total = state.total ?? 0;
    $("#view-tools").innerHTML = "";
    $("#chips").innerHTML = JV.chipsHtml(state, total, doc.list.count ?? total).replace(/Group by[^<]*/g, "");
    dashboard(doc, state);
    const display = Array.from({length: total}, (_, index) => ({index}));
    JV.mountTable(doc, state, {rows: [], key: "server", loading: state.total === null}, display);
  };
  S.paintTable = doc => {
    const {state} = S.tableState(doc);
    const viewport = $("#view .scroller.table");
    if (!viewport || !doc.tableDisplay?.length) return;
    const first = Math.max(0, Math.floor(viewport.scrollTop / 36) - 10);
    const last = Math.min(doc.tableDisplay.length, first + Math.ceil(viewport.clientHeight / 36) + 25);
    for (let page = Math.floor(first / PAGE_ROWS); page <= Math.floor(last / PAGE_ROWS); page++) loadPage(doc, state, page);
    JV.paintRows(doc, state, position => S.rowAt(doc, position));
  };
  let summaryKey = "";
  async function dashboard(doc, state) {
    const sample = doc.sample || [];
    state.statusKey = state.statusKey ?? JV.statusField(sample) ?? "";
    const key = `${doc.id}:${state.statusKey}:${state.breakdown}`;
    if (state.summary && summaryKey === key) return paint();
    summaryKey = key;
    const type = ["InstanceType", "hardwareProfile.vmSize", "type", "kind"].find(field => sample.some(row => M.getField(row.value, field) !== undefined));
    const region = ["Placement.AvailabilityZone", "location", "region", "metadata.namespace"].find(field => sample.some(row => M.getField(row.value, field) !== undefined));
    const fields = [state.statusKey, ...(state.breakdown ? [type, region] : [])].filter(Boolean);
    paint();
    if (!fields.length) return;
    try {
      const list = doc.list;
      const data = await JV.api(`/documents/${doc.serverId}/summary?path=${enc(list.path)}${list.flatten ? `&flatten=${encodeURIComponent(list.flatten)}` : ""}&fields=${encodeURIComponent(fields.join(","))}`);
      state.summary = data;
      state.summaryFields = {type, region};
      if (JV.active === doc && JV.viewOf(doc) === "table") paint();
    } catch (error) { state.summary = {error: error.message}; }
    function paint() {
      const summary = state.summary || {};
      const breakdowns = [];
      const named = {[state.statusKey]: "State", [state.summaryFields?.type]: "Type", [state.summaryFields?.region]: "Location"};
      for (const [field, items] of Object.entries(summary.breakdowns || {})) if (state.breakdown) breakdowns.push({key: field, label: named[field] || field, items});
      $("#dashboard").innerHTML = JV.dashboardHtml(doc, state, sample, {total: summary.total ?? doc.list.count, statusCounts: summary.breakdowns?.[state.statusKey] || [], breakdowns});
    }
  }
  // Every row for the current filters (for exports and copies), optionally only some positions.
  S.rowsFor = async (doc, positions) => {
    const {state} = S.tableState(doc);
    const total = Math.min(state.total ?? 0, 200000);
    const out = [];
    for (let offset = 0; offset < total; offset += 5000) {
      const data = await rows(doc, {offset, limit: 5000, sort: state.sort, filters: state.filters});
      out.push(...data.rows);
    }
    return positions ? positions.map(index => out[index]).filter(Boolean) : out;
  };

  // ---------- Cards and raw ----------
  S.renderCard = async doc => {
    JV.refs.card.clear();
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    $("#view-tools").innerHTML = `<span class="tool-note">Click a key or value to copy it · right-click for more</span>`;
    const path = doc.focus.length ? doc.focus : doc.selected;
    if (!path) { $("#view").innerHTML = `<div class="state-card"><p>Select a row or open a part of the file to see it as cards.</p></div>`; return; }
    $("#view").innerHTML = `<div class="state-card"><p>Loading…</p></div>`;
    try {
      const value = await S.valueAt(doc, path);
      if (JV.active !== doc || JV.viewOf(doc) !== "card") return;
      $("#view").innerHTML = `<div class="scroller cards">${JV.cardsHtml(doc, value, path)}</div>`;
    } catch (error) { $("#view").innerHTML = `<div class="state-card"><p>${esc(error.message)}</p></div>`; }
  };
  S.renderRaw = async doc => {
    const path = doc.focus.length ? doc.focus : doc.selected;
    if (!path) { $("#view").innerHTML = `<div class="state-card"><p>Select a row or open a part of the file to see it as raw JSON.</p></div>`; return; }
    try {
      doc.rawValue = await S.valueAt(doc, path);
      JV.renderRaw(doc, doc.rawValue);
    } catch (error) { $("#view").innerHTML = `<div class="state-card"><p>${esc(error.message)}</p></div>`; }
  };

  // ---------- Search ----------
  S.search = async doc => {
    const search = doc.search, id = search.id;
    try {
      const {search_id: sid} = await JV.api(`/documents/${doc.serverId}/search`, {method: "POST", body: {query: search.query, ...search.options}});
      doc.serverSearch = sid;
      let after = 0;
      for (;;) {
        if (doc.search.id !== id) { JV.api(`/documents/${doc.serverId}/search/${sid}`, {method: "DELETE"}).catch(() => {}); return; }
        const data = await JV.api(`/documents/${doc.serverId}/search/${sid}?after=${after}`);
        after += data.matches.length;
        JV.searchResults(doc, {id, matches: data.matches, done: data.done, scanned: data.scanned_bytes || 0});
        if (data.done) return;
        await new Promise(resolve => setTimeout(resolve, 300));
      }
    } catch (error) { JV.searchResults(doc, {id, matches: [], done: true, scanned: 0, error: error.message}); }
  };
  S.cancelSearch = doc => {
    if (doc.serverSearch) JV.api(`/documents/${doc.serverId}/search/${doc.serverSearch}`, {method: "DELETE"}).catch(() => {});
    doc.search.id++;
  };
})();
