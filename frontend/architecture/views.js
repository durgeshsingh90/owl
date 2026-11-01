"use strict";
// Page views. Each takes the main element and the app (state plus actions) and fills it.
AA.views = (() => {
  const {esc, status, mark, worst, flowLabel, title, when, meter, api, toast, CATEGORY_ORDER} = AA;

  const head = (heading, text, extra = "") =>
    `<div class="page-head"><div><h1>${esc(heading)}</h1>${text ? `<p>${text}</p>` : ""}</div>${extra}</div>`;
  const flowName = (r) => `${esc(r.source.name)} → ${esc(r.destination.name)}`;
  const reqRef = (r, env) => `data-open="${esc(r.requirement.id)}" data-env="${esc(env || r.environment)}"`;

  function wireOpen(root, app) {
    root.querySelectorAll("[data-open]").forEach((node) =>
      node.addEventListener("click", () => app.openRequirement(node.dataset.open, node.dataset.env)));
  }

  function categories(result) {
    const by = {};
    for (const check of result.checks) (by[check.category] ||= []).push(check.status);
    return CATEGORY_ORDER.filter((c) => by[c]).map((c) => [c, worst(by[c])]);
  }

  // Overview

  function overview(main, app) {
    const a = app.analysis();
    const h = a.health;
    const tile = (label, counts, unit = "PASS") => `<div class="card tile"><div class="label">${esc(label)}</div>
      <div class="value">${counts.pass}<small>/${counts.total} ${unit}</small></div>${meter(counts)}
      <div class="small muted">${[counts.fail && `${counts.fail} fail`, counts.warning && `${counts.warning} warning`, counts.unknown && `${counts.unknown} unknown`].filter(Boolean).join(" · ") || "&nbsp;"}</div></div>`;
    const critical = a.findings.filter((f) => f.severity === "critical" || f.severity === "high");
    const warnings = a.findings.filter((f) => f.severity === "warning");
    const unknowns = a.findings.filter((f) => f.severity === "info");
    const passed = a.results.filter((r) => r.status === "PASS");
    const findingList = (items, empty) => items.length ? `<ul class="issue-list">${items.map((f) => `<li>
        <span class="sev ${esc(f.severity)}">${esc(f.id)}</span>
        <div class="what"><button type="button" class="link" data-open="${esc(f.requirement)}" data-env="${esc(f.environment)}">${esc(f.title)}</button>
        <div class="cause">${esc(f.cause)}</div></div></li>`).join("")}</ul>` : `<p class="muted">${empty}</p>`;
    const envs = Object.keys(app.project().environments);
    main.innerHTML = head(app.project().name, `${esc(app.env)} · ${esc(app.project().description)}`,
      `<div class="small muted">Snapshot <b>${esc(a.snapshot.name)}</b> · ${esc(when(a.snapshot.created))}</div>`) + `
      <div class="grid tiles">
        <div class="card tile overall"><div class="label">Overall</div><div class="value">${h.percent}%<small> of checks pass</small></div>${meter(h.checks)}
          <div class="small muted">${h.checks.total} checks over ${h.connectivity.total} requirements</div></div>
        ${tile("Connectivity", h.connectivity)}${tile("DNS", h.dns)}${tile("Security", h.security)}${tile("Network", h.network)}
        ${tile("Return path & external", h.return_path)}${tile("Standards", h.standards)}
      </div>
      <div class="grid two section">
        <div class="card"><h2>Critical issues</h2>${findingList(critical, "No failing requirements in " + esc(app.env) + ".")}</div>
        <div class="card"><h2>Warnings</h2>${findingList(warnings, "No warnings.")}</div>
        <div class="card"><h2>Unknowns</h2>${findingList(unknowns, "Nothing is undetermined.")}</div>
        <div class="card"><h2>Passed</h2>${passed.length ? `<ul class="issue-list">${passed.map((r) => `<li>${mark("PASS")}<div class="what">
          <button type="button" class="link" ${reqRef(r)}>${flowName(r)}</button><div class="cause">${esc(r.requirement.direction)} · ${esc(flowLabel(r.requirement))}</div></div></li>`).join("")}</ul>`
          : `<p class="muted">No requirement passes yet.</p>`}</div>
      </div>
      <div class="section card"><h2>Environments</h2><div class="counts">${envs.map((env) => {
        const other = app.analyses[env];
        if (!other) return `<span>${esc(env)} <span class="muted">loading…</span></span>`;
        const s = other.health.connectivity;
        return `<span><a href="#overview" data-env-switch="${esc(env)}">${esc(env)}</a> <b>${s.pass}/${s.total}</b> pass${s.fail ? ` · <span style="color:var(--fail)">${s.fail} fail</span>` : ""}${s.unknown ? ` · ${s.unknown} unknown` : ""}</span>`;
      }).join("")}</div></div>`;
    wireOpen(main, app);
    main.querySelectorAll("[data-env-switch]").forEach((n) => n.addEventListener("click", (e) => {
      e.preventDefault();
      app.setEnv(n.dataset.envSwitch);
    }));
  }

  // Architecture graph

  function architecture(main, app) {
    const a = app.analysis();
    const filter = app.graphFilter;
    main.innerHTML = head("Architecture", `${esc(app.project().name)} · ${esc(app.env)} — every traced path. Click an edge for its analysis, a node to highlight its flows.`) + `
      <div class="graph-toolbar">
        <div class="segmented" role="group" aria-label="Direction">${["all", "inbound", "outbound"].map((d) =>
          `<button type="button" data-direction="${d}" aria-pressed="${filter.direction === d}">${title(d)}</button>`).join("")}</div>
        <div class="segmented" role="group" aria-label="Status">${["all", "FAIL", "UNKNOWN", "PASS"].map((s) =>
          `<button type="button" data-status="${s}" aria-pressed="${filter.status === s}">${s === "all" ? "Any status" : title(s.toLowerCase())}</button>`).join("")}</div>
        <button type="button" data-zoom="-1" aria-label="Zoom out">−</button><button type="button" data-zoom="1" aria-label="Zoom in">+</button>
        <div class="legend" style="margin-left:auto"><span><i style="background:var(--pass)"></i>Pass</span><span><i style="background:var(--fail)"></i>Fail</span>
          <span><i style="background:var(--warn)"></i>Warning</span><span><i style="background:var(--unknown)"></i>Unknown</span></div>
      </div>
      <div class="graph-wrap" id="graph"></div>
      <p class="small muted">Transit gateway hops are shown on the edges ("via TGW"). Return paths and checks are in each connection's analysis.</p>`;
    const results = a.results.filter((r) => (filter.direction === "all" || r.requirement.direction === filter.direction || r.requirement.direction === "bidirectional")
      && (filter.status === "all" || r.status === filter.status));
    const wrap = main.querySelector("#graph");
    let popover = null;
    const closePopover = () => {
      popover?.remove();
      popover = null;
    };
    const view = AA.graph.render(wrap, results, {
      scale: filter.scale,
      onEdge(edge, event) {
        closePopover();
        view.focus(edge.results);
        if (edge.results.length === 1) return app.openResult(edge.results[0]);
        popover = document.createElement("div");
        popover.className = "popover";
        const box = wrap.getBoundingClientRect();
        popover.style.left = Math.min(event.clientX - box.left + wrap.scrollLeft, wrap.scrollWidth - 300) + "px";
        popover.style.top = event.clientY - box.top + wrap.scrollTop + 10 + "px";
        popover.innerHTML = `<b>${esc(edge.results.length)} flows on this edge</b><ul>${edge.results.map((r, i) =>
          `<li>${status(r.status)}<button type="button" class="link" data-i="${i}">${flowName(r)} ${esc(flowLabel(r.requirement))}</button></li>`).join("")}</ul>`;
        popover.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => app.openResult(edge.results[+b.dataset.i])));
        wrap.appendChild(popover);
      },
      onNode(node) {
        closePopover();
        view.select(node.id);
        view.focus(node.results);
      },
    });
    wrap.addEventListener("click", () => {
      closePopover();
      view?.focus(null);
      view?.select(null);
    });
    main.querySelectorAll("[data-direction]").forEach((b) => b.addEventListener("click", () => {
      filter.direction = b.dataset.direction;
      architecture(main, app);
    }));
    main.querySelectorAll("[data-status]").forEach((b) => b.addEventListener("click", () => {
      filter.status = b.dataset.status;
      architecture(main, app);
    }));
    main.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      filter.scale = Math.min(2, Math.max(0.5, filter.scale + 0.15 * b.dataset.zoom));
      architecture(main, app);
    }));
  }

  // Connectivity matrix

  function rowsFor(app, allEnvironments) {
    const envs = allEnvironments ? Object.keys(app.project().environments) : [app.env];
    return envs.flatMap((env) => (app.analyses[env]?.results || []).map((r) => ({env, r})));
  }

  function matrixTable(rows, {showEnv, grouped}) {
    const directions = grouped ? ["inbound", "outbound", "bidirectional"] : [null];
    const cols = (showEnv ? 1 : 0) + 7;
    const body = directions.map((direction) => {
      const part = rows.filter(({r}) => !direction || r.requirement.direction === direction);
      if (!part.length) return "";
      return (direction ? `<tr class="group"><td colspan="${cols}">${esc(title(direction))}</td></tr>` : "") + part.map(({env, r}) => {
        const [protocol, port] = flowLabel(r.requirement).split("/");
        return `<tr class="clickable" ${reqRef(r, env)} tabindex="0">${showEnv ? `<td>${esc(env)}</td>` : ""}
          <td>${esc(r.source.name)}</td><td>${esc(r.destination.name)}</td><td>${esc(title(r.requirement.direction))}</td>
          <td>${esc(protocol)}</td><td>${esc(port || "")}</td><td>${status(r.status)}</td>
          <td class="small muted">${r.status === "PASS" ? "" : esc(r.summary)}</td></tr>`;
      }).join("");
    }).join("");
    return `<div class="table-wrap"><table class="data"><thead><tr>${showEnv ? "<th>Environment</th>" : ""}<th>Source</th><th>Destination</th>
      <th>Direction</th><th>Protocol</th><th>Port</th><th>Status</th><th>Why</th></tr></thead><tbody>${body ||
      `<tr><td colspan="${cols}" class="empty">No connections match.</td></tr>`}</tbody></table></div>`;
  }

  function connectivity(main, app) {
    const f = app.matrixFilter;
    const all = rowsFor(app, f.environment === "all");
    const options = (values, current, label) => `<option value="">${label}</option>` +
      [...new Set(values)].sort().map((v) => `<option ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("");
    const rows = all.filter(({r}) => (!f.direction || r.requirement.direction === f.direction) && (!f.status || r.status === f.status)
      && (!f.source || r.source.name === f.source) && (!f.destination || r.destination.name === f.destination)
      && (!f.protocol || flowLabel(r.requirement).split("/")[0] === f.protocol) && (!f.port || String(r.requirement.port ?? "") === f.port.trim()));
    main.innerHTML = head("Connectivity", "Required connections and what the supplied configuration says about each. Click a row for the full analysis.") + `
      <div class="filters">
        <select data-f="environment"><option value="current">${esc(app.env)} only</option><option value="all" ${f.environment === "all" ? "selected" : ""}>All environments</option></select>
        <select data-f="direction">${options(["inbound", "outbound", "bidirectional"], f.direction, "Any direction")}</select>
        <select data-f="status">${options(["PASS", "FAIL", "WARNING", "UNKNOWN"], f.status, "Any status")}</select>
        <select data-f="source">${options(all.map(({r}) => r.source.name), f.source, "Any source")}</select>
        <select data-f="destination">${options(all.map(({r}) => r.destination.name), f.destination, "Any destination")}</select>
        <select data-f="protocol">${options(all.map(({r}) => flowLabel(r.requirement).split("/")[0]), f.protocol, "Any protocol")}</select>
        <input data-f="port" type="search" placeholder="Port" value="${esc(f.port)}" inputmode="numeric" style="width:90px">
        <span class="small muted">${rows.length} of ${all.length}</span>
      </div>` + matrixTable(rows, {showEnv: f.environment === "all", grouped: true});
    main.querySelectorAll("[data-f]").forEach((input) => input.addEventListener(input.tagName === "INPUT" ? "change" : "input", async () => {
      f[input.dataset.f] = input.value;
      if (input.dataset.f === "environment" && input.value === "all") await app.loadAllEnvironments();
      connectivity(main, app);
    }));
    main.querySelectorAll("tr[data-open]").forEach((row) => row.addEventListener("keydown", (e) => e.key === "Enter" && row.click()));
    wireOpen(main, app);
  }

  // Requirements

  function requirements(main, app) {
    const a = app.analysis();
    main.innerHTML = head("Requirements", `What ${esc(app.project().name)} needs in ${esc(app.env)}, check by check.`) +
      `<div class="req-cards">${a.results.map((r) => `<button type="button" class="req-card" ${reqRef(r)}>
        <div style="display:flex;justify-content:space-between;gap:8px;width:100%"><span class="flow">${flowName(r)}</span>${status(r.status)}</div>
        <div class="meta">${esc(r.requirement.id)} · ${esc(r.requirement.direction.toUpperCase())} | ${esc(flowLabel(r.requirement))}${r.requirement.name ? " · " + esc(r.requirement.name) : ""}</div>
        <div class="cat-list">${categories(r).map(([c, s]) => `<span>${esc(c)}</span>${mark(s)}`).join("")}</div>
        <div class="summary">${esc(r.summary)}</div></button>`).join("")}</div>`;
    wireOpen(main, app);
  }

  // Findings

  function findings(main, app) {
    const a = app.analysis();
    const groups = [["Critical", (f) => f.severity === "critical" || f.severity === "high"], ["Warnings", (f) => f.severity === "warning"],
      ["Unknowns", (f) => f.severity === "info"]];
    main.innerHTML = head("Findings", `${esc(app.env)} — failures first, then what needs attention, then what the data can't confirm.`) +
      groups.map(([label, test]) => {
        const items = a.findings.filter(test);
        return `<div class="card section"><h2>${label} <span class="muted small">${items.length}</span></h2>${items.length ? `<ul class="issue-list">${items.map((f) => `<li>
          <span class="sev ${esc(f.severity)}">${esc(f.id)}</span><div class="what"><b>${esc(f.title)}</b>
          <div class="cause">Cause: ${esc(f.cause)}</div>
          <div class="cause">Affected: ${esc(f.environment)} · Requirement <button type="button" class="link" data-open="${esc(f.requirement)}" data-env="${esc(f.environment)}">${esc(f.requirement)}</button>${f.rule ? ` · Rule ${esc(f.rule)}` : ""}</div>
          </div></li>`).join("")}</ul>` : `<p class="muted">None.</p>`}</div>`;
      }).join("");
    wireOpen(main, app);
  }

  // Environments

  function environments(main, app) {
    const project = app.project();
    const byAccount = {};
    for (const [env, binding] of Object.entries(project.environments)) (byAccount[binding.account_id] ||= []).push(env);
    const names = Object.fromEntries((app.analysis().accounts || []).map((a) => [a.id, a]));
    main.innerHTML = head("Environments", "Environments grouped by the AWS account they run in. An account can host several environments.") +
      `<div class="env-grid">${Object.entries(byAccount).map(([account, envs]) => `<div class="account-group">
        <div><b>${esc(names[account]?.name || account)}</b> <span class="acct mono">${esc(account)}</span>
        ${envs.length > 1 ? `<span class="shared-flag">Shared by ${envs.length} environments</span>` : ""}
        ${names[account]?.missing ? `<div class="small" style="color:var(--fail)">Not in the snapshot</div>` : ""}</div>
        ${envs.map((env) => {
          const c = app.analyses[env]?.health.connectivity;
          return `<button type="button" class="req-card" data-env-switch="${esc(env)}"><span class="flow">${esc(env)}</span>
            <span class="meta">Account ${esc(account)}</span>${c ? `<span>${c.pass}/${c.total} connections pass${c.fail ? ` · ${c.fail} fail` : ""}${c.unknown ? ` · ${c.unknown} unknown` : ""}</span>${meter(c)}` : `<span class="muted">Loading…</span>`}</button>`;
        }).join("")}</div>`).join("")}</div>`;
    main.querySelectorAll("[data-env-switch]").forEach((n) => n.addEventListener("click", () => {
      app.setEnv(n.dataset.envSwitch);
      location.hash = "#overview";
    }));
  }

  // Accounts

  const tableBase = (rows, columns) => rows.length ? `<table class="data"><thead><tr>${columns.map(([label]) => `<th>${esc(label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${columns.map(([, value]) => `<td>${value(row)}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<p class="muted small">None.</p>`;
  const table = tableBase;
  const section = (label, count, body, open = false) => `<details class="inv" ${open ? "open" : ""}><summary>${esc(label)} <span class="muted">${count}</span></summary><div class="inner">${body}</div></details>`;
  const ports = (r) => r.from_port == null || r.from_port === -1 ? "all" : r.from_port === r.to_port ? r.from_port : `${r.from_port}-${r.to_port}`;
  const ruleText = (rules) => rules.map((r) => `${esc(r.protocol)} ${esc(ports(r))} ${esc([...r.cidrs, ...r.groups, ...r.prefix_lists].join(", "))}`).join("<br>") || `<span class="muted">none</span>`;
  const mono = (v) => `<span class="mono">${esc(v)}</span>`;

  async function accounts(main, app) {
    main.innerHTML = head("Accounts", "Architecture inventory from the selected snapshot, by account.") + `<div class="loading">Loading…</div>`;
    let list;
    try {
      list = (await api(`/snapshots/${encodeURIComponent(app.snapshotId())}/accounts`)).accounts;
    } catch (error) {
      main.querySelector(".loading").outerHTML = `<div class="error-box">${esc(error.message)}</div>`;
      return;
    }
    const projectAccounts = new Set(Object.values(app.project().environments).map((b) => b.account_id));
    list.sort((a, b) => (projectAccounts.has(b.id) - projectAccounts.has(a.id)) || a.id.localeCompare(b.id));
    const current = app.accountId && list.some((a) => a.id === app.accountId) ? app.accountId : list[0]?.id;
    main.innerHTML = head("Accounts", "Architecture inventory from the selected snapshot, by account.",
      `<select id="account-select" aria-label="Account">${list.map((a) => `<option value="${esc(a.id)}" ${a.id === current ? "selected" : ""}>${esc(a.name || a.id)} · ${esc(a.id)}${projectAccounts.has(a.id) ? " (project)" : ""}</option>`).join("")}</select>`) +
      `<div id="account-body"><div class="loading">Loading…</div></div>`;
    main.querySelector("#account-select").addEventListener("change", (e) => {
      app.accountId = e.target.value;
      accounts(main, app);
    });
    if (!current) {
      main.querySelector("#account-body").innerHTML = `<div class="empty">No accounts in this snapshot.</div>`;
      return;
    }
    const d = await api(`/snapshots/${encodeURIComponent(app.snapshotId())}/accounts/${encodeURIComponent(current)}`);
    const stamps = d.timestamps || {};
    const short = (iso) => new Date(iso).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
    const timeCell = (id) => {
      const t = stamps[id];
      if (!t) return '<span class="muted small">unknown</span>';
      const tip = [t.created && `Created ${t.created} (${t.created_source})${t.created_by ? " by " + t.created_by : ""}`,
        t.updated && `Updated ${t.updated} (${t.updated_source})${t.updated_by ? " by " + t.updated_by : ""}${t.updated_event ? " · " + t.updated_event : ""}`].filter(Boolean).join("\n");
      return `<span class="small" title="${esc(tip)}">${t.created ? `Created ${esc(short(t.created))}` : ""}${t.created && t.updated ? "<br>" : ""}${t.updated ? `Updated ${esc(short(t.updated))}` : ""}${t.updated_by ? `<br><span class="muted">${esc(t.updated_by)}</span>` : ""}</span>`;
    };
    const table = (rows, columns) => tableBase(rows, [...columns, ["Created / updated", (row) => timeCell(row.id)]]);
    const tableFor = Object.fromEntries(d.route_tables.flatMap((t) => t.subnets.map((s) => [s, t.id])));
    const naclFor = Object.fromEntries(d.nacls.flatMap((n) => n.subnets.map((s) => [s, n.id])));
    const acct = d.account;
    main.querySelector("#account-body").innerHTML = `<div class="card" style="margin-bottom:12px"><dl class="kv">
        <dt>Account</dt><dd>${mono(acct.id)} ${esc(acct.name)}</dd><dt>Type</dt><dd>${esc(title(acct.type))}</dd>
        <dt>Region</dt><dd>${esc(acct.region || "—")}</dd><dt>Environments</dt><dd>${esc((acct.environments || []).join(", ") || "—")}</dd></dl></div>` +
      section("VPCs", d.vpcs.length, table(d.vpcs, [["VPC", (v) => mono(v.id)], ["CIDR", (v) => esc(v.cidrs.join(", "))], ["Role", (v) => esc(title(v.role))],
        ["DNS support", (v) => esc(v.dns_support ?? "not supplied")], ["Name", (v) => esc(v.tags.Name || "")]]), true) +
      section("Subnets", d.subnets.length, table(d.subnets, [["Subnet", (s) => mono(s.id)], ["CIDR", (s) => esc(s.cidr)], ["AZ", (s) => esc(s.az)],
        ["Route table", (s) => mono(tableFor[s.id] || "main")], ["NACL", (s) => mono(naclFor[s.id] || "default")], ["Public", (s) => (s.public ? "Yes" : "")], ["Name", (s) => esc(s.tags.Name || "")]])) +
      section("Route tables", d.route_tables.length, table(d.route_tables, [["Route table", (t) => mono(t.id) + (t.main ? ' <span class="muted small">main</span>' : "")],
        ["Routes", (t) => t.routes.map((r) => `${esc(r.destination)} → ${mono(r.target)}${r.state !== "active" ? ` <span class="status FAIL">${esc(r.state)}</span>` : ""}`).join("<br>")]])) +
      section("TGW attachments", d.tgw_attachments.length, table(d.tgw_attachments, [["Attachment", (a) => mono(a.id)], ["Type", (a) => esc(a.type)],
        ["Resource", (a) => mono(a.resource)], ["Route table", (a) => mono(a.route_table || "—")], ["State", (a) => esc(a.state)]])) +
      (d.transit_gateways.length ? section("Transit gateways", d.transit_gateways.length, table(d.transit_gateways, [["TGW", (t) => mono(t.id)], ["Name", (t) => esc(t.tags.Name || "")]])) +
        section("TGW route tables", d.tgw_route_tables.length, table(d.tgw_route_tables, [["Route table", (t) => `${mono(t.id)} ${esc(t.name)}`],
          ["Routes", (t) => t.routes.map((r) => `${esc(r.destination)} → ${mono(r.attachment || "blackhole")} <span class="muted small">${esc(r.type)}</span>`).join("<br>")]])) : "") +
      section("Security groups", d.security_groups.length, table(d.security_groups, [["Group", (g) => `${mono(g.id)}<br><span class="small muted">${esc(g.name)}</span>`],
        ["Inbound", (g) => ruleText(g.inbound)], ["Outbound", (g) => ruleText(g.outbound)]])) +
      section("NACLs", d.nacls.length, table(d.nacls, [["NACL", (n) => mono(n.id)],
        ...["inbound", "outbound"].map((dir) => [title(dir), (n) => n[dir].map((r) => `#${r.number === 32767 ? "*" : r.number} ${esc(r.action)} ${esc(r.protocol)} ${esc(r.cidr)}${r.from_port != null ? " " + esc(ports(r)) : ""}`).join("<br>")])])) +
      section("Load balancers", d.load_balancers.length, table(d.load_balancers, [["Load balancer", (l) => `${esc(l.name)}<br><span class="small muted">${esc(l.type)} · ${esc(l.scheme)}</span>`],
        ["Listeners", (l) => l.listeners.map((x) => `${esc(x.protocol)}/${esc(x.port)}`).join(", ") || "none"], ["Subnets", (l) => l.subnets.map(mono).join("<br>")]])) +
      section("NAT gateways", d.nat_gateways.length, table(d.nat_gateways, [["NAT", (n) => mono(n.id)], ["Subnet", (n) => mono(n.subnet)], ["Public IPs", (n) => esc(n.public_ips.join(", "))]])) +
      section("Internet gateways", d.internet_gateways.length, table(d.internet_gateways, [["IGW", (i) => mono(i.id)], ["VPCs", (i) => esc(i.vpcs.join(", "))]])) +
      section("Firewalls", d.firewalls.length, table(d.firewalls, [["Firewall", (f) => esc(f.name)], ["Endpoints", (f) => esc(f.endpoints.join(", "))],
        ["Rule groups", (f) => f.rule_groups.map((g) => `${esc(g.name)} <span class="muted small">${g.rules.length} rules</span>`).join("<br>")], ["Default", (f) => esc(f.default_actions.join(", "))]])) +
      section("VPC endpoints", d.vpc_endpoints.length, table(d.vpc_endpoints, [["Endpoint", (e) => mono(e.id)], ["Type", (e) => esc(e.type)], ["Service", (e) => esc(e.service)]])) +
      section("DNS lookups (nslookup)", d.dns.lookups.length, tableBase(d.dns.lookups, [["Name", (l) => mono(l.name)], ["IP addresses", (l) => esc(l.ips.join(", ")) || "—"],
        ["Result", (l) => ({resolved: status("PASS", "Resolved"), timeout: status("UNKNOWN", "Timed out"), not_tried: status("UNKNOWN", "Not tried"), not_found: status("WARNING", "Not found")}[l.status] || status("UNKNOWN", l.status)) + (l.detail ? `<div class="small muted">${esc(l.detail)}</div>` : "")],
        ["From", (l) => esc(l.source)], ["Looked up", (l) => esc(new Date(l.looked_up_at).toLocaleString())]])) +
      section("DNS associations", d.dns.hosted_zones.length + d.dns.resolver_rules.length,
        table(d.dns.hosted_zones, [["Hosted zone", (z) => `${esc(z.name)} ${z.private ? '<span class="muted small">private</span>' : ""}`], ["VPCs", (z) => esc(z.vpcs.join(", "))], ["Records", (z) => z.records.length]]) +
        table(d.dns.resolver_rules, [["Resolver rule", (r) => `${mono(r.id)} ${esc(r.domain)}`], ["Targets", (r) => esc(r.target_ips.join(", "))], ["VPCs", (r) => esc(r.vpcs.join(", "))]]));
    // Every resource with all its properties, opened one at a time because accounts can be large.
    const box = document.createElement("details");
    box.className = "inv";
    box.innerHTML = `<summary>All resource properties <span class="muted">state.json</span></summary><div class="inner"><div class="loading">Loading…</div></div>`;
    main.querySelector("#account-body").appendChild(box);
    box.addEventListener("toggle", async () => {
      if (!box.open || box.dataset.loaded) return;
      box.dataset.loaded = "1";
      const inner = box.querySelector(".inner");
      let state;
      try {
        state = await api(`/download/state/${encodeURIComponent(current)}`);
      } catch (error) {
        inner.innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
        return;
      }
      const types = [...new Set(state.resources.map((r) => r.type))];
      inner.innerHTML = `<p class="small muted">${state.resources.length.toLocaleString()} resources · generated ${esc(new Date(state.generated_at).toLocaleString())}</p>` +
        types.map((type) => {
          const rows = state.resources.map((r, i) => [r, i]).filter(([r]) => r.type === type);
          return `<details class="inv"><summary>${esc(type)} <span class="muted">${rows.length}</span></summary><div class="inner">${rows.map(([r, i]) =>
            `<details data-resource="${i}"><summary class="mono">${esc(r.name ? `${r.name} · ` : "")}${esc(String(r.id).split("/").pop())}${r.created_at ? ` <span class="muted">created ${esc(short(r.created_at))}</span>` : ""}</summary><pre class="json"></pre></details>`).join("")}</div></details>`;
        }).join("");
      inner.querySelectorAll("[data-resource]").forEach((item) => item.addEventListener("toggle", () => {
        const pre = item.querySelector("pre");
        if (item.open && !pre.textContent) pre.textContent = JSON.stringify(state.resources[+item.dataset.resource].attributes, null, 2);
      }));
    });
  }

  // TAD

  async function tad(main, app) {
    await app.loadAllEnvironments();
    const catalog = await app.catalog();
    const a = app.analysis();
    const project = app.project();
    const results = a.results;
    const ids = new Set(results.flatMap((r) => [r.source.id, r.destination.id]));
    const services = catalog.services.filter((s) => ids.has(s.id) || project.shared_services.includes(s.id) || project.central_services.includes(s.id));
    const externals = catalog.external.filter((e) => ids.has(e.id));
    const byDir = (d) => results.filter((r) => r.requirement.direction === d || r.requirement.direction === "bidirectional");
    const checksOf = (cats) => results.flatMap((r) => r.checks.filter((c) => cats.includes(c.category)).map((c) => ({r, c})));
    const sections = [
      ["Project Overview", `<p>${esc(project.description)}</p><dl class="kv"><dt>Project</dt><dd>${esc(project.name)} (${mono(project.id)})</dd>
        <dt>Environment</dt><dd>${esc(app.env)}</dd><dt>AWS snapshot</dt><dd>${esc(a.snapshot.name)} · ${esc(when(a.snapshot.created))}</dd>
        <dt>Basis</dt><dd>Exported AWS configuration, analysed offline. Results state what the configuration supports; no connection was tested.</dd></dl>`],
      ["Architecture Overview", `<p>${esc(project.name)} has ${project.applications.length} application component(s) and ${results.length} connectivity requirement(s) in ${esc(app.env)};
        ${a.health.connectivity.pass} pass, ${a.health.connectivity.fail} fail, ${a.health.connectivity.unknown} cannot be determined (${a.health.percent}% of checks pass).</p>
        ${table(project.applications, [["Component", (w) => esc(w.name)], ["Description", (w) => esc(w.description)], ["Placement", (w) => {
          const p = w.placement[app.env] || w.placement["*"];
          return p ? `${mono(p.account)} ${mono(p.vpc)}${p.load_balancer ? ` behind ${esc(p.load_balancer)}` : ""}` : `<span class="muted">not in ${esc(app.env)}</span>`;
        }]])}`],
      ["Environment Architecture", table(Object.entries(project.environments), [["Environment", ([e]) => esc(e)], ["AWS account", ([, b]) => mono(b.account_id)],
        ["Shares account with", ([e, b]) => esc(Object.entries(project.environments).filter(([o, x]) => o !== e && x.account_id === b.account_id).map(([o]) => o).join(", ") || "—")]])],
      ["AWS Accounts", table([...a.accounts, ...services.map((s) => ({id: (s.placement["*"] || Object.values(s.placement)[0] || {}).account, name: s.name, type: s.kind}))]
        .filter((x, i, all) => x.id && all.findIndex((y) => y.id === x.id) === i), [["Account", (x) => mono(x.id)], ["Name", (x) => esc(x.name)], ["Type", (x) => esc(title(x.type || ""))]])],
      ["Network Architecture", `<p>Paths traced through the supplied routing:</p><ul>${[...new Set(results.map((r) => r.path.map((h) => h.label).join(" → ")))].map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`],
      ["Inbound Connectivity", matrixTable(byDir("inbound").map((r) => ({env: app.env, r})), {})],
      ["Outbound Connectivity", matrixTable(byDir("outbound").map((r) => ({env: app.env, r})), {})],
      ["Shared Services", table(services.filter((s) => !s.kind.startsWith("central")), [["Service", (s) => esc(s.name)], ["Description", (s) => esc(s.description)],
        ["Requirements", (s) => results.filter((r) => r.destination.id === s.id || r.source.id === s.id).map((r) => `${esc(r.requirement.id)} ${status(r.status)}`).join(" ") || "—"]]) +
        `<h3 style="margin-top:12px">Central services</h3>` + table(services.filter((s) => s.kind.startsWith("central")), [["Service", (s) => esc(s.name)], ["Description", (s) => esc(s.description)]])],
      ["DNS", table(checksOf(["DNS"]), [["Requirement", ({r}) => esc(r.requirement.id)], ["Name", ({c}) => esc(c.name.replace("Resolve ", ""))], ["Result", ({c}) => status(c.status)],
        ["Resolution", ({c}) => esc(c.actual).replace(/\n/g, "<br>")]])],
      ["On-Premises Connectivity", table(externals.filter((e) => e.type === "on-prem"), [["System", (e) => esc(e.name)], ["Networks", (e) => esc(e.networks.join(", "))],
        ["Connectivity", (e) => esc(e.connectivity?.type || "—")], ["Return routes", (e) => e.return_routes ? esc(e.return_routes.join(", ")) : `<span class="muted">not supplied</span>`],
        ["Requirements", (e) => results.filter((r) => r.destination.id === e.id).map((r) => `${esc(r.requirement.id)} ${status(r.status)}`).join(" ")]])],
      ["Third-Party Connectivity", table(externals.filter((e) => e.type === "third-party"), [["System", (e) => esc(e.name)], ["Endpoints", (e) => esc(e.endpoints.map((x) => `${x.hostname || ""} ${x.ip || "(no IP)"}`).join(", "))],
        ["Allowlist", (e) => e.allowlist ? esc(e.allowlist.join(", ")) : `<span class="muted">not supplied</span>`],
        ["Requirements", (e) => results.filter((r) => r.destination.id === e.id).map((r) => `${esc(r.requirement.id)} ${status(r.status)}`).join(" ")]])],
      ["Security", table(results, [["Connection", (r) => flowName(r)], ...["Security Group", "NACL", "Firewall"].map((cat) => [cat, (r) => {
        const list = r.checks.filter((c) => c.category === cat);
        return list.length ? mark(worst(list.map((c) => c.status))) + ` <span class="small muted">${list.length}</span>` : "—";
      }])]) + `<ul>${checksOf(["Security Group", "NACL", "Firewall"]).filter(({c}) => c.status === "FAIL").map(({r, c}) => `<li><b>${esc(r.requirement.id)}</b> ${esc(c.name)}: ${esc(c.actual)}</li>`).join("")}</ul>`],
      ["Architecture Rules", table(catalog.rules, [["Rule", (x) => `${mono(x.id)}<br>${esc(x.name)}`], ["Result", (x) => {
        const list = checksOf(["Standards"]).filter(({c}) => c.rule === x.id);
        return list.length ? list.map(({r, c}) => `${esc(r.requirement.id)} ${status(c.status)}`).join(" ") : `<span class="muted">not applicable</span>`;
      }]])],
      ["Findings", `<ul>${a.findings.filter((f) => f.severity === "critical" || f.severity === "high").map((f) => `<li><b>${esc(f.id)}</b> ${esc(f.title)} <span class="muted">${esc(f.cause)}</span></li>`).join("") || "<li>None.</li>"}</ul>`],
      ["Risks", `<ul>${[...a.findings.filter((f) => f.severity !== "info").map((f) => `${f.title} — ${f.cause}`),
        ...checksOf(["Return Path"]).filter(({c}) => c.status === "WARNING").map(({r, c}) => `${r.requirement.id}: ${c.actual.split("\n").pop()}`)]
        .filter((x, i, all) => all.indexOf(x) === i).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>None identified from the supplied data.</li>"}</ul>`],
      ["Unknowns / Assumptions", `<ul><li>Results are based on exported configuration only; live state, OS firewalls and application behaviour are not covered.</li>
        <li>Where the data does not show something, the result is UNKNOWN rather than PASS.</li>
        ${results.flatMap((r) => r.checks.filter((c) => c.status === "UNKNOWN").map((c) => `${r.requirement.id}: ${c.name} — ${c.actual.split("\n").pop()}`))
          .filter((x, i, all) => all.indexOf(x) === i).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`],
      ["Connectivity Matrix", matrixTable(rowsFor(app, true), {showEnv: true})],
      ["Architecture Diagram", `<div class="graph-wrap" id="tad-graph"></div>`],
    ];
    main.innerHTML = `<div class="tad">` + head(`${project.name} — Technical Architecture`, `${esc(app.env)} · generated from snapshot ${esc(a.snapshot.name)}`,
      `<button type="button" class="no-print" id="tad-print">Print / save as PDF</button>`) +
      `<ol class="toc">${sections.map(([name], i) => `<li><a href="#tad" data-section="${i}">${esc(name)}</a></li>`).join("")}</ol>` +
      sections.map(([name, body], i) => `<section id="tad-${i}"><h2>${i + 1}. ${esc(name)}</h2>${body}</section>`).join("") + `</div>`;
    AA.graph.render(main.querySelector("#tad-graph"), results, {scale: 0.85});
    main.querySelector("#tad-print").addEventListener("click", () => window.print());
    main.querySelectorAll("[data-section]").forEach((link) => link.addEventListener("click", (e) => {
      e.preventDefault();
      main.querySelector("#tad-" + link.dataset.section).scrollIntoView({behavior: "smooth"});
    }));
    main.querySelectorAll("tr[data-open]").forEach((row) => row.addEventListener("click", () => app.openRequirement(row.dataset.open, row.dataset.env)));
  }

  // Import

  function importView(main, app) {
    let files = [];
    main.innerHTML = head("Import AWS data", "Upload exported AWS configuration as JSON. It is validated, normalized and stored as a new snapshot; AWS itself is never contacted.") + `
      <div class="drop" id="drop">
        <b>Drop an export folder or JSON files here</b>
        <p class="small muted">AWS CLI output (describe-vpcs, describe-route-tables, describe-security-groups, describe-network-acls, describe-transit-gateway-*,
        elbv2 describe-*, network-firewall describe-*, route53, route53resolver), or one object combining several. Put each account in a folder named with its 12-digit id,
        or include an <span class="mono">Account</span> object.</p>
        <div class="actions"><label><input type="file" id="folder-input" webkitdirectory multiple hidden><span class="pill"><button type="button" id="pick-folder">Upload folder</button></span></label>
          <input type="file" id="file-input" accept=".json,application/json" multiple hidden><button type="button" id="pick-files">Choose files</button></div>
      </div>
      <div id="import-result" class="section"></div>
      <div class="section"><h2>Snapshots</h2><div class="table-wrap"><table class="data"><thead><tr><th>Snapshot</th><th>Imported</th><th>Files</th><th>Accounts</th><th>VPCs</th><th>Warnings</th><th></th></tr></thead>
      <tbody>${app.overview.snapshots.map((s) => `<tr><td>${esc(s.name)}<br><span class="mono muted">${esc(s.id)}</span></td><td>${esc(when(s.created))}</td><td>${s.files}</td>
        <td>${s.counts.accounts.count}</td><td>${s.counts.vpcs.count}</td><td>${s.warnings.length}</td>
        <td><button type="button" class="danger" data-delete="${esc(s.id)}">Delete</button></td></tr>`).join("")}</tbody></table></div>
      <p class="small muted">Stored in <span class="mono">${esc(app.overview.folder)}</span>.</p></div>`;
    const result = main.querySelector("#import-result");
    const folderInput = main.querySelector("#folder-input");
    const fileInput = main.querySelector("#file-input");
    main.querySelector("#pick-folder").addEventListener("click", () => folderInput.click());
    main.querySelector("#pick-files").addEventListener("click", () => fileInput.click());

    async function read(list) {
      const json = [...list].filter((f) => /\.json$/i.test(f.name));
      if (!json.length) return toast("No .json files found.");
      result.innerHTML = `<div class="loading">Reading ${json.length} file(s)…</div>`;
      files = await Promise.all(json.map(async (f) => ({name: f.webkitRelativePath || f.relativePath || f.name, content: await f.text()})));
      let check;
      try {
        check = await api("/snapshots/validate", {method: "POST", body: {files}});
      } catch (error) {
        result.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
        return;
      }
      const counts = Object.values(check.counts);
      result.innerHTML = `<div class="card"><h2>Validation</h2><p>${check.files} file(s) read.</p>
        <div class="grid tiles">${counts.map((c) => `<div><div class="small muted">${esc(c.label)}</div><b>${c.count.toLocaleString()}</b></div>`).join("")}</div>
        ${check.errors.length ? `<h3 class="section" style="color:var(--fail)">${check.errors.length} error(s) — fix these before importing</h3><ul class="messages errors">${check.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
        ${check.warnings.length ? `<h3 class="section">${check.warnings.length} warning(s)</h3><ul class="messages warnings">${check.warnings.slice(0, 200).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
        <div class="filters section"><input id="snapshot-name" placeholder="Snapshot name" value="${esc(new Date().toISOString().slice(0, 10))} export">
        <button type="button" class="primary" id="do-import" ${check.errors.length ? "disabled" : ""}>Import snapshot</button></div></div>`;
      result.querySelector("#do-import").addEventListener("click", async (event) => {
        event.target.disabled = true;
        try {
          const meta = await api("/snapshots", {method: "POST", body: {name: result.querySelector("#snapshot-name").value, files}});
          toast(`Imported ${meta.name}.`);
          await app.reload(meta.id);
          location.hash = "#overview";
        } catch (error) {
          event.target.disabled = false;
          toast(error.message);
        }
      });
    }

    folderInput.addEventListener("change", () => read(folderInput.files));
    fileInput.addEventListener("change", () => read(fileInput.files));
    const drop = main.querySelector("#drop");
    drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      drop.classList.add("over");
    });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
      if (!entries.length) return read(e.dataTransfer.files);
      const collected = [];
      const walk = (entry, path) => new Promise((resolve) => {
        if (entry.isFile) {
          entry.file((file) => {
            file.relativePath = path + file.name;
            collected.push(file);
            resolve();
          }, resolve);
          return;
        }
        const reader = entry.createReader();
        const all = [];
        const next = () => reader.readEntries(async (batch) => {
          if (!batch.length) {
            await Promise.all(all.map((child) => walk(child, path + entry.name + "/")));
            resolve();
            return;
          }
          all.push(...batch);
          next();
        }, resolve);
        next();
      });
      await Promise.all(entries.map((entry) => walk(entry, "")));
      read(collected);
    });
    main.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`Delete snapshot ${b.dataset.delete}? This removes its stored inventory.`)) return;
      try {
        await api(`/snapshots/${encodeURIComponent(b.dataset.delete)}`, {method: "DELETE"});
        await app.reload();
        importView(main, app);
      } catch (error) {
        toast(error.message);
      }
    }));
  }

  // Standards

  async function standards(main, app) {
    const catalog = await app.catalog();
    const a = app.analysis();
    main.innerHTML = head("Standards", `Architecture rules applied to every requirement they match. Add or override rules as JSON in <span class="mono">${esc(app.overview.folder)}/rules</span>.`) +
      catalog.rules.map((rule) => {
        const applied = a.results.flatMap((r) => r.checks.filter((c) => c.rule === rule.id).map((c) => ({r, c})));
        return `<div class="card section"><div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">
          <div><h3>${esc(rule.name)}</h3><div class="mono muted">${esc(rule.id)}</div></div><span class="sev ${rule.severity === "critical" ? "critical" : "high"}">${esc(rule.severity)}</span></div>
          <p class="small">${esc(rule.description)}</p>
          <div class="small muted">Applies to: ${esc([rule.direction && `${rule.direction} traffic`, ...Object.entries(rule.conditions).map(([k, v]) => `${k.replace(/_/g, " ")} ${v.join(" or ")}`)].filter(Boolean).join(" · ") || "every requirement")}</div>
          ${rule.required_path.length ? `<div class="rule-path">Required: ${rule.required_path.map((s) => `<span class="step">${esc(s)}</span>`).join('<span class="muted">→</span>')}</div>` : ""}
          ${rule.forbidden_path.length ? `<div class="rule-path">Forbidden: ${rule.forbidden_path.map((s) => `<span class="step banned">${esc(s)}</span>`).join("")}</div>` : ""}
          <div class="rule-path">${esc(app.env)}: ${applied.length ? applied.map(({r, c}) => `<button type="button" class="link" ${reqRef(r)}>${esc(r.requirement.id)}</button> ${status(c.status)}`).join(" ") : '<span class="muted">no matching requirements</span>'}</div>
        </div>`;
      }).join("");
    wireOpen(main, app);
  }

  // The connection analysis drawer

  function result(r) {
    const verdicts = {
      PASS: "Based on the supplied AWS configuration, this connectivity should work.",
      FAIL: "Based on the supplied AWS configuration, this connectivity will not work.",
      WARNING: "The configuration supports this connectivity, with an architectural concern.",
      UNKNOWN: "The supplied data is not enough to decide whether this connectivity works.",
    };
    const ribbon = (hops) => hops.map((h) => `<span class="hop" title="${esc(h.id)}">${esc(h.label)}</span>`).join('<span class="arrow">→</span>');
    const check = (c, i) => {
      const open = c.status === "FAIL" || (c.status !== "PASS" && c.blocking);
      return `<div class="check ${esc(c.status)}"><button type="button" aria-expanded="${open}" data-check="${i}">${mark(c.status)}
        <span class="cname">${esc(c.name)}<small>${esc((c.actual || "").split("\n").find((l) => l.startsWith("✗")) || (c.actual || "").split("\n")[0])}</small></span>
        <span class="cat">${esc(c.category)}${c.blocking ? "" : ' · <span class="advisory">advisory</span>'}</span></button>
        <dl class="check-detail" ${open ? "" : "hidden"}>
          ${c.detail ? `<dt>Rule</dt><dd>${esc(c.detail)}</dd>` : ""}
          <dt>Expected</dt><dd>${esc(c.expected || "—")}</dd><dt>Actual</dt><dd>${esc(c.actual || "—")}</dd>
          ${c.evidence.length ? `<dt>Evidence</dt><dd class="chips">${c.evidence.map((e) => `<button type="button" class="chip" data-kind="${esc(e.kind)}" data-id="${esc(e.id)}" title="${esc(e.kind)}">${esc(e.label ? `${e.id.split("/").pop()} · ${e.label}` : e.id.split("/").pop())}</button>`).join("")}</dd>` : ""}
        </dl></div>`;
    };
    const endpoint = (e) => `<dl class="kv"><dt>${esc(e.name)}</dt><dd>${esc(title(e.kind))}${e.role && e.role !== e.kind ? " · " + esc(title(e.role)) : ""}</dd>
      ${e.account ? `<dt>Account</dt><dd class="mono">${esc(e.account)}</dd>` : ""}${e.vpc ? `<dt>VPC</dt><dd class="mono">${esc(e.vpc)}</dd>` : ""}
      ${e.subnets.length ? `<dt>Subnets</dt><dd class="mono">${esc(e.subnets.join(", "))}</dd>` : ""}
      ${e.security_groups.length ? `<dt>Security groups</dt><dd class="mono">${esc(e.security_groups.join(", "))}</dd>` : ""}
      ${e.load_balancer ? `<dt>Load balancer</dt><dd>${esc(e.load_balancer)}</dd>` : ""}
      ${e.networks.length ? `<dt>Addresses</dt><dd class="mono">${esc(e.networks.join(", "))}</dd>` : ""}${e.hostname ? `<dt>Hostname</dt><dd class="mono">${esc(e.hostname)}</dd>` : ""}</dl>`;
    const req = r.requirement;
    return {
      title: `<div class="flow">${esc(r.source.name)} → ${esc(r.destination.name)}</div><div class="meta">${status(r.status)}
        <span>${esc(r.environment)}</span><span>${esc(req.direction.toUpperCase())}</span><span>${esc(flowLabel(req))}</span><span class="mono">${esc(req.id)}</span></div>`,
      body: `<div class="verdict ${esc(r.status)}"><b>${esc(verdicts[r.status])}</b><br>${r.status === "PASS" ? "" : esc(r.summary)}</div>
        <div class="group-title">Path</div><div class="path-ribbon">${ribbon(r.path)}</div>
        ${r.return_path.length ? `<div class="group-title">Return path</div><div class="path-ribbon">${ribbon(r.return_path)}</div>` : ""}
        <div class="group-title" style="display:flex;justify-content:space-between">Checks <label class="small" style="text-transform:none;letter-spacing:0;font-weight:400"><input type="checkbox" id="only-problems"> Problems only</label></div>
        <div id="checks">${r.checks.map(check).join("")}</div>
        <div class="group-title">Requirement</div><dl class="kv"><dt>Name</dt><dd>${esc(req.name || "—")}</dd>${req.hostname ? `<dt>Hostname</dt><dd class="mono">${esc(req.hostname)}</dd>` : ""}
          ${req.target_port ? `<dt>Target port</dt><dd>${esc(req.target_port)}</dd>` : ""}<dt>Required</dt><dd>${req.required ? "Yes" : "No"}</dd>${req.notes ? `<dt>Notes</dt><dd>${esc(req.notes)}</dd>` : ""}</dl>
        <div class="group-title">Source</div>${endpoint(r.source)}<div class="group-title">Destination</div>${endpoint(r.destination)}`,
    };
  }

  return {overview, architecture, connectivity, requirements, findings, environments, accounts, tad, import: importView, standards, result};
})();
