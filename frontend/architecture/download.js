"use strict";
// AWS download: tick the accounts to download from everything in the AWS config, then download
// them all (read-only) into the shared media folder. Each download becomes a snapshot.
AA.views.download = (() => {
  const {esc, api, toast} = AA;
  const TYPES = ["application", "shared-services", "central-network", "central-ingress", "central-egress", "secure-access", "dns", "other"];
  let data = null, search = "", polling = null;

  const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");

  function selected(id) {
    return data.config.accounts.find((a) => a.account_id === id);
  }

  function render(main, app) {
    const config = data.config;
    const job = data.job || {state: "idle"};
    const running = job.state === "running";
    const states = Object.fromEntries((job.accounts || []).map((a) => [a.account_id, a]));
    const shown = data.available.filter((a) => !search || `${a.account_id} ${a.profiles.join(" ")} ${selected(a.account_id)?.name || ""}`.toLowerCase().includes(search.toLowerCase()));
    main.innerHTML = `<div class="page-head"><div><h1>AWS download</h1>
        <p>Tick the accounts to download. Only describe, list and get calls are made; files go to a folder every project shares.</p></div>
        <div class="filters"><button type="button" id="dl-save">Save</button>
        <button type="button" class="primary" id="dl-start" ${running ? "disabled" : ""}>${running ? "Downloading…" : `Download ${config.accounts.length} account${config.accounts.length === 1 ? "" : "s"}`}</button></div></div>
      <div class="card"><dl class="kv"><dt>AWS config</dt><dd class="mono">${esc(data.aws_config)}</dd>
        <dt>Saved to</dt><dd class="mono">${esc(data.media)}</dd>
        <dt>Last download</dt><dd>${job.finished ? `${esc(new Date(job.finished * 1000).toLocaleString())} · ${esc(job.state)}${job.snapshot ? ` · snapshot ${esc(job.snapshot)}` : ""}` : running ? esc(job.message || "Running…") : "—"}</dd></dl>
        ${data.aws_config_error ? `<div class="error-box section">${esc(data.aws_config_error)}</div>` : ""}
        ${job.state === "failed" && job.error ? `<div class="error-box section">${esc(job.error)}</div>` : ""}</div>
      <div class="card section"><h2>Created and updated times</h2><div class="filters">
        <label class="small"><input type="checkbox" id="dl-config" ${config.history.config ? "checked" : ""}> AWS Config (creation and last change of every resource, where Config is recording)</label></div>
        <div class="filters"><label class="small"><input type="checkbox" id="dl-trail" ${config.history.cloudtrail ? "checked" : ""}> CloudTrail: who created and changed what, over the last</label>
        <input id="dl-days" value="${esc(config.history.days)}" inputmode="numeric" style="width:60px"> <span class="small">days (max 90), up to</span>
        <input id="dl-max" value="${esc(config.history.max_events)}" inputmode="numeric" style="width:90px"> <span class="small">events per region</span></div>
        <p class="small muted">Times from a resource's own AWS API come first (NAT, endpoints, TGW, load balancers, EKS, Resolver, firewall rules); Config and CloudTrail fill in the rest. Each time shows its source.</p>
        <div class="filters"><span class="small">Default regions</span><input id="dl-regions" value="${esc(config.regions.join(", "))}" placeholder="each profile's region" style="width:260px"></div></div>
      <div class="card section"><h2>Accounts <span class="muted small">${config.accounts.length} of ${data.available.length} selected</span></h2>
        <div class="filters"><input type="search" id="dl-search" placeholder="Search by account number, profile or name" value="${esc(search)}" style="width:320px">
          <button type="button" id="dl-all">Select shown</button><button type="button" id="dl-none">Clear shown</button>
          <input id="dl-manual" placeholder="Add an account number" class="mono" style="width:190px"><button type="button" id="dl-add">Add</button></div>
        <div class="table-wrap"><table class="data"><thead><tr><th></th><th>Account</th><th>Profile</th><th>Name</th><th>Type</th><th>Regions</th><th>Last download</th><th>Status</th></tr></thead><tbody>
        ${shown.map((a) => {
          const pick = selected(a.account_id);
          const s = states[a.account_id];
          const badge = !s ? "" : s.state === "done" ? AA.status(s.warnings ? "WARNING" : "PASS", s.warnings ? `${s.warnings} calls denied` : "Done")
            : s.state === "failed" ? AA.status("FAIL", "Failed") : s.state === "running" ? AA.status("UNKNOWN", "Downloading") : `<span class="small muted">Queued</span>`;
          return `<tr><td><input type="checkbox" data-pick="${esc(a.account_id)}" ${pick ? "checked" : ""} aria-label="Download ${esc(a.account_id)}"></td>
            <td class="mono">${esc(a.account_id)}</td>
            <td>${pick ? `<select data-f="profile" data-id="${esc(a.account_id)}"><option value="">${a.profiles.length ? "First match" : "None"}</option>${a.profiles.map((p) => `<option ${p === pick.profile ? "selected" : ""}>${esc(p)}</option>`).join("")}</select>`
              : a.profiles.length ? esc(a.profiles.join(", ")) : `<span style="color:var(--fail)">not in AWS config</span>`}</td>
            <td>${pick ? `<input data-f="name" data-id="${esc(a.account_id)}" value="${esc(pick.name)}" style="width:150px">` : ""}</td>
            <td>${pick ? `<select data-f="type" data-id="${esc(a.account_id)}">${TYPES.map((t) => `<option ${t === pick.type ? "selected" : ""}>${t}</option>`).join("")}</select>` : ""}</td>
            <td>${pick ? `<input data-f="regions" data-id="${esc(a.account_id)}" value="${esc(pick.regions.join(", "))}" placeholder="${esc(a.region || "default")}" style="width:140px">` : ""}</td>
            <td class="small">${a.downloaded ? esc(when(a.downloaded.downloaded_at)) : '<span class="muted">never</span>'}</td>
            <td>${badge}${s?.error ? `<div class="small" style="color:${s.state === "failed" ? "var(--fail)" : "var(--warn)"};max-width:280px">${esc(s.error)}</div>` : ""}</td></tr>`;
        }).join("") || `<tr><td colspan="8" class="empty">No accounts match.</td></tr>`}</tbody></table></div></div>`;
    wire(main, app);
  }

  function collect(main) {
    const config = data.config;
    const regions = (v) => v.split(/[\s,]+/).filter(Boolean);
    config.regions = regions(main.querySelector("#dl-regions").value);
    config.history = {config: main.querySelector("#dl-config").checked, cloudtrail: main.querySelector("#dl-trail").checked,
      days: Number(main.querySelector("#dl-days").value) || 90, max_events: Number(main.querySelector("#dl-max").value) || 5000};
  }

  async function save(main) {
    collect(main);
    data.config = await api("/download", {method: "PUT", body: data.config});
  }

  function wire(main, app) {
    const rerender = () => {
      collect(main);
      render(main, app);
    };
    main.querySelectorAll("[data-pick]").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.pick;
      if (box.checked && !selected(id)) {
        const known = data.available.find((a) => a.account_id === id);
        data.config.accounts.push({account_id: id, name: known?.profiles[0] || "", type: "application", regions: [], profile: ""});
      } else if (!box.checked) data.config.accounts = data.config.accounts.filter((a) => a.account_id !== id);
      rerender();
    }));
    main.querySelectorAll("[data-f]").forEach((input) => input.addEventListener("change", () => {
      const item = selected(input.dataset.id);
      item[input.dataset.f] = input.dataset.f === "regions" ? input.value.split(/[\s,]+/).filter(Boolean) : input.value;
    }));
    const search_ = main.querySelector("#dl-search");
    search_.addEventListener("input", () => {
      search = search_.value;
      clearTimeout(search_._t);
      search_._t = setTimeout(() => {
        rerender();
        const box = main.querySelector("#dl-search");
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
      }, 200);
    });
    const shownIds = () => [...main.querySelectorAll("[data-pick]")].map((b) => b.dataset.pick);
    main.querySelector("#dl-all").addEventListener("click", () => {
      for (const id of shownIds()) {
        if (!selected(id)) data.config.accounts.push({account_id: id, name: data.available.find((a) => a.account_id === id)?.profiles[0] || "", type: "application", regions: [], profile: ""});
      }
      rerender();
    });
    main.querySelector("#dl-none").addEventListener("click", () => {
      const ids = new Set(shownIds());
      data.config.accounts = data.config.accounts.filter((a) => !ids.has(a.account_id));
      rerender();
    });
    main.querySelector("#dl-add").addEventListener("click", () => {
      const id = main.querySelector("#dl-manual").value.trim();
      if (!/^\d{12}$/.test(id)) return toast("An AWS account number has 12 digits.");
      if (!data.available.some((a) => a.account_id === id)) data.available.push({account_id: id, profiles: [], region: "", downloaded: null});
      if (!selected(id)) data.config.accounts.push({account_id: id, name: "", type: "application", regions: [], profile: ""});
      rerender();
    });
    main.querySelector("#dl-save").addEventListener("click", async () => {
      try {
        await save(main);
        toast("Saved.");
        render(main, app);
      } catch (error) {
        toast(error.message);
      }
    });
    main.querySelector("#dl-start").addEventListener("click", async () => {
      try {
        await save(main);
        data.job = await api("/download/start", {method: "POST"});
        render(main, app);
        poll(main, app);
      } catch (error) {
        toast(error.message);
      }
    });
  }

  async function poll(main, app) {
    clearTimeout(polling);
    const job = await api("/download/status");
    if (location.hash !== "#download") return;
    const before = data.job?.state;
    data.job = job;
    const focused = document.activeElement?.closest?.("#main") && document.activeElement.matches("input,select");
    if (!focused) render(main, app);
    if (job.state === "running") {
      polling = setTimeout(() => poll(main, app), 1500);
    } else if (before === "running" && job.snapshot) {
      toast(job.state === "done" ? "Download finished." : "Download finished; some accounts failed.");
      await app.reload();  // each project picks its newest covering snapshot
      data = await api("/download");
      render(main, app);
    }
  }

  return async function download(main, app) {
    main.innerHTML = `<div class="loading">Loading…</div>`;
    data = await api("/download");
    render(main, app);
    if (data.job?.state === "running") poll(main, app);
  };
})();
