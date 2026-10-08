"use strict";
// Shared helpers for the JSON Visualizer page: escaping, storage, clipboard and the
// copy toast, dates, search highlighting, typed value rendering (with secret masking),
// menus, popovers and the local API. Everything hangs off one global, JV.
(() => {
  const JV = window.JV = window.JV || {};
  const M = JV.M = window.JVModel, C = JV.C = window.JVCloud;
  const $ = JV.$ = selector => document.querySelector(selector);
  const esc = JV.esc = value => String(value).replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[char]);
  JV.store = {
    get(key, fallback) { try { const value = localStorage.getItem(key); return value === null ? fallback : value; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage may be unavailable */ } },
    json(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
    setJson(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage may be full */ } },
  };
  JV.settings = Object.assign({limitMb: 50, timeZone: "local", redactExports: true, maskSecrets: true, rules: []}, JV.store.json("owl-json-settings", {}));
  JV.saveSettings = () => JV.store.setJson("owl-json-settings", JV.settings);

  // ---------- Paths ----------
  // A path id is built from its parts, so a child's id is its parent's plus one part.
  JV.childId = (parentId, part) => parentId + (typeof part === "number" ? "\u0001#" + part : "\u0001s" + part);
  JV.pathId = path => path.reduce(JV.childId, "");
  JV.samePath = (a, b) => Boolean(a && b) && a.length === b.length && a.every((part, index) => part === b[index]);
  JV.startsWith = (path, prefix) => prefix.length <= path.length && prefix.every((part, index) => part === path[index]);
  JV.lastKey = path => path.length ? path[path.length - 1] : undefined;
  JV.partText = part => typeof part === "number" ? `[${part}]` : String(part);

  // ---------- Toast and clipboard ----------
  let toastTimer;
  const CHECK = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#7EE2A8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>`;
  JV.toast = (message, {value = "", tone = "", html = false} = {}) => {
    const element = $("#toast");
    const short = String(value).length > 70 ? String(value).slice(0, 69).replace(/\s+/g, " ") + "…" : String(value).replace(/\s+/g, " ");
    element.innerHTML = `${tone === "warn" ? "" : CHECK}<span>${html ? message : esc(message)}</span>${value !== "" ? `<span class="toast-value">${esc(short)}</span>` : ""}`;
    element.className = `toast ${tone}`;
    element.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { element.hidden = true; }, 2600);
  };
  JV.copy = async (text, {label = "Copied", key} = {}) => {
    if (text.length > 10 * 1024 * 1024 && !confirm(`Copy ${M.formatBytes(text.length)} of text to the clipboard?`)) return false;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement("textarea"), {value: text});
      document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    }
    JV.toast(key !== undefined ? `${esc(label)} <b>${esc(JV.partText(key))}</b>` : esc(label), {value: text || "(empty)", html: true});
    return true;
  };
  JV.copyValue = (value, key) => JV.copy(M.copyText(value), {label: key === undefined ? "Copied value" : "Copied", key});

  // ---------- Dates ----------
  JV.formatDate = text => {
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    if (JV.settings.timeZone === "utc") return date.toISOString().replace("T", " ").replace(/\.\d+Z$|Z$/, "") + " UTC";
    const pad = number => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };
  JV.dateTitle = text => {
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return `${text}\nLocal: ${date.toLocaleString()}\nUTC: ${date.toISOString()}\n${M.relativeTime(date.getTime())}`;
  };

  // ---------- Search highlighting ----------
  let highlight = null;
  JV.setHighlight = doc => {
    highlight = null;
    const search = doc?.search;
    if (!search?.query || search.error) return;
    try {
      let source = search.options.regex ? search.query : window.JVSearch.escapeRegex(search.query);
      if (search.options.wholeWord) source = `\\b(?:${source})\\b`;
      highlight = new RegExp(source, search.options.caseSensitive ? "g" : "gi");
    } catch { highlight = null; }
  };
  const marked = JV.marked = text => {
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
  };

  // ---------- Secrets ----------
  // A detected secret shows masked until revealed (one value, or all in a document).
  JV.secretOf = (key, value) => {
    if (!JV.settings.maskSecrets || typeof value !== "string" || !window.JVSecrets) return null;
    try { return window.JVSecrets.detect(key === undefined ? "" : String(key), value, JV.settings.rules || []); } catch { return null; }
  };
  JV.isRevealed = id => {
    const doc = JV.active;
    return Boolean(doc && (doc.revealAll || (id !== undefined && doc.revealed?.has(id))));
  };

  // A resource's display name, masked when it is itself a secret (an access key id, say).
  JV.safeName = record => {
    const name = C.displayName(record);
    return name && JV.secretOf("", String(name)) ? window.JVSecrets.mask(String(name)) : name;
  };

  // ---------- Value rendering ----------
  JV.tagChips = (list, max = 6) => {
    const chips = list.slice(0, max).map(tag => `<span class="tag"><span class="tag-key">${marked(tag.key)}</span>${tag.value === "" ? "" : ` ${marked(M.cellText(tag.value))}`}</span>`).join("");
    return chips + (list.length > max ? `<span class="tag more">+${list.length - max}</span>` : "");
  };
  // The status of a resource: State.Name, status, provisioningState…
  JV.statusOf = record => {
    if (!record || typeof record !== "object" || Array.isArray(record)) return null;
    const candidates = [["State", record.State?.Name ?? record.State], ["status", record.status?.phase ?? record.status], ["Status", record.Status], ["provisioningState", record.provisioningState ?? record.properties?.provisioningState], ["powerState", record.powerState], ["phase", record.phase]];
    for (const [key, value] of candidates) {
      const tone = C.statusTone(key, value);
      if (tone) return {tone, value};
    }
    return null;
  };
  // One value inline: typed colour, status badge, dates, tag chips, container summary.
  // opts.id is the value's path id, used to remember revealed secrets.
  JV.valueHtml = (key, value, {limit = 300, id} = {}) => {
    const type = M.typeOf(value);
    if (type === "string") {
      const secret = JV.secretOf(key, value);
      if (secret && !JV.isRevealed(id)) return `<span class="secret" data-reveal="${esc(id ?? "")}" title="${esc(secret.label)} hidden · click to reveal">${esc(window.JVSecrets.mask(value))}</span>`;
      const tone = C.statusTone(key, value);
      if (tone) return `<span class="badge tone-${tone}">${marked(value)}</span>`;
      const kind = M.stringKind(value);
      if (kind === "date") {
        return `<span class="v t-date" title="${esc(JV.dateTitle(value))}">${marked(JV.formatDate(value))}<small> · ${esc(M.relativeTime(Date.parse(value)))}</small></span>`;
      }
      const text = value.length > limit ? value.slice(0, limit) + "…" : value;
      const embedded = value.length > 1 && /^\s*[[{]/.test(value) && /[\]}]\s*$/.test(value);
      return `<span class="v t-string k-${kind}"${value.length > limit ? ` title="${esc(M.formatBytes(value.length))} string; select it to see all"` : ""}>${marked(text)}</span>${embedded ? `<span class="embedded" title="This string holds JSON; select it to expand">JSON</span>` : ""}`;
    }
    if (type === "array") {
      if (C.isTagList(value)) return `<span class="tags">${JV.tagChips(C.tags({Tags: value}))}</span>`;
      if (value.length && value.length <= 8 && value.every(item => !M.isContainer(item))) {
        return `<span class="v t-meta">[ ${value.map(item => marked(M.cellText(item))).join(", ")} ]</span>`;
      }
      return `<span class="v t-meta">[ ${value.length.toLocaleString()} item${value.length === 1 ? "" : "s"} ]</span>`;
    }
    if (type === "object") {
      const count = Object.keys(value).length, name = JV.safeName(value), tags = C.tags(value);
      const status = JV.statusOf(value);
      return `<span class="v t-meta">{ ${count.toLocaleString()} key${count === 1 ? "" : "s"} }</span>${name ? ` <span class="v-name">${marked(name)}</span>` : ""}${status ? ` <span class="badge tone-${status.tone}">${esc(status.value)}</span>` : ""}${tags.length && !name ? ` <span class="tags">${JV.tagChips(tags, 3)}</span>` : ""}`;
    }
    if (type === "number") return `<span class="v t-number">${marked(String(value))}</span>`;
    if (type === "boolean") return `<span class="v t-boolean">${value}</span>`;
    return `<span class="v t-null">null</span>`;
  };

  // ---------- References (paths behind rendered elements) ----------
  JV.refs = {card: new Map(), detail: new Map(), graph: new Map(), diff: new Map()};
  let refSequence = 0;
  JV.ref = (region, path, value) => {
    const id = `${region}:${++refSequence}`;
    JV.refs[region].set(id, {path, value});
    return id;
  };

  // ---------- Menus, popovers, dialogs ----------
  JV.openMenu = (x, y, items) => {
    const menu = $("#menu");
    menu.innerHTML = items.map((item, index) => item === "-" ? `<span class="menu-sep" role="separator"></span>` : `<button type="button" role="menuitem" data-menu="${index}"${item.disabled ? " disabled" : ""}${item.primary ? ' class="primary-item"' : ""}><span>${esc(item.label)}</span>${item.hint ? `<small>${esc(item.hint)}</small>` : ""}</button>`).join("");
    menu.hidden = false;
    menu.items = items;
    const box = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(x, innerWidth - box.width - 8))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, innerHeight - box.height - 8))}px`;
    menu.querySelector("button:not(:disabled)")?.focus();
  };
  JV.closeMenu = () => { $("#menu").hidden = true; };
  JV.openPopover = (anchor, html) => {
    const popover = $("#popover");
    popover.innerHTML = html;
    popover.hidden = false;
    const box = anchor.getBoundingClientRect();
    popover.style.left = `${Math.max(8, Math.min(box.left, innerWidth - popover.offsetWidth - 8))}px`;
    popover.style.top = `${Math.min(box.bottom + 6, innerHeight - popover.offsetHeight - 8)}px`;
    return popover;
  };
  JV.closePopover = () => { $("#popover").hidden = true; };
  // A dialog filled with html; returns the <dialog>.
  JV.dialog = (html, {wide = false} = {}) => {
    const dialog = $("#app-dialog");
    dialog.className = wide ? "wide" : "";
    dialog.innerHTML = html;
    if (!dialog.open) dialog.showModal();
    return dialog;
  };
  JV.closeDialog = () => { if ($("#app-dialog").open) $("#app-dialog").close(); };

  // ---------- Local API ----------
  JV.api = async (path, {method = "GET", body, raw = false, headers = {}, signal} = {}) => {
    const options = {method, headers: {...headers}, signal};
    if (body !== undefined) {
      if (body instanceof Blob || body instanceof ArrayBuffer || typeof body === "string" && headers["Content-Type"]) options.body = body;
      else { options.body = JSON.stringify(body); options.headers["Content-Type"] = "application/json"; }
    }
    const response = await fetch(`/api/json-visualizer${path}`, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data); } catch { /* not JSON */ }
      if (response.status === 404 && /Not Found/.test(detail)) detail = "This needs the latest OWL backend: restart OWL (python dev.py restart).";
      throw Error(detail);
    }
    return raw ? response : response.json();
  };

  // Split a command line into arguments the way a shell would (quotes, escapes), without running a shell.
  JV.splitArgs = text => {
    const args = [];
    let current = "", quote = null, has = false;
    for (let index = 0; index < text.length; index++) {
      const char = text[index];
      if (quote) {
        if (char === quote) quote = null;
        else if (char === "\\" && quote === "\"" && index + 1 < text.length) current += text[++index];
        else current += char;
      } else if (char === "'" || char === "\"") { quote = char; has = true; }
      else if (char === "\\" && index + 1 < text.length) { current += text[++index]; has = true; }
      else if (/\s/.test(char)) { if (has || current) args.push(current); current = ""; has = false; }
      else { current += char; has = true; }
    }
    if (quote) throw Error("A quote is not closed.");
    if (has || current) args.push(current);
    return args;
  };

  JV.icon = {
    logo: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H6a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h2"></path><path d="M16 3h2a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-2"></path></svg>`,
    copy: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"></rect><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"></path></svg>`,
    search: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>`,
    upload: `<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V3"></path><path d="m7 8 5-5 5 5"></path><path d="M20 15v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4"></path></svg>`,
    star: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3L3 9.6l6.2-.9L12 3Z"></path></svg>`,
  };

  JV.SOURCE_LABEL = {aws: "AWS", azure: "Azure", gcp: "GCP", kubectl: "Kubernetes", terraform: "Terraform", unknown: ""};
  JV.FORMAT_LABEL = {json: "JSON", jsonl: "JSONL", json5: "JSON5"};
  // "AWS · EC2" style label from the source and the table layout that matched.
  JV.sourceBadge = (source, detail = "") => {
    const label = JV.SOURCE_LABEL[source];
    if (!label) return "";
    return `<span class="source source-${source}">${esc(label)}${detail ? ` · ${esc(detail)}` : ""}</span>`;
  };
})();
