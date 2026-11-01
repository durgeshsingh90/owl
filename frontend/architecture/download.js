"use strict";
// AWS download: one line per account, the name shown once. Click a stage to select it, test
// one line or all selected, click a status icon to sign in, then download what's selected.
AA.views.download = (() => {
  const {esc, api, toast} = AA;
  const TESTS_AT_ONCE = 6;
  const SHARED = "../bitbucket/assets/";
  const ICONS = {connected: `${SHARED}connected.png?v=2`, checking: `${SHARED}no-connection.gif?v=2`, failed: `${SHARED}disconnected.png?v=2`, unknown: "assets/not-tested.png"};
  let data = null, search = "", onlySelected = false, polling = null, login = null;
  const running = new Set();
  let searchTyping;

  const accounts = () => data.groups.flatMap((g) => g.accounts);
  const chosen = () => accounts().filter((a) => a.selected);
  const dateOnly = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "");
  const stamp = (iso) => (iso ? new Date(iso).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}) : "");

  function statusOf(a) {
    if (running.has(a.account_id)) return ["checking", "Testing…", ""];
    const i = a.info || {};
    if (i.connected === true) return ["connected", "Connected", `${i.arn || ""}. Last tested ${stamp(i.checked_at)}. Click to test again.`];
    if (i.connected === false) return ["failed", "Not connected", `${i.error || ""} Click to sign in.`];
    return ["unknown", "Not tested", "Click to sign in."];
  }

  function jobLabel(job) {
    if (job.state === "running") {
      const done = job.accounts.filter((a) => a.state === "done" || a.state === "failed").length;
      return `Downloading ${done} of ${job.accounts.length} · ${job.message || ""}`;
    }
    if (!job.finished) return "";
    const failed = job.accounts.filter((a) => a.state === "failed").length;
    return `Last download ${new Date(job.finished * 1000).toLocaleString()}${failed ? ` · ${failed} failed` : ""}${job.error ? ` · ${job.error}` : ""}`;
  }

  function notes(a, states) {
    const s = states[a.account_id], i = a.info || {};
    const denied = s?.denied?.length ? s.denied : i.denied || [];
    return [
      ...(s?.state === "failed" || s?.error ? [`<div class="dl-problem">${esc(s.error)}</div>`] : i.connected === false && i.error ? [`<div class="dl-problem">${esc(i.error)}</div>`] : []),
      ...Object.entries(i.errors || {}).slice(0, 5).map(([k, v]) => `<div class="dl-denied">${esc(k)}: ${esc(v)}</div>`),
      ...(denied.length ? [`<div class="dl-denied">No access: ${esc(denied.join(", "))}</div>`] : []),
    ].join("");
  }

  // One account's line. nameCell is the Name column: omitted while a rowspan covers it.
  function line(a, states, nameCell, first) {
    const [state, label, tip] = statusOf(a);
    const s = states[a.account_id], i = a.info || {};
    const denied = s?.denied?.length ? s.denied : i.denied || [];
    const problems = notes(a, states);
    const job = s?.state === "running" ? "Downloading…" : s?.state === "queued" ? "Queued" : stamp(a.downloaded_at);
    return `<tr class="${first ? "dl-first" : ""}">${nameCell}
      <td><button type="button" class="dl-pick" data-pick="${esc(a.account_id)}" aria-pressed="${a.selected}" title="${a.selected ? "Selected for download. Click to remove" : "Click to select for download"}">${esc(a.stage)}</button></td>
      <td class="mono">${esc(a.account_id)}</td>
      <td class="nowrap" ${i.created_error ? `title="${esc(i.created_error)}"` : ""}>${i.created ? esc(dateOnly(i.created)) : `<span class="muted">${i.created_error ? "No access" : "—"}</span>`}</td>
      <td class="small nowrap">${job ? esc(job) : '<span class="muted">Never</span>'}</td>
      <td><button type="button" class="dl-status ${state}" data-status="${esc(a.account_id)}" title="${esc(tip)}"><img src="${ICONS[state]}" alt="">${esc(label)}</button>${state === "connected" && denied.length ? `<div class="dl-partial">${denied.length} type${denied.length === 1 ? "" : "s"} denied</div>` : ""}</td>
      <td><button type="button" class="dl-test" data-test="${esc(a.account_id)}" ${running.has(a.account_id) ? "disabled" : ""}>Test</button></td></tr>${problems ? `<tr class="dl-note"><td colspan="6">${problems}</td></tr>` : ""}`;
  }

  const nameOf = (group, a) => `${esc(group.name)}${a.info?.account_name && a.info.account_name !== group.name ? `<div class="small muted dl-aws-name">${esc(a.info.account_name)}</div>` : ""}`;

  function lines(group, items, states) {
    const spans = items.length + items.filter((a) => notes(a, states)).length;
    return items.map((a, index) => line(a, states, index ? "" : `<td class="dl-name" rowspan="${spans}">${nameOf(group, a)}</td>`, !index)).join("");
  }

  // Anything can be searched: words can be in any order and match name, stage, number or profile.
  function matches(group, a, words) {
    const text = [group.name, a.account_id, a.stage, a.profile, ...(a.profiles || []), a.info?.account_name || ""].join(" ").toLowerCase();
    return words.every((word) => text.includes(word));
  }

  function list(main) {
    const job = data.job || {};
    const states = Object.fromEntries((job.accounts || []).map((a) => [a.account_id, a]));
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const rows = data.groups.map((g) => {
      const items = g.accounts.filter((a) => (!onlySelected || a.selected) && matches(g, a, words));
      return items.length ? lines(g, items, states) : "";
    }).join("");
    main.querySelector("#dl-list").innerHTML = rows || `<tr><td colspan="7" class="empty">No accounts match.</td></tr>`;
    const groupOf = new Map(data.groups.flatMap((g) => g.accounts.map((a) => [a.account_id, [g, a]])));
    const top = words.length || onlySelected ? [] : (data.top || []).map((id) => groupOf.get(id)).filter(Boolean);
    main.querySelector("#dl-top-box").hidden = !top.length;
    main.querySelector("#dl-top-count").textContent = top.length;
    main.querySelector("#dl-top").innerHTML = top.map(([g, a]) => line(a, states, `<td class="dl-name" rowspan="${notes(a, states) ? 2 : 1}">${nameOf(g, a)}</td>`, true)).join("");
    const count = chosen().length;
    const downloading = job.state === "running";
    const start = main.querySelector("#dl-start"), all = main.querySelector("#dl-test-all");
    start.textContent = downloading ? "Downloading…" : `Download selected (${count})`;
    start.disabled = downloading || !count;
    all.textContent = running.size ? "Testing…" : `Test selected (${count})`;
    all.disabled = Boolean(running.size) || !count;
    main.querySelector("#dl-total").textContent = data.total.toLocaleString();
    main.querySelector("#dl-unique").textContent = data.unique.toLocaleString();
    main.querySelector("#dl-selected").textContent = count.toLocaleString();
    main.querySelector("#dl-job").textContent = jobLabel(job);
    const banner = main.querySelector("#dl-login");
    banner.hidden = !login;
    if (login) {
      banner.innerHTML = login.login_status === "pending"
        ? `Waiting for you to approve the sign-in for <span class="mono">${esc(login.profile)}</span> in your browser${login.login_url ? `: <a href="${esc(login.login_url)}" target="_blank" rel="noopener noreferrer">open the approval page</a>` : ""}${login.login_code ? ` · code <b>${esc(login.login_code)}</b>` : ""}`
        : esc(login.error || "");
    }
  }

  async function runTests(main, ids) {
    ids.forEach((id) => running.add(id));
    list(main);
    const queue = [...ids];
    await Promise.all(Array.from({length: Math.min(TESTS_AT_ONCE, queue.length)}, async () => {
      while (queue.length) {
        const id = queue.shift();
        const account = accounts().find((a) => a.account_id === id);
        try {
          const r = await api(`/download/test/${id}`, {method: "POST"});
          account.info = {...account.info, connected: r.ok, arn: r.arn, error: r.error, denied: r.denied, errors: r.errors, checked_at: new Date().toISOString(),
            ...(r.created !== undefined ? {created: r.created, created_error: r.created_error, account_name: r.account_name} : {})};
        } catch (error) {
          account.info = {...account.info, connected: false, error: error.message, checked_at: new Date().toISOString()};
        }
        running.delete(id);
        if (main.querySelector("#dl-list")) list(main);
      }
    }));
  }

  async function connectionApi(path, options = {}) {
    const response = await fetch("/api/aws-accounts/connection" + path, {cache: "no-store", ...options, headers: {"Content-Type": "application/json"}});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "The sign-in couldn't start.");
    return body;
  }

  // OWL's sign-in: `aws sso login` for this account's profile. One approval usually covers
  // every account in the same SSO session, so everything selected is tested again afterwards.
  async function signIn(main, account) {
    if (!account.profile) return toast("This account has no profile in the AWS config.");
    try {
      await connectionApi("", {method: "PUT", body: JSON.stringify({profile: account.profile})});
      login = await connectionApi("/login", {method: "POST"});
      list(main);
      while (login.login_status === "pending" && main.querySelector("#dl-list")) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        login = await connectionApi("");
        if (main.querySelector("#dl-list")) list(main);
      }
      const failed = login.login_status !== "approved" && login.status !== "connected";
      if (failed && login.error) toast(login.error);
      login = null;
      if (main.querySelector("#dl-list")) {
        list(main);
        runTests(main, [...new Set([account.account_id, ...chosen().map((a) => a.account_id)])]);
      }
    } catch (error) {
      login = null;
      list(main);
      toast(error.message);
    }
  }

  function render(main, app) {
    main.innerHTML = `<div class="dl-head"><h1>AWS download</h1>
        <div class="dl-counts"><span><b id="dl-total"></b> total</span><span><b id="dl-unique"></b> unique</span><span><b id="dl-selected"></b> selected</span></div>
        <div class="dl-actions"><button type="button" id="dl-test-all"></button><button type="button" class="primary" id="dl-start"></button></div></div>
      ${data.error ? `<div class="error-box">${esc(data.error)}</div>` : ""}
      <div id="dl-login" class="dl-login" hidden></div>
      <div class="filters dl-filters"><input type="search" id="dl-search" placeholder="Search anything: name, account number, stage, profile" value="${esc(search)}">
        <label class="small"><input type="checkbox" id="dl-only" ${onlySelected ? "checked" : ""}> Selected only</label>
        <span id="dl-job" class="small muted" aria-live="polite"></span></div>
      <div id="dl-top-box" hidden><h2 class="dl-section">Most used <span class="muted small">top <span id="dl-top-count"></span></span></h2>
        <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-top"></tbody></table></div>
        <h2 class="dl-section">All accounts</h2></div>
      <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-list"></tbody></table></div>
      <p class="small muted">Accounts from <span class="mono">${esc(data.aws_config)}</span>, saved to <span class="mono">${esc(data.media)}</span>. Read-only.</p>`;
    main.querySelector("#dl-search").addEventListener("input", (e) => {
      search = e.target.value;
      clearTimeout(searchTyping);
      searchTyping = setTimeout(() => list(main), search.trim() ? 1000 : 0);
    });
    main.querySelector("#dl-only").addEventListener("change", (e) => {
      onlySelected = e.target.checked;
      list(main);
    });
    const onTableClick = async (e) => {
      const find = (id) => accounts().find((a) => a.account_id === id);
      const pick = e.target.closest("[data-pick]");
      if (pick) {
        const account = find(pick.dataset.pick);
        account.selected = !account.selected;
        list(main);
        try {
          await api("/download/selection", {method: "PUT", body: {account_ids: chosen().map((a) => a.account_id)}});
        } catch (error) {
          account.selected = !account.selected;
          list(main);
          toast(error.message);
        }
      }
      const test = e.target.closest("[data-test]");
      if (test && !running.has(test.dataset.test)) runTests(main, [test.dataset.test]);
      const status = e.target.closest("[data-status]");
      if (status && !running.has(status.dataset.status) && !login) {
        const account = find(status.dataset.status);
        if (account.info?.connected === true) runTests(main, [account.account_id]);
        else signIn(main, account);
      }
    };
    main.querySelector("#dl-list").addEventListener("click", onTableClick);
    main.querySelector("#dl-top").addEventListener("click", onTableClick);
    main.querySelector("#dl-test-all").addEventListener("click", () => runTests(main, chosen().map((a) => a.account_id)));
    main.querySelector("#dl-start").addEventListener("click", async () => {
      try {
        data.job = await api("/download/start", {method: "POST"});
        list(main);
        poll(main, app);
      } catch (error) {
        toast(error.message);
      }
    });
    list(main);
  }

  async function poll(main, app) {
    clearTimeout(polling);
    const before = data.job?.state;
    const job = await api("/download/status");
    if (location.hash !== "#download" || !main.querySelector("#dl-list")) return;
    data.job = job;
    list(main);
    if (job.state === "running") {
      polling = setTimeout(() => poll(main, app), 1500);
    } else if (before === "running" && job.snapshot) {
      toast(job.state === "done" ? "Download finished." : "Download finished; some accounts failed.");
      await app.reload();  // each project picks its newest covering snapshot
      location.hash = "#download";
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
