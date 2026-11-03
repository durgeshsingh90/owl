"use strict";
// AWS Inventory: reads what scripts/aws_inventory.py downloaded (its inventory.db) through
// /api/aws-inventory. Pages are hash routes: #/ overview, #/search?q=…, #/resource/<id>,
// #/accounts[/<profile>], #/changes?run=…, #/sync, #/compare[/<report>[/<tab>]],
// #/projects[/<report>[/<tab>]], #/settings.
(() => {
  const $ = selector => document.querySelector(selector);
  const view = $("#view");
  const API = "/api/aws-inventory";

  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const number = value => Number(value || 0).toLocaleString();
  const plural = (count, word, many) => `${number(count)} ${count === 1 ? word : many || word + "s"}`;
  const duration = seconds => {
    if (seconds == null) return "—";
    const s = Math.round(seconds);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`;
  };
  const clock = seconds => {
    const s = Math.max(0, Math.round(seconds || 0));
    return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(n => String(n).padStart(2, "0")).join(":");
  };
  const bytes = value => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let n = Number(value || 0), i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
  };
  // A run id (20261103T041200Z) or an ISO time, as "Nov 3 04:12".
  const when = value => {
    if (!value) return "never";
    const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/.exec(value);
    const date = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])) : new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  };

  async function get(path) {
    const response = await fetch(API + path, {cache: "no-store"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  // A change: POST/PUT/DELETE with a JSON body; the backend's message on failure.
  async function send(path, method = "POST", body = undefined) {
    const response = await fetch(API + path, {method, headers: {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `${response.status} ${response.statusText}`);
    return data;
  }

  // The sync page polls while a sync runs; any other page stops it.
  let timer = null;
  const stopPolling = () => { clearTimeout(timer); timer = null; };

  function notReady(data) {
    return `<div class="empty"><strong>${esc(data.message)}</strong>
      <span>Inventory folder: <code>${esc(data.folder)}</code></span>
      <span>From the owl folder: <code>python scripts/aws_inventory.py</code></span></div>`;
  }

  function failed(error) {
    view.innerHTML = `<div class="error" role="alert">Could not load this page: ${esc(error.message)}. Check that OWL's backend is running.</div>`;
  }

  const statusChip = status => {
    const kind = {ok: "ok", timeout: "bad", error: "bad", "never synced": "plain"}[status] || "warn";
    const text = {ok: "OK", timeout: "Timeout", error: "Failed", "never synced": "Never synced"}[status] || status || "?";
    return `<span class="chip ${kind}">${esc(text)}</span>`;
  };
  const changeText = c => c && (c.added || c.modified || c.removed) ? `+${c.added || 0} ~${c.modified || 0} −${c.removed || 0}` : "—";

  // ------------------------------------------------------------------ overview

  async function overview() {
    const data = await get("/overview");
    if (!data.ready) {
      view.innerHTML = `<header class="head"><div><p class="eyebrow">AWS Inventory</p><h1>Overview</h1></div></header>${notReady(data)}`;
      return;
    }
    $("#rail-database").textContent = `inventory.db · ${bytes(data.database_bytes)}`;
    const t = data.totals, run = data.last_run;
    const tiles = [
      ["Accounts ready", `${number(t.ready)} / ${number(t.accounts)}`, t.accounts - t.ready ? `${number(t.accounts - t.ready)} not ready or never synced` : "all signed in"],
      ["Resources", number(t.resources), `in ${plural(t.regions, "account region")}`],
      ["Last run", run ? clock(run.seconds) : "—", run ? `+${number(run.added)} added · ~${number(run.modified)} modified · −${number(run.removed)} removed` : "no sync recorded yet"],
      ["No permission", number(t.no_permission), `types or detail calls in ${plural(t.no_permission_accounts, "account")}`],
      ["Coverage gaps", plural(t.coverage_gaps, "account"), t.coverage_gaps ? `AWS lists ${number(t.coverage_missed)} resources we lack` : "nothing missing found"],
    ];
    const issueChip = {"sign-in": "bad", coverage: "warn", permission: ""};
    view.innerHTML = `
      <header class="head">
        <div><p class="eyebrow">AWS Inventory</p><h1>Overview</h1>
          <p>${run ? `Last sync ${run.stopped ? "stopped" : "finished"} ${esc(when(run.finished || run.run))}` : "No sync recorded yet"} · ${plural(t.accounts, "account")} · ${number(t.resources)} resources · inventory.db ${bytes(data.database_bytes)}</p></div>
        <div class="actions"><a class="button primary" href="#/search">Search the inventory</a><a class="button" href="#/sync">Sync</a></div>
      </header>
      <section class="tiles" aria-label="Totals">${tiles.map(([label, value, detail]) =>
        `<div class="tile"><span>${esc(label)}</span><strong class="num">${esc(value)}</strong><small>${esc(detail)}</small></div>`).join("")}</section>
      <div class="grid">
        <section class="card">
          <div class="card-head"><h2>Last run</h2>${run ? `<span class="chip ${run.stopped ? "warn" : "ok"}">${run.stopped ? "Stopped" : "Done"}</span>` : ""}</div>
          ${run ? `<p style="margin:0">${run.stopped ? "Stopped" : "Done"} in <strong>${clock(run.seconds)}</strong>: ${plural(run.accounts, "account")},
            +${number(run.added)} ~${number(run.modified)} −${number(run.removed)}, ${plural(run.errors, "error")}${run.not_ready ? `, ${number(run.not_ready)} not ready` : ""}.</p>
            <p class="muted" style="margin:0;font-size:13px">Skipped ${number(run.skipped_empty)} types that were empty before (until the weekly full sweep) and ${number(run.skipped_denied)} for no permission.</p>`
            : `<p class="muted" style="margin:0">Run <code class="mono">python scripts/aws_inventory.py</code> to sync.</p>`}
          <h2 style="margin-top:6px">Recent changes</h2>
          ${data.recent_changes.length ? data.recent_changes.map(c => `
            <${c.pk ? `a class="link-row" href="#/resource/${c.pk}"` : `div class="link-row"`}>
              <span class="chip ${c.action === "added" ? "ok" : c.action === "removed" ? "bad" : ""}">${esc(c.action)}</span>
              <strong>${esc(c.name || c.resource_id)}</strong><span class="muted">${esc(c.type)} · ${esc(c.profile || c.account_id)} · ${esc(when(c.run))}</span>
            </${c.pk ? "a" : "div"}>`).join("") : `<p class="muted" style="margin:0">No changes recorded yet.</p>`}
        </section>
        <section class="card">
          <h2>Needs a look</h2>
          ${data.issues.length ? data.issues.map(i => `
            <div class="issue"><span class="chip ${issueChip[i.kind] ?? ""}">${esc(i.kind)}</span>
              <div><strong>${esc(i.title)}</strong><small>${esc(i.detail || "")}</small></div></div>`).join("")
            : `<p class="muted" style="margin:0">Nothing needs a look: every account signed in and nothing is missing.</p>`}
        </section>
      </div>
      <section class="card">
        <div class="card-head"><h2>Saved accounts</h2>
          <label class="filter"><span class="sr">Filter accounts</span><input id="account-filter" type="search" placeholder="Filter by name or number"></label></div>
        <div class="table-box"><table>
          <thead><tr><th>Account</th><th>Profile</th><th>Regions</th><th>Sign-in</th><th>Last sync</th><th>Average</th><th>Resources</th><th>No permission</th><th>Last changes</th></tr></thead>
          <tbody id="account-rows">${data.accounts.map(a => `
            <tr data-text="${esc(`${a.account_id} ${a.profile} ${a.alias || ""}`.toLowerCase())}">
              <td class="num"><strong>${esc(a.account_id || "?")}</strong></td>
              <td><a href="#/accounts/${encodeURIComponent(a.profile)}">${esc(a.profile)}</a>${a.enabled ? "" : ` <span class="chip plain">not synced</span>`}${a.retry ? ` <span class="chip warn">retry flagged</span>` : ""}</td>
              <td class="muted">${esc(Array.isArray(a.regions) ? a.regions.join(", ") : a.regions)}</td>
              <td>${statusChip(a.status)}</td>
              <td class="muted num">${esc(when(a.last_sync))}</td>
              <td class="num">${duration(a.average_seconds)}</td>
              <td class="num">${number(a.resources)}</td>
              <td class="num">${a.no_permission ? number(a.no_permission) : "—"}</td>
              <td class="num">${changeText(a.changes)}</td>
            </tr>`).join("")}</tbody>
        </table></div>
      </section>`;
    $("#account-filter").addEventListener("input", event => {
      const text = event.target.value.trim().toLowerCase();
      document.querySelectorAll("#account-rows tr").forEach(row => { row.hidden = Boolean(text) && !row.dataset.text.includes(text); });
    });
  }

  // ------------------------------------------------------------------ search

  let filterCache = null;

  function highlight(text, terms) {
    let html = esc(text);
    for (const term of terms) {
      if (!term) continue;
      const pattern = new RegExp(esc(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
      html = html.replace(pattern, match => `<mark>${match}</mark>`);
    }
    return html;
  }

  async function search(params) {
    const q = params.get("q") || "";
    const chosen = {account: params.get("account") || "", type: params.get("type") || "", region: params.get("region") || ""};
    filterCache = filterCache || await get("/filters");
    if (!filterCache.ready) {
      view.innerHTML = `<header class="head"><div><h1>Search</h1></div></header>${notReady(filterCache)}`;
      return;
    }
    const option = (value, label, selected) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`;
    view.innerHTML = `
      <header class="head"><div><h1>Search</h1>
        <p>Every account, region and resource: names, ids, ARNs, IPs, CIDRs, tags, DNS names and nslookup answers, policy text.</p></div></header>
      <form class="card search-form" role="search" id="search-form">
        <div class="search-row">
          <label class="search-box"><span class="sr">Search the inventory</span>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>
            <input id="search-q" name="q" type="search" value="${esc(q)}" placeholder="10.20.1.15, sg-0a1…, api.example.sep, dev_ghdjegd" autocomplete="off"></label>
          <button class="button primary" type="submit">Search</button>
        </div>
        <div class="narrow">
          <label>Account <select name="account">${option("", `All ${filterCache.accounts.length} accounts`, chosen.account)}${filterCache.accounts.map(a => option(a.account_id, `${a.profile || a.account_id} (${a.account_id})`, chosen.account)).join("")}</select></label>
          <label>Type <select name="type">${option("", "All types", chosen.type)}${[...new Set(filterCache.types.map(t => t.type.split("/")[0]))].map(s => option(s, `${s} (all)`, chosen.type)).join("")}${filterCache.types.map(t => option(t.type, `${t.type} (${number(t.count)})`, chosen.type)).join("")}</select></label>
          <label>Region <select name="region">${option("", "All regions", chosen.region)}${filterCache.regions.map(r => option(r, r, chosen.region)).join("")}</select></label>
          <span class="try">Try: ${["0.0.0.0/0", "sg-", ".sep", "eks"].map(s => `<button type="button" data-try="${esc(s)}">${esc(s)}</button>`).join(" · ")}</span>
        </div>
      </form>
      <div id="search-results"></div>`;
    const form = $("#search-form");
    form.addEventListener("submit", event => {
      event.preventDefault();
      const next = new URLSearchParams();
      for (const [key, value] of new FormData(form)) if (String(value).trim()) next.set(key, String(value).trim());
      location.hash = `#/search?${next}`;
    });
    form.querySelectorAll("select").forEach(select => select.addEventListener("change", () => form.requestSubmit()));
    form.querySelectorAll("[data-try]").forEach(button => button.addEventListener("click", () => {
      $("#search-q").value = button.dataset.try;
      form.requestSubmit();
    }));
    if (!q) {
      $("#search-q").focus();
      return;
    }
    const box = $("#search-results");
    box.innerHTML = `<p class="muted">Searching…</p>`;
    const data = await get(`/search?${new URLSearchParams({q, ...chosen, limit: "100"})}`);
    const terms = q.split(/\s+/).filter(Boolean);
    const count = (key, label) => {
      const map = new Map();
      for (const hit of data.hits) map.set(hit[key], {label: label(hit), count: (map.get(hit[key])?.count || 0) + 1});
      return [...map].sort((a, b) => b[1].count - a[1].count);
    };
    const facet = (title, param, rows) => `<section class="card facet"><h2 style="font-size:14px">${esc(title)}</h2>${rows.map(([value, r]) =>
      `<button type="button" data-facet="${esc(param)}" data-value="${esc(value)}"><span>${esc(r.label)}</span><span class="muted num">${number(r.count)}</span></button>`).join("")}</section>`;
    const accounts = new Set(data.hits.map(h => h.account_id));
    box.innerHTML = data.hits.length ? `
      <div class="results">
        <section aria-label="Results" style="display:flex;flex-direction:column;gap:12px;min-width:0">
          <p class="muted" style="margin:0"><strong style="color:var(--text)">${plural(data.hits.length, "match", "matches")}</strong> in ${plural(accounts.size, "account")} · best first${data.hits.length >= data.limit ? " · showing the first 100, narrow it to see more" : ""}</p>
          ${data.hits.map(hit => `
            <a class="hit" href="#/resource/${hit.id}">
              <span class="hit-top"><span class="chip">${esc(hit.type)}</span><strong>${esc(hit.name || hit.resource_id)}</strong>
                <span class="muted" style="font-size:13px">${esc(hit.profile || hit.account_id)} (${esc(hit.account_id)}) · ${esc(hit.region)}</span></span>
              <span class="mono muted">id: ${esc(hit.resource_id)}</span>
              ${hit.where.map(w => {
                const [path, ...rest] = w.split(" = ");
                return `<span class="where">${esc(path)} = ${highlight(rest.join(" = "), terms)}</span>`;
              }).join("")}
            </a>`).join("")}
        </section>
        <aside aria-label="Narrow the results" style="display:flex;flex-direction:column;gap:16px;min-width:0">
          ${facet("By account", "account", count("account_id", h => h.profile || h.account_id))}
          ${facet("By type", "type", count("type", h => h.type))}
        </aside>
      </div>` : `<div class="empty"><strong>Nothing found for “${esc(q)}”.</strong><span>Search matches any part of a value; try fewer or shorter words, or clear the filters.</span></div>`;
    box.querySelectorAll("[data-facet]").forEach(button => button.addEventListener("click", () => {
      const next = new URLSearchParams(params);
      next.set(button.dataset.facet, button.dataset.value);
      location.hash = `#/search?${next}`;
    }));
  }

  // ------------------------------------------------------------------ resource

  async function resource(id, tab) {
    const data = await get(`/resources/${encodeURIComponent(id)}`);
    if (!data.ready) {
      view.innerHTML = notReady(data);
      return;
    }
    const r = data.resource, d = data.data || {};
    const rules = data.rules.length ? data.rules : [];
    const network = d._network || {};
    const tabs = [
      ["rules", `Rules${rules.length ? ` (${rules.length})` : ""}`, rules.length || network.inbound || network.subnets],
      ["links", `Uses and used by (${data.uses.length + data.used_by.length})`, true],
      ["dns", `DNS records (${data.dns.length})`, data.dns.length],
      ["json", "Full JSON", true],
      ["history", `History (${data.history.length})`, true],
    ].filter(t => t[2]);
    const current = tabs.some(t => t[0] === tab) ? tab : tabs[0][0];
    const ruleTable = (title, rows) => `
      <section class="card"><h2>${esc(title)}</h2>${rows.length ? `<div class="table-box"><table>
        <thead><tr>${rows[0].rule_number != null ? "<th>Rule</th><th>Action</th>" : ""}<th>Protocol</th><th>Ports</th><th>From / to</th><th>Note</th></tr></thead>
        <tbody>${rows.map(x => `<tr>${rows[0].rule_number != null ? `<td class="num">${esc(x.rule_number)}</td><td>${x.action === "deny" ? `<span class="chip bad">deny</span>` : `<span class="chip ok">allow</span>`}</td>` : ""}
          <td>${esc(x.protocol)}</td><td class="num">${esc(x.ports)}</td><td class="mono">${esc(x.peer || x.security_group || "")}</td><td class="muted">${esc(x.description || x.peer_type || "")}</td></tr>`).join("")}</tbody>
      </table></div>` : `<p class="muted" style="margin:0">None.</p>`}</section>`;
    const effective = (title, rows) => rows && rows.length ? ruleTable(title, rows.map(x => ({...x, description: `via ${x.security_group}`}))) : "";
    let body = "";
    if (current === "rules") {
      body = rules.length
        ? `<div class="grid">${ruleTable("Inbound", rules.filter(x => x.direction === "inbound"))}${ruleTable("Outbound", rules.filter(x => x.direction === "outbound"))}</div>`
        : `<div class="grid">${effective("Inbound (its security groups)", network.inbound)}${effective("Outbound (its security groups)", network.outbound)}</div>`;
      if (network.subnets && network.subnets.length) {
        body += `<section class="card"><h2>Subnets, network ACLs and routes</h2>${network.subnets.map(s => `
          <div class="link-row" style="flex-direction:column;align-items:flex-start">
            <strong>${esc(s.id)} <span class="muted">${esc(s.cidr || "")}</span></strong>
            <span class="muted">Network ACL ${esc((s.network_acl || {}).id || "unknown")} · route table ${esc((s.route_table || {}).id || "unknown")}</span>
            <span class="mono">${((s.route_table || {}).routes || []).map(rt => esc(`${rt.DestinationCidrBlock || rt.DestinationPrefixListId || "?"} → ${rt.GatewayId || rt.NatGatewayId || rt.TransitGatewayId || rt.VpcEndpointId || rt.VpcPeeringConnectionId || rt.NetworkInterfaceId || "?"}`)).join("<br>")}</span>
          </div>`).join("")}</section>`;
      }
    } else if (current === "links") {
      const row = x => `<${x.pk ? `a class="link-row" href="#/resource/${x.pk}"` : `div class="link-row"`}>
        <span class="chip">${esc(x.type || x.kind)}</span><strong>${esc(x.name || x.target_id || x.resource_id)}</strong>
        <span class="muted mono">${esc(x.target_id || x.resource_id || "")}</span></${x.pk ? "a" : "div"}>`;
      body = `<div class="grid">
        <section class="card"><h2>Uses (${data.uses.length})</h2>${data.uses.map(row).join("") || `<p class="muted" style="margin:0">No security groups, subnets or VPC recorded.</p>`}</section>
        <section class="card"><h2>Used by (${data.used_by.length})</h2>${data.used_by.map(row).join("") || `<p class="muted" style="margin:0">Nothing in this account points to it.</p>`}</section>
      </div>`;
    } else if (current === "dns") {
      body = `<section class="card"><div class="table-box"><table>
        <thead><tr><th>Name</th><th>Type</th><th>Value or alias</th><th>nslookup</th><th>Answers</th></tr></thead>
        <tbody>${data.dns.map(x => `<tr><td class="mono">${esc(String(x.name || "").replace(/\\052/g, "*"))}</td><td>${esc(x.type)}</td>
          <td class="mono">${esc(x.alias_target || x.record_values.join(", "))}</td>
          <td>${x.lookup_status ? `<span class="chip ${x.lookup_status === "ok" ? "ok" : "warn"}">${esc(x.lookup_status)}</span>` : "—"}</td>
          <td class="mono">${esc(x.lookup_addresses.join(", "))}</td></tr>`).join("")}</tbody></table></div></section>`;
    } else if (current === "json") {
      body = `<pre class="json">${esc(JSON.stringify(d, null, 2))}</pre>`;
    } else {
      body = `<section class="card"><h2>History</h2>${data.history.map(h => `
        <div class="link-row"><span class="chip ${h.action === "added" ? "ok" : h.action === "removed" ? "bad" : ""}">${esc(h.action)}</span>
          <strong class="num">${esc(when(h.run))}</strong><span class="muted mono">${esc(h.run)}</span></div>`).join("")
        || `<p class="muted" style="margin:0">No change recorded since it was first seen.</p>`}</section>`;
    }
    view.innerHTML = `
      <a href="javascript:history.back()" style="font-size:13px;font-weight:600">← Back</a>
      <header class="head">
        <div style="display:flex;flex-direction:column;gap:8px">
          <span class="chip" style="align-self:flex-start">${esc(r.type)}</span>
          <h1>${esc(r.name || r.resource_id)}</h1>
          <p class="mono" style="margin:0">${esc(r.resource_id)}${r.vpc_id ? ` · ${esc(r.vpc_id)}` : ""}</p>
          <p style="margin:0">${esc(r.profile || r.account_id)} (${esc(r.account_id)}) · ${esc(r.region)} · changed ${esc(when(r.last_changed))} · last seen ${esc(when(r.last_seen))}</p>
        </div>
        <div class="actions">
          <button class="button" type="button" id="copy-id">Copy ID</button>
          <button class="button" type="button" id="download-json">Download JSON</button>
        </div>
      </header>
      <div class="tabs" role="tablist" aria-label="Resource views">${tabs.map(([key, label]) =>
        `<button type="button" role="tab" aria-selected="${key === current}" data-tab="${key}">${esc(label)}</button>`).join("")}</div>
      ${body}`;
    view.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => {
      location.hash = `#/resource/${id}/${button.dataset.tab}`;
    }));
    $("#copy-id").addEventListener("click", async event => {
      try {
        await navigator.clipboard.writeText(r.resource_id);
        event.target.textContent = "Copied";
      } catch {
        event.target.textContent = "Copy failed";
      }
      setTimeout(() => { event.target.textContent = "Copy ID"; }, 1500);
    });
    $("#download-json").addEventListener("click", () => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], {type: "application/json"}));
      link.download = `${r.type.replace("/", "-")}-${String(r.resource_id).replace(/[^\w.-]+/g, "_")}.json`;
      link.click();
      URL.revokeObjectURL(link.href);
    });
  }

  // ------------------------------------------------------------------ accounts & permissions

  const ACCOUNT_VIEWS = {
    all: ["All", () => true],
    "not-ready": ["Not ready", a => a.status !== "ok" && a.enabled],
    denied: ["No permission", a => a.no_permission > 0],
    retry: ["Retry flagged", a => a.retry],
    off: ["Left out", a => !a.enabled],
  };

  async function accounts(params) {
    const data = await get("/overview");
    if (!data.ready) {
      view.innerHTML = `<header class="head"><div><h1>Accounts &amp; permissions</h1></div></header>${notReady(data)}`;
      return;
    }
    const shown = ACCOUNT_VIEWS[params.get("show")] ? params.get("show") : "all";
    const rows = data.accounts.filter(ACCOUNT_VIEWS[shown][1]);
    const t = data.totals;
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Run</p><h1>Accounts &amp; permissions</h1>
        <p>Every saved account: its regions, sign-in, speed and what it has no permission for. Open one to flag its permissions for a retry.</p></div>
        <div class="actions"><a class="button primary" href="#/sync">Sync</a></div></header>
      <section class="tiles" aria-label="Totals">
        <div class="tile"><span>Saved accounts</span><strong class="num">${number(t.accounts)}</strong><small>${number(data.accounts.filter(a => !a.enabled).length)} left out of the syncs</small></div>
        <div class="tile"><span>Signed in</span><strong class="num">${number(t.ready)}</strong><small>${number(data.accounts.filter(ACCOUNT_VIEWS["not-ready"][1]).length)} not ready or never synced</small></div>
        <div class="tile"><span>No permission</span><strong class="num">${number(t.no_permission)}</strong><small>types or detail calls in ${plural(t.no_permission_accounts, "account")}</small></div>
        <div class="tile"><span>Flagged for a retry</span><strong class="num">${number(data.accounts.filter(a => a.retry).length)}</strong><small>tried again on the next sync</small></div>
      </section>
      <section class="card">
        <div class="card-head">
          <div class="pills" role="group" aria-label="Show">${Object.entries(ACCOUNT_VIEWS).map(([key, [label, test]]) =>
            `<a class="pill" href="#/accounts${key === "all" ? "" : `?show=${key}`}"${key === shown ? ` aria-current="true"` : ""}>${esc(label)} <span class="num">${number(data.accounts.filter(test).length)}</span></a>`).join("")}</div>
          <label class="filter"><span class="sr">Filter accounts</span><input id="account-filter" type="search" placeholder="Filter by name or number"></label>
        </div>
        ${rows.length ? `<div class="table-box"><table>
          <thead><tr><th>Profile</th><th>Account</th><th>Regions</th><th>Sign-in</th><th>Last sync</th><th>Average</th><th>Resources</th><th>No permission</th></tr></thead>
          <tbody id="account-rows">${rows.map(a => `
            <tr data-text="${esc(`${a.account_id} ${a.profile} ${a.alias || ""}`.toLowerCase())}">
              <td><a href="#/accounts/${encodeURIComponent(a.profile)}"><strong>${esc(a.profile)}</strong></a>${a.enabled ? "" : ` <span class="chip plain">left out</span>`}${a.retry ? ` <span class="chip warn">retry flagged</span>` : ""}</td>
              <td class="num">${esc(a.account_id || "?")}</td>
              <td class="muted">${esc(Array.isArray(a.regions) ? a.regions.join(", ") : a.regions)}</td>
              <td>${statusChip(a.status)}</td>
              <td class="muted num">${esc(when(a.last_sync))}</td>
              <td class="num">${duration(a.average_seconds)}</td>
              <td class="num">${number(a.resources)}</td>
              <td class="num">${a.no_permission ? `<span class="chip warn">${number(a.no_permission)}</span>` : "—"}</td>
            </tr>`).join("")}</tbody></table></div>`
          : `<p class="muted" style="margin:0">No account is ${esc(ACCOUNT_VIEWS[shown][0].toLowerCase())}.</p>`}
      </section>`;
    $("#account-filter").addEventListener("input", event => {
      const text = event.target.value.trim().toLowerCase();
      document.querySelectorAll("#account-rows tr").forEach(row => { row.hidden = Boolean(text) && !row.dataset.text.includes(text); });
    });
  }

  async function accountPage(name) {
    const a = await get(`/accounts/${encodeURIComponent(name)}`);
    const regions = Array.isArray(a.regions) ? a.regions.join(", ") : a.regions;
    const resources = a.by_region.reduce((sum, r) => sum + r.count, 0);
    const retryText = !a.retry ? "" : a.retry.includes("*") ? "everything it had no permission for" : a.retry.join(", ");
    const cov = a.coverage;
    const missed = cov ? Object.entries(cov.missed_types || {}) : [];
    view.innerHTML = `
      <a href="#/accounts" style="font-size:13px;font-weight:600">← Accounts</a>
      <header class="head">
        <div style="display:flex;flex-direction:column;gap:8px">
          <h1>${esc(a.profile)}</h1>
          <p style="margin:0"><span class="num">${esc(a.account_id || "account number not known yet")}</span>${a.alias ? ` · ${esc(a.alias)}` : ""} · ${esc(regions)}
            ${a.synced ? statusChip(a.connectivity || "ok") : statusChip("never synced")}${a.enabled ? "" : ` <span class="chip plain">left out of the syncs</span>`}</p>
        </div>
        <div class="actions">
          <a class="button primary" href="#/sync?profile=${encodeURIComponent(a.profile)}">Sync this account</a>
          ${a.account_id ? `<a class="button" href="#/search?account=${encodeURIComponent(a.account_id)}">Search it</a>` : ""}
          <button class="button" type="button" id="toggle-enabled">${a.enabled ? "Leave out of syncs" : "Sync it again"}</button>
        </div>
      </header>
      <p id="account-message" class="notice" hidden></p>
      <section class="tiles" aria-label="Totals">
        <div class="tile"><span>Resources</span><strong class="num">${number(resources)}</strong><small>in ${plural(a.by_region.length, "region")}</small></div>
        <div class="tile"><span>Last sync</span><strong class="num">${esc(when(a.last_sync))}</strong><small>took ${duration(a.last_seconds)}${a.last_full_sweep ? ` · full sweep ${esc(when(a.last_full_sweep))}` : ""}</small></div>
        <div class="tile"><span>Average</span><strong class="num">${duration(a.average_seconds)}</strong><small>over the last ${plural(a.durations.length, "sync")}</small></div>
        <div class="tile"><span>No permission</span><strong class="num">${number(a.no_permission.length)}</strong><small>${a.retry ? "flagged for a retry" : "skipped on each sync"}</small></div>
      </section>
      <section class="card">
        <div class="card-head"><h2>No permission</h2>
          <div class="actions">${a.retry ? `<button class="button" type="button" id="clear-retry">Clear the retry flag</button>` : ""}
            ${a.no_permission.length ? `<button class="button" type="button" id="retry-chosen" disabled>Retry the chosen ones</button><button class="button primary" type="button" id="retry-all">Retry all on the next sync</button>` : ""}</div></div>
        ${a.retry ? `<div class="issue"><span class="chip warn">retry flagged</span><div><strong>The next sync tries ${esc(retryText)} again.</strong><small>What is still denied is remembered again; the flag clears itself after that sync.</small></div></div>` : ""}
        ${a.no_permission.length ? `<p class="muted" style="margin:0;font-size:13px">Syncs skip these, so a missing permission costs no time. Got the permission? Flag them and the next sync tries them again.</p>
          <div class="table-box"><table>
          <thead><tr><th><span class="sr">Choose</span></th><th>Type</th><th title="— means the list command itself">Detail call</th><th>Regions</th><th>Error</th><th>Last tried</th></tr></thead>
          <tbody>${a.no_permission.map(d => `<tr>
            <td><input type="checkbox" class="pick-type" value="${esc(d.type)}" aria-label="Retry ${esc(d.type)}"></td>
            <td class="mono">${esc(d.type)}</td><td class="mono">${esc(d.detail || "—")}</td>
            <td class="muted">${esc(d.regions.join(", "))}</td><td class="mono muted">${esc(d.error || "")}</td><td class="muted num">${esc(when(d.last_run))}</td></tr>`).join("")}</tbody>
          </table></div>` : `<p class="muted" style="margin:0">${a.synced ? "Nothing: this account had permission for every type and detail call." : "Not synced yet."}</p>`}
      </section>
      <div class="grid">
        <section class="card"><h2>Coverage</h2>
          ${!cov ? `<p class="muted" style="margin:0">Not checked yet.</p>`
            : !cov.checked ? `<p class="muted" style="margin:0">Not checked: no access to the Tagging API or AWS Config in this account.</p>`
            : missed.length ? `<p style="margin:0">AWS lists <strong>${number(cov.missed)}</strong> resource(s) the inventory lacks:</p>
                ${missed.map(([type, m]) => `<div class="link-row"><span class="chip warn">${number(m.count)}</span><span class="mono">${esc(type)}</span></div>`).join("")}`
            : `<p style="margin:0"><span class="chip ok">Complete</span> Everything AWS lists for this account is in the inventory.</p>`}
          ${cov && (cov.suggested_commands || []).length ? `<p class="muted" style="margin:0;font-size:13px">Not in aws_commands.json yet: ${esc(cov.suggested_commands.join(", "))}</p>` : ""}
        </section>
        <section class="card"><h2>Resources by region</h2>
          ${a.by_region.map(r => `<div class="link-row"><strong>${esc(r.region)}</strong><span class="muted num" style="margin-left:auto">${number(r.count)}</span></div>`).join("") || `<p class="muted" style="margin:0">None yet.</p>`}
          ${a.by_type.length ? `<h2 style="margin-top:6px">Most common types</h2>${a.by_type.slice(0, 8).map(r => `<div class="link-row"><span class="mono">${esc(r.type)}</span><span class="muted num" style="margin-left:auto">${number(r.count)}</span></div>`).join("")}` : ""}
        </section>
      </div>
      <section class="card"><div class="card-head"><h2>Recent changes</h2>${a.account_id ? `<a href="#/changes?account=${encodeURIComponent(a.account_id)}">All changes in this account</a>` : ""}</div>
        ${a.changes.map(changeRow).join("") || `<p class="muted" style="margin:0">No changes recorded yet.</p>`}</section>`;

    const message = (text, bad) => {
      const box = $("#account-message");
      box.hidden = false;
      box.className = bad ? "error" : "notice";
      box.textContent = text;
    };
    const act = async (button, work) => {
      button.disabled = true;
      try {
        const result = await work();
        await accountPage(name);
        if (result && result.message) message(result.message);
      } catch (error) {
        message(error.message, true);
        button.disabled = false;
      }
    };
    $("#toggle-enabled").addEventListener("click", event => act(event.target, () => send(`/accounts/${encodeURIComponent(a.profile)}/enabled`, "PUT", {enabled: !a.enabled})
      .then(() => ({message: a.enabled ? "Left out: the syncs skip this account until you sync it again. It stays saved." : "This account is in the syncs again."}))));
    const all = $("#retry-all"), chosen = $("#retry-chosen"), clear = $("#clear-retry");
    if (all) all.addEventListener("click", () => act(all, () => send(`/accounts/${encodeURIComponent(a.profile)}/retry`, "POST", {types: []})));
    if (clear) clear.addEventListener("click", () => act(clear, () => send(`/accounts/${encodeURIComponent(a.profile)}/retry`, "DELETE")));
    if (chosen) {
      const picks = () => [...new Set([...view.querySelectorAll(".pick-type:checked")].map(box => box.value))];
      view.querySelectorAll(".pick-type").forEach(box => box.addEventListener("change", () => {
        chosen.disabled = !picks().length;
        chosen.textContent = picks().length ? `Retry ${plural(picks().length, "type")}` : "Retry the chosen ones";
      }));
      chosen.addEventListener("click", () => act(chosen, () => send(`/accounts/${encodeURIComponent(a.profile)}/retry`, "POST", {types: picks()})));
    }
  }

  // ------------------------------------------------------------------ changes

  const actionChip = action => `<span class="chip ${action === "added" ? "ok" : action === "removed" ? "bad" : ""}">${esc(action)}</span>`;
  const changeRow = c => `
    <${c.pk ? `a class="link-row" href="#/resource/${c.pk}"` : `div class="link-row"`}>
      ${actionChip(c.action)}<strong>${esc(c.name || c.resource_id)}</strong>
      <span class="muted">${esc(c.type)} · ${esc(c.region)}${c.profile ? ` · ${esc(c.profile)}` : ""} · ${esc(when(c.run))}</span>
    </${c.pk ? "a" : "div"}>`;

  async function changes(params) {
    const data = await get(`/changes?${params}`);
    if (!data.ready) {
      view.innerHTML = `<header class="head"><div><h1>Changes</h1></div></header>${notReady(data)}`;
      return;
    }
    const link = changesFor => {
      const next = new URLSearchParams(params);
      for (const [key, value] of Object.entries(changesFor)) value ? next.set(key, value) : next.delete(key);
      if (!("offset" in changesFor)) next.delete("offset");
      return `#/changes?${next}`;
    };
    const run = data.runs.find(r => r.run === data.run) || {};
    const counts = data.counts, all = (counts.added || 0) + (counts.modified || 0) + (counts.removed || 0);
    const action = params.get("action") || "";
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Explore</p><h1>Changes</h1>
        <p>What each sync added, modified or removed. Open a modified one to see the fields that changed.</p></div></header>
      ${!data.runs.length ? `<div class="empty"><strong>No changes recorded yet.</strong><span>The first sync adds everything; later syncs show what changed.</span></div>` : `
      <form class="card search-form" id="changes-form">
        <div class="narrow">
          <label>Sync <select name="run">${data.runs.map(r => `<option value="${esc(r.run)}"${r.run === data.run ? " selected" : ""}>${esc(when(r.run))}${r.added != null ? ` · +${number(r.added)} ~${number(r.modified)} −${number(r.removed)}` : ""}${r.stopped ? " · stopped" : ""}</option>`).join("")}</select></label>
          <label>Account <select name="account"><option value="">All accounts</option>${data.accounts.map(a => `<option value="${esc(a.account_id)}"${a.account_id === params.get("account") ? " selected" : ""}>${esc(a.profile || a.account_id)} (${number(a.count)})</option>`).join("")}
            ${params.get("account") && !data.accounts.some(a => a.account_id === params.get("account")) ? `<option value="${esc(params.get("account"))}" selected>${esc(params.get("account"))} (0)</option>` : ""}</select></label>
          <label class="filter"><span class="sr">Filter by id, name or type</span><input name="q" type="search" value="${esc(params.get("q") || "")}" placeholder="id, name or type"></label>
          <input type="hidden" name="action" value="${esc(action)}">
        </div>
      </form>
      <section class="tiles" aria-label="This sync">
        <div class="tile"><span>Sync</span><strong class="num">${esc(when(data.run))}</strong><small>${run.seconds != null ? `${run.stopped ? "stopped" : "took"} ${clock(run.seconds)} · ${plural(run.accounts || 0, "account")}` : esc(data.run)}</small></div>
        ${[["added", "Added", "ok"], ["modified", "Modified", ""], ["removed", "Removed", "bad"]].map(([key, label]) =>
          `<div class="tile"><span>${label}</span><strong class="num">${number(counts[key])}</strong><small>${params.get("account") || params.get("q") ? "matching the filter" : "in every account"}</small></div>`).join("")}
      </section>
      <section class="card">
        <div class="card-head">
          <div class="pills" role="group" aria-label="Action">${[["", "All", all], ["added", "Added", counts.added], ["modified", "Modified", counts.modified], ["removed", "Removed", counts.removed]].map(([key, label, n]) =>
            `<a class="pill" href="${link({action: key})}"${key === action ? ` aria-current="true"` : ""}>${label} <span class="num">${number(n)}</span></a>`).join("")}</div>
          <span class="muted num">${data.total ? `${number(data.offset + 1)}–${number(data.offset + data.changes.length)} of ${number(data.total)}` : ""}</span>
        </div>
        ${data.changes.length ? `<div class="table-box"><table>
          <thead><tr><th>Change</th><th>Resource</th><th>Type</th><th>Account</th><th>Region</th><th><span class="sr">Fields</span></th></tr></thead>
          <tbody>${data.changes.map((c, i) => `<tr>
            <td>${actionChip(c.action)}</td>
            <td>${c.pk ? `<a href="#/resource/${c.pk}"><strong>${esc(c.name || c.resource_id)}</strong></a>` : `<strong>${esc(c.name || c.resource_id)}</strong>`}${c.name && c.name !== c.resource_id ? `<br><span class="mono muted">${esc(c.resource_id)}</span>` : ""}</td>
            <td class="mono">${esc(c.type)}</td><td>${esc(c.profile || c.account_id)}</td><td class="muted">${esc(c.region)}</td>
            <td>${c.action === "modified" ? `<button class="link-button" type="button" data-diff="${i}" aria-expanded="false">What changed</button>` : ""}</td>
          </tr><tr class="diff-row" id="diff-${i}" hidden><td colspan="6"></td></tr>`).join("")}</tbody></table></div>
          <div class="actions">${data.offset ? `<a class="button" href="${link({offset: String(Math.max(0, data.offset - data.limit))})}">Previous</a>` : ""}
            ${data.offset + data.changes.length < data.total ? `<a class="button" href="${link({offset: String(data.offset + data.limit)})}">Next ${number(Math.min(data.limit, data.total - data.offset - data.changes.length))}</a>` : ""}</div>`
          : `<p class="muted" style="margin:0">No ${action || "changes"} ${params.get("account") || params.get("q") ? "matching the filter " : ""}in this sync.</p>`}
      </section>`}`;
    const form = $("#changes-form");
    if (!form) return;
    form.addEventListener("submit", event => {
      event.preventDefault();
      const next = new URLSearchParams();
      for (const [key, value] of new FormData(form)) if (String(value).trim()) next.set(key, String(value).trim());
      location.hash = `#/changes?${next}`;
    });
    form.querySelectorAll("select").forEach(select => select.addEventListener("change", () => form.requestSubmit()));
    view.querySelectorAll("[data-diff]").forEach(button => button.addEventListener("click", async () => {
      const row = $(`#diff-${button.dataset.diff}`), c = data.changes[+button.dataset.diff];
      const open = row.hidden;
      row.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      if (!open || row.dataset.loaded) return;
      const cell = row.firstElementChild;
      cell.innerHTML = `<span class="muted">Loading…</span>`;
      try {
        const d = await get(`/changes/diff?${new URLSearchParams({run: data.run, account: c.account_id, region: c.region, type: c.type, id: c.resource_id})}`);
        const value = v => v === undefined ? "—" : esc(JSON.stringify(v));
        cell.innerHTML = d.diff.length ? `<table class="diff"><thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead><tbody>${d.diff.map(x =>
          `<tr><td class="mono">${esc(x.path)} <span class="chip ${x.op === "added" ? "ok" : x.op === "removed" ? "bad" : "plain"}">${esc(x.op)}</span></td>
            <td class="mono was">${value(x.old)}</td><td class="mono now">${value(x.new)}</td></tr>`).join("")}</tbody></table>`
          : `<span class="muted">No field changes were recorded for it.</span>`;
        row.dataset.loaded = "1";
      } catch (error) {
        cell.innerHTML = `<span class="muted">${esc(error.message)}</span>`;
      }
    }));
  }

  // ------------------------------------------------------------------ sync

  const PHASES = {"sign-in": "Checking sign-in", sync: "Listing and saving", database: "Writing the database",
    done: "Done", stopped: "Stopped", failed: "Failed", ended: "Ended"};

  function bar(label, done, total, note) {
    const share = total ? Math.min(1, done / total) : 0;
    return `<div class="bar-row">
      <div class="bar-label"><strong>${esc(label)}</strong><span class="muted num">${note}</span></div>
      <div class="bar" role="progressbar" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(share * 100)}"><span style="width:${(share * 100).toFixed(1)}%"></span></div>
    </div>`;
  }

  function running(data) {
    const s = data.status, stopping = s.stopping || s.phase === "stopping";
    const elapsed = s.elapsed != null && s.phase === "sync" ? s.elapsed : (Date.now() - Date.parse(s.started || s.updated || Date.now())) / 1000;
    const L = s.list || {}, S = s.save || {}, D = s.database || null, A = s.accounts || {}, W = s.workers || {};
    const eta = v => v == null ? "estimating" : clock(v);
    const pct = (done, total) => total ? `${Math.floor(Math.min(1, done / total) * 100)}%` : "0%";
    return `
      <header class="head"><div><p class="eyebrow">Run</p><h1>Sync</h1>
        <p><span class="chip ${stopping ? "warn" : ""}">${stopping ? "Stopping" : esc(PHASES[s.phase] || s.phase)}</span>
          ${s.phase === "sign-in" ? ` Checking that ${plural(A.total || 0, "account")} can sign in, all at once.` : ""}
          ${s.follow_up ? " Downloading the types AWS lists that aws_commands.json lacked (just added)." : ""}
          ${stopping ? " Finished accounts are kept; accounts in progress stop now and keep their finished regions." : ""}</p></div>
        <div class="actions"><button class="button ${stopping ? "danger" : ""}" type="button" id="stop-sync">${stopping ? "Quit at once" : "Stop"}</button></div></header>
      <section class="card bars">
        ${bar("List", L.done || 0, L.total || 0, `${number(L.done)}/${number(L.total)} ${pct(L.done, L.total)} · ETA ${L.total ? eta(L.eta) : "—"}`)}
        ${bar("Save", S.done || 0, S.total || 0, `${number(S.done)}/${S.estimated ? "~" : ""}${number(S.total)} ${pct(S.done, S.total)} · ETA ${S.total ? eta(S.eta) : "—"}`)}
        ${D || s.phase === "database" ? bar("Database", (D || {}).done || 0, (D || {}).total || 0, `${number((D || {}).done)}/${number((D || {}).total)} · accounts ${number((D || {}).accounts_done)}/${number((D || {}).accounts)} · ETA ${eta((D || {}).eta)}`) : ""}
      </section>
      <section class="tiles" aria-label="Progress">
        <div class="tile"><span>Elapsed</span><strong class="num">${clock(elapsed)}</strong><small>started ${esc(when(s.started))}</small></div>
        <div class="tile"><span>Total ETA</span><strong class="num">${s.phase === "sync" ? eta(s.eta) : "—"}</strong><small>from the last commands' pace</small></div>
        <div class="tile"><span>Accounts</span><strong class="num">${number(A.done)} / ${number(A.total)}</strong><small>${number(A.running)} running now</small></div>
        <div class="tile"><span>AWS workers</span><strong class="num">${W.limit ? `${number(W.active)} / ${number(W.limit)}` : "—"}</strong><small>more when the PC is free, fewer when busy</small></div>
      </section>
      ${(s.current || []).length ? `<section class="card"><h2>Running now</h2><div class="chips">${s.current.map(p => `<span class="chip">${esc(p)}</span>`).join("")}</div></section>` : ""}
      ${output(data)}`;
  }

  function output(data) {
    if (!data.started_here) {
      return data.running ? `<p class="muted">Started in a terminal (process ${esc(data.status.pid)}); its output is there. Stop works here too.</p>` : "";
    }
    return `<section class="card"><h2>Output</h2><pre class="json log" id="sync-log">${esc(data.log.join("\n")) || "Starting…"}</pre></section>`;
  }

  function idle(data, params) {
    const s = data.status || {};
    const preset = params.getAll("profile").filter(p => data.accounts.some(a => a.profile === p));
    const enabled = data.accounts.filter(a => a.enabled);
    const ended = ["done", "stopped", "failed", "ended"].includes(s.phase);
    return `
      <header class="head"><div><p class="eyebrow">Run</p><h1>Sync</h1>
        <p>Downloads what is new or changed in the saved accounts, then writes the database. The same as <code class="mono">python scripts/aws_inventory.py</code> in a terminal.</p></div></header>
      ${ended ? `<section class="card"><div class="card-head"><h2>Last sync</h2><span class="chip ${s.phase === "done" ? "ok" : s.phase === "stopped" ? "warn" : "bad"}">${esc(PHASES[s.phase])}</span></div>
        ${(s.earlier || []).map(e => `<p style="margin:0">${esc(e.message || "")}${e.run ? ` <a href="#/changes?run=${encodeURIComponent(e.run)}">Its changes</a>` : ""}</p>`).join("")}
        ${s.follow_up ? `<p class="muted" style="margin:0;font-size:13px">Then the types AWS lists that aws_commands.json lacked were added and downloaded:</p>` : ""}
        <p style="margin:0">${esc(s.message || (s.phase === "done" ? "Finished." : ""))}</p>
        <p class="muted" style="margin:0;font-size:13px">${s.finished ? `Ended ${esc(when(s.finished))}` : ""}${(s.not_ready || []).length ? ` · could not sign in: ${esc(s.not_ready.join(", "))}` : ""}${s.phase === "stopped" ? " · the database is caught up on the next sync" : ""}</p>
        ${s.run && s.phase !== "failed" ? `<div class="actions"><a class="button" href="#/changes?run=${encodeURIComponent(s.run)}">See its changes</a><a class="button" href="#/">Overview</a></div>` : ""}
      </section>` : ""}
      ${!data.accounts.length ? `<div class="empty"><strong>No saved accounts yet.</strong><span>Add them once in a terminal; it asks for the account numbers and regions:</span><code>python scripts/aws_inventory.py</code></div>` : `
      <form class="card" id="sync-form">
        <h2>Start a sync</h2>
        <fieldset class="choice"><legend class="sr">Which accounts</legend>
          <label><input type="radio" name="which" value="all"${preset.length ? "" : " checked"}> All ${plural(enabled.length, "account")}${enabled.length < data.accounts.length ? ` <span class="muted">(${number(data.accounts.length - enabled.length)} left out)</span>` : ""}</label>
          <label><input type="radio" name="which" value="some"${preset.length ? " checked" : ""}> Only the ones I choose</label>
        </fieldset>
        <div id="pick" ${preset.length ? "" : "hidden"}>
          <div class="card-head"><label class="filter"><span class="sr">Filter accounts</span><input id="pick-filter" type="search" placeholder="Filter by name or number"></label>
            <span class="muted" id="pick-count"></span></div>
          <div class="pick-list">${data.accounts.map(a => `<label data-text="${esc(`${a.profile} ${a.account_id || ""}`.toLowerCase())}">
            <input type="checkbox" name="profile" value="${esc(a.profile)}"${preset.includes(a.profile) ? " checked" : ""}>
            <span><strong>${esc(a.profile)}</strong> <span class="muted num">${esc(a.account_id || "")}</span>${a.enabled ? "" : ` <span class="chip plain">left out</span>`}${a.retry ? ` <span class="chip warn">retry flagged</span>` : ""}</span>
            <span class="muted num" style="margin-left:auto">${duration(a.average_seconds)}</span></label>`).join("")}</div>
        </div>
        <fieldset class="choice"><legend class="sr">Options</legend>
          <label><input type="checkbox" name="full"> Repeat every detail call, also for unchanged resources <span class="muted">(slower)</span></label>
          <label><input type="checkbox" name="sweep"> List every type, also those empty before <span class="muted">(the weekly full sweep, now; no-permission ones wait for a retry flag)</span></label>
        </fieldset>
        <p class="muted" style="margin:0;font-size:13px">Accounts that cannot sign in are skipped and shown at the end. Types AWS lists that aws_commands.json lacks are added and downloaded, as in the terminal with --yes.</p>
        <p id="sync-error" class="error" hidden></p>
        <div class="actions"><button class="button primary" type="submit" id="start-sync">Start sync</button>
          <span class="muted" id="sync-estimate" style="align-self:center"></span></div>
      </form>`}`;
  }

  async function syncPage(params) {
    const data = await get("/sync");
    if (!location.hash.startsWith("#/sync")) return;  // left the page while it loaded
    if (!data.running) {
      view.innerHTML = idle(data, params);
      wireStart(data);
      return;
    }
    view.innerHTML = running(data);
    $("#stop-sync").addEventListener("click", async event => {
      const now = event.target.textContent === "Quit at once";
      if (!confirm(now ? "Quit at once? Accounts in progress are not finished and the database is not written; the next sync catches up."
                       : "Stop the sync? Finished accounts are kept; accounts in progress keep the regions they finished.")) return;
      event.target.disabled = true;
      try {
        await send("/sync/stop");
      } catch (error) {
        alert(error.message);
      }
    });
    const log = $("#sync-log");
    if (log) log.scrollTop = log.scrollHeight;
    timer = setTimeout(() => {
      if (location.hash.startsWith("#/sync")) syncPage(params).catch(failed);
    }, 2000);
  }

  function wireStart(data) {
    const form = $("#sync-form");
    if (!form) return;
    const pick = $("#pick"), boxes = [...form.querySelectorAll("[name=profile]")];
    const chosen = () => form.which.value === "all" ? data.accounts.filter(a => a.enabled) : data.accounts.filter(a => boxes.some(b => b.checked && b.value === a.profile));
    const update = () => {
      pick.hidden = form.which.value === "all";
      const list = chosen();
      $("#pick-count").textContent = `${plural(boxes.filter(b => b.checked).length, "account")} chosen`;
      const known = list.filter(a => a.average_seconds);
      const total = known.reduce((sum, a) => sum + a.average_seconds, 0);
      $("#sync-estimate").textContent = !list.length ? "Choose at least one account."
        : `${plural(list.length, "account")}${known.length ? ` · about ${duration(total / known.length)} each on average, several at once` : ""}`;
      $("#start-sync").disabled = !list.length;
    };
    form.addEventListener("change", update);
    $("#pick-filter").addEventListener("input", event => {
      const text = event.target.value.trim().toLowerCase();
      form.querySelectorAll(".pick-list label").forEach(row => { row.hidden = Boolean(text) && !row.dataset.text.includes(text); });
    });
    update();
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = $("#start-sync"), error = $("#sync-error");
      button.disabled = true;
      error.hidden = true;
      try {
        await send("/sync", "POST", {profiles: form.which.value === "all" ? [] : chosen().map(a => a.profile),
          full: form.full.checked, sweep: form.sweep.checked});
        if (location.hash === "#/sync") await syncPage(new URLSearchParams());
        else location.hash = "#/sync";
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        button.disabled = false;
      }
    });
  }

  // ------------------------------------------------------------------ shared bits

  const verdictChip = v => v ? `<span class="chip ${v === "PASS" ? "ok" : v === "FAIL" ? "bad" : "warn"}">${esc(v)}</span>` : "";
  const words = text => String(text || "").split(/[\s,]+/).map(w => w.trim()).filter(Boolean);
  const findLink = (id, account) => `#/search?${new URLSearchParams({q: id, ...(account ? {account} : {})})}`;
  const download = (name, text, type = "application/json") => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([text], {type}));
    link.download = name;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  // Tabs inside a report page: #/<page>/<name>/<tab>.
  const tabBar = (base, tabs, current) => `<div class="tabs" role="tablist">${tabs.map(([key, label]) =>
    `<a role="tab" href="${base}/${key}" aria-selected="${key === current}">${esc(label)}</a>`).join("")}</div>`;
  const busy = (button, text) => { button.disabled = true; button.dataset.text = button.textContent; button.textContent = text; };
  const idleButton = button => { button.disabled = false; button.textContent = button.dataset.text || button.textContent; };

  // ------------------------------------------------------------------ compare

  // The profile that differs from this one only by its environment words: nonp -> prod.
  function twin(profile, accounts, envWords) {
    const strip = name => name.toLowerCase().split(/[-_.]/).filter(part => !envWords.includes(part)).join("-");
    const base = strip(profile);
    return accounts.find(a => a.profile && a.profile !== profile && strip(a.profile) === base);
  }

  async function comparePage() {
    const data = await get("/compare");
    if (!data.ready) {
      view.innerHTML = `<header class="head"><div><h1>Compare accounts</h1></div></header>${notReady(data)}`;
      return;
    }
    const option = a => `<option value="${esc(a.profile || a.account_id)}">${esc(a.profile || a.account_id)} (${esc(a.account_id)}) · ${number(a.resources)} resources</option>`;
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Analyse</p><h1>Compare accounts</h1>
        <p>Did prod get built like nonp? Pairs resources by name with the environment words read as the same (web-dev = web-mtf), then lists what is missing, extra and configured differently. It reads the database; sync both accounts first for a fresh answer.</p></div></header>
      ${data.accounts.length < 2 ? `<div class="empty"><strong>Needs two synced accounts.</strong><span>Sync them first.</span></div>` : `
      <form class="card" id="compare-form">
        <h2>New compare</h2>
        <div class="form-grid">
          <label>Compare from <small class="muted">the one built first, e.g. nonp</small><select name="source">${data.accounts.map(option).join("")}</select></label>
          <label>Compare to <small class="muted">the one that should match it, e.g. prod</small><select name="target">${data.accounts.map(option).join("")}</select></label>
          <label>Environment words in its names <small class="muted">empty: used before, or found in the names</small><input name="env_source" placeholder="e.g. dev, tst"></label>
          <label>Environment words in its names <small class="muted">empty: used before, or found in the names</small><input name="env_target" placeholder="e.g. mtf, prod"></label>
          <label>Only what changed in the source since <small class="muted">optional: the step you just did</small><input name="since" type="date"></label>
          <label>Only these services <small class="muted">optional, e.g. ec2 elbv2</small><input name="services" placeholder="all services"></label>
        </div>
        <p class="muted" id="pair-note" style="margin:0;font-size:13px"></p>
        <p id="compare-error" class="error" hidden></p>
        <div class="actions"><button class="button primary" type="submit" id="run-compare">Compare</button></div>
      </form>`}
      <section class="card"><h2>Past compares</h2>
        ${data.reports.length ? `<div class="table-box"><table>
          <thead><tr><th>From → to</th><th>When</th><th>Missing</th><th>Extra</th><th>Different</th><th>Not compared</th><th>Same</th></tr></thead>
          <tbody>${data.reports.map(r => `<tr>
            <td><a href="#/compare/${encodeURIComponent(r.name)}"><strong>${esc(r.source.profile)}</strong> → <strong>${esc(r.target.profile)}</strong></a>${r.since ? `<br><span class="muted">changed since ${esc(r.since)}</span>` : ""}</td>
            <td class="muted num">${esc(when(r.compared_at))}</td>
            <td>${r.missing ? `<span class="chip bad">${number(r.missing)}</span>` : "0"}</td>
            <td>${r.extra ? `<span class="chip warn">${number(r.extra)}</span>` : "0"}</td>
            <td>${r.different ? `<span class="chip warn">${number(r.different)}</span>` : "0"}</td>
            <td>${r.not_compared ? `<span class="chip plain">${number(r.not_compared)}</span>` : "0"}</td>
            <td class="num">${number(r.same)} / ${number(r.pairs)}</td></tr>`).join("")}</tbody></table></div>`
          : `<p class="muted" style="margin:0">None yet.</p>`}
      </section>`;
    const form = $("#compare-form");
    if (!form) return;
    const sourceOf = () => data.accounts.find(a => (a.profile || a.account_id) === form.source.value);
    const targetOf = () => data.accounts.find(a => (a.profile || a.account_id) === form.target.value);
    const fill = () => {
      const s = sourceOf(), t = targetOf();
      const pair = s && t && data.pairs.find(p => p.source === s.account_id && p.target === t.account_id);
      form.env_source.value = pair ? pair.words[0].join(", ") : "";
      form.env_target.value = pair ? pair.words[1].join(", ") : "";
      $("#pair-note").textContent = pair ? `Using the words from the last compare of this pair (${when(pair.used)}).`
        : "The words will be read from the names; the report shows which it found, and you can change them and compare again.";
    };
    form.source.addEventListener("change", () => {
      const match = twin(form.source.value, data.accounts, data.env_words);
      if (match) form.target.value = match.profile;
      else if (form.target.value === form.source.value) {
        const other = data.accounts.find(a => (a.profile || a.account_id) !== form.source.value);
        if (other) form.target.value = other.profile || other.account_id;
      }
      fill();
    });
    form.target.addEventListener("change", fill);
    const last = data.reports[0];
    if (last && data.accounts.some(a => a.profile === last.source.profile) && data.accounts.some(a => a.profile === last.target.profile)) {
      form.source.value = last.source.profile;
      form.target.value = last.target.profile;
      fill();
    } else {
      form.source.dispatchEvent(new Event("change"));
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = $("#run-compare"), error = $("#compare-error");
      error.hidden = true;
      if (form.source.value === form.target.value) {
        error.textContent = "Choose two different accounts.";
        error.hidden = false;
        return;
      }
      busy(button, "Comparing…");
      try {
        const result = await send("/compare", "POST", {source: form.source.value, target: form.target.value,
          env_source: words(form.env_source.value), env_target: words(form.env_target.value), since: form.since.value,
          services: words(form.services.value)});
        location.hash = `#/compare/${encodeURIComponent(result.name)}`;
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        idleButton(button);
      }
    });
  }

  function changeLines(changes) {
    const value = v => esc(typeof v === "string" ? v : JSON.stringify(v));
    return `<table class="diff"><thead><tr><th>Field</th><th>In the source</th><th>In the target</th></tr></thead><tbody>${changes.map(c => "only_in_source" in c
      ? `<tr><td class="mono">${esc(c.path)}</td><td class="mono was">${c.only_in_source.map(value).join("<br>") || "—"}</td><td class="mono now">${c.only_in_target.map(value).join("<br>") || "—"}</td></tr>`
      : `<tr><td class="mono">${esc(c.path)}</td><td class="mono was">${value(c.source)}</td><td class="mono now">${value(c.target)}</td></tr>`).join("")}</tbody></table>`;
  }

  async function compareReport(name, tab) {
    const {report: r} = await get(`/compare/reports/${encodeURIComponent(name)}`);
    const s = r.source, t = r.target;
    const tabs = [["missing", `Missing in target (${r.missing.length})`], ["different", `Different (${r.different.length})`],
      ["extra", `Extra in target (${r.extra.length})`], ["expected", `Expected differences (${r.expected.length})`],
      ["not-compared", `Not compared (${r.not_compared.length})`]];
    const current = tabs.some(x => x[0] === tab) ? tab : (r.missing.length || !r.different.length ? "missing" : "different");
    const rows = {missing: r.missing, extra: r.extra, different: r.different, expected: r.expected, "not-compared": r.not_compared}[current];
    const found = side => Object.entries(side.found_words || {});
    let body;
    if (current === "missing" || current === "extra") {
      const [id, account] = current === "missing" ? ["source_id", s.account] : ["target_id", t.account];
      body = rows.length ? `<div class="table-box"><table><thead><tr><th>Type</th><th>Name</th><th>Region</th><th>Id in the ${current === "missing" ? "source" : "target"}</th></tr></thead>
        <tbody>${rows.map(m => `<tr data-text="${esc(`${m.type} ${m.name} ${m[id]}`.toLowerCase())}"><td class="mono">${esc(m.type)}</td><td><strong>${esc(m.name)}</strong></td><td class="muted">${esc(m.region)}</td>
          <td class="mono"><a href="${findLink(m[id], account)}">${esc(m[id])}</a></td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted" style="margin:0">${current === "missing" ? "Nothing missing: everything in the source has its pair in the target." : "Nothing extra in the target."}</p>`;
    } else if (current === "different" || current === "expected") {
      body = rows.length ? rows.map(d => `<details class="change" data-text="${esc(`${d.type} ${d.name}`.toLowerCase())}">
          <summary><span class="chip">${esc(d.type)}</span><strong>${esc(d.name)}</strong><span class="muted">${esc(d.region)} · ${plural(d.changes.length, "field")}</span></summary>
          <p class="muted mono" style="margin:0 0 8px"><a href="${findLink(d.source_id, s.account)}">${esc(d.source_id)}</a> → <a href="${findLink(d.target_id, t.account)}">${esc(d.target_id)}</a></p>
          ${changeLines(d.changes)}</details>`).join("")
        : `<p class="muted" style="margin:0">${current === "different" ? "No differences in what was paired." : "No differences in the fields that usually differ (CIDRs, sizes, certificates)."}</p>`;
    } else {
      body = rows.length ? `<div class="table-box"><table><thead><tr><th>Type</th><th>Region</th><th>Why</th><th>Source</th><th>Target</th></tr></thead>
        <tbody>${rows.map(n => `<tr data-text="${esc(`${n.type} ${n.name || ""} ${n.why}`.toLowerCase())}"><td class="mono">${esc(n.type)}${n.name ? `<br><strong>${esc(n.name)}</strong>` : ""}</td><td class="muted">${esc(n.region)}</td>
          <td>${esc(n.why)}</td><td class="num">${n.source ?? ""}</td><td class="num">${n.target ?? ""}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted" style="margin:0">Everything was compared.</p>`;
    }
    view.innerHTML = `
      <a href="#/compare" style="font-size:13px;font-weight:600">← Compare accounts</a>
      <header class="head">
        <div><h1>${esc(s.profile)} → ${esc(t.profile)}</h1>
          <p>${esc(s.account)} synced ${esc(when(s.synced))} → ${esc(t.account)} synced ${esc(when(t.synced))} · compared ${esc(when(r.compared_at))} from the ${esc(r.read_from)}${r.since ? ` · only what changed in the source since ${esc(r.since)}` : ""}</p></div>
        <div class="actions"><button class="button" type="button" id="download-report">Download report</button></div>
      </header>
      <section class="tiles" aria-label="Totals">
        <div class="tile"><span>Paired</span><strong class="num">${number(r.pairs)}</strong><small>${number(r.same)} exactly the same</small></div>
        <div class="tile"><span>Missing in target</span><strong class="num">${number(r.missing.length)}</strong><small>the steps not done yet</small></div>
        <div class="tile"><span>Different</span><strong class="num">${number(r.different.length)}</strong><small>+ ${number(r.expected.length)} with expected differences</small></div>
        <div class="tile"><span>Extra in target</span><strong class="num">${number(r.extra.length)}</strong><small>not in the source</small></div>
        <div class="tile"><span>Not compared</span><strong class="num">${number(r.not_compared.length)}</strong><small>not counted as missing</small></div>
      </section>
      ${r.blind_spots.length ? `<section class="card"><h2>Blind spots: this compare cannot see these</h2>${r.blind_spots.map(b => `<div class="issue"><span class="chip warn">blind spot</span><div><strong>${esc(b)}</strong></div></div>`).join("")}</section>` : ""}
      <section class="card">
        <div class="card-head"><h2>Environment words</h2><span class="muted">read as the same word on both sides</span></div>
        <form class="form-grid" id="words-form">
          ${[[s, "env_source"], [t, "env_target"]].map(([side, key]) => `<div class="words">
            <label>${esc(side.profile)} <input name="${key}" value="${esc(side.env_words.join(", "))}"></label>
            <div class="chips">${found(side).map(([w, f]) => `<span class="chip ${side.env_words.includes(w) ? "" : "warn"}" title="e.g. ${esc(f.example)}">${esc(w)} · ${number(f.count)}${side.env_words.includes(w) ? "" : " · left out"}</span>`).join("") || `<span class="muted">no environment words found in its names</span>`}</div>
          </div>`).join("")}
          <div class="actions"><button class="button" type="submit" id="compare-again">Compare again with these words</button></div>
        </form>
        <p class="muted" style="margin:0;font-size:13px">A word marked “left out” is in some names but not used: those names show as missing or extra. Add it if that is on purpose.</p>
        <p id="compare-error" class="error" hidden></p>
      </section>
      ${tabBar(`#/compare/${encodeURIComponent(name)}`, tabs, current)}
      <section class="card">
        ${rows.length > 8 ? `<label class="filter"><span class="sr">Filter</span><input id="report-filter" type="search" placeholder="Filter by type or name"></label>` : ""}
        ${body}
      </section>`;
    $("#download-report").addEventListener("click", () => download(name, JSON.stringify(r, null, 2)));
    const filter = $("#report-filter");
    if (filter) filter.addEventListener("input", () => {
      const text = filter.value.trim().toLowerCase();
      view.querySelectorAll("[data-text]").forEach(row => { row.hidden = Boolean(text) && !row.dataset.text.includes(text); });
    });
    $("#words-form").addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.target, button = $("#compare-again"), error = $("#compare-error");
      busy(button, "Comparing…");
      try {
        const result = await send("/compare", "POST", {source: s.account, target: t.account, since: r.since || "",
          env_source: words(form.env_source.value), env_target: words(form.env_target.value)});
        location.hash = `#/compare/${encodeURIComponent(result.name)}`;
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        idleButton(button);
      }
    });
  }

  // ------------------------------------------------------------------ projects

  async function projectsPage() {
    const data = await get("/projects");
    if (!data.ready) {
      view.innerHTML = `<header class="head"><div><h1>Projects</h1></div></header>${notReady(data)}`;
      return;
    }
    const shared = new Set([...(data.last.shared || []), ...data.shared_accounts]);
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Analyse</p><h1>Projects</h1>
        <p>Everything a project uses: its accounts (by keyword), what the shared-services accounts run for it, its EKS clusters, how traffic gets in and out hop by hop, and how its DNS is answered. It reads the database and makes no AWS calls.</p></div></header>
      <form class="card" id="project-form">
        <h2>Map a project</h2>
        <div class="form-grid">
          <label>Keywords <small class="muted">in account names, tags or resource names</small><input name="keywords" required value="${esc((data.last.keywords || []).join(" "))}" placeholder="e.g. stablecoin security"></label>
          <label>Name for the report <small class="muted">optional: the first keyword</small><input name="name" placeholder="e.g. stablecoin"></label>
        </div>
        <div><strong style="font-size:13px">Shared-services accounts</strong> <span class="muted" style="font-size:13px">Transit Gateway, egress, DNS. None ticked: aws_project.json's, the ones used last time and those named ${esc(data.shared_words.join(", "))}.</span>
          <div class="pick-list" style="max-height:220px">${data.accounts.map(a => `<label><input type="checkbox" name="shared" value="${esc(a.account_id)}"${shared.has(a.account_id) || shared.has(a.profile) ? " checked" : ""}>
            <span><strong>${esc(a.profile || a.account_id)}</strong> <span class="muted num">${esc(a.account_id)}</span></span></label>`).join("")}</div></div>
        <p id="project-error" class="error" hidden></p>
        <div class="actions"><button class="button primary" type="submit" id="run-project">Map the project</button></div>
      </form>
      <section class="card"><h2>Past projects</h2>
        ${data.reports.length ? `<div class="table-box"><table>
          <thead><tr><th>Project</th><th>When</th><th>Accounts</th><th>EKS</th><th>Paths</th><th>Open</th></tr></thead>
          <tbody>${data.reports.map(r => `<tr>
            <td><a href="#/projects/${encodeURIComponent(r.name)}"><strong>${esc(r.project)}</strong></a><br><span class="muted">${esc(r.keywords.join(", "))}</span></td>
            <td class="muted num">${esc(when(r.generated))}</td>
            <td>${esc(r.applications.join(", "))}${r.shared.length ? `<br><span class="muted">shared: ${esc(r.shared.join(", "))}</span>` : ""}</td>
            <td class="num">${number(r.eks)}</td>
            <td>${verdictChip("PASS").replace("PASS", `${number(r.pass)} pass`)} ${r.fail ? verdictChip("FAIL").replace("FAIL", `${number(r.fail)} fail`) : ""} ${r.unknown ? verdictChip("UNKNOWN").replace("UNKNOWN", `${number(r.unknown)} unknown`) : ""}</td>
            <td class="num">${number(r.open)}</td></tr>`).join("")}</tbody></table></div>`
          : `<p class="muted" style="margin:0">None yet.</p>`}
      </section>`;
    const form = $("#project-form");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = $("#run-project"), error = $("#project-error");
      error.hidden = true;
      busy(button, "Mapping…");
      try {
        const result = await send("/projects", "POST", {keywords: words(form.keywords.value), name: form.name.value.trim(),
          shared: [...form.querySelectorAll("[name=shared]:checked")].map(box => box.value)});
        location.hash = `#/projects/${encodeURIComponent(result.name)}`;
      } catch (e) {
        error.textContent = e.message;
        error.hidden = false;
        idleButton(button);
      }
    });
  }

  const hopList = hops => `<ol class="hops">${hops.map(h => `<li>${verdictChip(h.verdict)}<div><strong>${esc(h.hop)}</strong><span class="muted mono">${esc(h.evidence)}</span></div></li>`).join("")}</ol>`;

  function pathCard(p) {
    const title = p.entry ? `${p.path} · ${p.entry}` : `${p.path} · ${p.from}`;
    return `<details class="change" data-text="${esc(`${p.path} ${p.entry || ""} ${p.from} ${p.exit || ""}`.toLowerCase())}"${p.verdict === "FAIL" ? " open" : ""}>
      <summary>${verdictChip(p.verdict)}<strong>${esc(title)}</strong><span class="muted">port ${esc(p.port)}${p.target_group ? ` · target group ${esc(p.target_group)}` : ""}${p.exit ? ` · out through ${esc(p.exit)}` : ""}${p.to ? ` · to ${esc(p.to)}` : ""}</span></summary>
      ${(p.reaches || []).length ? `<p style="margin:0 0 8px">Reaches: ${esc(p.reaches.join(", "))}</p>` : ""}
      ${hopList(p.hops || [])}
      ${(p.targets || []).map(t => `<div class="target"><p style="margin:8px 0">${verdictChip(t.verdict)} <strong>to ${esc(t.target)}</strong> <span class="muted">${esc(t.owner || "")}</span></p>${hopList(t.hops)}</div>`).join("")}
    </details>`;
  }

  async function projectReport(name, tab) {
    const data = await get(`/projects/reports/${encodeURIComponent(name)}`);
    const r = data.report, apps = r.accounts.application, shared = r.accounts.shared;
    const fails = r.paths.filter(p => p.verdict === "FAIL").length;
    const tabs = [["overview", "Overview"], ["diagram", "Diagram"], ["paths", `Paths (${r.paths.length})`],
      ["dns", `DNS (${r.dns.names.length})`], ["entry-exit", `Entry and exit (${r.entry_points.length + r.exit_points.length})`],
      ["open", `What's open (${r.open.length})`], ["shared", `Shared resources (${r.shared_resources.length})`]];
    const current = tabs.some(x => x[0] === tab) ? tab : "overview";
    const sev = {high: "bad", medium: "warn", low: "plain"};
    let body = "";
    if (current === "overview") {
      body = `
        <div class="grid">
          <section class="card"><h2>Application accounts</h2>${apps.map(a => `<a class="link-row" href="#/accounts/${encodeURIComponent(a.profile || a.account)}"><strong>${esc(a.profile || a.account)}</strong><span class="muted num">${esc(a.account)} · synced ${esc(when(a.synced))}</span></a>`).join("")}
            <h2 style="margin-top:6px">Shared-services accounts</h2>${shared.map(a => `<a class="link-row" href="#/projects/${encodeURIComponent(name)}/shared"><strong>${esc(a.profile || a.account)}</strong><span class="muted">${number(a.resources_for_project)} resources for this project</span></a>`).join("") || `<p class="muted" style="margin:0">None: Transit Gateway, egress and Resolver paths stop at the project accounts.</p>`}</section>
          <section class="card"><h2>EKS clusters</h2>${r.eks.map(c => `<div class="link-row" style="flex-direction:column;align-items:flex-start">
              <span><span class="chip">${esc(c.env || "?")}</span> <strong>${esc(c.name)}</strong> <span class="muted">${esc(c.account)} · ${esc(c.region)} · Kubernetes ${esc(c.version || "?")}</span></span>
              <span>API ${c.endpoint_public ? `<span class="chip ${(c.public_access_cidrs || ["0.0.0.0/0"]).includes("0.0.0.0/0") ? "bad" : "warn"}">public ${esc((c.public_access_cidrs || ["0.0.0.0/0"]).join(", "))}</span>` : `<span class="chip ok">private only</span>`}
                · ${plural(c.nodegroups.length, "node group")} · ${plural(c.nodes.length, "node")} · ${plural(c.subnets.length, "subnet")}</span>
              ${c.nodegroups.map(g => `<span class="muted mono">${esc(g.name)}: ${esc((g.instance_types || []).join(", "))} ${g.scaling ? `${g.scaling.minSize}–${g.scaling.maxSize} (${g.scaling.desiredSize} now)` : ""}</span>`).join("")}
            </div>`).join("") || `<p class="muted" style="margin:0">No EKS cluster in the application accounts.</p>`}</section>
        </div>
        ${r.blind_spots.length ? `<section class="card"><h2>Blind spots</h2>${r.blind_spots.map(b => `<div class="issue"><span class="chip warn">blind spot</span><div><strong>${esc(b)}</strong></div></div>`).join("")}</section>` : ""}
        <section class="card"><div class="card-head"><h2>Paths that fail</h2><a href="#/projects/${encodeURIComponent(name)}/paths">All ${number(r.paths.length)} paths</a></div>
          ${r.paths.filter(p => p.verdict === "FAIL").map(pathCard).join("") || `<p class="muted" style="margin:0">No path fails.</p>`}</section>`;
    } else if (current === "diagram") {
      body = `<section class="card">
        <div class="card-head"><p class="muted" style="margin:0">Accounts hold VPCs, VPCs hold subnets. Lines are paths (green pass, red fail, amber unknown), routes and DNS (dotted). Click a box to see its links.</p>
          <div class="actions"><label class="check"><input type="checkbox" id="show-dns" checked> DNS</label><label class="check"><input type="checkbox" id="show-subnets" checked> Subnets</label>
            <button class="button" type="button" id="download-tad">TAD JSON</button><button class="button" type="button" id="download-mmd"${data.mermaid ? "" : " disabled"}>Mermaid</button></div></div>
        <div class="tad-wrap"><div class="tad" id="tad"></div></div>
        <div id="tad-detail" class="tad-detail" hidden></div>
      </section>`;
    } else if (current === "paths") {
      body = `<section class="card"><label class="filter"><span class="sr">Filter</span><input id="report-filter" type="search" placeholder="Filter paths"></label>
        ${r.paths.map(pathCard).join("") || `<p class="muted" style="margin:0">No paths: no load balancer or EKS cluster in the project.</p>`}</section>
        ${r.vpc_endpoints.length ? `<section class="card"><h2>VPC endpoints the clusters use</h2><div class="table-box"><table><thead><tr><th></th><th>Service</th><th>Endpoint</th><th>Cluster</th><th>Evidence</th></tr></thead>
          <tbody>${r.vpc_endpoints.map(e => `<tr><td>${verdictChip(e.verdict)}</td><td class="mono">${esc(e.service)} <span class="muted">${esc(e.type)}</span></td><td class="mono">${esc(e.endpoint)}</td><td>${esc(e.cluster)}</td><td class="muted mono">${esc(e.evidence)}</td></tr>`).join("")}</tbody></table></div></section>` : ""}`;
    } else if (current === "dns") {
      body = `${r.dns.vpcs.map(v => `<section class="card">
          <div class="card-head"><h2>${esc(v.name)} <span class="muted mono" style="font-weight:400">${esc(v.vpc)} · ${esc(v.account)}</span></h2>
            <span>${v.dns_support === false ? `<span class="chip bad">DNS off</span>` : `<span class="chip ok">DNS on</span>`} <span class="muted">servers ${esc(v.dhcp_dns_servers.join(", "))}</span></span></div>
          <p class="muted" style="margin:0;font-size:13px">A name is answered by the first of these that matches it, in this order:</p>
          ${v.resolution_order.map((o, i) => `<details class="change"${o.verdict === "FAIL" ? " open" : ""}><summary><span class="num muted">${i + 1}</span>${verdictChip(o.verdict)}<strong class="mono">${esc(o.domain)}</strong><span class="muted">${esc(o.answered_by)}</span></summary>
            ${(o.checks || []).length ? hopList(o.checks) : `<p class="muted" style="margin:0">Nothing to check.</p>`}</details>`).join("")}
        </section>`).join("")}
        ${r.dns.inbound_endpoints.length ? `<section class="card"><h2>Inbound endpoints</h2><p class="muted" style="margin:0;font-size:13px">Where other networks send queries for these names.</p>${r.dns.inbound_endpoints.map(e => `<div class="link-row"><strong>${esc(e.name || e.endpoint)}</strong><span class="mono">${esc(e.ips.join(", "))}</span><span class="muted">allows from ${esc(e.allowed_from.join(", "))} · ${esc(e.account)}</span></div>`).join("")}</section>` : ""}
        <section class="card"><h2>The project's records</h2>${r.dns.names.length ? `<div class="table-box"><table><thead><tr><th>Name</th><th>Type</th><th>Lands on</th><th>nslookup</th><th>Note</th></tr></thead>
          <tbody>${r.dns.names.map(n => `<tr><td class="mono">${esc(n.name)}<br><span class="muted">${n.private ? "private" : "public"} zone ${esc(n.zone)}</span></td><td>${esc(n.type)}</td><td>${esc(n.target || "—")}</td>
            <td>${n.nslookup ? `<span class="chip ${n.nslookup === "ok" ? "ok" : "warn"}">${esc(n.nslookup)}</span>` : "—"}${(n.answers || []).map(a => `<br><span class="mono">${esc(a.ip)}</span> <span class="muted">${esc(a.owner || "unknown owner")}</span>`).join("")}</td>
            <td>${n.note ? `<span class="chip warn">${esc(n.note)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted" style="margin:0">No records in the project's zones.</p>`}</section>`;
    } else if (current === "entry-exit") {
      const table = (rows, cols) => rows.length ? `<div class="table-box"><table><thead><tr><th></th>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr></thead>
        <tbody>${rows.map(x => `<tr><td>${verdictChip(x.verdict)}</td>${cols.map(c => `<td>${esc(c[1](x))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<p class="muted" style="margin:0">None.</p>`;
      body = `<section class="card"><h2>Entry points: how traffic gets in</h2>${table(r.entry_points, [["Kind", x => x.kind], ["Name", x => x.name + (x.port ? ` :${x.port}` : "")], ["Reaches", x => (x.reaches || []).join(", ")], ["Account", x => `${x.account} · ${x.region}`]])}</section>
        <section class="card"><h2>Exit points: how traffic gets out</h2>${table(r.exit_points, [["Kind", x => x.kind], ["Through", x => x.through], ["From", x => x.from], ["Account", x => `${x.account} · ${x.region}`]])}</section>`;
    } else if (current === "open") {
      body = `<section class="card">${r.open.length ? `<div class="table-box"><table><thead><tr><th>Severity</th><th>What</th><th>Where</th><th>Detail</th><th>Account</th></tr></thead>
        <tbody>${r.open.map(o => `<tr><td><span class="chip ${sev[o.severity] || "warn"}">${esc(o.severity)}</span></td><td>${esc(o.kind)}</td><td class="mono"><a href="${findLink(o.where.split(" ")[0], o.account)}">${esc(o.where)}</a></td><td class="mono">${esc(o.detail)}</td><td class="muted">${esc(o.account)}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted" style="margin:0">Nothing open to the internet found.</p>`}</section>`;
    } else {
      body = `<section class="card"><p class="muted" style="margin:0;font-size:13px">What the shared-services accounts run for this project, and why each one was picked.</p>
        ${r.shared_resources.length > 8 ? `<label class="filter"><span class="sr">Filter</span><input id="report-filter" type="search" placeholder="Filter by type, id or reason"></label>` : ""}
        ${r.shared_resources.length ? `<div class="table-box"><table><thead><tr><th>Type</th><th>Resource</th><th>Why</th><th>Account</th></tr></thead>
        <tbody>${r.shared_resources.map(x => `<tr data-text="${esc(`${x.type} ${x.id} ${x.name || ""} ${x.why}`.toLowerCase())}"><td class="mono">${esc(x.type)}</td>
          <td>${x.pk ? `<a href="#/resource/${x.pk}"><strong>${esc(x.name || x.id)}</strong></a>` : `<strong>${esc(x.name || x.id)}</strong>`}${x.name && x.name !== x.id ? `<br><span class="mono muted">${esc(x.id)}</span>` : ""}</td>
          <td>${esc(x.why)}</td><td class="muted">${esc(x.account)} · ${esc(x.region)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted" style="margin:0">None.</p>`}</section>`;
    }
    view.innerHTML = `
      <a href="#/projects" style="font-size:13px;font-weight:600">← Projects</a>
      <header class="head">
        <div><h1>${esc(r.project)}</h1><p>Keywords ${esc(r.keywords.join(", "))} · mapped ${esc(when(r.generated))} · ${plural(apps.length, "application account")}, ${plural(shared.length, "shared account")}</p></div>
        <div class="actions"><button class="button" type="button" id="download-report">Download report</button></div>
      </header>
      <section class="tiles" aria-label="Totals">
        <div class="tile"><span>Paths checked</span><strong class="num">${number(r.paths.length)}</strong><small>${number(r.paths.filter(p => p.verdict === "PASS").length)} pass · ${number(fails)} fail · ${number(r.paths.filter(p => p.verdict === "UNKNOWN").length)} unknown</small></div>
        <div class="tile"><span>EKS clusters</span><strong class="num">${number(r.eks.length)}</strong><small>${number(r.eks.filter(c => c.endpoint_public).length)} with a public API</small></div>
        <div class="tile"><span>Entry / exit points</span><strong class="num">${number(r.entry_points.length)} / ${number(r.exit_points.length)}</strong><small>how traffic gets in and out</small></div>
        <div class="tile"><span>What's open</span><strong class="num">${number(r.open.length)}</strong><small>${number(r.open.filter(o => o.severity === "high").length)} high</small></div>
      </section>
      ${tabBar(`#/projects/${encodeURIComponent(name)}`, tabs, current)}
      ${body}`;
    $("#download-report").addEventListener("click", () => download(name, JSON.stringify(r, null, 2)));
    const filter = $("#report-filter");
    if (filter) filter.addEventListener("input", () => {
      const text = filter.value.trim().toLowerCase();
      view.querySelectorAll("[data-text]").forEach(row => { row.hidden = Boolean(text) && !row.dataset.text.includes(text); });
    });
    if (current === "diagram") {
      $("#download-tad").addEventListener("click", () => download(name.replace(/\.json$/, ".tad.json"), JSON.stringify(data.tad, null, 2)));
      $("#download-mmd").addEventListener("click", () => download(name.replace(/\.json$/, ".mmd"), data.mermaid, "text/plain"));
      diagram(data.tad, $("#tad"), $("#tad-detail"));
    }
  }

  // The TAD model as nested boxes (account > VPC > subnet) with its edges drawn over them.
  const TAD_ICON = {internet: "◎", account: "▣", vpc: "⬚", subnet: "▢", internet_gateway: "IGW", nat_gateway: "NAT", transit_gateway: "TGW",
    load_balancer: "LB", eks_cluster: "EKS", network: "NET", exit: "OUT", resolver_rule: "DNS", hosted_zone: "ZONE", dns_name: "REC"};
  const DNS_KINDS = new Set(["resolver_rule", "hosted_zone", "dns_name"]);

  function diagram(model, box, detail) {
    const byId = new Map(model.nodes.map(n => [n.id, n]));
    const children = new Map();
    for (const n of model.nodes) {
      const parent = n.parent && byId.has(n.parent) ? n.parent : null;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(n);
    }
    const idOf = new Map(model.nodes.map((n, i) => [n.id, `tad-${i}`]));
    const containers = new Set(["account", "vpc", "subnet"]);
    const render = n => {
      const kids = children.get(n.id) || [];
      const extra = n.kind === "account" ? `<span class="chip ${n.role === "shared" ? "plain" : ""}">${esc(n.role || "")}</span>`
        : n.kind === "subnet" ? `<span class="chip ${n.public ? "warn" : "plain"}">${n.public ? "public" : "private"}</span>`
        : n.kind === "eks_cluster" ? `<span class="chip ${n.endpoint === "public" ? "warn" : "ok"}">API ${esc(n.endpoint)}</span>` : "";
      if (containers.has(n.kind)) {
        const inner = kids.filter(k => k.kind === "subnet"), others = kids.filter(k => k.kind !== "subnet");
        return `<div class="tad-box tad-${n.kind}" id="${idOf.get(n.id)}" data-node="${esc(n.id)}" data-kind="${n.kind}">
          <div class="tad-title"><span class="tad-icon">${TAD_ICON[n.kind]}</span><span>${esc(n.label)}</span>${extra}</div>
          ${others.length ? `<div class="tad-items">${others.map(render).join("")}</div>` : ""}
          ${inner.length ? `<div class="tad-subnets">${inner.map(render).join("")}</div>` : ""}
        </div>`;
      }
      return `<button type="button" class="tad-node tad-${n.kind}" id="${idOf.get(n.id)}" data-node="${esc(n.id)}" data-kind="${n.kind}">
        <span class="tad-icon">${TAD_ICON[n.kind] || "•"}</span><span>${esc(n.label)}</span>${extra}</button>`;
    };
    const top = children.get(null) || [];
    const outside = top.filter(n => !containers.has(n.kind) && !DNS_KINDS.has(n.kind));
    const accounts = top.filter(n => containers.has(n.kind));
    const dns = top.filter(n => DNS_KINDS.has(n.kind));
    box.innerHTML = `<svg class="tad-lines" aria-hidden="true"><defs>${["pass", "fail", "unknown", "plain"].map(k =>
        `<marker id="arrow-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="tad-arrow-${k}"/></marker>`).join("")}</defs></svg>
      <div class="tad-col tad-outside">${outside.map(render).join("")}</div>
      <div class="tad-col tad-accounts">${accounts.map(render).join("")}</div>
      <div class="tad-col tad-dns">${dns.map(render).join("")}</div>`;
    const svg = box.querySelector("svg");
    let selected = null;
    const draw = () => {
      const area = box.getBoundingClientRect();
      svg.setAttribute("width", box.scrollWidth);
      svg.setAttribute("height", box.scrollHeight);
      const showDns = $("#show-dns").checked;
      box.querySelectorAll(".tad-dns").forEach(el => { el.hidden = !showDns; });
      box.querySelectorAll(".tad-subnets").forEach(el => { el.classList.toggle("folded", !$("#show-subnets").checked); });
      const visible = el => el && el.offsetParent !== null && !el.closest(".folded, [hidden]");
      // A node inside a folded subnet list is drawn at its VPC.
      const anchor = id => {
        let el = document.getElementById(idOf.get(id));
        while (el && !visible(el)) el = el.parentElement && el.parentElement.closest("[data-node]");
        return el;
      };
      const rect = el => {
        const r = el.getBoundingClientRect();
        return {l: r.left - area.left, r: r.right - area.left, t: r.top - area.top, b: r.bottom - area.top,
          x: r.left - area.left + r.width / 2, y: r.top - area.top + r.height / 2};
      };
      // From the sides of the two boxes that face each other; nested boxes go out to the right and back.
      const route = (a, b) => {
        const pull = (d, min) => Math.max(min, Math.abs(d) / 2);
        if (a.r <= b.l) { const k = pull(b.l - a.r, 30); return [a.r, a.y, b.l, b.y, a.r + k, a.y, b.l - k, b.y]; }
        if (b.r <= a.l) { const k = pull(a.l - b.r, 30); return [a.l, a.y, b.r, b.y, a.l - k, a.y, b.r + k, b.y]; }
        if (a.b <= b.t) { const k = pull(b.t - a.b, 24); return [a.x, a.b, b.x, b.t, a.x, a.b + k, b.x, b.t - k]; }
        if (b.b <= a.t) { const k = pull(a.t - b.b, 24); return [a.x, a.t, b.x, b.b, a.x, a.t - k, b.x, b.b + k]; }
        const out = Math.max(a.r, b.r) + 36;
        return [a.r, a.y, b.r, b.y, out, a.y, out, b.y];
      };
      let lines = "";
      for (const e of model.edges) {
        if (!showDns && (e.kind === "dns" || e.kind === "alias")) continue;
        const a = anchor(e.from), b = anchor(e.to);
        if (!a || !b || a === b) continue;
        const ra = rect(a), rb = rect(b);
        const [p1x, p1y, p2x, p2y, c1x, c1y, c2x, c2y] = route(ra, rb);
        const c = `C${c1x} ${c1y} ${c2x} ${c2y}`;
        const kind = e.verdict ? e.verdict.toLowerCase() : "plain";
        const lit = selected && (e.from === selected || e.to === selected);
        const dim = selected && !lit;
        lines += `<path d="M${p1x} ${p1y} ${c} ${p2x} ${p2y}" class="tad-line tad-${kind}${e.kind === "dns" || e.kind === "alias" ? " tad-dotted" : ""}${lit ? " lit" : ""}${dim ? " dim" : ""}" marker-end="url(#arrow-${kind})"><title>${esc(`${e.kind}${e.label ? ` ${e.label}` : ""}${e.verdict ? ` ${e.verdict}` : ""}`)}</title></path>`;
        if (e.label && lit) {
          lines += `<text x="${(p1x + p2x) / 2}" y="${(p1y + p2y) / 2 - 4}" class="tad-label">${esc(e.label)}</text>`;
        }
      }
      svg.querySelectorAll("path.tad-line, text").forEach(el => el.remove());
      svg.insertAdjacentHTML("beforeend", lines);
    };
    box.addEventListener("click", event => {
      const el = event.target.closest("[data-node]");
      if (!el) return;
      event.stopPropagation();
      const id = el.dataset.node, n = byId.get(id);
      selected = selected === id ? null : id;
      box.querySelectorAll(".selected").forEach(x => x.classList.remove("selected"));
      if (selected) el.classList.add("selected");
      const links = model.edges.filter(e => e.from === id || e.to === id);
      detail.hidden = !selected;
      detail.innerHTML = selected ? `<div class="card-head"><h2>${esc(n.label)}</h2><span class="chip plain">${esc(n.kind.replace(/_/g, " "))}</span></div>
        <p class="mono muted" style="margin:0">${esc(n.id)}</p>
        ${links.map(e => `<div class="link-row">${verdictChip(e.verdict)}<span class="chip plain">${esc(e.kind)}</span><span>${e.from === id ? "to" : "from"} <strong>${esc((byId.get(e.from === id ? e.to : e.from) || {}).label || (e.from === id ? e.to : e.from))}</strong></span><span class="muted">${esc(e.label || "")}</span></div>`).join("") || `<p class="muted" style="margin:0">No lines to or from it.</p>`}
        ${/^(vpc|subnet|sg|igw|nat|tgw|vpce)-/.test(n.id) || n.id.startsWith("arn:") ? `<a href="${findLink(n.id)}">Find it in the inventory</a>` : ""}` : "";
      draw();
    });
    ["show-dns", "show-subnets"].forEach(id => $(`#${id}`).addEventListener("change", draw));
    const observer = new ResizeObserver(draw);
    observer.observe(box);
    draw();
  }

  // ------------------------------------------------------------------ settings

  const SETTING_GROUPS = [
    ["Accounts and regions", [
      ["regions", "Regions for new accounts", "list", "Comma separated, or all. Each saved account keeps its own regions."],
      ["global_region", "Region for global services", "text", "IAM, Route 53, CloudFront are read through this region."]]],
    ["Speed", [
      ["max_parallel_calls", "AWS commands at once", "auto", "auto: 2 per CPU core, more when the PC is free and fewer when it is busy."],
      ["account_workers", "Accounts at once", "auto", "auto: at least 4, more while the AWS workers have room."],
      ["region_workers", "Regions at once per account", "number"],
      ["type_workers", "Types at once per region", "number"],
      ["detail_workers", "Detail calls at once per type", "number"]]],
    ["Timeouts", [
      ["connect_timeout", "Connect timeout (seconds)", "number"],
      ["read_timeout", "Read timeout (seconds)", "number"],
      ["call_timeout", "Longest one command may take (seconds)", "number"],
      ["timeout_retries", "Tries again after a timeout", "number", "Then it is saved as a timeout."]]],
    ["Daily runs", [
      ["smart_skip", "Skip what was empty or denied before", "bool", "Daily runs skip types that were empty or had no permission; the weekly sweep lists everything."],
      ["full_sweep_days", "Full sweep every (days)", "number"],
      ["detail_refresh_hours", "Repeat detail calls for unchanged resources after (hours)", "number"]]],
    ["DNS lookups", [
      ["dns_lookup", "nslookup Route 53 records", "bool", "Every IP an answer returns is saved and searchable."],
      ["dns_match", "Only names matching", "text", "A regex, e.g. \\.sep\\.?$ for .sep only. Empty: every name."],
      ["dns_workers", "Lookups at once", "number"],
      ["dns_timeout", "Lookup timeout (seconds)", "number"]]],
    ["Types AWS lists that aws_commands.json lacks", [
      ["add_commands", "At the end of a sync", "choice:ask=Ask (in a terminal; the app adds them)|yes=Add and download them|no=Only report them"]]],
    ["Output", [
      ["write_db", "Write the database at the end of a sync", "bool", "Search, compare and projects read it."],
      ["keep_history", "Keep old versions of changed resources", "bool"],
      ["logs", "Log lines and run.log", "bool", "Off: only the progress bars and a summary."],
      ["status_seconds", "Progress lines every (seconds) when not in a terminal", "number"],
      ["eta_window", "ETA from the last this many commands", "number"]]],
    ["AWS CLI", [
      ["aws_cli", "AWS CLI command", "text", "aws, or a full path."],
      ["verify_ssl", "Verify SSL", "bool", "Off adds --no-verify-ssl to every command, as your network needs."]]],
  ];

  async function settingsPage() {
    const data = await get("/settings");
    const st = data.settings, db = data.database;
    const field = ([key, label, kind, help]) => {
      const value = st[key];
      let input;
      if (kind === "bool") input = `<input type="checkbox" name="${key}"${value ? " checked" : ""}>`;
      else if (kind.startsWith("choice:")) input = `<select name="${key}">${kind.slice(7).split("|").map(o => { const [v, t] = o.split("="); return `<option value="${v}"${v === value ? " selected" : ""}>${esc(t)}</option>`; }).join("")}</select>`;
      else if (kind === "list") input = `<input name="${key}" value="${esc(Array.isArray(value) ? value.join(", ") : value)}">`;
      else if (kind === "auto") input = `<input name="${key}" value="${esc(value)}" placeholder="auto or a number">`;
      else input = `<input name="${key}" value="${esc(value)}"${kind === "number" ? ` inputmode="numeric"` : ""}>`;
      return `<label class="${kind === "bool" ? "setting check" : "setting"}" data-kind="${kind}">${kind === "bool" ? input : ""}<span><strong>${esc(label)}</strong>${help ? `<small class="muted">${esc(help)}</small>` : ""}</span>${kind === "bool" ? "" : input}</label>`;
    };
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Analyse</p><h1>Settings</h1>
        <p>The inventory script's settings, saved in aws_accounts.json through <code class="mono">aws_inventory.py config</code>; a terminal run uses them too.</p></div></header>
      <div class="grid">
        <section class="card"><h2>Database</h2>
          ${db.exists ? `<p style="margin:0"><strong>${bytes(db.bytes)}</strong> · ${plural(db.accounts, "account")} · ${number(db.resources)} resources · search ${esc(db.search || "?")}</p>` : `<p class="muted" style="margin:0">Not written yet.</p>`}
          <p class="mono muted" style="margin:0;overflow-wrap:anywhere">${esc(db.path)}</p>
          <p class="muted" style="margin:0;font-size:13px">A sync writes it at its end. A stopped sync, or one with the database off, leaves it behind; this catches it up.</p>
          <p id="db-message" class="notice" hidden></p>
          <div class="actions"><button class="button" type="button" id="update-db">Bring it up to date</button></div>
        </section>
        <section class="card"><h2>Files</h2>
          <p style="margin:0">Inventory folder: <strong>${bytes(data.folder_bytes)}</strong></p>
          <p class="mono muted" style="margin:0;overflow-wrap:anywhere">${esc(data.folder)}</p>
          ${Object.entries(data.files).map(([name, path]) => `<div class="link-row"><strong class="mono">${esc(name)}</strong><span class="muted mono" style="overflow-wrap:anywhere">${esc(path)}</span></div>`).join("")}
        </section>
      </div>
      <form class="settings" id="settings-form">
        ${SETTING_GROUPS.map(([title, fields]) => `<section class="card"><h2>${esc(title)}</h2>${fields.map(field).join("")}</section>`).join("")}
        <div class="save-bar"><span id="settings-message" class="muted" role="status"></span><button class="button primary" type="submit" id="save-settings" disabled>Save changes</button></div>
      </form>
      <div class="grid">
        <section class="card"><h2>Resource types</h2>
          <p style="margin:0"><strong>${number(data.commands.types)}</strong> types in aws_commands.json, across ${plural(Object.keys(data.commands.services).length, "service")}. Types AWS lists that it lacks are added at the end of a sync.</p>
          <div class="chips">${Object.entries(data.commands.services).map(([sv, n]) => `<span class="chip plain">${esc(sv)} ${number(n)}</span>`).join("")}</div>
        </section>
        <section class="card"><h2>Compare and projects</h2>
          <p style="margin:0">Environment words compare looks for: <span class="muted">${esc(data.compare.env_words.join(", "))}</span></p>
          <p style="margin:0">Shared-services accounts: <span class="muted">${esc(data.project.shared_accounts.join(", ") || "none listed")}; also any account named ${esc(data.project.shared_words.join(", "))}</span></p>
          <p class="muted" style="margin:0;font-size:13px">Change these in aws_compare.json and aws_project.json.</p>
        </section>
      </div>`;
    const form = $("#settings-form"), save = $("#save-settings"), message = $("#settings-message");
    const read = el => {
      const kind = el.closest("[data-kind]").dataset.kind;
      if (kind === "bool") return el.checked;
      if (kind === "list") return el.value.trim().toLowerCase() === "all" ? "all" : words(el.value);
      if (kind === "number") return Number(el.value);
      if (kind === "auto") return el.value.trim() === "auto" ? "auto" : Number(el.value);
      return el.value;
    };
    const changed = () => {
      const out = {};
      form.querySelectorAll("[name]").forEach(el => {
        const value = read(el);
        if (JSON.stringify(value) !== JSON.stringify(st[el.name])) out[el.name] = value;
      });
      return out;
    };
    const check = () => {
      const c = changed(), bad = Object.entries(c).filter(([, v]) => typeof v === "number" && (!Number.isFinite(v) || v < 0));
      save.disabled = !Object.keys(c).length || bad.length > 0;
      message.textContent = bad.length ? `Not a number: ${bad.map(([k]) => k).join(", ")}` : Object.keys(c).length ? `${plural(Object.keys(c).length, "change")} not saved` : "";
      message.className = bad.length ? "error-text" : "muted";
    };
    form.addEventListener("input", check);
    form.addEventListener("change", check);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const c = changed();
      busy(save, "Saving…");
      try {
        await send("/settings", "PUT", {values: c});
        await settingsPage();
        $("#settings-message").textContent = `Saved ${plural(Object.keys(c).length, "change")}.`;
      } catch (e) {
        message.textContent = e.message;
        message.className = "error-text";
        idleButton(save);
      }
    });
    $("#update-db").addEventListener("click", async event => {
      const button = event.target, box = $("#db-message");
      busy(button, "Updating…");
      try {
        const result = await send("/database/update", "POST");
        box.className = "notice";
        box.textContent = result.message;
      } catch (e) {
        box.className = "error";
        box.textContent = e.message;
      }
      box.hidden = false;
      idleButton(button);
    });
  }

  // ------------------------------------------------------------------ routing

  async function route() {
    const [path, query] = (location.hash.replace(/^#/, "") || "/").split("?");
    const parts = path.split("/").filter(Boolean);
    const page = parts[0] || "overview";
    const nav = page === "resource" ? "search" : page;
    document.querySelectorAll("[data-nav]").forEach(link => {
      if (link.dataset.nav === nav) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const titles = {search: "Search", resource: "Resource", accounts: "Accounts & permissions", changes: "Changes", sync: "Sync",
      compare: "Compare accounts", projects: "Projects", settings: "Settings"};
    document.title = `OWL · AWS Inventory · ${titles[page] || "Overview"}`;
    stopPolling();
    const params = new URLSearchParams(query || "");
    try {
      if (page === "overview") await overview();
      else if (page === "search") await search(params);
      else if (page === "resource" && parts[1]) await resource(parts[1], parts[2]);
      else if (page === "accounts" && parts[1]) await accountPage(decodeURIComponent(parts[1]));
      else if (page === "accounts") await accounts(params);
      else if (page === "changes") await changes(params);
      else if (page === "sync") await syncPage(params);
      else if (page === "compare" && parts[1]) await compareReport(decodeURIComponent(parts[1]), parts[2]);
      else if (page === "compare") await comparePage();
      else if (page === "projects" && parts[1]) await projectReport(decodeURIComponent(parts[1]), parts[2]);
      else if (page === "projects") await projectsPage();
      else if (page === "settings") await settingsPage();
      else await overview();
    } catch (error) {
      failed(error);
    }
    view.focus({preventScroll: true});
    window.scrollTo(0, 0);
  }

  window.addEventListener("hashchange", route);
  route();
})();
