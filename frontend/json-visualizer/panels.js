"use strict";
// Panels and dialogs: the details panel, the home page, the query bar (JMESPath / jq),
// snapshot compare, the command palette, the command runner, history and snapshots,
// export, merging paged files, saved table views and bookmarks.
(() => {
  const JV = window.JV, M = JV.M, C = JV.C, $ = JV.$, esc = JV.esc;

  JV.select = (doc, path) => {
    doc.selected = path;
    JV.renderDetail(doc);
    JV.renderBreadcrumb(doc);
    JV.setHash(path);
  };
  JV.setHash = path => {
    try { history.replaceState(null, "", "#" + encodeURIComponent(M.formatPath(path, "jmes"))); } catch { /* sandboxed */ }
  };

  // ---------- Details panel ----------
  const NOUNS = {"EC2 instances": "instance", "Azure VMs": "VM", "Security groups": "security group", VPCs: "VPC", Subnets: "subnet", Volumes: "volume", "Kubernetes objects": "object", "Azure resources": "resource", "Terraform resources": "resource"};
  // How often the selected field's value occurs across the resources ("Same value in this file").
  function sameValues(doc, path) {
    const located = JV.rowFieldOf(doc, path);
    if (!located) return "";
    const rows = doc.unwrap ? doc.unwrap.rows : doc.value.map((value, index) => ({value, path: [index]}));
    const counts = new Map();
    for (const row of rows) {
      const value = M.getField(row.value, located.field);
      if (value === undefined || M.isContainer(value)) continue;
      const text = M.cellText(value);
      counts.set(text, (counts.get(text) || 0) + 1);
    }
    if (counts.size < 2 && rows.length < 2) return "";
    const noun = NOUNS[doc.template?.name] || "item";
    const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 6);
    return `<section class="detail-section"><h3>Same value in this file</h3>${top.map(([value, count]) => `<button type="button" class="same-row" data-same-field="${esc(located.field)}" data-same-value="${esc(value)}" title="Show only these"><span>${esc(value || "(empty)")}</span><span class="tool-note">${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}</span></button>`).join("")}</section>`;
  }
  JV.renderDetail = doc => {
    JV.refs.detail.clear();
    const path = doc.selected;
    if (!path || doc.status !== "ready") {
      $("#detail").innerHTML = `<div class="detail-empty"><b>Nothing selected</b><p>Select a row or a node to see it here, with its paths and copy actions.</p><p class="tool-note">Click any key, value or cell to copy it. Right-click for paths in JMESPath, jq or JavaScript.</p></div>`;
      return;
    }
    let value;
    if (doc.kind === "server") {
      value = JV.server.cachedValue(doc, path);
      if (value === undefined) {
        $("#detail").innerHTML = `<div class="detail-empty"><p>Loading…</p></div>`;
        JV.server.valueAt(doc, path).then(() => { if (JV.active === doc && JV.samePath(doc.selected, path)) JV.renderDetail(doc); }).catch(error => { $("#detail").innerHTML = `<div class="detail-empty"><p>${esc(error.message)}</p></div>`; });
        return;
      }
    } else value = M.getAt(doc.value, path);
    const type = M.typeOf(value);
    const key = JV.lastKey(path);
    const object = M.isContainer(value) && !Array.isArray(value);
    const link = JV.consoleLinkOf(value);
    const parts = [];
    const noun = object && (JV.useUnwrap(doc) || doc.sub) && JV.viewOf(doc) === "table" ? NOUNS[doc.template?.name] || "item" : "node";
    const name = object ? JV.safeName(value) : null;
    const status = object ? JV.statusOf(value) : null;
    const bookmarked = JV.bookmarksOf(doc).some(item => JV.samePath(item.path, path));
    parts.push(`<div class="detail-head"><span class="tool-note">Selected ${esc(noun)}</span><span class="detail-title">${esc(name || (path.length ? JV.partText(key) : "root"))}</span>${status ? `<span class="badge tone-${status.tone}">${esc(status.value)}</span>` : ""}
      ${!M.isContainer(value) ? `<span class="detail-value t-${type}${type === "string" ? ` k-${M.stringKind(value)}` : ""}">${type === "string" ? (JV.secretOf(key, value) && !JV.isRevealed(JV.pathId(path)) ? JV.valueHtml(key, value, {id: JV.pathId(path)}) : `"${JV.marked(value.length > 400 ? value.slice(0, 400) + "…" : value)}"`) : esc(String(value))}</span>` : `<span class="tool-note">${esc(M.preview(value))}</span>`}</div>`);
    if (object) {
      const fields = Object.keys(value).filter(field => !M.isContainer(value[field]) && C.isKeyField(field)).slice(0, 10);
      const more = Object.keys(value).filter(field => !M.isContainer(value[field]) && !fields.includes(field)).slice(0, Math.max(0, 8 - fields.length));
      const list = [...fields, ...more];
      if (list.length) parts.push(`<div class="field-list">${list.map(field => {
        const id = JV.ref("detail", [...path, field], value[field]);
        return `<button type="button" class="field" data-ref="${id}" title="Copy ${esc(field)}"><span class="field-key">${esc(field)}</span><span class="field-value">${JV.valueHtml(field, value[field], {limit: 120, id: JV.pathId([...path, field])})}${JV.icon.copy}</span></button>`;
      }).join("")}</div>`);
      const tags = C.tags(value);
      if (tags.length) parts.push(`<section class="detail-section"><h3>Tags</h3><div class="tags block">${JV.tagChips(tags, 60)}</div></section>`);
      const groups = Array.isArray(value.SecurityGroups) ? value.SecurityGroups : Array.isArray(value.Groups) ? value.Groups : null;
      if (groups?.length && doc.kind === "client") parts.push(`<section class="detail-section"><h3>Security groups</h3>${groups.map((group, index) => `<button type="button" class="link mono" data-goto-ref="${JV.ref("detail", [...path, value.SecurityGroups ? "SecurityGroups" : "Groups", index], group)}" data-goto-id="${esc(group.GroupId || "")}">${esc(group.GroupId || "")}${group.GroupName ? ` · ${esc(group.GroupName)}` : ""}</button>`).join("")}</section>`);
      const viewer = JV.specialViewer(value);
      if (viewer) parts.push(viewer);
    } else if (Array.isArray(value)) {
      const viewer = C.isTagList(value) ? `<div class="tags block">${JV.tagChips(C.tags({Tags: value}), 80)}</div>` : "";
      if (viewer) parts.push(`<section class="detail-section"><h3>Tags</h3>${viewer}</section>`);
    }
    parts.push(`<section class="detail-section"><h3>Path</h3>${[["jmes", "JMESPath"], ["jq", "jq"], ["js", "JavaScript"]].map(([syntax, label]) => `<div class="path-box"><div class="path-head"><span>${label}</span><button type="button" class="link" data-copy-path="${syntax}">Copy</button></div><code>${esc(M.formatPath(path, syntax))}</code></div>`).join("")}</section>`);
    if (!M.isContainer(value) && doc.kind === "client") parts.push(sameValues(doc, path));
    if (type === "string") {
      const kind = M.stringKind(value);
      if (kind === "date") parts.push(`<p class="tool-note">${esc(JV.dateTitle(value)).replace(/\n/g, "<br>")}</p>`);
      if (value.length > 400) parts.push(`<details class="detail-section"><summary>Full text (${esc(M.formatBytes(value.length))})</summary><pre class="full-value">${JV.marked(value)}</pre></details>`);
      if (kind === "url" && /^https?:\/\//.test(value)) parts.push(`<a class="console" href="${esc(value)}" target="_blank" rel="noopener noreferrer">Open link ↗</a>`);
      // A string that holds JSON opens as cards right here, or in its own tab.
      if (/^\s*[[{]/.test(value) && value.length < 20 * 1024 * 1024) {
        try {
          const parsed = JSON.parse(value);
          parts.push(`<section class="detail-section"><h3>Embedded JSON <button type="button" class="link small" data-detail="open-embedded">Open in a new tab</button></h3>${JV.cardBody("detail", parsed, path, 1)}</section>`);
        } catch { /* not JSON after all */ }
      }
    }
    parts.push(`<div class="detail-actions"><button type="button" data-detail="copy">Copy value</button>${M.isContainer(value) ? `<button type="button" data-detail="copy-min">Minified</button><button type="button" data-detail="focus">Open here</button>` : ""}<button type="button" data-detail="tree">Show in tree</button><button type="button" data-detail="bookmark" aria-pressed="${bookmarked}" title="${bookmarked ? "Remove bookmark" : "Bookmark this node"}">${JV.icon.star}${bookmarked ? "Bookmarked" : "Bookmark"}</button></div>`);
    if (link) parts.push(`<a class="console-button" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>`);
    if (M.isContainer(value)) parts.push(`<details class="detail-section all-fields"${object && Object.keys(value).length <= 30 ? "" : ""}><summary>All fields</summary>${JV.cardBody("detail", value, path, 1)}</details>`);
    $("#detail").innerHTML = `<div class="detail-inner">${parts.join("")}</div>`;
  };
  $("#detail").addEventListener("click", async event => {
    const doc = JV.active;
    if (!doc?.selected) return;
    const value = JV.valueAt(doc, doc.selected);
    const key = JV.lastKey(doc.selected);
    const pathButton = event.target.closest("[data-copy-path]");
    if (pathButton) return JV.copy(M.formatPath(doc.selected, pathButton.dataset.copyPath), {label: "Copied path of", key: key ?? "root"});
    const action = event.target.closest("[data-detail]")?.dataset.detail;
    if (action === "copy") return JV.copySelected(doc);
    if (action === "copy-min") return JV.copy(JSON.stringify(value), {label: "Copied minified", key: key ?? "root"});
    if (action === "focus") return JV.focus(doc, doc.selected);
    if (action === "tree") return JV.reveal(doc, doc.selected);
    if (action === "bookmark") return JV.toggleBookmark(doc, doc.selected);
    if (action === "open-embedded") return JV.openValue(JSON.parse(value), `${JV.partText(key)} (embedded)`);
    const same = event.target.closest("[data-same-field]");
    if (same) { JV.focus(doc, [], "table"); return JV.setFilter(doc, same.dataset.sameField, `=${same.dataset.sameValue}`); }
    const gotoRef = event.target.closest("[data-goto-ref]");
    if (gotoRef) {
      const id = gotoRef.dataset.gotoId;
      if (id) { doc.search.query = id; doc.search.options = {...doc.search.options, regex: false, wholeWord: false}; JV.runSearch(doc); return; }
      return JV.reveal(doc, JV.refs.detail.get(gotoRef.dataset.gotoRef).path);
    }
    const focusRef = event.target.closest("[data-focus-ref]");
    if (focusRef) { event.preventDefault(); return JV.focus(doc, JV.refs.detail.get(focusRef.dataset.focusRef).path, focusRef.dataset.focusView || "auto"); }
    const element = event.target.closest("[data-ref]");
    if (element && !event.target.closest("summary") || element && event.target.closest(".ck")) {
      if (event.target.closest("summary")) event.preventDefault();
      const entry = JV.refs.detail.get(element.dataset.ref);
      JV.copyValue(entry.value, JV.lastKey(entry.path));
    }
  });

  // ---------- Bookmarks (per file, this browser) ----------
  const BOOKMARKS = "owl-json-bookmarks";
  const docKey = doc => `${doc.name}|${doc.size}`;
  JV.bookmarksOf = doc => JV.store.json(BOOKMARKS, {})[docKey(doc)] || [];
  JV.toggleBookmark = (doc, path) => {
    const all = JV.store.json(BOOKMARKS, {});
    const list = all[docKey(doc)] || [];
    const index = list.findIndex(item => JV.samePath(item.path, path));
    if (index >= 0) list.splice(index, 1);
    else {
      const value = JV.valueAt(doc, path);
      const name = M.isContainer(value) && !Array.isArray(value) ? C.displayName(value) : null;
      list.push({path, label: name ? `${name}` : M.formatPath(path, "jmes")});
    }
    all[docKey(doc)] = list;
    JV.store.setJson(BOOKMARKS, all);
    JV.toast(index >= 0 ? "Bookmark removed" : "Bookmarked", {value: M.formatPath(path, "jmes")});
    JV.render();
  };
  JV.bookmarksHtml = doc => {
    const list = JV.bookmarksOf(doc);
    if (!list.length) return "";
    return `<div class="side-block"><span class="side-title">Bookmarks</span>${list.map((item, index) => `<button type="button" class="outline-item" data-bookmark="${index}" style="--depth:0" title="${esc(M.formatPath(item.path, "jmes"))}"><span class="o-name">★ ${esc(item.label)}</span></button>`).join("")}</div>`;
  };

  // ---------- Saved table views ----------
  JV.savedViewsHtml = doc => {
    if (doc.kind !== "client" || JV.viewOf(doc) !== "table") return "";
    const {state} = JV.tableState(doc);
    const saved = JV.savedViews(doc, state);
    const base = state.template || (doc.sub ? doc.sub : "This table");
    return `<div class="side-block"><span class="side-title">Saved views</span>
      <button type="button" class="outline-item" data-saved-view="@default" style="--depth:0"><span class="o-name">${esc(base)} · default columns</span></button>
      ${saved.map(view => `<div class="saved-row"><button type="button" class="outline-item" data-saved-view="${esc(view.name)}" style="--depth:0"><span class="o-name">${esc(base)} · ${esc(view.name)}</span></button><button type="button" class="mini" data-delete-view="${esc(view.name)}" aria-label="Delete view ${esc(view.name)}">×</button></div>`).join("")}
      <button type="button" class="link side-link" data-save-view>+ Save current view</button></div>`;
  };
  $("#outline").addEventListener("click", event => {
    const doc = JV.active;
    if (!doc) return;
    const bookmark = event.target.closest("[data-bookmark]");
    if (bookmark) return JV.reveal(doc, JV.bookmarksOf(doc)[Number(bookmark.dataset.bookmark)].path);
    const saved = event.target.closest("[data-saved-view]");
    if (saved) return JV.applySavedView(doc, saved.dataset.savedView);
    const remove = event.target.closest("[data-delete-view]");
    if (remove) { JV.deleteView(doc, JV.tableState(doc).state, remove.dataset.deleteView); return JV.render(); }
    if (event.target.closest("[data-save-view]")) {
      JV.dialog(`<form class="dialog-form" id="save-view-form"><h2>Save this table view</h2><p class="tool-note">Columns, their order and widths, sorting, filters and grouping are saved for every file of this kind.</p>
        <label class="field-label">Name <input id="view-name" required placeholder="networking" autocomplete="off"></label>
        <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Save view</button></div></form>`);
      $("#view-name").focus();
    }
  });

  // ---------- Home ----------
  // "AWS · EC2" in the Recent table: the layout's short name next to the source.
  const SHORT = {"EC2 instances": "EC2", "Azure VMs": "VM", "Security groups": "Security groups", VPCs: "VPC", Subnets: "Subnets", Volumes: "EBS", "CloudTrail events": "CloudTrail", "CloudTrail lookup": "CloudTrail", "CloudWatch log events": "Logs", "Lambda functions": "Lambda", "gcloud instances": "Compute", "Kubernetes objects": "", "Azure resources": "", "Terraform resources": ""};
  JV.renderHome = async () => {
    const [recent, snapshots] = await Promise.all([JV.recentList(), JV.api("/snapshots").then(data => data.snapshots || []).catch(() => [])]);
    if (JV.screen !== "home") return;
    const rows = [
      ...recent.map(entry => ({kind: "recent", key: entry.key, name: entry.name, source: entry.source, detail: entry.detail, format: entry.format, server: entry.server, size: entry.size, at: entry.openedAt, available: Boolean(entry.blob)})),
      ...snapshots.map(snapshot => ({kind: "snapshot", key: snapshot.id, name: snapshot.name, source: snapshot.source, detail: snapshot.labels?.command || "", format: snapshot.format, size: snapshot.size, at: snapshot.created_at * 1000, available: true, labels: snapshot.labels})),
    ].sort((a, b) => b.at - a.at).slice(0, 8);
    const opened = time => {
      const date = new Date(time), now = new Date();
      if (now - date < 3600_000) return M.relativeTime(time);
      if (date.toDateString() === now.toDateString()) return `Today, ${date.toTimeString().slice(0, 5)}`;
      if (new Date(now - 86400_000).toDateString() === date.toDateString()) return "Yesterday";
      return date.toLocaleDateString(undefined, {day: "numeric", month: "short"});
    };
    $("#home-recent").innerHTML = rows.length ? `<div class="recent-head"><h2>Recent</h2><button type="button" class="link" data-history>Show all history</button></div>
      <div class="recent-card"><table class="recent-table"><thead><tr><th>File</th><th>Source</th><th>Format</th><th class="right">Size</th><th class="right">Opened</th></tr></thead><tbody>
      ${rows.map(row => `<tr><td><button type="button" class="recent-open" data-open-${row.kind}="${esc(row.key)}"${row.available ? "" : ' disabled title="Too large to keep in the browser; open the file again"'}>${esc(row.name)}</button>${row.kind === "snapshot" ? ' <span class="pill" title="Saved snapshot on this machine">Snapshot</span>' : ""}</td>
        <td>${JV.sourceBadge(row.source, SHORT[row.detail] ?? "") || '<span class="tool-note">—</span>'}</td>
        <td class="muted">${esc(JV.FORMAT_LABEL[row.format] || "JSON")}${row.server ? " · server mode" : ""}</td><td class="right mono">${esc(M.formatBytes(row.size))}</td><td class="right muted">${esc(opened(row.at))}</td></tr>`).join("")}
      </tbody></table></div>` : "";
    $("#home-limit").textContent = `Up to ${JV.settings.limitMb} MB parsed in your browser`;
  };
  $("#home").addEventListener("click", event => {
    if (event.target.closest("#home-open")) return JV.pickFiles();
    if (event.target.closest("#home-paste")) return JV.pasteFromClipboard();
    if (event.target.closest("#home-url")) return JV.openUrlDialog();
    if (event.target.closest("[data-history]")) return JV.openHistory();
    const recent = event.target.closest("[data-open-recent]");
    if (recent) return JV.openRecent(recent.dataset.openRecent);
    const snapshot = event.target.closest("[data-open-snapshot]");
    if (snapshot) return JV.openSnapshot(snapshot.dataset.openSnapshot);
  });

  // ---------- Open a URL through the local server ----------
  JV.openUrlDialog = () => {
    JV.dialog(`<form class="dialog-form" id="url-form"><h2>Open a URL</h2><p class="tool-note">An https link or an S3 pre-signed URL. The local OWL server downloads it, so it works without CORS.</p>
      <label class="field-label">URL <input id="url-input" type="url" required placeholder="https://bucket.s3.amazonaws.com/export.json?X-Amz-Signature=…" autocomplete="off"></label>
      <p id="url-error" class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Open</button></div></form>`, {wide: true});
    $("#url-input").focus();
  };
  async function openUrl(url) {
    const response = await JV.api("/fetch", {method: "POST", body: {url}, raw: true});
    const name = decodeURIComponent(response.headers.get("X-File-Name") || "download.json");
    const blob = await response.blob();
    JV.closeDialog();
    JV.openFiles([new File([blob], name)]);
  }

  // ---------- Snapshots and history ----------
  JV.openSnapshot = async (id, fields = {}) => {
    try {
      const [meta, response] = await Promise.all([JV.api(`/snapshots/${encodeURIComponent(id)}`), JV.api(`/snapshots/${encodeURIComponent(id)}/content`, {raw: true})]);
      const blob = await response.blob();
      const doc = JV.newDoc({name: meta.name, size: blob.size, blob: new File([blob], meta.name), snapshotId: id, labels: meta.labels || {}, derived: true, ...fields});
      JV.docs.push(doc);
      JV.activate(doc);
      JV.load(doc);
      return doc;
    } catch (error) { JV.toast(`Could not open the snapshot: ${error.message}`, {tone: "warn"}); }
  };
  JV.saveSnapshot = doc => {
    const labels = doc.labels || {};
    JV.dialog(`<form class="dialog-form" id="snapshot-form"><h2>Save as snapshot</h2><p class="tool-note">Keeps a compressed copy on this machine (in OWL's data folder) with labels, to reopen or compare later.</p>
      <label class="field-label">Name <input id="snap-name" value="${esc(doc.name)}" required></label>
      <div class="field-grid">${["account", "environment", "region", "command"].map(key => `<label class="field-label">${key[0].toUpperCase() + key.slice(1)} <input data-label="${key}" value="${esc(labels[key] || "")}" placeholder="${{account: "123456789012", environment: "prod", region: "eu-west-1", command: "aws ec2 describe-instances"}[key]}"></label>`).join("")}</div>
      <p id="snap-error" class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Save snapshot</button></div></form>`);
    $("#app-dialog").dataset.doc = doc.id;
  };
  async function submitSnapshot() {
    const doc = JV.docs.find(item => item.id === Number($("#app-dialog").dataset.doc));
    const labels = Object.fromEntries([...document.querySelectorAll("[data-label]")].map(input => [input.dataset.label, input.value.trim()]).filter(([, value]) => value));
    const body = doc.blob || new Blob([doc.text ?? JSON.stringify(doc.value)], {type: "application/json"});
    const query = new URLSearchParams({name: $("#snap-name").value.trim() || doc.name, labels: JSON.stringify(labels), format: doc.detected || "json", source: doc.source || "unknown", command: labels.command || ""});
    const record = await JV.api(`/snapshots?${query}`, {method: "POST", body, headers: {"Content-Type": "application/octet-stream"}});
    doc.snapshotId = record.id;
    doc.labels = labels;
    JV.closeDialog();
    JV.toast("Saved snapshot", {value: record.name});
  }
  JV.openHistory = async () => {
    const [recent, data] = await Promise.all([JV.recentList(), JV.api("/snapshots").catch(error => ({error: error.message, snapshots: []}))]);
    const settings = data.settings || {retention_days: 0, max_snapshots: 200};
    JV.dialog(`<div class="dialog-form history"><div class="dialog-head"><h2>History</h2><button type="button" class="icon-button" data-close-dialog aria-label="Close">×</button></div>
      <h3>Snapshots on this machine <span class="tool-note">${data.snapshots.length} · ${esc(M.formatBytes(data.total_bytes || 0))} compressed</span></h3>
      ${data.error ? `<p class="dialog-error">${esc(data.error)}</p>` : ""}
      <div class="history-list">${data.snapshots.map(snapshot => `<div class="history-row"><div><b>${esc(snapshot.name)}</b><div class="tool-note">${esc(Object.entries(snapshot.labels || {}).map(([key, value]) => `${key}: ${value}`).join(" · ") || "No labels")} · ${esc(M.formatBytes(snapshot.size))} · ${esc(new Date(snapshot.created_at * 1000).toLocaleString())}</div></div>
        <div class="row-actions"><button type="button" data-history-open="${esc(snapshot.id)}">Open</button><button type="button" data-history-labels="${esc(snapshot.id)}">Labels</button><button type="button" data-history-delete="${esc(snapshot.id)}" aria-label="Delete ${esc(snapshot.name)}">Delete</button></div></div>`).join("") || '<p class="tool-note">No snapshots yet. Right-click a tab → Save as snapshot, or run a command.</p>'}</div>
      <h3>Opened in this browser <span class="tool-note">${recent.length}</span></h3>
      <div class="history-list">${recent.map(entry => `<div class="history-row"><div><b>${esc(entry.name)}</b><div class="tool-note">${esc([JV.SOURCE_LABEL[entry.source], JV.FORMAT_LABEL[entry.format], M.formatBytes(entry.size), new Date(entry.openedAt).toLocaleString()].filter(Boolean).join(" · "))}</div></div><div class="row-actions"><button type="button" data-history-recent="${esc(entry.key)}"${entry.blob ? "" : " disabled"}>Open</button></div></div>`).join("") || '<p class="tool-note">Nothing yet.</p>'}</div>
      <form id="retention-form" class="retention"><h3>Keep snapshots</h3><label>Delete after <input id="retention-days" type="number" min="0" max="3650" value="${settings.retention_days}"> days (0 = keep)</label><label>Keep at most <input id="retention-max" type="number" min="1" max="10000" value="${settings.max_snapshots}"> snapshots</label><button type="submit">Save</button></form>
      <form id="purge-form" class="purge"><h3>Delete all history</h3><p class="tool-note">Deletes every snapshot and big-file index on this machine and the recent files kept in this browser.</p><label>Type <code>delete all</code> <input id="purge-text" autocomplete="off"></label><button type="submit" class="danger">Delete all history</button></form></div>`, {wide: true});
  };
  JV.app = JV.app || {};
  $("#app-dialog").addEventListener("click", async event => {
    const open = event.target.closest("[data-history-open]");
    if (open) { JV.closeDialog(); return JV.openSnapshot(open.dataset.historyOpen); }
    const recent = event.target.closest("[data-history-recent]");
    if (recent) { JV.closeDialog(); return JV.openRecent(recent.dataset.historyRecent); }
    const remove = event.target.closest("[data-history-delete]");
    if (remove) {
      if (!confirm("Delete this snapshot?")) return;
      await JV.api(`/snapshots/${encodeURIComponent(remove.dataset.historyDelete)}`, {method: "DELETE"}).catch(error => JV.toast(error.message, {tone: "warn"}));
      return JV.openHistory();
    }
    const labels = event.target.closest("[data-history-labels]");
    if (labels) {
      const record = await JV.api(`/snapshots/${encodeURIComponent(labels.dataset.historyLabels)}`);
      const text = prompt("Labels as key=value, separated by commas (account, environment, region, command…)", Object.entries(record.labels || {}).map(([key, value]) => `${key}=${value}`).join(", "));
      if (text === null) return;
      const parsed = Object.fromEntries(text.split(",").map(part => part.split("=").map(item => item.trim())).filter(([key, value]) => key && value));
      await JV.api(`/snapshots/${encodeURIComponent(record.id)}`, {method: "PATCH", body: {labels: parsed}}).catch(error => JV.toast(error.message, {tone: "warn"}));
      return JV.openHistory();
    }
    const run = event.target.closest("[data-run-saved]");
    if (run) return runSaved(Number(run.dataset.runSaved));
    const forget = event.target.closest("[data-delete-saved]");
    if (forget) { await JV.api(`/commands/${forget.dataset.deleteSaved}`, {method: "DELETE"}); return JV.openRunner(); }
    if (event.target.closest("[data-run-cancel]") && runJob) { await JV.api(`/run/${runJob}/cancel`, {method: "POST"}).catch(() => {}); }
    const paletteItem = event.target.closest("[data-palette]");
    if (paletteItem) { const item = paletteItems[Number(paletteItem.dataset.palette)]; JV.closeDialog(); item?.action(); }
    const merge = event.target.closest("[data-merge-go]");
    if (merge) return doMerge();
  });
  $("#app-dialog").addEventListener("submit", async event => {
    const form = event.target;
    const handlers = {
      "snapshot-form": submitSnapshot,
      "url-form": () => openUrl($("#url-input").value.trim()),
      "save-view-form": () => { const doc = JV.active; JV.saveView(doc, JV.tableState(doc).state, $("#view-name").value.trim()); JV.closeDialog(); JV.render(); JV.toast("Saved view", {value: $("#view-name")?.value || ""}); },
      "retention-form": async () => { await JV.api("/settings", {method: "PUT", body: {retention_days: Number($("#retention-days").value) || 0, max_snapshots: Number($("#retention-max").value) || 200}}); JV.toast("Saved"); JV.openHistory(); },
      "purge-form": async () => {
        if ($("#purge-text").value.trim().toLowerCase() !== "delete all") return JV.toast("Type delete all to confirm", {tone: "warn"});
        await JV.api("/snapshots", {method: "DELETE", body: {confirm: "delete all"}}).catch(error => JV.toast(error.message, {tone: "warn"}));
        await JV.forgetRecent();
        JV.closeDialog();
        JV.toast("All history deleted");
        JV.render();
      },
      "runner-form": startRun,
      "settings-form": saveSettingsForm,
      "export-form": doExport,
      "palette-form": () => { const first = $("#palette-list [data-palette]:not([hidden])"); if (first) { const item = paletteItems[Number(first.dataset.palette)]; JV.closeDialog(); item?.action(); } },
    };
    if (!handlers[form.id]) return;
    event.preventDefault();
    try { await handlers[form.id](); } catch (error) {
      const target = form.querySelector(".dialog-error");
      if (target) { target.textContent = error.message; target.hidden = false; } else JV.toast(error.message, {tone: "warn"});
    }
  });

  // ---------- Settings ----------
  JV.openSettings = () => {
    const settings = JV.settings;
    JV.dialog(`<form class="dialog-form" id="settings-form"><h2>Settings</h2>
      <label class="field-label">Parse in the browser up to <span><input id="set-limit" type="number" min="1" max="4000" value="${settings.limitMb}"> MB</span><small class="tool-note">Bigger files open through the local server.</small></label>
      <label class="check"><input type="checkbox" id="set-utc"${settings.timeZone === "utc" ? " checked" : ""}> Show times in UTC (else local time)</label>
      <label class="check"><input type="checkbox" id="set-mask"${settings.maskSecrets ? " checked" : ""}> Mask detected secrets (keys, tokens, passwords, connection strings)</label>
      <label class="check"><input type="checkbox" id="set-redact"${settings.redactExports ? " checked" : ""}> Redact secrets in exports by default</label>
      <label class="field-label">Extra secret rules <small class="tool-note">One per line: <code>key:</code>regex or <code>value:</code>regex</small><textarea id="set-rules" spellcheck="false" placeholder="key:^internal_.*token$&#10;value:^xox[bp]-">${esc((settings.rules || []).map(rule => rule.keyPattern ? `key:${rule.keyPattern}` : `value:${rule.valuePattern}`).join("\n"))}</textarea></label>
      <p class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Save</button></div></form>`);
  };
  function saveSettingsForm() {
    const rules = $("#set-rules").value.split("\n").map(line => line.trim()).filter(Boolean).map(line => {
      const [kind, ...rest] = line.split(":");
      const pattern = rest.join(":").trim();
      new RegExp(pattern);
      return kind.trim() === "key" ? {keyPattern: pattern, label: "Custom rule"} : {valuePattern: pattern, label: "Custom rule"};
    });
    Object.assign(JV.settings, {limitMb: Math.max(1, Number($("#set-limit").value) || 50), timeZone: $("#set-utc").checked ? "utc" : "local", maskSecrets: $("#set-mask").checked, redactExports: $("#set-redact").checked, rules});
    JV.saveSettings();
    for (const doc of JV.docs) if (doc.status === "ready" && doc.kind === "client") doc.secretCount = JV.settings.maskSecrets && window.JVSecrets ? window.JVSecrets.scan(doc.value, rules, 10000).length : 0;
    JV.closeDialog();
    JV.render();
  }
  $("#settings-button").addEventListener("click", () => JV.openSettings());

  // ---------- Query bar (JMESPath / jq) ----------
  const QUERY_HISTORY = "owl-json-queries";
  const queryKey = doc => doc.template?.name || JV.SOURCE_LABEL[doc.source] || "generic";
  JV.renderQueryBar = doc => {
    const bar = $("#query-bar");
    if (doc.status !== "ready" || ["raw", "graph"].includes(JV.viewOf(doc))) { bar.hidden = true; return; }
    bar.hidden = false;
    const query = doc.query;
    const focused = document.activeElement?.id === "query-input";
    if (focused && bar.querySelector("#query-input")) return renderQueryResult(doc);
    bar.innerHTML = `<div class="query-line"><select id="query-lang" aria-label="Query language"><option value="jmes"${query.lang === "jmes" ? " selected" : ""}>JMESPath</option><option value="jq"${query.lang === "jq" ? " selected" : ""}>jq</option></select>
      <input id="query-input" class="mono" value="${esc(query.text)}" placeholder="${query.lang === "jq" ? ".Reservations[].Instances[] | select(.State.Name == \"running\") | .InstanceId" : "Reservations[].Instances[?State.Name=='running'].[InstanceId, PrivateIpAddress]"}" spellcheck="false" autocomplete="off" aria-label="Query"${doc.kind === "server" ? " disabled title=\"Queries run on files opened in the browser\"" : ""}>
      <span id="query-count" class="query-count"></span>
      <button type="button" class="tool" data-query="history" data-popover-anchor title="Recent and saved queries">History</button>
      <button type="button" class="tool" data-query="save" title="Save this query for this kind of file">Save</button>
      <button type="button" class="tool" data-query="tab" title="Open the result as a new tab">Open as tab</button>
      <button type="button" class="tool" data-query="cli" title="Copy an aws command with this --query">Copy as CLI</button></div>
      <div id="query-result" class="query-result" hidden></div>`;
    renderQueryResult(doc);
  };
  function renderQueryResult(doc) {
    const query = doc.query;
    const count = $("#query-count"), result = $("#query-result");
    if (!count) return;
    if (!query.text.trim()) { count.textContent = ""; result.hidden = true; return; }
    if (query.error) { count.textContent = query.error; count.className = "query-count bad"; result.hidden = true; return; }
    const value = query.result;
    const n = Array.isArray(value) ? value.length : value === null || value === undefined ? 0 : 1;
    count.textContent = `${n.toLocaleString()} result${n === 1 ? "" : "s"}`;
    count.className = "query-count good";
    const text = JSON.stringify(value, null, 2) ?? "null";
    result.hidden = false;
    result.innerHTML = `<pre>${esc(text.length > 200000 ? text.slice(0, 200000) + "\n…" : text)}</pre>`;
  }
  let queryTimer;
  function runQuery(doc) {
    const query = doc.query;
    query.error = "";
    query.result = undefined;
    if (!query.text.trim()) return renderQueryResult(doc);
    try {
      if (query.lang === "jq") {
        const outputs = window.JVJq.run(doc.value, query.text);
        query.result = outputs.length === 1 ? outputs[0] : outputs;
        query.outputs = outputs.length;
      } else query.result = window.JVJmes.search(doc.value, query.text);
    } catch (error) { query.error = error.message; }
    renderQueryResult(doc);
  }
  function rememberQuery(doc, saved = false) {
    const all = JV.store.json(QUERY_HISTORY, {});
    const key = queryKey(doc);
    const entry = all[key] || {recent: [], saved: []};
    const item = {lang: doc.query.lang, text: doc.query.text};
    entry.recent = [item, ...entry.recent.filter(old => old.text !== item.text || old.lang !== item.lang)].slice(0, 20);
    if (saved) entry.saved = [item, ...entry.saved.filter(old => old.text !== item.text || old.lang !== item.lang)];
    all[key] = entry;
    JV.store.setJson(QUERY_HISTORY, all);
  }
  $("#query-bar").addEventListener("input", event => {
    const doc = JV.active;
    if (!doc || event.target.id !== "query-input") return;
    doc.query.text = event.target.value;
    clearTimeout(queryTimer);
    queryTimer = setTimeout(() => runQuery(doc), 250);
  });
  $("#query-bar").addEventListener("change", event => {
    const doc = JV.active;
    if (!doc || event.target.id !== "query-lang") return;
    doc.query.lang = event.target.value;
    JV.renderQueryBar(doc);
    runQuery(doc);
  });
  $("#query-bar").addEventListener("keydown", event => {
    if (event.target.id === "query-input" && event.key === "Enter" && JV.active) { event.preventDefault(); runQuery(JV.active); if (!JV.active.query.error) rememberQuery(JV.active); }
  });
  $("#query-bar").addEventListener("click", event => {
    const doc = JV.active;
    const action = event.target.closest("[data-query]")?.dataset.query;
    if (!doc || !action) return;
    const query = doc.query;
    if (action === "history") {
      const entry = JV.store.json(QUERY_HISTORY, {})[queryKey(doc)] || {recent: [], saved: []};
      const items = [...entry.saved.map(item => ({...item, saved: true})), ...entry.recent];
      if (!items.length) return JV.toast("No queries yet for this kind of file", {tone: "warn"});
      const rect = event.target.getBoundingClientRect();
      return JV.openMenu(rect.left, rect.top - Math.min(items.length, 14) * 34 - 12, items.slice(0, 14).map(item => ({label: `${item.saved ? "★ " : ""}${item.text}`, hint: item.lang === "jq" ? "jq" : "JMESPath", action: () => { Object.assign(query, {lang: item.lang, text: item.text}); JV.renderQueryBar(doc); runQuery(doc); }})));
    }
    if (!query.text.trim()) return JV.toast("Type a query first", {tone: "warn"});
    if (query.error) return JV.toast(query.error, {tone: "warn"});
    if (action === "save") { rememberQuery(doc, true); return JV.toast("Saved query", {value: query.text}); }
    if (action === "tab") { rememberQuery(doc); return JV.openValue(query.result ?? null, `${doc.name} · query`); }
    if (action === "cli") {
      if (query.lang !== "jmes") return JV.toast("Copy as CLI works with JMESPath (the AWS CLI's --query)", {tone: "warn"});
      const base = doc.labels?.command || "aws <service> <command>";
      const quoted = `'${query.text.replace(/'/g, `'\\''`)}'`;
      rememberQuery(doc);
      return JV.copy(`${base.replace(/\s+--query\s+('[^']*'|"[^"]*"|\S+)/, "")} --query ${quoted}`, {label: "Copied CLI command"});
    }
  });

  // ---------- Export ----------
  JV.openExport = () => {
    const doc = JV.active;
    if (!doc || doc.status !== "ready") return;
    const table = JV.viewOf(doc) === "table";
    const hasQuery = doc.query.text.trim() && !doc.query.error && doc.query.result !== undefined;
    JV.dialog(`<form class="dialog-form" id="export-form"><h2>Export</h2>
      <fieldset><legend>What</legend>
        <label class="check"><input type="radio" name="scope" value="table"${table ? " checked" : " disabled"}> The table as shown (filters, sort and visible columns)</label>
        <label class="check"><input type="radio" name="scope" value="node"${table ? "" : " checked"}> The current ${doc.focus.length ? "node" : "document"}${doc.focus.length ? ` (${esc(M.formatPath(doc.focus, "jmes"))})` : ""}</label>
        <label class="check"><input type="radio" name="scope" value="query"${hasQuery ? "" : " disabled"}> The query result</label>
        ${doc.selected ? `<label class="check"><input type="radio" name="scope" value="selected"> The selected node (${esc(M.formatPath(doc.selected, "jmes"))})</label>` : ""}
      </fieldset>
      <label class="field-label">Format <select id="export-format"><option value="csv">CSV</option><option value="xlsx">Excel (.xlsx)</option><option value="md">Markdown table</option><option value="json" selected>JSON (formatted)</option><option value="min">JSON (minified)</option><option value="yaml">YAML</option></select></label>
      <label class="check"><input type="checkbox" id="export-redact"${JV.settings.redactExports ? " checked" : ""}> Redact secrets (access keys, tokens, passwords, connection strings)</label>
      <p class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="submit" class="primary">Download</button></div></form>`);
    if (table) $("#export-format").value = "csv";
  };
  function download(data, name, type) {
    const url = URL.createObjectURL(data instanceof Blob ? data : new Blob([data], {type}));
    const link = Object.assign(document.createElement("a"), {href: url, download: name});
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  JV.download = download;
  // Header and rows (strings) for table formats; from the table, or from any value.
  function tabular(value) {
    const rows = Array.isArray(value) ? value : [value];
    if (rows.every(item => !M.isContainer(item))) return {header: ["value"], rows: rows.map(item => [M.cellText(item)])};
    const keys = M.columnKeys(rows);
    return {header: keys, rows: rows.map(item => keys.map(key => M.cellText(M.getField(item, key))))};
  }
  async function doExport() {
    const doc = JV.active;
    const scope = document.querySelector("[name=scope]:checked")?.value || "node";
    const format = $("#export-format").value;
    const redact = $("#export-redact").checked;
    const clean = value => redact && window.JVSecrets ? window.JVSecrets.redact(value, JV.settings.rules || []) : value;
    const base = doc.name.replace(/\.(json5?|jsonl|ndjson|txt)(\.gz)?$/i, "") + (scope === "query" ? "-query" : doc.focus.length ? "-" + JV.partText(JV.lastKey(doc.focus)).replace(/[^\w.-]+/g, "") : "");
    let header, rows, value;
    if (scope === "table") {
      const {state, source} = JV.tableState(doc);
      const columns = JV.orderedColumns(state);
      const list = doc.kind === "server" ? await JV.server.rowsFor(doc, null) : JV.tableView(state, source).map(index => source.rows[index]);
      const cleaned = list.map(row => ({...row, value: clean(row.value)}));
      header = columns.map(column => column.label);
      rows = cleaned.map(row => columns.map(column => M.cellText(JV.fieldOf(row, column.key))));
      value = cleaned.map(row => row.value);
    } else {
      value = clean(scope === "query" ? doc.query.result : scope === "selected" ? (doc.kind === "server" ? await JV.server.valueAt(doc, doc.selected) : M.getAt(doc.value, doc.selected)) : doc.kind === "server" ? await JV.server.valueAt(doc, doc.focus) : JV.focusValue(doc));
      ({header, rows} = tabular(value));
    }
    const X = window.JVExport;
    if (format === "csv") download(M.toCsv(header, rows), `${base}.csv`, "text/csv");
    else if (format === "xlsx") download(new Blob([X.toXlsx(header, rows, "Export")], {type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}), `${base}.xlsx`);
    else if (format === "md") download(X.toMarkdownTable(header, rows), `${base}.md`, "text/markdown");
    else if (format === "yaml") download(X.toYaml(value), `${base}.yaml`, "application/yaml");
    else download(JSON.stringify(value, null, format === "min" ? 0 : 2), `${base}${format === "min" ? ".min" : ""}.json`, "application/json");
    JV.closeDialog();
    JV.toast(`Exported${redact ? " (secrets redacted)" : ""}`, {value: `${base}.${format === "min" ? "min.json" : format}`});
  }

  // ---------- Merge paged files ----------
  // Pages of one output share their shape: both lists, or objects with the same keys.
  const sameShape = (a, b) => Array.isArray(a) ? Array.isArray(b) : M.isContainer(a) && M.isContainer(b) && !Array.isArray(b) && Object.keys(a).filter(key => !PAGING.has(key)).sort().join() === Object.keys(b).filter(key => !PAGING.has(key)).sort().join();
  JV.openMerge = doc => {
    const others = JV.docs.filter(item => item !== doc && item.status === "ready" && item.kind === "client");
    if (!others.length) return JV.toast("Open the other pages of this output first", {tone: "warn"});
    JV.dialog(`<div class="dialog-form"><h2>Merge pages</h2><p class="tool-note">Joins paged CLI output (NextToken pages, JSON Lines parts) into one document: lists with the same key are joined and paging keys dropped.</p>
      <div class="merge-list"><label class="check"><input type="checkbox" checked disabled> ${esc(doc.name)}</label>${others.map(item => `<label class="check"><input type="checkbox" data-merge="${item.id}"${sameShape(doc.value, item.value) ? " checked" : ""}> ${esc(item.name)}</label>`).join("")}</div>
      <p class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-close-dialog>Cancel</button><button type="button" class="primary" data-merge-go>Merge into a new tab</button></div></div>`);
    $("#app-dialog").dataset.doc = doc.id;
  };
  const PAGING = new Set(["NextToken", "nextToken", "Marker", "NextMarker", "IsTruncated", "nextLink", "@odata.nextLink", "ContinuationToken", "NextContinuationToken", "continue"]);
  JV.mergeValues = values => {
    if (values.every(Array.isArray)) return values.flat();
    if (values.every(value => M.isContainer(value) && !Array.isArray(value))) {
      const out = {};
      for (const value of values) {
        for (const [key, item] of Object.entries(value)) {
          if (PAGING.has(key)) continue;
          if (key === "metadata" && item && typeof item === "object" && "continue" in item) { out[key] = {...item, continue: undefined}; continue; }
          if (Array.isArray(item) && Array.isArray(out[key])) out[key] = out[key].concat(item);
          else if (!(key in out)) out[key] = item;
        }
      }
      return out;
    }
    throw Error("These documents have different shapes (a list and an object) and can't be merged.");
  };
  function doMerge() {
    const doc = JV.docs.find(item => item.id === Number($("#app-dialog").dataset.doc));
    const chosen = [doc, ...[...document.querySelectorAll("[data-merge]:checked")].map(box => JV.docs.find(item => item.id === Number(box.dataset.merge)))];
    try {
      const merged = JV.mergeValues(chosen.map(item => item.value));
      JV.closeDialog();
      JV.openValue(merged, `Merged (${chosen.length} files)`);
    } catch (error) { const target = $("#app-dialog .dialog-error"); target.textContent = error.message; target.hidden = false; }
  }

  // ---------- Command runner ----------
  let runJob = null, runner = null;
  JV.openRunner = async (preset = {}) => {
    try { runner = await JV.api("/runner"); } catch (error) { runner = {error: error.message, tools: {}, aws_profiles: [], aws_regions: [], kube_contexts: [], saved: []}; }
    const tools = Object.entries(runner.tools || {});
    const tool = preset.tool || tools.find(([, path]) => path)?.[0] || "aws";
    JV.dialog(`<form class="dialog-form" id="runner-form"><div class="dialog-head"><h2>Run a command</h2><button type="button" class="icon-button" data-close-dialog aria-label="Close">×</button></div>
      <p class="tool-note">Read-only commands only (describe, list, get, show). They run on this machine with your CLI profiles; output opens here and is kept as a snapshot.</p>
      ${runner.error ? `<p class="dialog-error">${esc(runner.error)}</p>` : ""}
      <div class="runner-line"><select id="run-tool" aria-label="Tool">${["aws", "az", "kubectl", "gcloud"].map(name => `<option value="${name}"${name === tool ? " selected" : ""}${runner.tools?.[name] ? "" : " disabled"}>${name}${runner.tools?.[name] ? "" : " (not installed)"}</option>`).join("")}</select>
        <input id="run-args" class="mono" required value="${esc(preset.args ? preset.args.join(" ") : "")}" placeholder="ec2 describe-instances" spellcheck="false" autocomplete="off" aria-label="Command arguments"></div>
      <div class="field-grid runner-fields">
        <label class="field-label" data-for="aws">Profile <select id="run-profile"><option value="">default</option>${(runner.aws_profiles || []).map(name => `<option${name === preset.profile ? " selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
        <label class="field-label" data-for="aws">Region <select id="run-region"><option value="">from profile</option>${(runner.aws_regions || []).map(name => `<option${name === preset.region ? " selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
        <label class="field-label" data-for="az">Subscription <input id="run-subscription" value="${esc(preset.subscription || "")}" placeholder="current"></label>
        <label class="field-label" data-for="kubectl">Context <select id="run-context"><option value="">current</option>${(runner.kube_contexts || []).map(name => `<option${name === preset.context ? " selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
        <label class="field-label" data-for="gcloud">Project <input id="run-project" value="${esc(preset.project || "")}" placeholder="current"></label>
        <label class="field-label">Timeout <span><input id="run-timeout" type="number" min="5" max="900" value="120"> s</span></label>
        <label class="field-label">Save as <input id="run-save" placeholder="optional name" value="${esc(preset.name || "")}"></label>
      </div>
      <div id="run-status" class="run-status" hidden></div>
      <p class="dialog-error" hidden></p>
      <div class="dialog-actions"><button type="button" data-run-cancel hidden>Cancel run</button><button type="submit" class="primary" id="run-go">Run</button></div>
      ${(runner.saved || []).length ? `<h3>Saved commands</h3><div class="history-list">${runner.saved.map(command => `<div class="history-row"><div><b>${esc(command.name)}</b><div class="tool-note mono">${esc([command.tool, ...(command.args || [])].join(" "))}${command.profile ? ` · ${esc(command.profile)}` : ""}${command.region ? ` · ${esc(command.region)}` : ""}</div></div><div class="row-actions"><button type="button" data-run-saved="${command.id}">Run</button><button type="button" data-delete-saved="${command.id}" aria-label="Delete ${esc(command.name)}">Delete</button></div></div>`).join("")}</div>` : ""}</form>`, {wide: true});
    toolFields();
    $("#run-args").focus();
  };
  function toolFields() {
    const tool = $("#run-tool")?.value;
    document.querySelectorAll("[data-for]").forEach(field => { field.hidden = field.dataset.for !== tool; });
    if ($("#run-args")) $("#run-args").placeholder = {aws: "ec2 describe-instances", az: "vm list", kubectl: "get pods -A", gcloud: "compute instances list"}[tool];
  }
  $("#app-dialog").addEventListener("change", event => { if (event.target.id === "run-tool") toolFields(); });
  async function startRun(preset) {
    const tool = preset?.tool || $("#run-tool").value;
    let args = preset?.args || JV.splitArgs($("#run-args").value.trim());
    if (args[0] === tool) args = args.slice(1);
    const body = preset || {tool, args, profile: $("#run-profile").value || undefined, region: $("#run-region").value || undefined, subscription: $("#run-subscription").value.trim() || undefined, context: $("#run-context").value || undefined, project: $("#run-project").value.trim() || undefined, timeout: Number($("#run-timeout").value) || 120, save_as: $("#run-save").value.trim() || undefined};
    const status = $("#run-status");
    const {job_id: id} = await JV.api("/run", {method: "POST", body});
    runJob = id;
    if (status) { status.hidden = false; status.textContent = "Running…"; }
    document.querySelector("[data-run-cancel]")?.removeAttribute("hidden");
    if ($("#run-go")) $("#run-go").disabled = true;
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 800));
      const job = await JV.api(`/run/${id}`);
      if ($("#run-status")) $("#run-status").innerHTML = `<b>${esc(job.state)}</b> · ${esc(job.command || "")} · ${Math.round(job.seconds || 0)} s${job.stderr ? `<pre>${esc(job.stderr.slice(-4000))}</pre>` : ""}`;
      if (job.state === "running") continue;
      runJob = null;
      if ($("#run-go")) $("#run-go").disabled = false;
      document.querySelector("[data-run-cancel]")?.setAttribute("hidden", "");
      if (job.state !== "done") throw Error(job.error || `The command ${job.state}.`);
      JV.closeDialog();
      const labels = {...(job.snapshot?.labels || {}), command: job.command};
      await JV.openSnapshot(job.snapshot_id, {labels});
      return;
    }
  }
  async function runSaved(id) {
    const command = runner?.saved?.find(item => item.id === id);
    if (!command) return;
    try { await startRun({tool: command.tool, args: command.args, profile: command.profile || undefined, region: command.region || undefined, ...(command.extra || {}), command_id: id}); }
    catch (error) { const target = $("#app-dialog .dialog-error"); if (target) { target.textContent = error.message; target.hidden = false; } else JV.toast(error.message, {tone: "warn"}); }
  }

  // ---------- Command palette ----------
  let paletteItems = [];
  function paletteActions() {
    const doc = JV.active;
    const ready = JV.screen === "doc" && doc?.status === "ready";
    const items = [
      {label: "Open files…", hint: "⌘O", action: JV.pickFiles},
      {label: "Paste JSON", hint: "⌘V", action: JV.pasteFromClipboard},
      {label: "Open a URL…", action: JV.openUrlDialog},
      {label: "Run a command…", action: () => JV.openRunner()},
      {label: "Compare two documents", action: () => JV.openDiff()},
      {label: "History and snapshots", action: JV.openHistory},
      {label: "Settings", action: JV.openSettings},
      {label: "Home", action: JV.showHome},
      {label: "Toggle light / dark theme", action: () => $("#theme-toggle").click()},
    ];
    if (ready) {
      items.push(
        ...["tree", "table", "card", "raw"].map(view => ({label: `View as ${view === "card" ? "cards" : view}`, action: () => { doc.view = view; JV.disposeRaw(); JV.render(); }})),
        {label: "Search keys and values", hint: "⌘F", action: () => $("#search-input").focus()},
        {label: "Go to path…", action: () => $("#goto-input").focus()},
        {label: "Expand all", action: () => { doc.view = "tree"; JV.render(); JV.expandAll(doc); }},
        {label: "Collapse all", action: () => { doc.view = "tree"; JV.render(); JV.collapseAll(doc); }},
        {label: "Export…", action: JV.openExport},
        {label: "Query with JMESPath", action: () => { doc.query.lang = "jmes"; JV.renderQueryBar(doc); $("#query-input")?.focus(); }},
        {label: "Query with jq", action: () => { doc.query.lang = "jq"; JV.renderQueryBar(doc); $("#query-input")?.focus(); }},
        {label: "Save as snapshot…", action: () => JV.saveSnapshot(doc)},
        {label: "Merge with other tabs…", action: () => JV.openMerge(doc)},
        {label: doc.revealAll ? "Hide secrets" : "Reveal all secrets", action: () => { doc.revealAll = !doc.revealAll; JV.render(); }},
        {label: "Close tab", action: () => JV.closeDoc(doc)},
      );
      if (doc.selected) items.push(
        {label: "Copy selected value", hint: "⌘C", action: () => JV.copySelected(doc)},
        {label: "Copy selected path (JMESPath)", hint: "⇧⌘C", action: () => JV.copy(M.formatPath(doc.selected, "jmes"), {label: "Copied path"})},
        {label: "Bookmark selected node", action: () => JV.toggleBookmark(doc, doc.selected)},
      );
      if (doc.source === "aws") items.push({label: "Show the relationship graph", action: () => { doc.view = "graph"; JV.render(); }});
    }
    for (const other of JV.docs) items.push({label: `Switch to ${other.name}`, hint: "tab", action: () => JV.activate(other)});
    return items;
  }
  JV.openPalette = () => {
    paletteItems = paletteActions();
    JV.dialog(`<form class="dialog-form palette" id="palette-form"><input id="palette-input" placeholder="Type a command" autocomplete="off" spellcheck="false" aria-label="Command">
      <div id="palette-list" class="palette-list" role="listbox">${paletteItems.map((item, index) => `<button type="button" role="option" data-palette="${index}"><span>${esc(item.label)}</span>${item.hint ? `<small>${esc(item.hint)}</small>` : ""}</button>`).join("")}</div></form>`);
    $("#palette-input").focus();
  };
  $("#app-dialog").addEventListener("input", event => {
    if (event.target.id !== "palette-input") return;
    const words = event.target.value.toLowerCase().split(/\s+/).filter(Boolean);
    document.querySelectorAll("#palette-list [data-palette]").forEach(button => { button.hidden = !words.every(word => button.textContent.toLowerCase().includes(word)); });
  });
  $("#app-dialog").addEventListener("keydown", event => {
    if (!$("#palette-list")) return;
    const buttons = [...document.querySelectorAll("#palette-list [data-palette]:not([hidden])")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); buttons[Math.min(buttons.length - 1, index + 1)]?.focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); (index <= 0 ? $("#palette-input") : buttons[index - 1]).focus(); }
  });

  // ---------- Compare (snapshot diff) ----------
  const IGNORE = "owl-json-diff-ignore";
  JV.diff = {before: null, after: null, filter: "all", ignore: JV.store.json(IGNORE, ["LaunchTime", "*.AttachTime", "UsageOperationUpdateTime"]), limit: 200};
  const sideOf = doc => ({name: doc.name, value: doc.value, labels: doc.labels || {}, meta: `${doc.template?.name || JV.SOURCE_LABEL[doc.source] || JV.FORMAT_LABEL[doc.detected] || "JSON"}${doc.unwrap ? ` · ${doc.unwrap.rows.length.toLocaleString()} ${doc.unwrap.label.toLowerCase()}` : ""}`, when: doc.snapshotId ? "snapshot" : "open tab"});
  JV.openDiff = (doc = JV.active) => {
    const ready = JV.docs.filter(item => item.status === "ready" && item.kind === "client");
    if (doc && doc.status === "ready" && doc.kind === "client") {
      JV.diff.after = sideOf(doc);
      const other = ready.filter(item => item !== doc).pop();
      JV.diff.before = other ? sideOf(other) : null;
    }
    JV.diff.result = null;
    JV.screen = "diff";
    JV.render();
  };
  function compute() {
    const {before, after} = JV.diff;
    if (!before || !after) return null;
    const D = window.JVDiff;
    const rowsA = C.unwrap(before.value), rowsB = C.unwrap(after.value);
    if (rowsA && rowsB) return {kind: "records", ...D.diffRecords(rowsA.rows.map(row => row.value), rowsB.rows.map(row => row.value), {ignore: JV.diff.ignore})};
    return {kind: "values", ...D.diffValues(before.value, after.value, {ignore: JV.diff.ignore})};
  }
  const show = value => value === undefined ? "—" : JSON.stringify(value);
  const sideLabel = side => side ? [side.labels.environment, side.labels.region, side.labels.account].filter(Boolean).join(" · ") || side.name : "";
  JV.renderDiff = () => {
    const state = JV.diff;
    if (!state.result && state.before && state.after) {
      try { state.result = compute(); state.error = ""; } catch (error) { state.error = error.message; }
    }
    const result = state.result;
    const slot = (key, title) => {
      const side = state[key];
      return `<button type="button" class="diff-slot" data-diff-slot="${key}" data-popover-anchor><span class="slot-title">${title}</span><span class="slot-name">${side ? esc(sideLabel(side)) : "Choose a document…"}</span><span class="tool-note">${side ? `${esc(side.name)} · ${esc(side.meta)}` : "An open tab, a snapshot or a file (or drop a file here)"}</span></button>`;
    };
    const title = state.after ? `What changed in ${esc(state.after.labels.environment ? `${state.after.labels.environment} ${JV.SOURCE_LABEL[C.detectSource(state.after.value)] || ""}`.trim() : state.after.name)}` : "Compare two documents";
    let body = "";
    if (state.error) body = `<p class="dialog-error">${esc(state.error)}</p>`;
    else if (!result) body = `<div class="state-card"><p>Choose a before and an after document. Resources are matched by their ID (InstanceId, ARN, Azure id), not by their position in the list.</p></div>`;
    else if (result.kind === "values") {
      body = `<article class="change"><div class="change-head"><span class="badge tone-warn">Changed</span><span class="change-name">${result.fields.length.toLocaleString()} field${result.fields.length === 1 ? "" : "s"}</span></div>${fieldTable(result.fields)}</article>`;
    } else {
      const summary = result.summary;
      const kinds = {all: null, added: "added", removed: "removed", changed: "changed"};
      const changes = result.changes.filter(change => !kinds[state.filter] || change.kind === kinds[state.filter]);
      body = `<div class="stats">
        <div class="stat"><div class="stat-label">Added</div><div class="stat-value good-text">+${summary.added.toLocaleString()}</div></div>
        <div class="stat"><div class="stat-label">Removed</div><div class="stat-value bad-text">−${summary.removed.toLocaleString()}</div></div>
        <div class="stat"><div class="stat-label">Changed</div><div class="stat-value warn-text">${summary.changed.toLocaleString()}</div></div>
        <div class="stat"><div class="stat-label">Unchanged</div><div class="stat-value muted">${summary.unchanged.toLocaleString()}</div></div></div>
        <div class="diff-controls"><span class="tool-note">Ignoring</span>${state.ignore.map(pattern => `<span class="chip-filter neutral"><span>${esc(pattern)}</span><button type="button" data-unignore="${esc(pattern)}" aria-label="Stop ignoring ${esc(pattern)}">×</button></span>`).join("")}<button type="button" class="chip-add" data-ignore-add data-popover-anchor>+ Ignore field</button>
        <span class="segmented small">${["all", "added", "removed", "changed"].map(name => `<button type="button" data-diff-filter="${name}" aria-pressed="${state.filter === name}">${name === "all" ? "All changes" : name[0].toUpperCase() + name.slice(1)}</button>`).join("")}</span></div>
        <div class="changes">${changes.slice(0, state.limit).map(change => `<article class="change"><div class="change-head"><span class="badge tone-${change.kind === "added" ? "good" : change.kind === "removed" ? "bad" : "warn"}">${change.kind[0].toUpperCase() + change.kind.slice(1)}</span><span class="change-name">${esc(change.name || change.id)}</span><span class="mono link-text">${esc(change.id)}</span><span class="change-sum">${change.kind === "changed" ? `${change.fields.length} field${change.fields.length === 1 ? "" : "s"}` : change.kind === "added" ? "new" : "gone"}</span></div>${fieldTable(change.fields)}</article>`).join("") || '<p class="tool-note">No changes of this kind.</p>'}
        ${changes.length > state.limit ? `<button type="button" class="more-cards" data-diff-more>Show more (${(changes.length - state.limit).toLocaleString()} left)</button>` : ""}</div>`;
    }
    $("#diff").innerHTML = `<div class="diff-page"><div class="diff-title"><h1>${title}</h1><p class="muted">${result?.kind === "records" ? `Resources matched by ${esc(result.idKey || "their ID")}, not by array position.` : "Field-by-field comparison."}</p></div>
      <div class="diff-slots">${slot("before", "Before")}<span class="arrow" aria-hidden="true">→</span>${slot("after", "After")}<button type="button" class="tool" data-diff-swap title="Swap before and after">⇄ Swap</button></div>${body}</div>`;
  };
  function fieldTable(fields) {
    if (!fields.length) return "";
    return `<div class="viewer-scroll"><table class="diff-table"><tbody>${fields.slice(0, 200).map(field => `<tr><td class="field-path">${esc(field.path)}</td><td>${field.before === undefined ? '<span class="muted">—</span>' : `<span class="old">${esc(show(field.before))}</span>`}</td><td>${field.after === undefined ? '<span class="muted">—</span>' : `<span class="new">${esc(show(field.after))}</span>`}</td></tr>`).join("")}</tbody></table></div>`;
  }
  async function pickSide(key, anchor) {
    const tabs = JV.docs.filter(item => item.status === "ready" && item.kind === "client");
    const snapshots = await JV.api("/snapshots").then(data => data.snapshots || []).catch(() => []);
    const rect = anchor.getBoundingClientRect();
    JV.openMenu(rect.left, rect.bottom + 4, [
      ...tabs.map(doc => ({label: doc.name, hint: "open tab", action: () => { JV.diff[key] = sideOf(doc); JV.diff.result = null; JV.renderDiff(); }})),
      ...(snapshots.length ? ["-"] : []),
      ...snapshots.slice(0, 20).map(snapshot => ({label: snapshot.name, hint: [snapshot.labels?.environment, new Date(snapshot.created_at * 1000).toLocaleString(undefined, {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"})].filter(Boolean).join(" · "), action: async () => {
        const text = await (await JV.api(`/snapshots/${encodeURIComponent(snapshot.id)}/content`, {raw: true})).text();
        const parsed = window.JVParse.parseDocument(text, {name: snapshot.name});
        const unwrap = C.unwrap(parsed.value);
        JV.diff[key] = {name: snapshot.name, value: parsed.value, labels: snapshot.labels || {}, meta: `${C.template(unwrap?.rows.map(row => row.value) || [])?.name || JV.SOURCE_LABEL[snapshot.source] || "JSON"} · ${new Date(snapshot.created_at * 1000).toLocaleString()}`};
        JV.diff.result = null;
        JV.renderDiff();
      }})),
      "-",
      {label: "Choose a file…", action: () => { const input = Object.assign(document.createElement("input"), {type: "file"}); input.onchange = () => JV.diffDrop(key, [...input.files]); input.click(); }},
    ]);
  }
  JV.diffDrop = async (key, files) => {
    try {
      const file = files[0];
      if (file.size > JV.settings.limitMb * 1024 * 1024) throw Error(`${file.name} is larger than ${JV.settings.limitMb} MB.`);
      let bytes = new Uint8Array(await file.arrayBuffer());
      if (bytes[0] === 0x1f && bytes[1] === 0x8b) bytes = new Uint8Array(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer());
      const parsed = window.JVParse.parseDocument(window.JVParse.decodeBytes(bytes).text, {name: file.name});
      const unwrap = C.unwrap(parsed.value);
      JV.diff[key] = {name: file.name, value: parsed.value, labels: {}, meta: C.template(unwrap?.rows.map(row => row.value) || [])?.name || JV.FORMAT_LABEL[parsed.format]};
      if (files[1] && key === "before") return JV.diffDrop("after", files.slice(1));
      JV.diff.result = null;
      JV.renderDiff();
    } catch (error) { JV.toast(`Could not read the file: ${error.message}`, {tone: "warn"}); }
  };
  JV.diffExportMenu = anchor => {
    const result = JV.diff.result;
    if (!result) return JV.toast("Choose two documents first", {tone: "warn"});
    const rect = anchor.getBoundingClientRect();
    const labels = {beforeLabel: sideLabel(JV.diff.before), afterLabel: sideLabel(JV.diff.after)};
    JV.openMenu(rect.left, rect.bottom + 4, [
      {label: "Export as Markdown", action: () => download(result.kind === "records" ? window.JVDiff.toMarkdown(result, labels) : `# Changes\n\n| Path | Before | After |\n| --- | --- | --- |\n${result.fields.map(field => `| ${field.path} | ${show(field.before)} | ${show(field.after)} |`).join("\n")}\n`, "diff.md", "text/markdown")},
      {label: "Export as JSON", action: () => download(JSON.stringify(result, null, 2), "diff.json", "application/json")},
    ]);
  };
  $("#diff").addEventListener("click", event => {
    const state = JV.diff;
    const slot = event.target.closest("[data-diff-slot]");
    if (slot) return pickSide(slot.dataset.diffSlot, slot);
    if (event.target.closest("[data-diff-swap]")) { [state.before, state.after] = [state.after, state.before]; state.result = null; return JV.renderDiff(); }
    const filter = event.target.closest("[data-diff-filter]");
    if (filter) { state.filter = filter.dataset.diffFilter; return JV.renderDiff(); }
    if (event.target.closest("[data-diff-more]")) { state.limit += 200; return JV.renderDiff(); }
    const unignore = event.target.closest("[data-unignore]");
    if (unignore) { state.ignore = state.ignore.filter(item => item !== unignore.dataset.unignore); JV.store.setJson(IGNORE, state.ignore); state.result = null; return JV.renderDiff(); }
    const add = event.target.closest("[data-ignore-add]");
    if (add) {
      JV.openPopover(add, `<form class="filter-pop" id="ignore-form"><label>Field to ignore <input id="ignore-input" placeholder="LaunchTime or *.AttachTime" spellcheck="false"></label><div class="dialog-actions"><button type="submit" class="primary">Ignore</button></div></form>`);
      $("#ignore-input").focus();
    }
  });
  $("#popover").addEventListener("submit", event => {
    if (event.target.id !== "ignore-form") return;
    event.preventDefault();
    const pattern = $("#ignore-input").value.trim();
    JV.closePopover();
    if (!pattern) return;
    JV.diff.ignore = [...new Set([...JV.diff.ignore, pattern])];
    JV.store.setJson(IGNORE, JV.diff.ignore);
    JV.diff.result = null;
    JV.renderDiff();
  });
})();
