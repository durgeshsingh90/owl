"use strict";
// Shared helpers for the Architecture Analyser (window.AA).
window.AA = (() => {
  const STATUSES = ["FAIL", "UNKNOWN", "WARNING", "PASS"];
  const MARKS = {PASS: "✓", FAIL: "✗", WARNING: "!", UNKNOWN: "?"};
  // Checks in the order traffic meets them.
  const CATEGORY_ORDER = ["Endpoint", "DNS", "Routing", "TGW", "Firewall", "NAT", "Internet", "Peering", "VPN", "Direct Connect", "PrivateLink", "Load Balancer",
    "Security Group", "NACL", "Return Path", "External", "Standards"];

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }

  async function api(path, options = {}) {
    const init = {...options, headers: {"Content-Type": "application/json", ...(options.headers || {})}};
    if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
    const response = await fetch("/api/architecture" + path, init);
    let data = null;
    try {
      data = await response.json();
    } catch {
      /* Not JSON. */
    }
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Request failed (${response.status})`);
    }
    return data;
  }

  function status(value, label) {
    return `<span class="status ${esc(value)}">${MARKS[value] || ""} ${esc(label || value)}</span>`;
  }

  function mark(value) {
    return `<span class="mark ${esc(value)}" title="${esc(value)}">${MARKS[value] || "·"}</span>`;
  }

  function worst(values) {
    const list = [...values];
    return STATUSES.find((s) => list.includes(s)) || "PASS";
  }

  function flowLabel(requirement) {
    let protocol = (requirement.protocol || "TCP").toUpperCase();
    let port = requirement.port;
    if (protocol === "HTTPS") [protocol, port] = ["TCP", port || 443];
    if (protocol === "HTTP") [protocol, port] = ["TCP", port || 80];
    return protocol + (port != null ? "/" + port : "");
  }

  function title(text) {
    return String(text || "").replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function when(seconds) {
    if (!seconds) return "";
    return new Date(seconds * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
  }

  let toastTimer;
  function toast(message) {
    const node = document.querySelector("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.hidden = true), 3200);
  }

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem("owl-architecture-" + key);
      localStorage.setItem("owl-architecture-" + key, value);
    } catch {
      /* Storage may be unavailable. */
    }
    return null;
  }

  function meter(counts) {
    const total = counts.total || 0;
    if (!total) return `<div class="meter" style="background:var(--raised)"></div>`;
    const part = (n, cls) => (n ? `<span class="${cls}" style="width:${(100 * n) / total}%"></span>` : "");
    return `<div class="meter">${part(counts.pass, "p")}${part(counts.warning, "w")}${part(counts.unknown, "u")}</div>`;
  }

  return {STATUSES, CATEGORY_ORDER, esc, api, status, mark, worst, flowLabel, title, when, toast, store, meter};
})();
