"use strict";
// The main views: a virtualised tree, a virtualised table (with summary cards, filter
// chips, grouping, multi-row selection and remembered layouts), key/value cards with
// security-rule and IAM-policy viewers, raw JSON (Monaco) and a resource graph.
(() => {
  const JV = window.JV, M = JV.M, C = JV.C, $ = JV.$, esc = JV.esc;
  const ROW = 30, TABLE_ROW = 36, PAGE = 2000, MORE = 5000, EXPAND_CAP = 200000;

  // ---------- Tree ----------
  // The visible rows: the focus node, then every expanded node's children (paged).
  JV.buildRows = doc => {
    const rootValue = JV.focusValue(doc), rootId = JV.pathId(doc.focus);
    const rows = [{id: rootId, key: doc.focus.length ? JV.lastKey(doc.focus) : null, value: rootValue, depth: 0, parent: null, root: true}];
    const stack = [];
    const open = row => {
      if (!M.isContainer(row.value) || !doc.expanded.has(row.id)) return;
      const keys = Array.isArray(row.value) ? null : Object.keys(row.value);
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
      const row = {id: JV.childId(frame.row.id, part), key: part, value: frame.row.value[part], depth: frame.row.depth + 1, parent: frame.row};
      rows.push(row);
      open(row);
    }
    return rows;
  };
  JV.rowPath = (doc, row) => {
    const parts = [];
    for (let at = row; at && !at.root; at = at.parent) parts.push(at.key);
    return [...doc.focus, ...parts.reverse()];
  };

  let treeFrame = 0;
  JV.renderTree = doc => {
    if (doc.kind === "server") return JV.server.renderTree(doc);
    doc.rows = JV.buildRows(doc);
    treeTools(doc, doc.rows.length);
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    mountTree(doc);
  };
  function treeTools(doc, count) {
    $("#view-tools").innerHTML = `<button type="button" class="tool" data-tree="expand-all" title="Expand everything (up to ${EXPAND_CAP.toLocaleString()} rows)">Expand all</button>
      <button type="button" class="tool" data-tree="collapse-all">Collapse all</button>
      <button type="button" class="tool" data-tree="depth" title="Expand to a depth">Depth ${doc.depth || 2}</button>
      <span class="tool-note">${count.toLocaleString()} rows</span>`;
  }
  JV.treeTools = treeTools;
  function mountTree(doc) {
    let viewport = $("#view .scroller.tree");
    const key = `${doc.id}:${JV.pathId(doc.focus)}`;
    if (!viewport || viewport.dataset.key !== key) {
      $("#view").innerHTML = `<div class="scroller tree" tabindex="0" role="tree" aria-label="JSON tree"><div class="tree-card"><div class="spacer"></div><div class="rows"></div></div></div>`;
      viewport = $("#view .scroller.tree");
      viewport.dataset.key = key;
      viewport.addEventListener("scroll", () => { cancelAnimationFrame(treeFrame); treeFrame = requestAnimationFrame(() => JV.paintTree(JV.active)); });
      const saved = doc.scroll.get(`tree:${JV.pathId(doc.focus)}:`);
      if (saved) requestAnimationFrame(() => { viewport.scrollTop = saved; });
    }
    viewport.querySelector(".spacer").style.height = `${doc.rows.length * ROW + 16}px`;
    JV.paintTree(doc);
  }
  JV.mountTree = mountTree;
  JV.paintTree = doc => {
    const viewport = $("#view .scroller.tree");
    if (!viewport || !doc?.rows) return;
    const first = Math.max(0, Math.floor(viewport.scrollTop / ROW) - 15);
    const last = Math.min(doc.rows.length, first + Math.ceil(viewport.clientHeight / ROW) + 30);
    const selectedId = doc.selected ? JV.pathId(doc.selected) : null;
    const current = doc.search.matches[doc.search.index];
    const currentId = current ? JV.pathId(current.path) : null;
    const matchIds = doc.search.matchIds || new Set();
    const bookmarks = new Set(JV.bookmarksOf(doc).map(item => JV.pathId(item.path)));
    const html = [];
    for (let index = first; index < last; index++) {
      const row = doc.rows[index];
      const indent = row.depth * 20 + 10;
      if (row.more) {
        html.push(`<div class="tr more" style="top:${index * ROW + 8}px;padding-left:${indent + 20}px"><button type="button" class="link" data-more="${index}">Show ${Math.min(MORE, row.remaining).toLocaleString()} more</button><span class="tool-note">${row.remaining.toLocaleString()} not shown</span>${row.remaining > MORE ? ` <button type="button" class="link" data-more-all="${index}">Show all</button>` : ""}</div>`);
        continue;
      }
      const container = row.loading ? false : M.isContainer(row.value) || row.container;
      const open = container && doc.expanded.has(row.id);
      const isIndex = typeof row.key === "number";
      const keyText = row.root ? (doc.focus.length ? esc(JV.partText(row.key)) : "") : isIndex ? `[${row.key}]` : JV.marked(row.key);
      const classes = ["tr"];
      if (row.id === selectedId) classes.push("selected");
      if (matchIds.has(row.id)) classes.push("match");
      if (row.id === currentId) classes.push("current");
      const keyField = !isIndex && C.isKeyField(row.key);
      const value = row.preview !== undefined ? row.preview : JV.valueHtml(row.key, row.value, {id: row.id});
      html.push(`<div class="${classes.join(" ")}" role="treeitem" aria-level="${row.depth + 1}"${container ? ` aria-expanded="${open}"` : ""} style="top:${index * ROW + 8}px;padding-left:${indent}px" data-index="${index}">
        ${container ? `<button type="button" class="chev" data-toggle="${index}" tabindex="-1" aria-label="${open ? "Collapse" : "Expand"}">${open ? "▾" : "▸"}</button>` : `<span class="chev"></span>`}${keyText ? `<span class="k${isIndex ? " index" : ""}${keyField ? " keyfield" : ""}" data-copy-row="${index}" title="Click to copy the value · double-click to ${container ? "expand" : "select"}">${keyText}</span>${isIndex ? "" : `<span class="colon">:</span>`}` : ""}
        <span class="val" data-copy-row="${index}">${value}</span>${bookmarks.has(row.id) ? `<span class="bookmark-mark" title="Bookmarked">★</span>` : ""}
        <span class="copy-pill" data-copy-row="${index}" title="Copy value">${JV.icon.copy}Click to copy</span></div>`);
    }
    viewport.querySelector(".rows").innerHTML = html.join("");
  };
  JV.toggleRow = (doc, row) => {
    if (!(M.isContainer(row.value) || row.container)) return;
    if (doc.expanded.has(row.id)) doc.expanded.delete(row.id);
    else doc.expanded.add(row.id);
    if (doc.kind === "server") return JV.server.renderTree(doc);
    JV.renderTree(doc);
  };
  // Breadth first, so a cap still opens the upper levels everywhere.
  JV.expandAll = (doc, maxDepth = Infinity) => {
    const startId = JV.pathId(doc.focus);
    let level = [{id: startId, value: JV.focusValue(doc)}], depth = 0, rows = 1;
    const expanded = new Set([...doc.expanded].filter(id => !id.startsWith(startId) || id === startId));
    expanded.add(startId);
    while (level.length && depth < maxDepth && rows < EXPAND_CAP) {
      const next = [];
      for (const node of level) {
        if (!M.isContainer(node.value)) continue;
        expanded.add(node.id);
        const keys = Array.isArray(node.value) ? null : Object.keys(node.value);
        const limit = Math.min(keys ? keys.length : node.value.length, PAGE);
        rows += limit;
        for (let index = 0; index < limit; index++) {
          const part = keys ? keys[index] : index;
          if (M.isContainer(node.value[part])) next.push({id: JV.childId(node.id, part), value: node.value[part]});
        }
        if (rows >= EXPAND_CAP) break;
      }
      level = next;
      depth++;
    }
    doc.expanded = expanded;
    if (rows >= EXPAND_CAP) JV.toast(`Expanded until ${EXPAND_CAP.toLocaleString()} rows; open deeper parts one by one.`, {tone: "warn"});
    JV.renderTree(doc);
  };
  JV.collapseAll = doc => {
    const startId = JV.pathId(doc.focus);
    doc.expanded = new Set([...doc.expanded].filter(id => !id.startsWith(startId)));
    doc.expanded.add(startId);
    JV.renderTree(doc);
  };
  JV.scrollToRow = (doc, index) => {
    const viewport = $("#view .scroller.tree");
    if (!viewport) return;
    const top = index * ROW;
    if (top < viewport.scrollTop + ROW || top > viewport.scrollTop + viewport.clientHeight - ROW * 2) viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 3);
    JV.paintTree(doc);
  };

  // Open every ancestor of a path in the tree (paging far enough), then select it.
  JV.reveal = (doc, path, {select = true} = {}) => {
    if (doc.kind === "server") return JV.server.reveal(doc, path);
    if (!JV.startsWith(path, doc.focus) || JV.viewOf(doc) !== "tree") {
      doc.focus = JV.startsWith(path, doc.focus) && !doc.sub ? doc.focus : [];
      doc.sub = null;
      doc.view = "tree";
      JV.disposeRaw();
    }
    let id = JV.pathId(doc.focus), value = JV.focusValue(doc);
    doc.expanded.add(id);
    for (const part of path.slice(doc.focus.length, -1)) {
      const keys = Array.isArray(value) ? null : Object.keys(value);
      const position = keys ? keys.indexOf(part) : part;
      if (position >= (doc.pages.get(id) || PAGE)) doc.pages.set(id, position + 1);
      id = JV.childId(id, part);
      value = value?.[part];
      doc.expanded.add(id);
    }
    if (path.length > doc.focus.length && M.isContainer(value)) {
      const keys = Array.isArray(value) ? null : Object.keys(value);
      const position = keys ? keys.indexOf(JV.lastKey(path)) : JV.lastKey(path);
      if (position >= (doc.pages.get(id) || PAGE)) doc.pages.set(id, position + 1);
    }
    if (select) { doc.selected = path; JV.setHash(path); }
    JV.render();
    const target = JV.pathId(path);
    const index = doc.rows.findIndex(row => row.id === target);
    if (index >= 0) JV.scrollToRow(doc, index);
  };

  // ---------- Table ----------
  // Rows of the table: resources unwrapped from their envelope, one field of every
  // resource (`sub`), or the focus array / object.
  JV.tableSource = doc => {
    const baseRows = doc.unwrap ? doc.unwrap.rows : Array.isArray(doc.value) ? doc.value.map((value, index) => ({value, path: [index]})) : [];
    if (doc.sub) {
      const rows = [];
      for (const row of baseRows) {
        const list = row.value?.[doc.sub];
        if (Array.isArray(list)) list.forEach((item, index) => rows.push({value: item, path: [...row.path, doc.sub, index], parent: row}));
      }
      return {rows, key: `sub:${doc.sub}`, parentLabel: true};
    }
    if (JV.useUnwrap(doc)) return {rows: doc.unwrap.rows, key: "unwrap"};
    const value = JV.focusValue(doc);
    if (Array.isArray(value)) return {rows: value.map((item, index) => ({value: item, path: [...doc.focus, index], key: index})), key: JV.pathId(doc.focus)};
    if (M.isContainer(value)) return {rows: Object.keys(value).map(key => ({value: value[key], path: [...doc.focus, key], key})), key: JV.pathId(doc.focus), keyed: true};
    return {rows: [{value, path: doc.focus, key: JV.lastKey(doc.focus)}], key: JV.pathId(doc.focus)};
  };
  const LAYOUTS = "owl-json-layouts";
  // The key layouts are remembered under: the command type (EC2 instances…) or the columns.
  function layoutKey(doc, state) {
    const base = state.template || `cols:${state.columns.slice(0, 6).map(column => column.key).join(",")}`;
    return doc.sub ? `${base}>${doc.sub}` : base;
  }
  const layoutOf = state => ({columns: state.columns.map(({key, label, width, visible, pinned}) => ({key, label, width, visible, pinned})), sort: state.sort, filters: state.filters, groupBy: state.groupBy || null});
  function applyLayout(state, layout) {
    if (!layout) return;
    const known = new Map(state.columns.map(column => [column.key, column]));
    const ordered = [];
    for (const saved of layout.columns || []) {
      const column = known.get(saved.key) || (saved.key.startsWith("@tag:") ? {key: saved.key, label: saved.label || saved.key.slice(5), width: 160} : null);
      if (!column) continue;
      Object.assign(column, {width: saved.width || column.width, visible: saved.visible !== false, pinned: Boolean(saved.pinned)});
      ordered.push(column);
      known.delete(saved.key);
    }
    state.columns = [...ordered, ...[...known.values()].map(column => ({...column, visible: false}))];
    state.sort = layout.sort || [];
    state.filters = {...(layout.filters || {})};
    state.groupBy = layout.groupBy || null;
    state.view = null;
  }
  JV.rememberLayout = (doc, state) => {
    const all = JV.store.json(LAYOUTS, {});
    const key = layoutKey(doc, state);
    all[key] = {...(all[key] || {}), last: layoutOf(state)};
    JV.store.setJson(LAYOUTS, all);
  };
  JV.savedViews = (doc, state) => JV.store.json(LAYOUTS, {})[layoutKey(doc, state)]?.saved || [];
  JV.saveView = (doc, state, name) => {
    const all = JV.store.json(LAYOUTS, {});
    const key = layoutKey(doc, state);
    const entry = all[key] || {};
    entry.saved = [...(entry.saved || []).filter(view => view.name !== name), {name, layout: layoutOf(state)}];
    all[key] = entry;
    JV.store.setJson(LAYOUTS, all);
  };
  JV.deleteView = (doc, state, name) => {
    const all = JV.store.json(LAYOUTS, {});
    const key = layoutKey(doc, state);
    if (all[key]) all[key].saved = (all[key].saved || []).filter(view => view.name !== name);
    JV.store.setJson(LAYOUTS, all);
  };
  JV.applySavedView = (doc, name) => {
    const {state} = JV.tableState(doc);
    if (name === "@default") { doc.tables.delete(JV.tableSource(doc).key); const fresh = JV.tableState(doc, {fresh: true}).state; JV.rememberLayout(doc, fresh); }
    else applyLayout(state, JV.savedViews(doc, state).find(view => view.name === name)?.layout);
    JV.rememberLayout(doc, JV.tableState(doc).state);
    doc.view = "table";
    JV.render();
  };

  JV.tableState = (doc, {fresh = false} = {}) => {
    if (doc.kind === "server") return JV.server.tableState(doc);
    const source = JV.tableSource(doc);
    let state = doc.tables.get(source.key);
    if (state && state.rowsRef === source.rows.length) return {state, source};
    const records = source.rows.map(row => row.value);
    const objectRows = records.some(record => M.isContainer(record) && !Array.isArray(record));
    const keys = objectRows ? M.columnKeys(records) : [];
    const known = doc.sub ? null : C.template(records);
    const hasNames = objectRows && records.slice(0, 50).some(record => C.tags(record).length || record?.Name || record?.name);
    let visible;
    if (known) visible = known.columns.map(column => Array.isArray(column) ? column[0] : column).filter(key => key === "@name" ? hasNames : keys.includes(key));
    else {
      const keyFields = keys.filter(key => C.isKeyField(key.split(".").pop()));
      visible = [...new Set([...keyFields, ...keys])].slice(0, 10);
    }
    const labels = new Map(known ? known.columns.filter(Array.isArray) : []);
    const all = [...(source.keyed ? ["@key"] : []), ...(source.parentLabel ? ["@parent"] : []), ...(hasNames && !keys.includes("@name") ? ["@name"] : []), ...keys, ...(objectRows ? [] : ["@value"])];
    const shown = new Set([...(source.keyed ? ["@key"] : []), ...(source.parentLabel ? ["@parent"] : []), ...visible, ...(objectRows ? [] : ["@value"])]);
    const order = [...shown, ...all.filter(key => !shown.has(key))];
    const label = key => labels.get(key) || {"@name": "Name", "@key": "Key", "@value": "Value", "@parent": "Resource"}[key] || key;
    const columns = order.map(key => ({key, label: label(key), width: key === "@key" || key === "@parent" ? 190 : 170, visible: shown.has(key), pinned: false}));
    state = {columns, sort: [], filters: {}, groupBy: null, collapsed: new Set(), selection: new Set(), anchor: -1, rowsRef: source.rows.length, template: known?.name || "", selected: -1, view: null, dashboard: true, breakdown: false, coverageTag: null};
    if (!fresh) applyLayout(state, JV.store.json(LAYOUTS, {})[layoutKey(doc, state)]?.last);
    doc.tables.set(source.key, state);
    return {state, source};
  };
  // A cell's value: dotted fields, the display name, a tag (@tag:Owner), the row key or its parent resource.
  JV.fieldOf = (row, key) => {
    if (key === "@key") return row.key;
    if (key === "@value") return row.value;
    if (key === "@parent") return row.parent ? C.displayName(row.parent.value) : undefined;
    if (key.startsWith("@tag:")) {
      const tag = C.tags(row.value).find(item => item.key === key.slice(5));
      return tag ? tag.value : undefined;
    }
    return M.getField(row.value, key, C.displayName);
  };
  JV.orderedColumns = state => {
    const visible = state.columns.filter(column => column.visible);
    return [...visible.filter(column => column.pinned), ...visible.filter(column => !column.pinned)];
  };
  JV.columnLabel = (state, key) => state.columns.find(column => column.key === key)?.label || (key.startsWith("@tag:") ? `Tag:${key.slice(5)}` : key);
  // Filtered and sorted row indexes, cached until a filter or the sort changes.
  JV.tableView = (state, source) => {
    const signature = JSON.stringify([state.filters, state.sort]);
    if (state.view && state.viewSignature === signature) return state.view;
    const filters = Object.entries(state.filters).filter(([, text]) => String(text).trim()).map(([key, text]) => [key, M.filterTest(text)]);
    const indexes = [];
    for (let index = 0; index < source.rows.length; index++) {
      const row = source.rows[index];
      if (filters.every(([key, test]) => test(JV.fieldOf(row, key)))) indexes.push(index);
    }
    if (state.sort.length) {
      const keys = state.sort.map(sort => ({dir: sort.dir, values: new Map(indexes.map(index => [index, JV.fieldOf(source.rows[index], sort.key)]))}));
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
    state.display = null;
    return indexes;
  };
  // What the virtual list shows: rows, or group headers followed by their rows.
  JV.tableDisplay = (state, source) => {
    const indexes = JV.tableView(state, source);
    if (!state.groupBy) return indexes.map(index => ({index}));
    if (state.display && state.displaySignature === `${state.viewSignature}|${state.groupBy}|${[...state.collapsed].join(",")}`) return state.display;
    const groups = new Map();
    for (const index of indexes) {
      const value = M.cellText(JV.fieldOf(source.rows[index], state.groupBy));
      if (!groups.has(value)) groups.set(value, []);
      groups.get(value).push(index);
    }
    const display = [];
    for (const [value, members] of [...groups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))) {
      display.push({group: value, count: members.length});
      if (!state.collapsed.has(value)) for (const index of members) display.push({index});
    }
    state.display = display;
    state.displaySignature = `${state.viewSignature}|${state.groupBy}|${[...state.collapsed].join(",")}`;
    return display;
  };

  // The field that holds each resource's status (State.Name, status.phase…).
  const STATUS_FIELDS = ["State.Name", "State", "status.phase", "status", "Status", "provisioningState", "properties.provisioningState", "powerState", "phase", "Phase", "state"];
  const TYPE_FIELDS = ["InstanceType", "hardwareProfile.vmSize", "machineType", "VolumeType", "DBInstanceClass", "type", "Type", "kind"];
  const REGION_FIELDS = ["Placement.AvailabilityZone", "AvailabilityZone", "location", "zone", "region", "Region", "metadata.namespace", "provider_name"];
  JV.statusField = rows => STATUS_FIELDS.find(key => rows.slice(0, 50).some(row => typeof M.getField(row.value, key) === "string" && C.statusTone(key.split(".").pop() === "Name" ? "State" : key.split(".").pop(), M.getField(row.value, key))));
  const firstField = (rows, keys) => keys.find(key => rows.slice(0, 50).some(row => { const value = M.getField(row.value, key); return value !== undefined && !M.isContainer(value); }));
  function counts(rows, key) {
    const map = new Map();
    for (const row of rows) {
      const value = M.cellText(M.getField(row.value, key));
      if (value !== "") map.set(value, (map.get(value) || 0) + 1);
    }
    return [...map].sort((a, b) => b[1] - a[1]);
  }
  // Summary cards over every resource: count, top statuses, a missing tag; and breakdowns.
  JV.dashboardHtml = (doc, state, rows, {total, statusCounts, tagMissing, tagKey, tagKeys, breakdowns} = {}) => {
    if (!state.dashboard || !rows.length && !total) return "";
    const noun = doc.unwrap?.label || (doc.sub ? doc.sub : doc.detected === "jsonl" ? "Records" : "Rows");
    const cards = [`<div class="stat"><div class="stat-label">${esc(noun)}</div><div class="stat-value">${(total ?? rows.length).toLocaleString()}</div></div>`];
    const statusKey = state.statusKey;
    // Healthy states first (running, available…), then the most common others.
    const rank = value => ({good: 0, warn: 1, bad: 2})[C.statusTone("State", value)] ?? 3;
    for (const [value, count] of [...(statusCounts || [])].sort((a, b) => rank(a[0]) - rank(b[0]) || b[1] - a[1]).slice(0, 3)) {
      const tone = C.statusTone("State", value) || "none";
      cards.push(`<button type="button" class="stat tone-text-${tone}" data-stat-filter="${esc(statusKey)}" data-stat-value="${esc(value)}" title="Show only ${esc(value)}"><div class="stat-label">${esc(value[0].toUpperCase() + value.slice(1))}</div><div class="stat-value">${count.toLocaleString()}</div></button>`);
    }
    if (tagKey) {
      cards.push(`<div class="stat"><div class="stat-label">Missing <select class="stat-select" data-coverage aria-label="Tag to check">${tagKeys.map(key => `<option${key === tagKey ? " selected" : ""}>${esc(key)}</option>`).join("")}</select> tag</div><button type="button" class="stat-value warn-text" data-stat-filter="@tag:${esc(tagKey)}" data-stat-value="empty" title="Show resources without this tag">${tagMissing.toLocaleString()}</button><div class="stat-sub">${total ? Math.round(tagMissing / total * 100) : 0}% of ${esc(noun.toLowerCase())}</div></div>`);
    }
    const bars = state.breakdown ? `<div class="breakdowns">${breakdowns.map(({key, label, items}) => {
      const max = items[0]?.[1] || 1;
      return `<section class="breakdown"><h3>${esc(label)} <small>${esc(key)}</small></h3>${items.slice(0, 8).map(([value, count]) => `<button type="button" class="bar" data-stat-filter="${esc(key)}" data-stat-value="${esc(value)}" title="Filter where ${esc(key)} = ${esc(value)}"><span class="bar-label">${esc(value)}</span><span class="bar-track"><span class="bar-fill" style="width:${Math.max(2, count / max * 100)}%"></span></span><span class="bar-count">${count.toLocaleString()}</span></button>`).join("")}</section>`;
    }).join("")}</div>` : "";
    return `<div class="stats">${cards.join("")}<button type="button" class="stat-toggle" data-breakdown aria-pressed="${state.breakdown}" title="Breakdowns by state, type and location">${state.breakdown ? "Hide breakdown" : "Breakdown"}</button></div>${bars}`;
  };
  function dashboard(doc, state, source) {
    const rows = source.rows;
    if (!rows.length || !rows.some(row => row.value && typeof row.value === "object" && !Array.isArray(row.value))) return "";
    state.statusKey = state.statusKey ?? JV.statusField(rows) ?? "";
    const statusCounts = state.statusKey ? counts(rows, state.statusKey) : [];
    const tagCounts = new Map();
    for (const row of rows.slice(0, 20000)) for (const tag of C.tags(row.value)) tagCounts.set(tag.key, (tagCounts.get(tag.key) || 0) + 1);
    const tagKeys = [...tagCounts.keys()].sort();
    const tagKey = tagKeys.length ? (state.coverageTag && tagCounts.has(state.coverageTag) ? state.coverageTag : tagCounts.has("Owner") ? "Owner" : [...tagCounts].sort((a, b) => a[1] - b[1])[0][0]) : null;
    const tagMissing = tagKey ? rows.filter(row => !C.tags(row.value).some(tag => tag.key === tagKey && tag.value !== "")).length : 0;
    const breakdowns = [];
    if (state.breakdown) {
      if (state.statusKey) breakdowns.push({key: state.statusKey, label: "State", items: statusCounts});
      const type = firstField(rows, TYPE_FIELDS), region = firstField(rows, REGION_FIELDS);
      if (type) breakdowns.push({key: type, label: "Type", items: counts(rows, type)});
      if (region) breakdowns.push({key: region, label: "Location", items: counts(rows, region)});
    }
    return JV.dashboardHtml(doc, state, rows, {total: rows.length, statusCounts, tagMissing, tagKey, tagKeys, breakdowns});
  }

  // A filter chip's words: State.Name = running, Tag:Environment ≠ dev, InstanceType contains m5.
  JV.filterWords = expression => {
    const text = String(expression).trim();
    const match = /^(!=|=|>=|<=|>|<)\s*(.*)$/.exec(text);
    if (match) return `${match[1] === "!=" ? "≠" : match[1]} ${match[2]}`;
    if (/^(empty|!empty|not empty)$/i.test(text)) return /^empty$/i.test(text) ? "is empty" : "is not empty";
    if (/\.\./.test(text)) return `between ${text.replace("..", " and ")}`;
    return `contains ${text}`;
  };
  JV.chipsHtml = (state, shown, total) => {
    const chips = Object.entries(state.filters).filter(([, text]) => String(text).trim()).map(([key, text]) => `<span class="chip-filter"><span>${esc(JV.columnLabel(state, key))} ${esc(JV.filterWords(text))}</span><button type="button" data-remove-filter="${esc(key)}" aria-label="Remove filter ${esc(JV.columnLabel(state, key))}">×</button></span>`);
    const group = state.groupBy ? `<span class="chip-filter group"><span>Grouped by ${esc(JV.columnLabel(state, state.groupBy))}</span><button type="button" data-remove-group aria-label="Stop grouping">×</button></span>` : "";
    const selection = state.selection.size ? `<span class="selection-bar">${state.selection.size.toLocaleString()} selected <button type="button" class="link" data-selection="csv">Copy as CSV</button><button type="button" class="link" data-selection="json">Copy as JSON</button><button type="button" class="link" data-selection="clear">Clear</button></span>` : "";
    const columns = JV.orderedColumns(state).length;
    return `${chips.join("")}${group}<button type="button" class="chip-add" data-add-filter data-popover-anchor>+ Filter</button>${selection}
      <span class="chips-meta">Showing ${shown.toLocaleString()} of ${total.toLocaleString()} · <button type="button" class="link" data-action-columns data-popover-anchor>Columns (${columns})</button>${state.sort.length ? ` · <button type="button" class="link" data-clear-sort>Clear sort</button>` : ""}</span>`;
  };

  let tableFrame = 0;
  JV.renderTable = doc => {
    if (doc.kind === "server") return JV.server.renderTable(doc);
    const {state, source} = JV.tableState(doc);
    const indexes = JV.tableView(state, source);
    $("#view-tools").innerHTML = "";
    $("#dashboard").innerHTML = dashboard(doc, state, source);
    $("#chips").innerHTML = JV.chipsHtml(state, indexes.length, source.rows.length);
    JV.mountTable(doc, state, source, JV.tableDisplay(state, source));
  };
  // Table markup shared with server mode: header, sticky pinned columns, virtual rows.
  JV.mountTable = (doc, state, source, display) => {
    const columns = JV.orderedColumns(state);
    const widths = `52px ${columns.map(column => `${column.width}px`).join(" ")}`;
    let left = 52;
    const sticky = columns.map(column => {
      if (!column.pinned) return "";
      const style = `position:sticky;left:${left}px;z-index:2`;
      left += column.width;
      return style;
    });
    const sortOf = key => { const index = state.sort.findIndex(sort => sort.key === key); return index < 0 ? "" : `${state.sort[index].dir > 0 ? "▲" : "▼"}${state.sort.length > 1 ? index + 1 : ""}`; };
    const header = `<div class="th-row" role="row" style="grid-template-columns:${widths}"><div class="th gutter" role="columnheader"><input type="checkbox" data-select-all aria-label="Select all rows"${state.selection.size && state.selection.size >= (state.view?.length || 0) ? " checked" : ""}></div>${columns.map((column, index) => `<div class="th${column.pinned ? " pinned" : ""}" role="columnheader" style="${sticky[index]}" draggable="true" data-column="${esc(column.key)}" title="${esc(column.key)} · click to sort, Shift+click to add a sort · drag to move">
        <span class="th-label">${esc(column.label)}</span><span class="sort">${sortOf(column.key)}</span>
        <button type="button" class="th-menu" data-column-menu="${esc(column.key)}" title="Column options" aria-label="Options for ${esc(column.label)}">⋯</button><span class="resize" data-resize="${esc(column.key)}"></span></div>`).join("")}</div>`;
    const width = 52 + columns.reduce((sum, column) => sum + column.width, 0);
    let viewport = $("#view .scroller.table");
    const key = `${doc.id}:${source.key}:${columns.map(column => column.key + column.width + column.pinned).join("|")}`;
    if (!viewport || viewport.dataset.key !== key) {
      const scroll = viewport && viewport.dataset.key.split(":").slice(0, 2).join(":") === `${doc.id}:${source.key}` ? [viewport.scrollTop, viewport.scrollLeft] : [doc.scroll.get(`table:${JV.pathId(doc.focus)}:${doc.sub || ""}`) || 0, 0];
      $("#view").innerHTML = `<div class="scroller table"><div class="table-card" style="min-width:${width}px"><div class="thead">${header}</div><div class="tbody" role="rowgroup"><div class="rows"></div></div></div></div>`;
      viewport = $("#view .scroller.table");
      viewport.dataset.key = key;
      viewport.setAttribute("role", "grid");
      viewport.tabIndex = 0;
      viewport.addEventListener("scroll", () => { cancelAnimationFrame(tableFrame); tableFrame = requestAnimationFrame(() => JV.paintTable(JV.active)); });
      [viewport.scrollTop, viewport.scrollLeft] = scroll;
    } else viewport.querySelector(".thead").innerHTML = header;
    viewport.setAttribute("aria-rowcount", String(display.length));
    viewport.querySelector(".tbody").style.height = `${Math.max(1, display.length) * TABLE_ROW}px`;
    doc.tableColumns = columns;
    doc.tableSticky = sticky;
    doc.tableWidths = widths;
    doc.tableDisplay = display;
    if (!display.length) viewport.querySelector(".rows").innerHTML = `<p class="table-empty">${source.loading ? "Loading rows…" : "No rows match these filters."}</p>`;
    JV.paintTable(doc);
  };
  JV.paintTable = doc => {
    const viewport = $("#view .scroller.table");
    if (!viewport || !doc?.tableDisplay) return;
    if (doc.kind === "server") return JV.server.paintTable(doc);
    const {state, source} = JV.tableState(doc);
    JV.paintRows(doc, state, position => source.rows[doc.tableDisplay[position].index]);
  };
  // Paint the visible part of the virtual table; rowAt(position) gives a row or undefined.
  JV.paintRows = (doc, state, rowAt) => {
    const viewport = $("#view .scroller.table");
    const display = doc.tableDisplay, columns = doc.tableColumns, sticky = doc.tableSticky;
    if (!display.length) return;
    const headerHeight = viewport.querySelector(".thead").offsetHeight;
    const first = Math.max(0, Math.floor((viewport.scrollTop - headerHeight) / TABLE_ROW) - 10);
    const last = Math.min(display.length, first + Math.ceil(viewport.clientHeight / TABLE_ROW) + 25);
    const html = [];
    for (let position = first; position < last; position++) {
      const item = display[position];
      if (item.group !== undefined) {
        html.push(`<div class="tr-group" style="top:${position * TABLE_ROW}px" data-group="${esc(item.group)}"><button type="button" class="chev" data-group-toggle="${esc(item.group)}" aria-label="Toggle group">${state.collapsed.has(item.group) ? "▸" : "▾"}</button><span class="group-name">${esc(item.group || "(empty)")}</span><span class="group-count">${item.count.toLocaleString()}</span></div>`);
        continue;
      }
      const index = item.index, row = rowAt(position);
      const selected = state.selected === index, chosen = state.selection.has(index);
      if (!row) { html.push(`<div class="tr-row loading" style="top:${position * TABLE_ROW}px;grid-template-columns:${doc.tableWidths}"><div class="td gutter">${(position + 1).toLocaleString()}</div><div class="td">Loading…</div></div>`); continue; }
      html.push(`<div class="tr-row${selected ? " selected" : ""}${chosen ? " chosen" : ""}" role="row" style="top:${position * TABLE_ROW}px;grid-template-columns:${doc.tableWidths}" data-row="${index}">
        <div class="td gutter"><input type="checkbox" data-choose="${index}" aria-label="Select row ${position + 1}"${chosen ? " checked" : ""}><span>${(item.number ?? position + 1).toLocaleString()}</span></div>${columns.map((column, at) => {
          const value = JV.fieldOf(row, column.key);
          const name = column.key.startsWith("@tag:") ? column.key.slice(5) : column.key.split(".").pop();
          const id = JV.pathId(column.key.startsWith("@") ? row.path : [...row.path, ...column.key.split(".")]);
          const first = at === 0 && column.key === "@name" || column.key === "@parent";
          return `<div class="td${column.pinned ? " pinned" : ""}${first ? " strong" : ""}" role="gridcell" style="${sticky[at]}" data-cell="${esc(column.key)}" title="${esc(M.cellText(value).slice(0, 500))}">${value === undefined ? "" : column.key === "@key" || column.key === "@parent" ? `<span class="v">${JV.marked(M.cellText(value))}</span>` : JV.valueHtml(name, value, {limit: 200, id})}</div>`;
        }).join("")}</div>`);
    }
    viewport.querySelector(".rows").innerHTML = html.join("");
  };
  JV.selectTableRow = (doc, index, scroll = false) => {
    const {state, source} = JV.tableState(doc);
    state.selected = index;
    const row = doc.kind === "server" ? JV.server.rowAt(doc, index) : source.rows[index];
    doc.selected = row?.path || null;
    doc.selectedRow = row || null;
    JV.paintTable(doc);
    JV.renderDetail(doc);
    if (doc.selected) JV.setHash(doc.selected);
    if (scroll) {
      const viewport = $("#view .scroller.table");
      const position = doc.tableDisplay.findIndex(item => item.index === index);
      const top = position * TABLE_ROW, header = viewport.querySelector(".thead").offsetHeight;
      if (top < viewport.scrollTop || top > viewport.scrollTop + viewport.clientHeight - header - TABLE_ROW * 2) viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 3);
    }
  };
  const invalidate = state => { state.view = null; state.display = null; };
  JV.invalidate = invalidate;
  JV.setFilter = (doc, key, expression) => {
    const {state} = JV.tableState(doc);
    if (expression === null || expression === "") delete state.filters[key]; else state.filters[key] = expression;
    if (key.startsWith("@tag:") && !state.columns.some(column => column.key === key)) state.columns.push({key, label: `Tag:${key.slice(5)}`, width: 160, visible: true, pinned: false});
    invalidate(state);
    state.selection.clear();
    JV.rememberLayout(doc, state);
    if (JV.viewOf(doc) !== "table") { doc.view = "table"; return JV.render(); }
    JV.renderTable(doc);
  };
  // Rows chosen with the checkboxes (or Ctrl/Shift+click), as CSV or JSON.
  JV.copySelection = async (doc, kind) => {
    const {state, source} = JV.tableState(doc);
    const order = (state.view || []).filter(index => state.selection.has(index));
    const rows = doc.kind === "server" ? await JV.server.rowsFor(doc, order) : order.map(index => source.rows[index]);
    if (kind === "json") return JV.copy(JSON.stringify(rows.map(row => row.value), null, 2), {label: `Copied ${rows.length} rows as JSON`});
    const columns = JV.orderedColumns(state);
    JV.copy(M.toCsv(columns.map(column => column.label), rows.map(row => columns.map(column => M.cellText(JV.fieldOf(row, column.key))))), {label: `Copied ${rows.length} rows as CSV`});
  };

  function columnChooser(doc, anchor) {
    const {state} = JV.tableState(doc);
    JV.openPopover(anchor, `<div class="chooser"><div class="chooser-head"><b>Columns</b><input id="column-find" placeholder="Find a column" spellcheck="false" aria-label="Find a column"><button type="button" class="link" data-columns="all">All</button><button type="button" class="link" data-columns="none">None</button></div>
      <ol class="chooser-list">${state.columns.map((column, index) => `<li data-column-item="${esc(column.key.toLowerCase())}"><label><input type="checkbox" data-column-show="${index}" ${column.visible ? "checked" : ""}> <span>${esc(column.label)}</span></label>
        <button type="button" class="mini" data-column-pin="${index}" aria-pressed="${column.pinned}" title="Pin to the left" aria-label="Pin ${esc(column.label)}">Pin</button><button type="button" class="mini" data-column-move="${index}" data-dir="-1" aria-label="Move ${esc(column.label)} up">↑</button><button type="button" class="mini" data-column-move="${index}" data-dir="1" aria-label="Move ${esc(column.label)} down">↓</button></li>`).join("")}</ol></div>`);
    $("#column-find").focus();
  }
  JV.columnChooser = columnChooser;
  // The "+ Filter" popover: a column (or a tag) and an expression.
  function filterPopover(doc, anchor, presetKey = "") {
    const {state, source} = JV.tableState(doc);
    const tagKeys = new Set();
    for (const row of (source.rows || []).slice(0, 2000)) for (const tag of C.tags(row?.value)) tagKeys.add(tag.key);
    const options = [...state.columns.map(column => [column.key, column.label]), ...[...tagKeys].sort().filter(key => !state.columns.some(column => column.key === `@tag:${key}`)).map(key => [`@tag:${key}`, `Tag:${key}`])];
    JV.openPopover(anchor, `<form class="filter-pop" id="filter-form"><label>Column <select id="filter-column">${options.map(([key, label]) => `<option value="${esc(key)}"${key === presetKey ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
      <label>Value <input id="filter-value" placeholder="text, =x, !=x, >n, a..b, empty" spellcheck="false" value="${esc(state.filters[presetKey] || "")}"></label>
      <div class="dialog-actions"><button type="submit" class="primary">Add filter</button></div></form>`);
    $("#filter-value").focus();
  }
  JV.filterPopover = filterPopover;

  // ---------- Cards ----------
  // Key/value grid of an object; nested objects as sections, lists of objects as links to a table.
  JV.cardBody = (region, value, path, depth = 0) => {
    if (!M.isContainer(value)) return `<div class="kv"><span class="cv" data-ref="${JV.ref(region, path, value)}">${JV.valueHtml(JV.lastKey(path), value, {limit: 4000, id: JV.pathId(path)})}</span></div>`;
    if (Array.isArray(value)) {
      if (!value.length) return `<div class="kv empty-note">Empty list</div>`;
      if (C.isTagList(value)) return `<div class="tags block">${JV.tagChips(C.tags({Tags: value}), 100)}</div>`;
      if (value.every(item => !M.isContainer(item))) return `<div class="chips">${value.slice(0, 200).map((item, index) => `<span class="chip" data-ref="${JV.ref(region, [...path, index], item)}">${JV.valueHtml(JV.lastKey(path), item, {limit: 200, id: JV.pathId([...path, index])})}</span>`).join("")}${value.length > 200 ? `<span class="chip more">+${(value.length - 200).toLocaleString()}</span>` : ""}</div>`;
      const names = value.slice(0, 5).map(item => C.displayName(item)).filter(Boolean);
      return `<div class="list-link"><button type="button" class="link" data-focus-ref="${JV.ref(region, path, value)}" data-focus-view="table">View ${value.length.toLocaleString()} item${value.length === 1 ? "" : "s"} as a table →</button>${names.length ? `<span class="tool-note">${names.map(esc).join(", ")}${value.length > names.length ? ", …" : ""}</span>` : ""}</div>`;
    }
    const keys = Object.keys(value);
    const ordered = [...keys.filter(key => C.isKeyField(key) && !M.isContainer(value[key])), ...keys.filter(key => !(C.isKeyField(key) && !M.isContainer(value[key])))];
    const limit = depth ? 300 : 2000;
    const lines = ordered.slice(0, limit).map(key => {
      const child = value[key], childPath = [...path, key], id = JV.ref(region, childPath, child);
      const label = `<span class="ck${C.isKeyField(key) ? " keyfield" : ""}" data-ref="${id}" title="Click to copy the value">${JV.marked(key)}</span>`;
      if (M.isContainer(child) && !(Array.isArray(child) && (C.isTagList(child) || child.every(item => !M.isContainer(item))))) {
        if (depth >= 3) return `<div class="kv">${label}<span class="cv"><button type="button" class="link" data-focus-ref="${id}">${esc(M.preview(child))} open →</button></span></div>`;
        const count = Array.isArray(child) ? child.length : Object.keys(child).length;
        const viewer = JV.specialViewer(child, childPath, region);
        return `<details class="section" ${depth < 1 && count <= 60 || viewer ? "open" : ""}><summary>${label}<span class="tool-note">${esc(M.preview(child))}</span><button type="button" class="link small" data-focus-ref="${id}" title="Open this part on its own">open →</button></summary>${viewer || JV.cardBody(region, child, childPath, depth + 1)}</details>`;
      }
      return `<div class="kv">${label}<span class="cv" data-ref="${id}">${M.isContainer(child) ? JV.cardBody(region, child, childPath, depth + 1) : JV.valueHtml(key, child, {limit: 2000, id: JV.pathId(childPath)})}</span></div>`;
    });
    if (keys.length > limit) lines.push(`<div class="kv empty-note">${(keys.length - limit).toLocaleString()} more keys; open the tree to see them</div>`);
    return `<div class="card-grid">${lines.join("") || '<div class="kv empty-note">Empty object</div>'}</div>`;
  };
  JV.consoleLinkOf = value => M.isContainer(value) && !Array.isArray(value) ? [value.Arn, value.ARN, value.arn, value.id, value.IamInstanceProfile?.Arn].map(C.consoleLink).find(Boolean) || (value.InstanceId ? {label: "Open in AWS Console", url: `https://console.aws.amazon.com/ec2/home#InstanceDetails:instanceId=${encodeURIComponent(value.InstanceId)}`} : null) : C.consoleLink(value);
  JV.cardHeader = (region, value, path) => {
    const name = JV.safeName(value), status = JV.statusOf(value), tags = C.tags(value);
    const link = JV.consoleLinkOf(value);
    return `<header class="card-head"><h2>${esc(name || (path.length ? JV.partText(JV.lastKey(path)) : "Document"))}</h2>${status ? `<span class="badge tone-${status.tone}">${esc(status.value)}</span>` : ""}${link ? `<a class="console" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>` : ""}</header>${tags.length ? `<div class="tags block">${JV.tagChips(tags, 40)}</div>` : ""}`;
  };
  JV.renderCard = doc => {
    if (doc.kind === "server") return JV.server.renderCard(doc);
    JV.refs.card.clear();
    const value = JV.focusValue(doc);
    $("#view-tools").innerHTML = `<span class="tool-note">Click a key or value to copy it · right-click for more</span>`;
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    $("#view").innerHTML = `<div class="scroller cards">${JV.cardsHtml(doc, value)}</div>`;
    const saved = doc.scroll.get(`card:${JV.pathId(doc.focus)}:`);
    if (saved) $("#view .scroller").scrollTop = saved;
  };
  JV.cardsHtml = (doc, value, base = doc.focus) => {
    if (Array.isArray(value) && value.some(M.isContainer)) {
      const shown = Math.min(value.length, doc.cardLimit || 100);
      return value.slice(0, shown).map((item, index) => `<article class="card">${JV.cardHeader("card", item, [...base, index])}${JV.specialViewer(item, [...base, index], "card") || ""}${JV.cardBody("card", item, [...base, index], 1)}</article>`).join("") +
        (value.length > shown ? `<button type="button" class="more-cards" data-more-cards>Show ${Math.min(100, value.length - shown)} more of ${(value.length - shown).toLocaleString()}</button>` : "");
    }
    return `<article class="card">${JV.cardHeader("card", value, base)}${JV.specialViewer(value, base, "card") || ""}${JV.cardBody("card", value, base)}</article>`;
  };

  // ---------- Security rules and IAM policies ----------
  const OPEN_CIDR = /^(0\.0\.0\.0\/0|::\/0|\*|Internet|Any)$/i;
  const ports = (from, to) => from === undefined || from === -1 || from === null ? "All" : from === to || to === undefined ? String(from) : `${from}–${to}`;
  const protocolName = protocol => ({"-1": "All", 6: "TCP", 17: "UDP", 1: "ICMP", tcp: "TCP", udp: "UDP", icmp: "ICMP"})[String(protocol).toLowerCase()] || String(protocol ?? "All");
  // AWS security group (IpPermissions / IpPermissionsEgress) or Azure NSG (securityRules).
  function ruleRows(value) {
    if (!value || typeof value !== "object") return null;
    const rows = [];
    for (const [direction, list] of [["Inbound", value.IpPermissions], ["Outbound", value.IpPermissionsEgress]]) {
      if (!Array.isArray(list)) continue;
      for (const rule of list) {
        const sources = [
          ...(rule.IpRanges || []).map(range => [range.CidrIp, range.Description]),
          ...(rule.Ipv6Ranges || []).map(range => [range.CidrIpv6, range.Description]),
          ...(rule.UserIdGroupPairs || []).map(pair => [pair.GroupId + (pair.GroupName ? ` (${pair.GroupName})` : ""), pair.Description]),
          ...(rule.PrefixListIds || []).map(prefix => [prefix.PrefixListId, prefix.Description]),
        ];
        for (const [source, description] of sources.length ? sources : [["—", ""]]) rows.push({direction, access: "Allow", protocol: protocolName(rule.IpProtocol), ports: ports(rule.FromPort, rule.ToPort), source, description: description || "", open: OPEN_CIDR.test(source || "")});
      }
    }
    const nsg = value.securityRules || value.properties?.securityRules;
    if (Array.isArray(nsg)) {
      for (const rule of [...nsg].sort((a, b) => (a.priority ?? a.properties?.priority ?? 0) - (b.priority ?? b.properties?.priority ?? 0))) {
        const props = rule.properties || rule;
        const source = props.sourceAddressPrefix || (props.sourceAddressPrefixes || []).join(", ") || "—";
        const direction = props.direction === "Outbound" ? "Outbound" : "Inbound";
        rows.push({direction, access: props.access || "Allow", protocol: protocolName(props.protocol), ports: props.destinationPortRange || (props.destinationPortRanges || []).join(", ") || "All", source, description: `${rule.name || ""}${props.priority !== undefined ? ` · priority ${props.priority}` : ""}`, open: direction === "Inbound" && props.access !== "Deny" && OPEN_CIDR.test(source)});
      }
    }
    return rows.length || Array.isArray(value.IpPermissions) || Array.isArray(nsg) ? rows : null;
  }
  JV.ruleViewer = value => {
    const rows = ruleRows(value);
    if (!rows) return "";
    const open = rows.filter(row => row.open && row.direction === "Inbound").length;
    return `<section class="viewer"><h3>Security rules${open ? ` <span class="badge tone-bad">${open} open to the internet</span>` : ""}</h3>
      <div class="viewer-scroll"><table class="mini-table"><thead><tr><th>Direction</th><th>Access</th><th>Protocol</th><th>Ports</th><th>Source / destination</th><th>Description</th></tr></thead><tbody>
      ${rows.map(row => `<tr class="${row.open ? "flag" : ""}"><td>${esc(row.direction)}</td><td>${esc(row.access)}</td><td>${esc(row.protocol)}</td><td class="mono">${esc(row.ports)}</td><td class="mono">${esc(row.source)}${row.open ? ' <span class="badge tone-bad">open</span>' : ""}</td><td>${esc(row.description)}</td></tr>`).join("") || '<tr><td colspan="6">No rules</td></tr>'}
      </tbody></table></div></section>`;
  };
  // The policy document inside an IAM object (also URL-encoded JSON strings, as AWS returns them).
  function policyOf(value) {
    if (!value || typeof value !== "object") return null;
    if (Array.isArray(value.Statement) || value.Statement && typeof value.Statement === "object") return value;
    for (const key of ["PolicyDocument", "Document", "policyDocument", "AssumeRolePolicyDocument", "Policy", "policy"]) {
      let candidate = value[key];
      if (typeof candidate === "string") {
        try { candidate = JSON.parse(/^%7B/i.test(candidate) ? decodeURIComponent(candidate) : candidate); } catch { candidate = null; }
      }
      if (candidate && typeof candidate === "object" && candidate.Statement) return candidate;
    }
    return null;
  }
  JV.policyViewer = value => {
    const policy = policyOf(value);
    if (!policy) return "";
    const statements = Array.isArray(policy.Statement) ? policy.Statement : [policy.Statement];
    const list = item => item === undefined ? "" : (Array.isArray(item) ? item : [item]).map(entry => typeof entry === "string" ? entry : JSON.stringify(entry));
    const wild = text => text === "*" || /(^|:)\*$/.test(text) || /^\*:/.test(text);
    let flags = 0;
    const rows = statements.map(statement => {
      const actions = list(statement.Action ?? statement.NotAction), resources = list(statement.Resource ?? statement.NotResource);
      const principal = statement.Principal === undefined ? "" : typeof statement.Principal === "string" ? statement.Principal : JSON.stringify(statement.Principal);
      const risky = statement.Effect === "Allow" && (actions.some(wild) || resources.some(wild) || statement.NotAction || principal === "*" || /"\*"/.test(principal));
      if (risky) flags++;
      const cell = items => items.map(text => `<span class="mono${wild(text) ? " wild" : ""}">${esc(text)}</span>`).join("<br>");
      return `<tr class="${risky ? "flag" : ""}"><td><span class="badge ${statement.Effect === "Deny" ? "tone-bad" : "tone-good"}">${esc(statement.Effect || "")}</span>${statement.Sid ? `<div class="tool-note">${esc(statement.Sid)}</div>` : ""}</td><td>${statement.NotAction ? '<b class="tool-note">NotAction</b><br>' : ""}${cell(actions)}</td><td>${statement.NotResource ? '<b class="tool-note">NotResource</b><br>' : ""}${cell(resources)}${principal ? `<div class="tool-note">Principal: ${esc(principal)}</div>` : ""}</td><td class="mono">${statement.Condition ? esc(JSON.stringify(statement.Condition)) : ""}</td></tr>`;
    });
    return `<section class="viewer"><h3>Policy statements${flags ? ` <span class="badge tone-bad">${flags} with wildcards</span>` : ""}</h3>
      <div class="viewer-scroll"><table class="mini-table"><thead><tr><th>Effect</th><th>Action</th><th>Resource</th><th>Condition</th></tr></thead><tbody>${rows.join("")}</tbody></table></div></section>`;
  };
  JV.specialViewer = value => JV.ruleViewer(value) + JV.policyViewer(value) || null;

  // ---------- Raw (Monaco) ----------
  let monaco = null, rawEditor = null, monacoLoading = null;
  function loadMonaco() {
    if (monaco) return Promise.resolve(monaco);
    if (monacoLoading) return monacoLoading;
    monacoLoading = new Promise((resolve, reject) => {
      const script = Object.assign(document.createElement("script"), {src: "../vendor/monaco/vs/loader.js"});
      script.onload = () => {
        // An absolute path: Monaco's web workers resolve it from their own URL.
        window.require.config({paths: {vs: new URL("../vendor/monaco/vs", location.href).href.replace(/\/$/, "")}});
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
  JV.disposeRaw = () => {
    rawEditor?.getModel()?.dispose();
    rawEditor?.dispose();
    rawEditor = null;
  };
  JV.renderRaw = async (doc, valueOverride) => {
    if (doc.kind === "server" && valueOverride === undefined) return JV.server.renderRaw(doc);
    const value = valueOverride !== undefined ? valueOverride : JV.focusValue(doc);
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    $("#view-tools").innerHTML = `<button type="button" class="tool" data-raw="copy">Copy formatted</button><button type="button" class="tool" data-raw="min">Copy minified</button><span class="tool-note">Read-only · fold with the arrows in the gutter</span>`;
    if (rawEditor && rawEditor.owlDoc === doc && JV.samePath(rawEditor.owlFocus, doc.focus) && valueOverride === undefined) return;
    JV.disposeRaw();
    if (value === undefined) { $("#view").innerHTML = `<div class="state-card"><p>Select something small enough to show as text.</p></div>`; return; }
    const text = JSON.stringify(value, null, 2) ?? String(value);
    if (text.length > 150 * 1024 * 1024) {
      $("#view").innerHTML = `<div class="state-card"><h2>Too large to show as text</h2><p>${esc(M.formatBytes(text.length))} formatted. Open a smaller part from the tree or the outline.</p></div>`;
      return;
    }
    $("#view").innerHTML = `<div class="raw-host"><p class="tool-note pad">Loading editor…</p></div>`;
    try { await loadMonaco(); } catch { $("#view").innerHTML = `<pre class="raw-fallback">${esc(text.slice(0, 2_000_000))}</pre>`; return; }
    if (JV.active !== doc || JV.viewOf(doc) !== "raw") return;
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
  };

  // ---------- Relationship graph ----------
  // VPC → subnet → instance → ENI → security group, built from the IDs in the output.
  JV.buildGraph = value => {
    const nodes = new Map(), edges = new Set();
    const node = (kind, id, fields = {}) => {
      if (!id || typeof id !== "string") return null;
      const key = `${kind}:${id}`;
      const existing = nodes.get(key) || {kind, id, name: "", path: null};
      Object.assign(existing, Object.fromEntries(Object.entries(fields).filter(([, item]) => item)));
      nodes.set(key, existing);
      return key;
    };
    const link = (from, to) => { if (from && to) edges.add(`${from}>${to}`); };
    let visited = 0;
    const walk = (item, path) => {
      if (visited++ > 300000 || !item || typeof item !== "object") return;
      if (Array.isArray(item)) { item.forEach((child, index) => walk(child, [...path, index])); return; }
      const name = C.displayName(item);
      if (item.InstanceId) {
        const instance = node("instance", item.InstanceId, {name, path});
        const subnet = node("subnet", item.SubnetId), vpc = node("vpc", item.VpcId);
        link(subnet || vpc, instance);
        if (subnet) link(vpc, subnet);
        const enis = (item.NetworkInterfaces || []).map(eni => {
          const key = node("eni", eni.NetworkInterfaceId, {name: eni.PrivateIpAddress});
          link(instance, key);
          for (const group of eni.Groups || []) link(key, node("sg", group.GroupId, {name: group.GroupName}));
          return key;
        });
        if (!enis.length) for (const group of item.SecurityGroups || []) link(instance, node("sg", group.GroupId, {name: group.GroupName}));
      } else if (item.NetworkInterfaceId && item.SubnetId) {
        const eni = node("eni", item.NetworkInterfaceId, {name: item.PrivateIpAddress, path});
        link(node("subnet", item.SubnetId), eni);
        for (const group of item.Groups || []) link(eni, node("sg", group.GroupId, {name: group.GroupName}));
      } else if (item.SubnetId && item.CidrBlock) {
        link(node("vpc", item.VpcId), node("subnet", item.SubnetId, {name: name !== item.SubnetId ? name : item.CidrBlock, path}));
      } else if (item.VpcId && item.CidrBlock) node("vpc", item.VpcId, {name: name !== item.VpcId ? name : item.CidrBlock, path});
      else if (item.GroupId && item.IpPermissions) node("sg", item.GroupId, {name: item.GroupName, path});
      for (const [key, child] of Object.entries(item)) if (child && typeof child === "object" && !["Tags", "NetworkInterfaces", "SecurityGroups"].includes(key)) walk(child, [...path, key]);
    };
    walk(value, []);
    return {nodes, edges: [...edges].map(edge => edge.split(">"))};
  };
  JV.renderGraph = doc => {
    JV.refs.graph.clear();
    $("#dashboard").innerHTML = "";
    $("#chips").innerHTML = "";
    const {nodes, edges} = JV.buildGraph(doc.value);
    const kinds = [["vpc", "VPCs"], ["subnet", "Subnets"], ["instance", "Instances"], ["eni", "Network interfaces"], ["sg", "Security groups"]];
    const CAP = 80, W = 210, H = 38, GAP = 70, TOP = 40;
    const columns = kinds.map(([kind, label]) => ({kind, label, items: [...nodes.entries()].filter(([, item]) => item.kind === kind).sort((a, b) => (a[1].name || a[1].id).localeCompare(b[1].name || b[1].id))}));
    const place = new Map();
    columns.forEach((column, x) => column.items.slice(0, CAP).forEach(([key], y) => place.set(key, {x: x * (W + GAP) + 16, y: TOP + y * (H + 10)})));
    const height = TOP + Math.max(1, ...columns.map(column => Math.min(CAP, column.items.length) + (column.items.length > CAP ? 1 : 0))) * (H + 10) + 20;
    const width = kinds.length * (W + GAP);
    $("#view-tools").innerHTML = `<span class="tool-note">${nodes.size.toLocaleString()} resources · ${edges.length.toLocaleString()} links · from IDs found in this output · click a box to open it</span>`;
    if (!nodes.size) { $("#view").innerHTML = `<div class="state-card"><h2>No VPC, subnet or instance IDs found</h2><p>Open output from describe-instances, describe-subnets, describe-vpcs or describe-security-groups.</p></div>`; return; }
    const lines = edges.filter(([from, to]) => place.has(from) && place.has(to)).map(([from, to]) => {
      const a = place.get(from), b = place.get(to);
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
      return `<path d="M${x1},${y1} C${x1 + GAP / 2},${y1} ${x2 - GAP / 2},${y2} ${x2},${y2}" class="edge"></path>`;
    }).join("");
    const boxes = columns.map((column, x) => `<text x="${x * (W + GAP) + 16}" y="22" class="col-title">${esc(column.label)} (${column.items.length})</text>` + column.items.slice(0, CAP).map(([key, item]) => {
      const {x: left, y: top} = place.get(key);
      const ref = JV.ref("graph", item.path, item);
      return `<g class="node node-${item.kind}${item.path ? "" : " ghost"}" data-graph="${ref}" tabindex="0" role="button" aria-label="${esc(item.name || item.id)}"><rect x="${left}" y="${top}" width="${W}" height="${H}" rx="9"></rect><text x="${left + 10}" y="${top + 16}" class="node-name">${esc((item.name || item.id).slice(0, 28))}</text><text x="${left + 10}" y="${top + 30}" class="node-id">${esc(item.id.slice(0, 30))}</text></g>`;
    }).join("") + (column.items.length > CAP ? `<text x="${x * (W + GAP) + 16}" y="${TOP + CAP * (H + 10) + 16}" class="node-id">+${column.items.length - CAP} more</text>` : "")).join("");
    $("#view").innerHTML = `<div class="scroller graph"><svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Resource relationship graph">${lines}${boxes}</svg></div>`;
  };

  // ---------- Copy and menus ----------
  JV.valueAt = (doc, path) => doc.kind === "server" ? JV.server.cachedValue(doc, path) : M.getAt(doc.value, path);
  JV.copySelected = async doc => {
    const path = doc.selected;
    const value = doc.kind === "server" ? await JV.server.valueAt(doc, path) : M.getAt(doc.value, path);
    JV.copyValue(value, JV.lastKey(path));
  };
  // The resource row (in the unwrapped list) a path sits in, and the field inside it.
  JV.rowFieldOf = (doc, path) => {
    if (doc.kind !== "client") return null;
    const rows = doc.unwrap ? doc.unwrap.rows : Array.isArray(doc.value) ? doc.value.map((value, index) => ({value, path: [index]})) : null;
    if (!rows?.length) return null;
    const base = rows[0].path.length;
    const row = rows.find(item => item.path.length === base && JV.startsWith(path, item.path));
    if (!row) return null;
    const rest = path.slice(base);
    if (!rest.length || rest.some(part => typeof part === "number")) return null;
    return {row, field: rest.join(".")};
  };
  JV.nodeMenu = (doc, path, value, extra = []) => {
    const key = JV.lastKey(path);
    const link = JV.consoleLinkOf(value);
    const rowField = !M.isContainer(value) ? JV.rowFieldOf(doc, path) : null;
    const parent = path.slice(0, -1);
    const bookmarked = JV.bookmarksOf(doc).some(item => JV.samePath(item.path, path));
    const copyKey = key ?? "root";
    return [
      {label: "Copy value", hint: "⌘C", primary: true, action: () => JV.copyValue(doc.kind === "server" && value === undefined ? "" : value, key)},
      {label: "Copy key", disabled: key === undefined, action: () => JV.copy(String(key), {label: "Copied key", key})},
      "-",
      {label: "Copy path · JMESPath", hint: "⇧⌘C", action: () => JV.copy(M.formatPath(path, "jmes"), {label: "Copied path of", key: copyKey})},
      {label: "Copy path · jq", action: () => JV.copy(M.formatPath(path, "jq"), {label: "Copied path of", key: copyKey})},
      {label: "Copy path · JavaScript", action: () => JV.copy(M.formatPath(path, "js"), {label: "Copied path of", key: copyKey})},
      {label: "Copy as minified JSON", action: () => JV.copy(JSON.stringify(value) ?? "", {label: "Copied minified", key: copyKey})},
      ...extra,
      "-",
      ...(rowField ? [{label: `Filter where ${rowField.field.split(".").pop()} = this`, action: () => { JV.focus(doc, [], "table"); JV.setFilter(doc, rowField.field, `=${M.cellText(value)}`); }}] : []),
      ...(parent.length || path.length ? [{label: "Show siblings as table", disabled: !path.length, action: () => JV.focus(doc, parent, "table")}] : []),
      ...(M.isContainer(value) ? [
        {label: "Open here as table", action: () => JV.focus(doc, path, "table")},
        {label: "Open here as card", action: () => JV.focus(doc, path, "card")},
        {label: "Open here as tree", action: () => JV.focus(doc, path, "tree")},
        {label: "Open here as raw JSON", action: () => JV.focus(doc, path, "raw")},
      ] : [{label: "Show in tree", action: () => JV.reveal(doc, path)}]),
      {label: bookmarked ? "Remove bookmark" : "Bookmark this node", action: () => JV.toggleBookmark(doc, path)},
      ...(link ? [{label: link.label, action: () => window.open(link.url, "_blank", "noopener")}] : []),
    ];
  };

  // ---------- Keyboard in the tree and table ----------
  JV.viewKeys = (doc, event) => {
    const view = JV.viewOf(doc);
    if (view === "tree" && doc.rows) {
      const selectedId = doc.selected ? JV.pathId(doc.selected) : null;
      const index = doc.rows.findIndex(row => row.id === selectedId);
      const row = doc.rows[index];
      const go = next => {
        next = Math.max(0, Math.min(doc.rows.length - 1, next));
        if (doc.rows[next]?.more) next += next > index ? -1 : 1;
        const target = doc.rows[next];
        if (!target || target.more) return;
        JV.select(doc, JV.rowPath(doc, target));
        JV.scrollToRow(doc, next);
      };
      const keys = {
        ArrowDown: () => go(index + 1), ArrowUp: () => go(index - 1),
        PageDown: () => go(index + 20), PageUp: () => go(index - 20), Home: () => go(0), End: () => go(doc.rows.length - 1),
        ArrowRight: () => { if (!row) return go(0); if ((M.isContainer(row.value) || row.container) && !doc.expanded.has(row.id)) JV.toggleRow(doc, row); else go(index + 1); },
        ArrowLeft: () => { if (!row) return; if ((M.isContainer(row.value) || row.container) && doc.expanded.has(row.id)) JV.toggleRow(doc, row); else if (row.parent) go(doc.rows.indexOf(row.parent)); },
        Enter: () => row && JV.toggleRow(doc, row),
        c: () => row && JV.copySelected(doc),
      };
      if (keys[event.key]) { event.preventDefault(); keys[event.key](); }
    } else if (view === "table" && doc.tableDisplay) {
      const {state} = JV.tableState(doc);
      const positions = doc.tableDisplay.map(item => item.index).filter(index => index !== undefined);
      const position = positions.indexOf(state.selected);
      const go = next => { const target = positions[Math.max(0, Math.min(positions.length - 1, next))]; if (target !== undefined) JV.selectTableRow(doc, target, true); };
      const keys = {
        ArrowDown: () => go(position + 1), ArrowUp: () => go(position - 1), PageDown: () => go(position + 20), PageUp: () => go(position - 20),
        Home: () => go(0), End: () => go(positions.length - 1),
        c: () => position >= 0 && doc.selectedRow && JV.copy(JSON.stringify(doc.selectedRow.value, null, 2), {label: "Copied row", key: C.displayName(doc.selectedRow.value) || "row"}),
        Enter: () => position >= 0 && doc.selected && JV.reveal(doc, doc.selected),
        " ": () => { if (position < 0) return; state.selection.has(state.selected) ? state.selection.delete(state.selected) : state.selection.add(state.selected); JV.renderTable(doc); },
      };
      if (keys[event.key]) { event.preventDefault(); keys[event.key](); }
    }
  };

  // ---------- Events in the view ----------
  const view = $("#view");
  view.addEventListener("click", event => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready") return;
    const current = JV.viewOf(doc);
    if (current === "tree") {
      const more = event.target.closest("[data-more],[data-more-all]");
      if (more) {
        const row = doc.rows[Number(more.dataset.more ?? more.dataset.moreAll)];
        if (doc.kind === "server") return JV.server.more(doc, row, more.dataset.moreAll !== undefined);
        const total = Array.isArray(row.parent.value) ? row.parent.value.length : Object.keys(row.parent.value).length;
        doc.pages.set(row.parent.id, more.dataset.moreAll !== undefined ? total : (doc.pages.get(row.parent.id) || PAGE) + MORE);
        return JV.renderTree(doc);
      }
      const toggle = event.target.closest("[data-toggle]");
      if (toggle) return JV.toggleRow(doc, doc.rows[Number(toggle.dataset.toggle)]);
      const element = event.target.closest(".tr[data-index]");
      if (!element) return;
      const row = doc.rows[Number(element.dataset.index)];
      if (row.more) return;
      JV.select(doc, JV.rowPath(doc, row));
      JV.paintTree(doc);
      // One click on a key, value or the pill copies; the second click of a double-click does not.
      if (event.target.closest("[data-copy-row]") && event.detail <= 1) JV.copySelected(doc);
      return;
    }
    if (current === "table") {
      const {state, source} = JV.tableState(doc);
      const menuButton = event.target.closest("[data-column-menu]");
      if (menuButton) return JV.columnMenu(doc, menuButton.dataset.columnMenu, menuButton);
      const groupToggle = event.target.closest("[data-group-toggle],.tr-group");
      if (groupToggle) {
        const group = (groupToggle.dataset.groupToggle ?? groupToggle.dataset.group);
        state.collapsed.has(group) ? state.collapsed.delete(group) : state.collapsed.add(group);
        state.display = null;
        return JV.renderTable(doc);
      }
      if (event.target.closest("[data-select-all]")) {
        const all = event.target.checked;
        state.selection = new Set(all ? JV.tableView(state, source) : []);
        return JV.renderTable(doc);
      }
      const choose = event.target.closest("[data-choose]");
      if (choose) {
        const index = Number(choose.dataset.choose);
        if (event.shiftKey && state.anchor >= 0) {
          const order = doc.tableDisplay.map(item => item.index).filter(item => item !== undefined);
          const [from, to] = [order.indexOf(state.anchor), order.indexOf(index)].sort((a, b) => a - b);
          for (const item of order.slice(from, to + 1)) state.selection.add(item);
        } else choose.checked ? state.selection.add(index) : state.selection.delete(index);
        state.anchor = index;
        return JV.renderTable(doc);
      }
      const header = event.target.closest(".th[data-column]");
      if (header && !event.target.closest(".resize")) {
        const key = header.dataset.column;
        const existing = state.sort.find(sort => sort.key === key);
        if (event.shiftKey) {
          if (existing) existing.dir = -existing.dir; else state.sort.push({key, dir: 1});
        } else state.sort = existing && state.sort.length === 1 ? (existing.dir > 0 ? [{key, dir: -1}] : []) : [{key, dir: 1}];
        invalidate(state);
        JV.rememberLayout(doc, state);
        return JV.renderTable(doc);
      }
      const rowElement = event.target.closest(".tr-row[data-row]");
      if (!rowElement) return;
      const index = Number(rowElement.dataset.row);
      // Ctrl/⌘+click and Shift+click choose rows for copying several at once.
      if (event.metaKey || event.ctrlKey || event.shiftKey) {
        if (event.shiftKey && state.anchor >= 0) {
          const order = doc.tableDisplay.map(item => item.index).filter(item => item !== undefined);
          const [from, to] = [order.indexOf(state.anchor), order.indexOf(index)].sort((a, b) => a - b);
          for (const item of order.slice(from, to + 1)) state.selection.add(item);
        } else state.selection.has(index) ? state.selection.delete(index) : state.selection.add(index);
        state.anchor = index;
        return JV.renderTable(doc);
      }
      state.anchor = index;
      JV.selectTableRow(doc, index);
      const cell = event.target.closest("[data-cell]");
      if (cell && !event.target.closest(".gutter")) {
        const row = doc.kind === "server" ? JV.server.rowAt(doc, index) : source.rows[index];
        const key = cell.dataset.cell;
        JV.copyValue(JV.fieldOf(row, key), JV.columnLabel(state, key));
      }
      return;
    }
    if (current === "card") {
      if (event.target.closest("[data-more-cards]")) { doc.cardLimit = (doc.cardLimit || 100) + 100; return JV.renderCard(doc); }
      const focusRef = event.target.closest("[data-focus-ref]");
      if (focusRef) {
        event.preventDefault();
        const entry = JV.refs.card.get(focusRef.dataset.focusRef);
        return JV.focus(doc, entry.path, focusRef.dataset.focusView || "auto");
      }
      const element = event.target.closest("[data-ref]");
      if (element && (!event.target.closest("summary") || event.target.closest(".ck"))) {
        if (event.target.closest("summary")) event.preventDefault();
        const entry = JV.refs.card.get(element.dataset.ref);
        JV.select(doc, entry.path);
        JV.copyValue(entry.value, JV.lastKey(entry.path));
      }
    }
    if (current === "graph") {
      const node = event.target.closest("[data-graph]");
      if (!node) return;
      const entry = JV.refs.graph.get(node.dataset.graph);
      if (entry.path) JV.focus(doc, entry.path, "card");
      else { doc.search.query = entry.value.id; JV.runSearch(doc); }
    }
  });
  view.addEventListener("keydown", event => {
    const node = event.target.closest?.("[data-graph]");
    if (node && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); node.dispatchEvent(new MouseEvent("click", {bubbles: true})); }
  });
  view.addEventListener("dblclick", event => {
    const doc = JV.active;
    if (!doc || JV.viewOf(doc) !== "tree") return;
    const key = event.target.closest(".k[data-copy-row]");
    if (key) JV.toggleRow(doc, doc.rows[Number(key.dataset.copyRow)]);
  });
  document.addEventListener("click", event => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready") return;
    const tree = event.target.closest("[data-tree]");
    if (tree) {
      if (tree.dataset.tree === "expand-all") return doc.kind === "server" ? JV.server.expandAll(doc) : JV.expandAll(doc);
      if (tree.dataset.tree === "collapse-all") return doc.kind === "server" ? JV.server.collapseAll(doc) : JV.collapseAll(doc);
      if (tree.dataset.tree === "depth") {
        return JV.openMenu(tree.getBoundingClientRect().left, tree.getBoundingClientRect().bottom + 4, [1, 2, 3, 4, 5, 6, 8].map(depth => ({label: `Depth ${depth}`, action: () => { doc.depth = depth; if (doc.kind === "server") return JV.server.expandAll(doc, depth); JV.collapseAll(doc); JV.expandAll(doc, depth); }})));
      }
    }
    const raw = event.target.closest("[data-raw]");
    if (raw) {
      const value = doc.kind === "server" ? doc.rawValue : JV.focusValue(doc);
      return JV.copy(JSON.stringify(value, null, raw.dataset.raw === "min" ? 0 : 2) ?? "", {label: raw.dataset.raw === "min" ? "Copied minified JSON" : "Copied formatted JSON"});
    }
    // Table chrome: filter chips, summary cards, columns, selection.
    const remove = event.target.closest("[data-remove-filter]");
    if (remove) return JV.setFilter(doc, remove.dataset.removeFilter, null);
    if (event.target.closest("[data-remove-group]")) { const {state} = JV.tableState(doc); state.groupBy = null; state.display = null; JV.rememberLayout(doc, state); return JV.renderTable(doc); }
    if (event.target.closest("[data-clear-sort]")) { const {state} = JV.tableState(doc); state.sort = []; invalidate(state); JV.rememberLayout(doc, state); return JV.renderTable(doc); }
    const add = event.target.closest("[data-add-filter]");
    if (add) return filterPopover(doc, add);
    const columns = event.target.closest("[data-action-columns]");
    if (columns) return columnChooser(doc, columns);
    const selection = event.target.closest("[data-selection]");
    if (selection) {
      const {state} = JV.tableState(doc);
      if (selection.dataset.selection === "clear") { state.selection.clear(); return JV.renderTable(doc); }
      return JV.copySelection(doc, selection.dataset.selection);
    }
    const stat = event.target.closest("[data-stat-filter]");
    if (stat && stat.dataset.statFilter) {
      const value = stat.dataset.statValue;
      return JV.setFilter(doc, stat.dataset.statFilter, value === "empty" ? "empty" : `=${value}`);
    }
    if (event.target.closest("[data-breakdown]")) { const {state} = JV.tableState(doc); state.breakdown = !state.breakdown; return JV.renderTable(doc); }
  });
  document.addEventListener("change", event => {
    const doc = JV.active;
    if (!doc) return;
    if (event.target.closest("[data-coverage]")) { const {state} = JV.tableState(doc); state.coverageTag = event.target.value; JV.renderTable(doc); }
  });
  JV.columnMenu = (doc, key, anchor) => {
    const {state, source} = JV.tableState(doc);
    const column = state.columns.find(item => item.key === key);
    const rect = anchor.getBoundingClientRect();
    JV.openMenu(rect.left, rect.bottom + 2, [
      {label: "Sort ascending", action: () => { state.sort = [{key, dir: 1}]; invalidate(state); JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      {label: "Sort descending", action: () => { state.sort = [{key, dir: -1}]; invalidate(state); JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      {label: "Filter…", action: () => filterPopover(doc, anchor, key)},
      {label: state.groupBy === key ? "Stop grouping" : "Group by this column", action: () => { state.groupBy = state.groupBy === key ? null : key; state.collapsed.clear(); state.display = null; JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      "-",
      {label: column.pinned ? "Unpin" : "Pin to the left", action: () => { column.pinned = !column.pinned; JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      {label: "Hide column", action: () => { column.visible = false; JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      {label: "Copy column values", hint: "one per line", action: async () => {
        const rows = doc.kind === "server" ? await JV.server.rowsFor(doc, null) : JV.tableView(state, source).map(index => source.rows[index]);
        JV.copy(rows.map(row => M.cellText(JV.fieldOf(row, key))).join("\n"), {label: "Copied column", key: column.label});
      }},
    ]);
  };

  // Right-click menus on tree rows, cells and card values.
  document.addEventListener("contextmenu", event => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready" || JV.screen !== "doc") return;
    let path, value, extra = [];
    const treeRow = event.target.closest("#view .tr[data-index]");
    const cell = event.target.closest("#view .td[data-cell]");
    const refElement = event.target.closest("[data-ref]");
    if (treeRow) {
      const row = doc.rows[Number(treeRow.dataset.index)];
      if (row.more) return;
      path = JV.rowPath(doc, row); value = row.value;
      JV.select(doc, path); JV.paintTree(doc);
    } else if (cell) {
      const {state, source} = JV.tableState(doc);
      const index = Number(cell.closest("[data-row]").dataset.row);
      const row = doc.kind === "server" ? JV.server.rowAt(doc, index) : source.rows[index];
      const key = cell.dataset.cell;
      JV.selectTableRow(doc, index);
      value = JV.fieldOf(row, key);
      path = key.startsWith("@") ? row.path : [...row.path, ...key.split(".")];
      if (doc.kind === "client" && M.getAt(doc.value, path) === undefined) path = row.path;
      const text = M.cellText(value);
      extra = [
        "-",
        {label: "Copy row as JSON", action: () => JV.copy(JSON.stringify(row.value, null, 2), {label: "Copied row", key: C.displayName(row.value) || "row"})},
        {label: `Filter where ${JV.columnLabel(state, key)} = this`, action: () => JV.setFilter(doc, key, `=${text}`)},
        {label: `Filter where ${JV.columnLabel(state, key)} ≠ this`, action: () => JV.setFilter(doc, key, `!=${text}`)},
        {label: `Group by ${JV.columnLabel(state, key)}`, action: () => { state.groupBy = key; state.collapsed.clear(); state.display = null; JV.rememberLayout(doc, state); JV.renderTable(doc); }},
      ];
    } else if (refElement) {
      const region = refElement.closest("#detail") ? "detail" : "card";
      const entry = JV.refs[region].get(refElement.dataset.ref);
      if (!entry) return;
      ({path, value} = entry);
    } else return;
    event.preventDefault();
    JV.openMenu(event.clientX, event.clientY, JV.nodeMenu(doc, path, value, extra));
  });

  // Table: resizing, column dragging, the chooser and the filter popover.
  view.addEventListener("pointerdown", event => {
    const handle = event.target.closest("[data-resize]");
    if (!handle || !JV.active) return;
    event.preventDefault();
    const doc = JV.active;
    const {state} = JV.tableState(doc);
    const column = state.columns.find(item => item.key === handle.dataset.resize);
    const startX = event.clientX, startWidth = column.width;
    const move = moveEvent => { column.width = Math.max(60, Math.min(1200, startWidth + moveEvent.clientX - startX)); JV.renderTable(doc); };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); JV.rememberLayout(doc, state); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  let draggedColumn = null;
  view.addEventListener("dragstart", event => {
    const header = event.target.closest(".th[data-column]");
    if (!header) return;
    draggedColumn = header.dataset.column;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", draggedColumn);
  });
  view.addEventListener("dragover", event => { if (draggedColumn && event.target.closest(".th[data-column]")) event.preventDefault(); });
  view.addEventListener("drop", event => {
    const header = event.target.closest(".th[data-column]");
    if (!draggedColumn || !header || !JV.active) return;
    event.preventDefault();
    event.stopPropagation();
    const doc = JV.active;
    const {state} = JV.tableState(doc);
    const from = state.columns.findIndex(column => column.key === draggedColumn);
    const [column] = state.columns.splice(from, 1);
    state.columns.splice(state.columns.findIndex(item => item.key === header.dataset.column), 0, column);
    draggedColumn = null;
    JV.rememberLayout(doc, state);
    JV.renderTable(doc);
  });
  view.addEventListener("dragend", () => { draggedColumn = null; });
  const popover = $("#popover");
  popover.addEventListener("input", event => {
    if (event.target.id !== "column-find") return;
    const text = event.target.value.toLowerCase();
    popover.querySelectorAll("[data-column-item]").forEach(item => { item.hidden = Boolean(text) && !item.dataset.columnItem.includes(text); });
  });
  popover.addEventListener("change", event => {
    const box = event.target.closest("[data-column-show]");
    if (!box || !JV.active) return;
    const doc = JV.active;
    const {state} = JV.tableState(doc);
    state.columns[Number(box.dataset.columnShow)].visible = box.checked;
    JV.rememberLayout(doc, state);
    JV.renderTable(doc);
  });
  popover.addEventListener("submit", event => {
    if (event.target.id !== "filter-form" || !JV.active) return;
    event.preventDefault();
    const key = $("#filter-column").value, text = $("#filter-value").value.trim();
    JV.closePopover();
    if (text) JV.setFilter(JV.active, key, text);
  });
  popover.addEventListener("click", event => {
    const doc = JV.active;
    if (!doc || !event.target.closest("[data-columns],[data-column-pin],[data-column-move]")) return;
    const {state} = JV.tableState(doc);
    const all = event.target.closest("[data-columns]");
    const pin = event.target.closest("[data-column-pin]");
    const move = event.target.closest("[data-column-move]");
    if (all) state.columns.forEach(column => { column.visible = all.dataset.columns === "all"; });
    else if (pin) state.columns[Number(pin.dataset.columnPin)].pinned = !state.columns[Number(pin.dataset.columnPin)].pinned;
    else if (move) {
      const index = Number(move.dataset.columnMove), target = index + Number(move.dataset.dir);
      if (target < 0 || target >= state.columns.length) return;
      [state.columns[index], state.columns[target]] = [state.columns[target], state.columns[index]];
    }
    JV.rememberLayout(doc, state);
    JV.renderTable(doc);
    columnChooser(doc, document.querySelector("[data-action-columns]") || $("#chips"));
  });
})();
