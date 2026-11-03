"use strict";
// AWS Inventory: reads what scripts/aws_inventory.py downloaded (its inventory.db) through
// /api/aws-inventory. Pages are hash routes: #/ overview, #/search?q=…, #/resource/<id>.
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
              <td>${esc(a.profile)}${a.enabled ? "" : ` <span class="chip plain">not synced</span>`}${a.retry ? ` <span class="chip warn">retry flagged</span>` : ""}</td>
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

  // ------------------------------------------------------------------ the screens still to build

  const NEXT = {
    sync: ["Sync", "Start a sync, follow the List, Save and Database bars, and stop it cleanly.", "python scripts/aws_inventory.py"],
    accounts: ["Accounts & permissions", "Each account's regions, what it has no permission for, and retrying them.", "python scripts/aws_inventory.py permissions"],
    changes: ["Changes", "What each sync added, modified or removed, with the field diffs.", "python scripts/aws_inventory.py changes --diff"],
    compare: ["Compare accounts", "Did prod get built like nonp? Missing, extra and different, with blind spots first.", "python scripts/aws_compare.py"],
    projects: ["Projects", "A project's accounts, EKS, entry and exit points, hop-by-hop paths, DNS and diagram.", "python scripts/aws_project.py stablecoin"],
    settings: ["Settings", "Regions, speed, daily runs, DNS lookups, uncovered types and the database.", "python scripts/aws_inventory.py config"],
  };

  function next(key) {
    const [title, what, command] = NEXT[key];
    view.innerHTML = `
      <header class="head"><div><p class="eyebrow">Coming next</p><h1>${esc(title)}</h1><p>${esc(what)}</p></div></header>
      <div class="empty"><strong>This screen is not built in the app yet.</strong>
        <span>It works from a terminal today, in the owl folder:</span><code>${esc(command)}</code></div>`;
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
    document.title = `OWL · AWS Inventory · ${page === "resource" ? "Resource" : (NEXT[page] || [page === "search" ? "Search" : "Overview"])[0]}`;
    try {
      if (page === "overview") await overview();
      else if (page === "search") await search(new URLSearchParams(query || ""));
      else if (page === "resource" && parts[1]) await resource(parts[1], parts[2]);
      else if (NEXT[page]) next(page);
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
