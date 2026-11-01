"use strict";
// Project setup: the accounts a project involves, its environments, applications and flows.
// Pickers list real resources from the selected snapshot, so refresh after adding accounts.
AA.views.setup = (() => {
  const {esc, api, toast, title} = AA;
  const ACCOUNT_TYPES = ["application", "shared-services", "central-network", "central-ingress", "central-egress", "secure-access", "dns", "other"];
  const HOP_KINDS = [
    ["workload", "Application / service"], ["subnet", "Subnet"], ["vpc", "VPC"], ["nat-gateway", "NAT gateway"],
    ["internet-gateway", "Internet gateway"], ["tgw", "Transit gateway"], ["firewall", "Network Firewall"], ["vpc-peering", "VPC peering"],
    ["vpn-connection", "VPN connection"], ["dx-gateway", "Direct Connect gateway"],
    ["vpc-endpoint", "VPC endpoint (PrivateLink)"], ["endpoint-service", "Endpoint service"], ["load-balancer", "Load balancer"],
    ["eks-cluster", "EKS cluster"], ["external", "External system"],
  ];
  const NO_ACCOUNT = new Set(["workload", "external"]);
  let draft = null, profiles = null, catalog = null, accountSearch = "";
  const cache = new Map();

  function slug(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
  }

  function resources(app, kind, account) {
    const key = `${app.snapshotId()}|${kind}|${account}`;
    if (!cache.has(key)) {
      cache.set(key, api(`/snapshots/${encodeURIComponent(app.snapshotId())}/resources?kind=${encodeURIComponent(kind)}&account=${encodeURIComponent(account || "")}`)
        .then((d) => d.items).catch(() => []));
    }
    return cache.get(key);
  }

  // Fills a <select> once its resources load; keeps a saved value even when it isn't in the snapshot.
  async function fill(select, app, kind, account, selected, {vpc = "", empty = "Choose…"} = {}) {
    const wanted = new Set([].concat(selected || []).filter(Boolean));
    let items = await resources(app, kind, account);
    if (vpc) items = items.filter((i) => !i.vpc || i.vpc === vpc);
    // A saved value may be an id or a name (load balancers and clusters are often named).
    const picked = (i) => wanted.has(i.id) || wanted.has(i.label);
    const known = new Set(items.flatMap((i) => (picked(i) ? [i.id, i.label] : [i.id])));
    select.innerHTML = (select.multiple ? "" : `<option value="">${esc(empty)}</option>`) +
      items.map((i) => `<option value="${esc(i.id)}" ${picked(i) ? "selected" : ""}>${esc(i.label)}${i.label !== i.id ? ` (${esc(i.id.split("/").pop())})` : ""} · ${esc(i.detail)}</option>`).join("") +
      [...wanted].filter((id) => !known.has(id)).map((id) => `<option value="${esc(id)}" selected>${esc(id)} (not in snapshot)</option>`).join("");
    if (!items.length && !wanted.size && !select.multiple) select.innerHTML = `<option value="">Nothing in the snapshot — refresh first</option>`;
  }

  const accountOptions = (selected, blank = "Account…") => `<option value="">${blank}</option>` + draft.aws_accounts.map((a) =>
    `<option value="${esc(a.account_id)}" ${a.account_id === selected ? "selected" : ""}>${esc(a.name || a.account_id)} · ${esc(a.account_id)}</option>`).join("");

  function render(main, app) {
    const envs = Object.keys(draft.environments);
    const workloads = [...draft.applications.map((w) => [w.id, w.name || w.id, "Application"]), ...catalog.services.map((s) => [s.id, s.name, "Service"])];
    const externals = catalog.external.map((e) => [e.id, e.name, title(e.type)]);
    const picked = new Set(draft.aws_accounts.map((a) => a.account_id));
    const matches = (profiles.accounts || []).filter((a) => !picked.has(a.account_id) && (!accountSearch ||
      `${a.account_id} ${a.profiles.join(" ")}`.toLowerCase().includes(accountSearch.toLowerCase()))).slice(0, 40);

    main.innerHTML = `<div class="page-head"><div><h1>${draft._new ? "New project" : "Project setup"}</h1>
        <p>Pick the AWS accounts involved, map environments, define where applications run and describe each flow hop by hop.</p></div>
        <div class="filters"><button type="button" id="new-project">New project</button><button type="button" id="save">Save</button>
        <button type="button" class="primary" id="save-download">Save &amp; download AWS details</button></div></div>

      <div class="card"><h2>Project</h2><div class="form-grid">
        <label>Name<input data-p="name" value="${esc(draft.name)}" placeholder="Payment Platform"></label>
        <label>Id<input data-p="id" value="${esc(draft.id)}" ${draft._new ? "" : "disabled"} class="mono"></label>
        <label class="wide">Description<input data-p="description" value="${esc(draft.description)}"></label></div></div>

      <div class="card section"><h2>AWS accounts <span class="muted small">${draft.aws_accounts.length} selected · only these are downloaded</span></h2>
        <table class="data"><thead><tr><th>Account</th><th>Name</th><th>Type</th><th>Profile</th><th>Regions</th><th></th></tr></thead><tbody>
        ${draft.aws_accounts.map((a, i) => {
          const choices = (profiles.accounts || []).find((p) => p.account_id === a.account_id)?.profiles || [];
          return `<tr><td class="mono">${esc(a.account_id)}</td><td><input data-acc="${i}" data-f="name" value="${esc(a.name)}"></td>
          <td><select data-acc="${i}" data-f="type">${ACCOUNT_TYPES.map((t) => `<option ${t === a.type ? "selected" : ""}>${t}</option>`).join("")}</select></td>
          <td><select data-acc="${i}" data-f="profile"><option value="">${choices.length ? "First match" : "None in AWS config"}</option>${choices.map((p) => `<option ${p === a.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></td>
          <td><input data-acc="${i}" data-f="regions" value="${esc(a.regions.join(", "))}" placeholder="profile region" style="width:150px"></td>
          <td><button type="button" class="icon-button" data-remove-acc="${i}" aria-label="Remove account">✕</button></td></tr>`;
        }).join("") || `<tr><td colspan="6" class="muted">No accounts yet — add them below.</td></tr>`}</tbody></table>
        <div class="filters section"><input type="search" id="account-search" placeholder="Search ${(profiles.accounts || []).length} accounts in AWS config by number or profile" value="${esc(accountSearch)}" style="width:380px">
          <input id="account-manual" placeholder="or type an account number" class="mono" style="width:190px"><button type="button" id="account-add">Add</button></div>
        ${profiles.error ? `<div class="error-box">${esc(profiles.error)}</div>` : ""}
        <div class="pick-list">${matches.map((a) => `<button type="button" class="pick" data-pick="${esc(a.account_id)}"><span class="mono">${esc(a.account_id)}</span> ${esc(a.profiles.join(", "))}</button>`).join("")}</div></div>

      <div class="card section"><h2>Environments</h2>
        <table class="data"><tbody>${envs.map((env) => `<tr><td><input data-env-name="${esc(env)}" value="${esc(env)}" style="width:120px"></td>
          <td><select data-env-account="${esc(env)}">${accountOptions(draft.environments[env].account_id)}</select></td>
          <td><button type="button" class="icon-button" data-remove-env="${esc(env)}" aria-label="Remove environment">✕</button></td></tr>`).join("")}</tbody></table>
        <button type="button" id="add-env" class="section">Add environment</button></div>

      <div class="card section"><h2>Applications</h2><p class="small muted">Where each application runs in each environment. Flows can start or end at an application.</p>
        ${draft.applications.map((w, wi) => `<div class="sub-card"><div class="form-grid"><label>Name<input data-app="${wi}" data-f="name" value="${esc(w.name)}"></label>
          <label>Id<input data-app="${wi}" data-f="id" value="${esc(w.id)}" class="mono"></label><div class="right"><button type="button" class="danger" data-remove-app="${wi}">Remove</button></div></div>
          <table class="data placement"><thead><tr><th>Env</th><th>Account</th><th>VPC</th><th>Subnets</th><th>Security groups</th><th>Load balancer</th></tr></thead><tbody>
          ${envs.map((env) => {
            const p = w.placement[env] || {};
            return `<tr data-place="${wi}" data-env="${esc(env)}"><td>${esc(env)}</td>
              <td><select data-pf="account">${accountOptions(p.account || draft.environments[env].account_id)}</select></td>
              <td><select data-pf="vpc" data-kind="vpc"></select></td><td><select data-pf="subnets" data-kind="subnet" multiple size="3"></select></td>
              <td><select data-pf="security_groups" data-kind="security-group" multiple size="3"></select></td><td><select data-pf="load_balancer" data-kind="load-balancer"></select></td></tr>`;
          }).join("")}</tbody></table></div>`).join("")}
        <button type="button" id="add-app">Add application</button></div>

      <div class="card section"><h2>Flows</h2><p class="small muted">Describe each connection. "Hop by hop" pins every resource it passes (pick the exact NAT gateway, endpoint, NLB, cluster…); every link between hops is checked.</p>
        ${draft.requirements.map((r, ri) => `<div class="sub-card">
          <div class="form-grid"><label class="wide">Name<input data-req="${ri}" data-f="name" value="${esc(r.name)}"></label>
            <label>Id<input data-req="${ri}" data-f="id" value="${esc(r.id)}" class="mono"></label>
            <label>Direction<select data-req="${ri}" data-f="direction">${["outbound", "inbound", "bidirectional"].map((d) => `<option ${d === r.direction ? "selected" : ""}>${d}</option>`).join("")}</select></label>
            <label>Protocol<select data-req="${ri}" data-f="protocol">${["TCP", "UDP", "HTTPS", "ICMP"].map((d) => `<option ${d === (r.protocol || "TCP").toUpperCase() ? "selected" : ""}>${d}</option>`).join("")}</select></label>
            <label>Port<input data-req="${ri}" data-f="port" value="${esc(r.port ?? "")}" inputmode="numeric" style="width:90px"></label>
            <label>Hostname<input data-req="${ri}" data-f="hostname" value="${esc(r.hostname || "")}" placeholder="optional, for DNS"></label>
            <div class="right"><button type="button" class="danger" data-remove-req="${ri}">Remove</button></div></div>
          <div class="filters"><span class="small muted">Environments</span>${envs.map((env) => `<label class="small"><input type="checkbox" data-req-env="${ri}" value="${esc(env)}" ${envsOf(r).includes(env) ? "checked" : ""}> ${esc(env)}</label>`).join("")}
            <div class="segmented small" style="margin-left:auto">${["simple", "hops"].map((m) => `<button type="button" data-mode="${ri}" data-value="${m}" aria-pressed="${(r.hops?.length ? "hops" : "simple") === m}">${m === "simple" ? "Source → destination" : "Hop by hop"}</button>`).join("")}</div></div>
          ${r.hops?.length ? `<ol class="hops">${r.hops.map((h, hi) => `<li data-hop="${ri}" data-index="${hi}">
              <select data-hf="kind">${HOP_KINDS.map(([k, label]) => `<option value="${k}" ${k === h.kind ? "selected" : ""}>${label}</option>`).join("")}</select>
              ${NO_ACCOUNT.has(h.kind) ? "" : `<select data-hf="account">${accountOptions(h.account, "Any account")}</select>`}
              <select data-hf="id" class="hop-resource">${h.kind === "workload" ? options(workloads, h.id) : h.kind === "external" ? options(externals, h.id) : ""}</select>
              <input data-hf="port" value="${esc(h.port ?? "")}" placeholder="port after" title="The port from this hop on, when it changes (NLB 443 → pods 8443)" style="width:90px">
              <button type="button" class="icon-button" data-move="-1" aria-label="Move up">↑</button><button type="button" class="icon-button" data-move="1" aria-label="Move down">↓</button>
              <button type="button" class="icon-button" data-remove-hop aria-label="Remove hop">✕</button></li>`).join("")}</ol>
              <button type="button" data-add-hop="${ri}">Add hop</button>`
            : `<div class="filters"><label>Source <select data-req="${ri}" data-f="source">${options([...workloads, ...externals], r.source)}</select></label>
              <label>Destination <select data-req="${ri}" data-f="destination">${options([...workloads, ...externals], r.destination)}</select></label></div>`}
        </div>`).join("")}
        <button type="button" id="add-req">Add flow</button></div>`;
    wire(main, app);
  }

  function options(list, selected) {
    return `<option value="">Choose…</option>` + list.map(([id, name, group]) => `<option value="${esc(id)}" ${id === selected ? "selected" : ""}>${esc(name)} · ${esc(group)}</option>`).join("");
  }

  function envsOf(r) {
    return r.environments?.length ? r.environments : r.environment ? [r.environment] : [];
  }

  function wire(main, app) {
    const rerender = () => render(main, app);
    const on = (selector, event, handler) => main.querySelectorAll(selector).forEach((n) => n.addEventListener(event, (e) => handler(n, e)));
    on("[data-p]", "change", (n) => {
      draft[n.dataset.p] = n.value;
      if (n.dataset.p === "name" && draft._new && !draft._idTouched) {
        draft.id = slug(n.value);
        main.querySelector('[data-p="id"]').value = draft.id;
      }
      if (n.dataset.p === "id") draft._idTouched = true;
    });
    on("[data-acc]", "change", (n) => {
      const a = draft.aws_accounts[+n.dataset.acc];
      a[n.dataset.f] = n.dataset.f === "regions" ? n.value.split(/[\s,]+/).filter(Boolean) : n.value;
    });
    on("[data-remove-acc]", "click", (n) => {
      draft.aws_accounts.splice(+n.dataset.removeAcc, 1);
      rerender();
    });
    const addAccount = (id) => {
      if (!/^\d{12}$/.test(id)) return toast("An AWS account number has 12 digits.");
      if (draft.aws_accounts.some((a) => a.account_id === id)) return;
      const known = (profiles.accounts || []).find((a) => a.account_id === id);
      draft.aws_accounts.push({account_id: id, name: known?.name || "", type: draft.aws_accounts.length ? "shared-services" : "application", regions: [], profile: ""});
      rerender();
    };
    on("[data-pick]", "click", (n) => addAccount(n.dataset.pick));
    on("#account-add", "click", () => addAccount(main.querySelector("#account-manual").value.trim()));
    on("#account-search", "input", (n) => {
      accountSearch = n.value;
      clearTimeout(n._t);
      n._t = setTimeout(() => {
        rerender();
        const box = main.querySelector("#account-search");
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
      }, 1000);
    });
    on("[data-env-account]", "change", (n) => (draft.environments[n.dataset.envAccount].account_id = n.value));
    on("[data-env-name]", "change", (n) => {
      const old = n.dataset.envName, name = n.value.trim().toUpperCase();
      if (!name || name === old || draft.environments[name]) return rerender();
      draft.environments = Object.fromEntries(Object.entries(draft.environments).map(([k, v]) => [k === old ? name : k, v]));
      draft.applications.forEach((w) => {
        if (w.placement[old]) {
          w.placement[name] = w.placement[old];
          delete w.placement[old];
        }
      });
      draft.requirements.forEach((r) => (r.environments = envsOf(r).map((e) => (e === old ? name : e))));
      rerender();
    });
    on("[data-remove-env]", "click", (n) => {
      delete draft.environments[n.dataset.removeEnv];
      rerender();
    });
    on("#add-env", "click", () => {
      const name = ["DEV", "STAGE", "MTF", "PROD", "TEST", "UAT"].find((e) => !draft.environments[e]) || `ENV${Object.keys(draft.environments).length + 1}`;
      draft.environments[name] = {account_id: draft.aws_accounts[0]?.account_id || ""};
      rerender();
    });
    on("[data-app]", "change", (n) => (draft.applications[+n.dataset.app][n.dataset.f] = n.value));
    on("[data-remove-app]", "click", (n) => {
      draft.applications.splice(+n.dataset.removeApp, 1);
      rerender();
    });
    on("#add-app", "click", () => {
      const index = draft.applications.length + 1;
      draft.applications.push({id: `app-${index}`, name: `Application ${index}`, kind: "application", placement: {}});
      rerender();
    });
    // Placement rows: account → VPC → subnets, security groups, load balancer.
    main.querySelectorAll("tr[data-place]").forEach((row) => {
      const workload = draft.applications[+row.dataset.place];
      const env = row.dataset.env;
      const place = () => (workload.placement[env] ||= {account: "", vpc: "", subnets: [], security_groups: [], load_balancer: ""});
      const current = workload.placement[env] || {};
      const account = current.account || draft.environments[env].account_id;
      const vpcSelect = row.querySelector('[data-pf="vpc"]');
      fill(vpcSelect, app, "vpc", account, current.vpc).then(() => {
        const vpc = vpcSelect.value;
        fill(row.querySelector('[data-pf="subnets"]'), app, "subnet", account, current.subnets, {vpc});
        fill(row.querySelector('[data-pf="security_groups"]'), app, "security-group", account, current.security_groups, {vpc});
        fill(row.querySelector('[data-pf="load_balancer"]'), app, "load-balancer", account, current.load_balancer, {vpc, empty: "None"});
      });
      row.querySelectorAll("[data-pf]").forEach((select) => select.addEventListener("change", () => {
        const field = select.dataset.pf;
        place()[field] = select.multiple ? [...select.selectedOptions].map((o) => o.value) : select.value;
        if (field === "account" || field === "vpc") {
          if (field === "account") place().vpc = "";
          Object.assign(place(), {subnets: [], security_groups: [], load_balancer: ""});
          rerender();
        }
      }));
    });
    // Flows
    on("[data-req]", "change", (n) => {
      const r = draft.requirements[+n.dataset.req];
      r[n.dataset.f] = n.dataset.f === "port" ? (n.value.trim() ? Number(n.value) : null) : n.value;
    });
    on("[data-req-env]", "change", (n) => {
      const r = draft.requirements[+n.dataset.reqEnv];
      const list = new Set(envsOf(r));
      n.checked ? list.add(n.value) : list.delete(n.value);
      r.environments = [...list];
      r.environment = "";
    });
    on("[data-remove-req]", "click", (n) => {
      draft.requirements.splice(+n.dataset.removeReq, 1);
      rerender();
    });
    on("#add-req", "click", () => {
      const used = new Set(draft.requirements.map((r) => r.id));
      let i = draft.requirements.length + 1;
      while (used.has(`conn-${String(i).padStart(3, "0")}`)) i++;
      draft.requirements.push({id: `conn-${String(i).padStart(3, "0")}`, name: "", environments: Object.keys(draft.environments), source: "", destination: "",
        direction: "outbound", protocol: "TCP", port: 443, hostname: "", hops: [{kind: "workload", id: ""}, {kind: "subnet", id: "", account: ""}]});
      rerender();
    });
    on("[data-mode]", "click", (n) => {
      const r = draft.requirements[+n.dataset.mode];
      if (n.dataset.value === "hops" && !r.hops?.length) {
        r.hops = [{kind: "workload", id: r.source || ""}, {kind: catalog.external.some((e) => e.id === r.destination) ? "external" : "workload", id: r.destination || ""}];
      } else if (n.dataset.value === "simple" && r.hops?.length) {
        r.source = r.hops[0].kind === "workload" || r.hops[0].kind === "external" ? r.hops[0].id : "";
        const last = r.hops[r.hops.length - 1];
        r.destination = last.kind === "workload" || last.kind === "external" ? last.id : "";
        r.hops = [];
      }
      rerender();
    });
    main.querySelectorAll("li[data-hop]").forEach((row) => {
      const r = draft.requirements[+row.dataset.hop];
      const index = +row.dataset.index;
      const hop = r.hops[index];
      const resource = row.querySelector('[data-hf="id"]');
      if (!NO_ACCOUNT.has(hop.kind)) fill(resource, app, hop.kind, hop.account || "", hop.id);
      row.querySelectorAll("[data-hf]").forEach((input) => input.addEventListener("change", () => {
        const field = input.dataset.hf;
        if (field === "port") hop.port = input.value.trim() ? Number(input.value) : null;
        else hop[field] = input.value;
        if (field === "kind" || field === "account") {
          hop.id = "";
          if (field === "kind" && NO_ACCOUNT.has(hop.kind)) hop.account = "";
          rerender();
        }
      }));
      row.querySelectorAll("[data-move]").forEach((b) => b.addEventListener("click", () => {
        const to = index + Number(b.dataset.move);
        if (to < 0 || to >= r.hops.length) return;
        [r.hops[index], r.hops[to]] = [r.hops[to], r.hops[index]];
        rerender();
      }));
      row.querySelector("[data-remove-hop]").addEventListener("click", () => {
        r.hops.splice(index, 1);
        rerender();
      });
    });
    on("[data-add-hop]", "click", (n) => {
      const r = draft.requirements[+n.dataset.addHop];
      const last = r.hops[r.hops.length - 1];
      r.hops.push({kind: "subnet", id: "", account: last?.account || ""});
      rerender();
    });
    on("#new-project", "click", () => {
      draft = blank();
      rerender();
    });
    on("#save", "click", () => save(app, false));
    on("#save-download", "click", () => save(app, true));
  }

  function blank() {
    return {_new: true, id: "", name: "", description: "", environments: {DEV: {account_id: ""}, PROD: {account_id: ""}},
      shared_services: [], central_services: [], aws_accounts: [], applications: [], requirements: []};
  }

  function clean() {
    const value = JSON.parse(JSON.stringify(draft));
    delete value._new;
    delete value._idTouched;
    value.requirements = value.requirements.map((r) => {
      const out = {...r};
      if (out.hops?.length) {
        out.hops = out.hops.filter((h) => h.id).map((h) => Object.fromEntries(Object.entries(h).filter(([, v]) => v !== "" && v != null)));
        delete out.source;
        delete out.destination;
      } else delete out.hops;
      if (!out.hostname) delete out.hostname;
      if (out.port == null || out.port === "") delete out.port;
      return out;
    });
    return value;
  }

  async function save(app, thenDownload) {
    const value = clean();
    if (!value.name.trim()) return toast("Give the project a name.");
    value.id = value.id || slug(value.name);
    if (!/^[A-Za-z0-9._-]{1,100}$/.test(value.id)) return toast("The id can use letters, digits, dot, dash and underscore.");
    if (Object.values(value.environments).some((b) => !b.account_id)) return toast("Choose an account for every environment.");
    try {
      await api(`/projects/${encodeURIComponent(value.id)}`, {method: "PUT", body: value});
      AA.store("project", value.id);
      document.querySelector("#project-select").value = value.id;
      await app.reload();
      document.querySelector("#project-select").value = value.id;
      await app.selectProject(value.id);
      draft = null;
      toast("Project saved.");
      location.hash = "#setup";
      if (thenDownload) await app.refreshNow();
      else app.rerender();
    } catch (error) {
      toast(error.message);
    }
  }

  return async function setup(main, app) {
    main.innerHTML = `<div class="loading">Loading…</div>`;
    [profiles, catalog] = await Promise.all([profiles || api("/aws-profiles"), app.catalog()]);
    const project = app.project();
    if (!draft || (!draft._new && draft.id !== project?.id)) {
      draft = project ? JSON.parse(JSON.stringify(project)) : blank();
      draft.aws_accounts ||= [];
      // Accounts named by environments belong in the list too.
      for (const binding of Object.values(draft.environments)) {
        if (binding.account_id && !draft.aws_accounts.some((a) => a.account_id === binding.account_id)) {
          draft.aws_accounts.push({account_id: binding.account_id, name: "", type: "application", regions: [], profile: ""});
        }
      }
    }
    if (!app.snapshotId()) {
      main.innerHTML = `<div class="empty">Import or download AWS data first.</div>`;
      return;
    }
    render(main, app);
  };
})();
