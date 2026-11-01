"use strict";
// AWS download: one row per mc-<name>-<stage> name with a button per stage. Click a stage to
// select it, test connectivity for one stage or all selected, then download the selected ones.
AA.views.download = (() => {
  const {esc, api, toast} = AA;
  const TESTS_AT_ONCE = 6;
  let data = null, search = "", onlySelected = false, polling = null;
  const tests = {};  // account id -> {state: "running"} or the test result

  const accounts = () => data.groups.flatMap((g) => g.accounts);
  const chosen = () => accounts().filter((a) => a.selected);

  function testState(a) {
    const t = tests[a.account_id];
    if (!t) return ["", "Not tested"];
    if (t.state === "running") return ["running", "Testing…"];
    if (!t.ok) return ["bad", t.error];
    if (t.denied?.length || Object.keys(t.errors || {}).length) return ["partial", `Connected: ${t.arn}. ${t.allowed.length} types readable, ${t.denied.length} denied`];
    return ["ok", `Connected: ${t.arn}. All ${t.allowed?.length || 0} types readable`];
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

  function row(group, states) {
    const problems = group.accounts.flatMap((a) => {
      const t = tests[a.account_id], s = states[a.account_id];
      const denied = s?.denied?.length ? s.denied : t?.denied || [];
      const text = s?.state === "failed" || s?.error ? s.error : t && t.state !== "running" && !t.ok ? t.error : "";
      return [text ? `<div class="dl-problem"><b>${esc(a.stage)}</b> ${esc(text)}</div>` : "",
        ...(t?.state !== "running" ? Object.entries(t?.errors || {}).map(([k, v]) => `<div class="dl-denied"><b>${esc(a.stage)}</b> ${esc(k)}: ${esc(v)}</div>`) : []),
        denied.length ? `<div class="dl-denied"><b>${esc(a.stage)}</b> no access: ${esc(denied.join(", "))}</div>` : ""].filter(Boolean);
    }).join("");
    return `<div class="dl-row"><div class="dl-name">${esc(group.name)}</div><div class="dl-stages">${group.accounts.map((a) => {
      const [state, tip] = testState(a);
      const s = states[a.account_id];
      const done = s?.state === "done" ? " · downloaded" : s?.state === "running" ? " · downloading" : a.downloaded_at ? ` · downloaded ${new Date(a.downloaded_at).toLocaleString()}` : "";
      return `<span class="dl-stage ${a.selected ? "on" : ""}"><button type="button" class="dl-pick" data-pick="${esc(a.account_id)}" aria-pressed="${a.selected}" title="${esc(`${a.profile || "No profile"} · ${a.account_id}${done}`)}">${esc(a.stage)}</button><button type="button" class="dl-test ${state}" data-test="${esc(a.account_id)}" aria-label="Test ${esc(group.name)} ${esc(a.stage)}" title="${esc(tip)}">${state === "ok" ? "✓" : state === "partial" ? "!" : state === "bad" ? "✗" : state === "running" ? "…" : "Test"}</button></span>`;
    }).join("")}</div>${problems}</div>`;
  }

  function list(main) {
    const job = data.job || {};
    const states = Object.fromEntries((job.accounts || []).map((a) => [a.account_id, a]));
    const text = search.trim().toLowerCase();
    const shown = data.groups.filter((g) => (!onlySelected || g.accounts.some((a) => a.selected))
      && (!text || g.name.includes(text) || g.accounts.some((a) => a.account_id.includes(text) || a.stage.includes(text))));
    main.querySelector("#dl-list").innerHTML = shown.map((g) => row(g, states)).join("") || `<div class="empty">No accounts match.</div>`;
    const count = chosen().length;
    const running = job.state === "running";
    const testing = Object.values(tests).some((t) => t.state === "running");
    const start = main.querySelector("#dl-start"), all = main.querySelector("#dl-test-all");
    start.textContent = running ? "Downloading…" : `Download selected (${count})`;
    start.disabled = running || !count;
    all.textContent = testing ? "Testing…" : `Test selected (${count})`;
    all.disabled = testing || !count;
    main.querySelector("#dl-total").textContent = data.total.toLocaleString();
    main.querySelector("#dl-unique").textContent = data.unique.toLocaleString();
    main.querySelector("#dl-selected").textContent = count.toLocaleString();
    main.querySelector("#dl-job").textContent = jobLabel(job);
  }

  async function runTests(main, ids) {
    ids.forEach((id) => (tests[id] = {state: "running"}));
    list(main);
    const queue = [...ids];
    await Promise.all(Array.from({length: Math.min(TESTS_AT_ONCE, queue.length)}, async () => {
      while (queue.length) {
        const id = queue.shift();
        try {
          tests[id] = await api(`/download/test/${id}`, {method: "POST"});
        } catch (error) {
          tests[id] = {ok: false, error: error.message};
        }
        if (main.querySelector("#dl-list")) list(main);
      }
    }));
  }

  function render(main, app) {
    main.innerHTML = `<div class="dl-head"><h1>AWS download</h1>
        <div class="dl-counts"><span><b id="dl-total"></b> total</span><span><b id="dl-unique"></b> unique</span><span><b id="dl-selected"></b> selected</span></div>
        <div class="dl-actions"><button type="button" id="dl-test-all"></button><button type="button" class="primary" id="dl-start"></button></div></div>
      ${data.error ? `<div class="error-box">${esc(data.error)}</div>` : ""}
      <div class="filters dl-filters"><input type="search" id="dl-search" placeholder="Search name, stage or account number" value="${esc(search)}">
        <label class="small"><input type="checkbox" id="dl-only" ${onlySelected ? "checked" : ""}> Selected only</label>
        <span id="dl-job" class="small muted" aria-live="polite"></span></div>
      <div id="dl-list" class="card dl-list"></div>
      <p class="small muted">Accounts from <span class="mono">${esc(data.aws_config)}</span>, saved to <span class="mono">${esc(data.media)}</span>. Read-only.</p>`;
    main.querySelector("#dl-search").addEventListener("input", (e) => {
      search = e.target.value;
      list(main);
    });
    main.querySelector("#dl-only").addEventListener("change", (e) => {
      onlySelected = e.target.checked;
      list(main);
    });
    main.querySelector("#dl-list").addEventListener("click", async (e) => {
      const pick = e.target.closest("[data-pick]");
      if (pick) {
        const account = accounts().find((a) => a.account_id === pick.dataset.pick);
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
      if (test && tests[test.dataset.test]?.state !== "running") runTests(main, [test.dataset.test]);
    });
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
      const fresh = await api("/download");
      data = fresh;
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
