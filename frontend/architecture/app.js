"use strict";
// Architecture Analyser: state, loading, routing, the analysis drawer and the evidence viewer.
(() => {
  const {esc, api, toast, store} = AA;
  const VIEWS = ["setup", "download", "timeline", "overview", "architecture", "connectivity", "requirements", "findings", "environments", "accounts", "tad", "import", "standards"];
  const main = document.querySelector("#main");
  const drawer = document.querySelector("#drawer");
  const modal = document.querySelector("#modal");
  let catalogPromise = null;

  const app = {
    overview: null, projectData: null, env: "", analyses: {}, accountId: "",
    graphFilter: {direction: "all", status: "all", scale: 1},
    matrixFilter: {environment: "current", direction: "", status: "", source: "", destination: "", protocol: "", port: ""},
    project: () => app.projectData,
    analysis: () => app.analyses[app.env],
    snapshotId: () => document.querySelector("#snapshot-select").value,
    catalog: () => (catalogPromise ||= api("/catalog")),

    async reload(snapshotId) {
      app.overview = await api("/overview");
      catalogPromise = null;
      fillSelects(snapshotId);
      await loadProject(!snapshotId);
    },
    async selectProject(id) {
      document.querySelector("#project-select").value = id;
      await loadProject();
    },
    refreshNow: () => openRefresh(true),
    rerender: () => render(),
    async setEnv(env) {
      app.env = env;
      store("env", env);
      renderEnvSwitch();
      await ensureAnalysis(env);
      render();
    },
    async loadAllEnvironments() {
      await Promise.all(Object.keys(app.project().environments).map(ensureAnalysis));
    },
    async openRequirement(id, env) {
      env = env || app.env;
      await ensureAnalysis(env);
      const found = app.analyses[env].results.find((r) => r.requirement.id === id);
      if (found) app.openResult(found);
    },
    openResult(result) {
      const view = AA.views.result(result);
      document.querySelector("#drawer-title").innerHTML = view.title;
      const body = document.querySelector("#drawer-body");
      body.innerHTML = view.body;
      body.scrollTop = 0;
      body.querySelectorAll("[data-check]").forEach((button) => button.addEventListener("click", () => {
        const detail = button.nextElementSibling;
        detail.hidden = !detail.hidden;
        button.setAttribute("aria-expanded", String(!detail.hidden));
      }));
      body.querySelector("#only-problems").addEventListener("change", (e) => {
        body.querySelectorAll(".check").forEach((c) => (c.hidden = e.target.checked && c.classList.contains("PASS")));
      });
      body.querySelectorAll(".chip[data-kind]").forEach((chip) => chip.addEventListener("click", () => showObject(chip.dataset.kind, chip.dataset.id)));
      drawer.hidden = false;
      document.querySelector("#drawer-close").focus();
    },
  };

  async function showObject(kind, id) {
    document.querySelector("#modal-title").innerHTML = `<div class="flow">${esc(id.split("/").pop())}</div><div class="meta">${esc(kind.replace(/_/g, " "))} · normalized AWS object</div>`;
    const body = document.querySelector("#modal-body");
    body.innerHTML = `<div class="loading">Loading…</div>`;
    modal.hidden = false;
    try {
      const data = await api(`/snapshots/${encodeURIComponent(app.snapshotId())}/object?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
      const t = data.times;
      const line = (label, at, by, source, extra = "") => at ? `<dt>${label}</dt><dd>${esc(new Date(at).toLocaleString())}${by ? ` by <span class="mono">${esc(by)}</span>` : ""} <span class="muted small">(${esc(source)}${extra})</span></dd>` : "";
      body.innerHTML = (t ? `<dl class="kv" style="margin-bottom:12px">${line("Created", t.created, t.created_by, t.created_source)}${line("Updated", t.updated, t.updated_by, t.updated_source, t.updated_event ? " · " + t.updated_event : "")}
        ${t.changes ? `<dt>Changes</dt><dd>${t.changes} in the CloudTrail window</dd>` : ""}</dl>` : `<p class="small muted">Created and updated times not known for this resource.</p>`) +
        `<pre class="json">${esc(JSON.stringify(data.object, null, 2))}</pre>`;
    } catch (error) {
      body.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
    }
  }

  app.showObject = showObject;

  async function ensureAnalysis(env) {
    if (app.analyses[env]) return app.analyses[env];
    const query = new URLSearchParams({project: app.project().id, environment: env, snapshot: app.snapshotId()});
    app.analyses[env] = await api("/analysis?" + query);
    renderEnvSwitch();
    return app.analyses[env];
  }

  function fillSelects(snapshotId) {
    const projectSelect = document.querySelector("#project-select");
    const wanted = projectSelect.value || store("project");
    projectSelect.innerHTML = app.overview.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    if (app.overview.projects.some((p) => p.id === wanted)) projectSelect.value = wanted;
    const snapshotSelect = document.querySelector("#snapshot-select");
    const keep = snapshotId || snapshotSelect.value;
    snapshotSelect.innerHTML = app.overview.snapshots.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc(AA.when(s.created))}</option>`).join("");
    if (app.overview.snapshots.some((s) => s.id === keep)) snapshotSelect.value = keep;
  }

  async function loadProject(pickSnapshot = true) {
    const id = document.querySelector("#project-select").value;
    app.analyses = {};
    if (!id) {
      app.projectData = null;
      location.hash = "#setup";
      render();
      return;
    }
    store("project", id);
    app.projectData = await api(`/projects/${encodeURIComponent(id)}`);
    if (pickSnapshot) {
      const wanted = new Set(Object.values(app.projectData.environments).map((b) => b.account_id));
      const covering = app.overview.snapshots.find((s) => [...wanted].every((a) => (s.accounts || []).includes(a)));
      if (covering) document.querySelector("#snapshot-select").value = covering.id;
    }
    const envs = Object.keys(app.projectData.environments);
    const preferred = store("env");
    app.env = envs.includes(app.env) ? app.env : envs.includes(preferred) ? preferred : envs[envs.length - 1];
    renderEnvSwitch();
    if (!app.overview.snapshots.length) {
      location.hash = "#import";
      render();
      return;
    }
    await ensureAnalysis(app.env);
    render();
    // The other environments load in the background for the environment strip.
    app.loadAllEnvironments().then(() => {
      renderEnvSwitch();
      if (["overview", "environments"].includes(currentView())) render();
    }).catch(() => {});
  }

  function renderEnvSwitch() {
    const box = document.querySelector("#env-switch");
    const project = app.project();
    if (!project) return;
    const accounts = Object.values(project.environments).map((b) => b.account_id);
    box.innerHTML = Object.entries(project.environments).map(([env, binding]) => {
      const a = app.analyses[env];
      const shared = accounts.filter((x) => x === binding.account_id).length > 1;
      const tip = `${env}: account ${binding.account_id}${shared ? " (shared with another environment)" : ""}`;
      return `<button type="button" data-env="${esc(env)}" aria-pressed="${env === app.env}" title="${esc(tip)}">
        <span class="env-dot ${a ? esc(AA.worst(a.results.map((r) => r.status))) : ""}"></span>${esc(env)}</button>`;
    }).join("");
    box.querySelectorAll("[data-env]").forEach((b) => b.addEventListener("click", () => app.setEnv(b.dataset.env)));
    const count = app.analysis()?.findings.filter((f) => f.severity === "critical" || f.severity === "high").length;
    document.querySelector("#findings-count").textContent = count || "";
  }

  function currentView() {
    const view = location.hash.slice(1);
    return VIEWS.includes(view) ? view : "overview";
  }

  async function render() {
    const view = currentView();
    document.querySelectorAll("#sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
    if (view === "setup" || view === "download" || view === "timeline") {
      try {
        await AA.views[view](main, app);
      } catch (error) {
        main.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
      }
      return;
    }
    if (!app.project()) return;
    if (view !== "import" && !app.analysis()) {
      main.innerHTML = `<div class="empty">No AWS snapshot yet. <a href="#import">Import AWS data</a> to analyse ${esc(app.project().name)}.</div>`;
      return;
    }
    try {
      await AA.views[view](main, app);
    } catch (error) {
      console.error(error);
      main.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
    }
  }

  document.querySelector("#project-select").addEventListener("change", () => loadProject().catch((e) => toast(e.message)));
  document.querySelector("#snapshot-select").addEventListener("change", () => loadProject(false).catch((e) => toast(e.message)));
  document.querySelector("#refresh-button").addEventListener("click", () => openRefresh().catch((e) => toast(e.message)));

  // Refresh: download the project's AWS accounts with boto3, then analyse the new snapshot.
  let polling = null;
  async function openRefresh(startNow = false) {
    const project = app.project();
    if (!project) return;
    document.querySelector("#modal-title").innerHTML = `<div class="flow">Refresh AWS details</div><div class="meta">${esc(project.name)} · read-only download of the project's accounts only</div>`;
    const body = document.querySelector("#modal-body");
    body.innerHTML = `<div class="loading">Loading…</div>`;
    modal.hidden = false;
    const info = await api(`/projects/${encodeURIComponent(project.id)}/aws`);
    const draw = (job) => {
      const states = Object.fromEntries((job.accounts || []).map((a) => [a.account_id, a]));
      const running = job.state === "running";
      body.innerHTML = `<p class="small muted">Profiles come from <span class="mono">${esc(info.config)}</span>. Files go to the shared folder
        <span class="mono">${esc(info.media)}</span>, one folder per account, used by every project.</p>
        ${info.config_error ? `<div class="error-box">${esc(info.config_error)}</div>` : ""}
        <table class="data"><thead><tr><th>Account</th><th>Profile</th><th>Regions</th><th>Last download</th><th></th></tr></thead><tbody>
        ${info.accounts.map((a) => {
          const s = states[a.account_id];
          const badge = !s ? "" : s.state === "done" ? AA.status(s.warnings ? "WARNING" : "PASS", s.warnings ? `${s.warnings} denied` : "Done")
            : s.state === "failed" ? AA.status("FAIL", "Failed") : s.state === "running" ? AA.status("UNKNOWN", "Downloading") : `<span class="muted small">Queued</span>`;
          return `<tr><td><span class="mono">${esc(a.account_id)}</span><br><span class="small muted">${esc(a.name || a.type || "")}${a.environments.length ? " · " + esc(a.environments.join(", ")) : ""}</span></td>
            <td>${a.profile ? `<span class="mono">${esc(a.profile)}</span>` : `<span style="color:var(--fail)">No profile in AWS config</span>`}</td>
            <td>${esc(a.regions.join(", ") || "profile region")}</td>
            <td class="small">${a.downloaded ? esc(new Date(a.downloaded.downloaded_at).toLocaleString()) : '<span class="muted">never</span>'}</td>
            <td>${badge}${s?.error ? `<div class="small" style="color:${s.state === "failed" ? "var(--fail)" : "var(--warn)"};max-width:260px">${esc(s.error)}</div>` : ""}</td></tr>`;
        }).join("")}</tbody></table>
        <div class="filters section">
          <button type="button" class="primary" id="refresh-start" ${running ? "disabled" : ""}>${running ? "Downloading…" : "Download now"}</button>
          <span class="small muted">${esc(job.message || "")}${job.state === "failed" ? `<span style="color:var(--fail)">${esc(job.error)}</span>` : ""}
          ${job.snapshot ? `Snapshot ${esc(job.snapshot)} created.` : ""}</span></div>
        <p class="small muted">Only describe, list and get calls are made. Accounts are listed in the project's <span class="mono">environments</span> and <span class="mono">aws_accounts</span>.</p>`;
      body.querySelector("#refresh-start").addEventListener("click", start);
    };
    const button = document.querySelector("#refresh-button");
    const poll = async () => {
      const job = await api(`/projects/${encodeURIComponent(project.id)}/refresh`);
      if (!modal.hidden) draw(job);
      button.classList.toggle("running", job.state === "running");
      if (job.state === "running") {
        polling = setTimeout(poll, 1200);
        return;
      }
      polling = null;
      if (job.snapshot && job.snapshot !== app.snapshotId()) {
        toast(job.state === "done" ? "AWS details refreshed." : "Refreshed with some accounts failing.");
        await app.reload(job.snapshot);
        Object.assign(info, await api(`/projects/${encodeURIComponent(project.id)}/aws`));
        if (!modal.hidden) draw(job);
      }
    };
    async function start() {
      try {
        draw(await api(`/projects/${encodeURIComponent(project.id)}/refresh`, {method: "POST"}));
        clearTimeout(polling);
        poll();
      } catch (error) {
        toast(error.message);
      }
    }
    draw(info.refresh);
    if (startNow && info.refresh.state !== "running") await start();
    else if (info.refresh.state === "running" && !polling) poll();
  }
  window.addEventListener("hashchange", () => {
    render();
    main.focus();
  });
  const closeDrawer = () => (drawer.hidden = true);
  document.querySelector("#drawer-close").addEventListener("click", closeDrawer);
  document.querySelector("#modal-close").addEventListener("click", () => (modal.hidden = true));
  modal.addEventListener("click", (e) => e.target === modal && (modal.hidden = true));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!modal.hidden) modal.hidden = true;
    else closeDrawer();
  });

  app.reload().catch((error) => {
    main.innerHTML = `<div class="error-box">Couldn't load the Architecture Analyser: ${esc(error.message)}</div>`;
  });
  window.AAapp = app;
})();
