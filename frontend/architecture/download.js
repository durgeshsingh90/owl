"use strict";
// AWS download: one line per account, the name shown once. Click a stage to select it, test
// one line, all selected or all tested before, then download what's connected. Signing in to
// AWS happens outside this page.
AA.views.download = (() => {
  const {esc, api, toast} = AA;
  const TESTS_AT_ONCE = 20;
  const SHARED = "../bitbucket/assets/";
  const ICONS = {connected: `${SHARED}connected.png?v=2`, checking: `${SHARED}no-connection.gif?v=2`, failed: `${SHARED}disconnected.png?v=2`, unknown: "assets/not-tested.png"};
  let data = null, search = "", onlySelected = false, polling = null;
  const running = new Set();
  let searchTyping, ticker, editingTag = "", tagFilter = "";
  const everTested = (a) => a.info?.connected === true || a.info?.connected === false;

  const accounts = () => data.groups.flatMap((g) => g.accounts);
  const chosen = () => accounts().filter((a) => a.selected);
  const dateOnly = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "");
  const stamp = (iso) => (iso ? new Date(iso).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}) : "");

  // Never tried: Not tested. Tried but never worked: Not connected. Worked before: Disconnected.
  function statusOf(a) {
    if (running.has(a.account_id)) return ["checking", "Testing…", ""];
    const i = a.info || {};
    if (isConnected(a)) return ["connected", "Connected", `${i.arn || ""}. Last tested ${stamp(i.checked_at)}.`];
    if (a.connects > 0 || i.connected === true) return ["failed", "Disconnected", i.error || "The session has ended."];
    if (i.connected === false) return ["failed", "Not connected", i.error || ""];
    return ["unknown", "Not tested", "Not tested yet."];
  }

  function jobLabel(job) {
    if (job.state === "running") {
      const finished = job.accounts.filter((a) => a.state === "done" || a.state === "failed");
      const failed = finished.filter((a) => a.state === "failed").length;
      return `${finished.length}/${job.accounts.length} accounts${failed ? ` · ${failed} failed` : ""} · ${job.message || ""}`;
    }
    if (!job.finished) return "";
    const failed = job.accounts.filter((a) => a.state === "failed").length;
    return `Last download ${new Date(job.finished * 1000).toLocaleString()} · ${job.accounts.length - failed}/${job.accounts.length} downloaded${failed ? ` · ${failed} failed` : ""}${job.error ? ` · ${job.error}` : ""}`;
  }

  const isConnected = (a) => a.info?.connected === true && (!a.session || new Date(a.session.expires_at).getTime() > Date.now());

  function timeLeft(session) {
    const left = Math.floor((new Date(session.expires_at).getTime() - Date.now()) / 1000);
    if (left <= 0) return "Expired";
    const pad = (n) => String(n).padStart(2, "0");
    return `${session.source === "sso" ? "" : "~"}${Math.floor(left / 3600)}h ${pad(Math.floor(left / 60) % 60)}m ${pad(left % 60)}s left`;
  }

  // Counts down once a second without redrawing the table; at zero the row shows Not connected.
  function tick(main) {
    let expired = false;
    main.querySelectorAll(".dl-timer").forEach((node) => {
      const account = accounts().find((a) => a.account_id === node.dataset.account);
      if (!account?.session) return;
      node.textContent = timeLeft(account.session);
      const left = new Date(account.session.expires_at).getTime() - Date.now();
      node.classList.toggle("soon", left < 3600000);
      if (left <= 0 && account.info?.connected) {
        account.info = {...account.info, connected: false, error: "The session has ended."};
        expired = true;
      }
    });
    if (expired) list(main);
  }

  const deniedOf = (a, s) => [...new Set([...(a.info?.denied_types || []), ...(a.info?.denied || []), ...(s?.denied || [])])].sort();

  const downloadingNow = (job) => job.state === "running";

  function notes(a, states) {
    const s = states[a.account_id], i = a.info || {};
    const denied = deniedOf(a, s);
    return [
      ...(s?.state === "failed" || s?.error ? [`<div class="dl-problem">${esc(s.error)}</div>`] : i.connected === false && i.error ? [`<div class="dl-problem">${esc(i.error)}</div>`] : []),
      ...Object.entries(i.errors || {}).slice(0, 5).map(([k, v]) => `<div class="dl-denied">${esc(k)}: ${esc(v)}</div>`),
      ...(denied.length ? [`<div class="dl-denied">No access, not called again: ${esc(denied.join(", "))} <button type="button" class="link" data-retry="${esc(a.account_id)}">Try again</button></div>`] : []),
    ].join("");
  }

  // One account's line. nameCell is the Name column: omitted while a rowspan covers it.
  function line(a, states, nameCell, first) {
    const [state, label, tip] = statusOf(a);
    const s = states[a.account_id], i = a.info || {};
    const denied = deniedOf(a, s);
    const problems = notes(a, states);
    const job = s?.state === "running" ? "Downloading…" : s?.state === "queued" ? "Queued" : stamp(a.downloaded_at);
    return `<tr class="${first ? "dl-first" : ""}">${nameCell}
      <td><button type="button" class="dl-pick" data-pick="${esc(a.account_id)}" aria-pressed="${a.selected}" title="${a.selected ? "Selected for Test selected. Click to remove" : "Click to select for Test selected"}">${esc(a.stage)}</button></td>
      <td class="mono">${esc(a.account_id)}</td>
      <td class="nowrap" ${i.created_error ? `title="${esc(i.created_error)}"` : ""}>${i.created ? esc(dateOnly(i.created)) : `<span class="muted">${i.created_error ? "No access" : "—"}</span>`}</td>
      <td class="small nowrap">${job ? esc(job) : '<span class="muted">Never</span>'}</td>
      <td><div class="dl-tags">${(a.tags || []).map((t) => `<span class="dl-tag">${esc(t)}<button type="button" data-untag="${esc(t)}" data-account="${esc(a.account_id)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("")}${editingTag === a.account_id ? `<input class="dl-tag-input" data-tag-input="${esc(a.account_id)}" list="dl-tag-list" placeholder="New or existing" aria-label="Add a tag" maxlength="40">` : `<button type="button" class="dl-tag-add" data-tag-add="${esc(a.account_id)}" title="Add a tag" aria-label="Add a tag">+</button>`}</div></td>
      <td class="dl-count" title="New connections made: testing again during a live session doesn't count">${a.connects || 0}</td>
      <td><span class="dl-status ${state}" title="${esc(tip)}"><img src="${ICONS[state]}" alt="">${esc(label)}</span>${state === "connected" && a.session ? `<div class="dl-timer" data-expires="${esc(a.session.expires_at)}" data-account="${esc(a.account_id)}" title="${esc(a.session.source === "sso" ? `Session ends ${stamp(a.session.expires_at)}, from the AWS sign-in` : `About 12 hours from the first successful test, ending ${stamp(a.session.expires_at)}`)}">${esc(timeLeft(a.session))}</div>` : ""}${state === "connected" && denied.length ? `<div class="dl-partial">${denied.length} type${denied.length === 1 ? "" : "s"} denied</div>` : ""}</td>
      <td><button type="button" class="dl-test" data-test="${esc(a.account_id)}" ${running.has(a.account_id) ? "disabled" : ""}>Test</button></td></tr>${problems ? `<tr class="dl-note"><td colspan="8">${problems}</td></tr>` : ""}`;
  }

  const nameOf = (group, a) => `${esc(group.name)}${a.info?.account_name && a.info.account_name !== group.name ? `<div class="small muted dl-aws-name">${esc(a.info.account_name)}</div>` : ""}`;

  function lines(group, items, states) {
    const spans = items.length + items.filter((a) => notes(a, states)).length;
    return items.map((a, index) => line(a, states, index ? "" : `<td class="dl-name" rowspan="${spans}">${nameOf(group, a)}</td>`, !index)).join("");
  }

  // Anything can be searched: words can be in any order and match name, stage, number or profile.
  function matches(group, a, words) {
    const text = [group.name, a.account_id, a.stage, a.profile, ...(a.profiles || []), ...(a.tags || []), a.info?.account_name || ""].join(" ").toLowerCase();
    return words.every((word) => text.includes(word));
  }

  function list(main) {
    const job = data.job || {};
    const states = Object.fromEntries((job.accounts || []).map((a) => [a.account_id, a]));
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const rows = data.groups.map((g) => {
      const items = g.accounts.filter((a) => (!onlySelected || a.selected) && (!tagFilter || (a.tags || []).includes(tagFilter)) && matches(g, a, words));
      return items.length ? lines(g, items, states) : "";
    }).join("");
    main.querySelector("#dl-list").innerHTML = rows || `<tr><td colspan="9" class="empty">No accounts match.</td></tr>`;
    const groupOf = new Map(data.groups.flatMap((g) => g.accounts.map((a) => [a.account_id, [g, a]])));
    const browsing = !words.length && !onlySelected && !tagFilter;
    main.querySelector("#dl-tag-list").innerHTML = (data.all_tags || []).map((t) => `<option value="${esc(t)}">`).join("");
    const tagSelect = main.querySelector("#dl-tag-filter");
    tagSelect.innerHTML = `<option value="">Any tag</option>` + (data.all_tags || []).map((t) => `<option ${t === tagFilter ? "selected" : ""}>${esc(t)}</option>`).join("");
    tagSelect.hidden = !(data.all_tags || []).length;
    const section = (ids, box, body, count, extra) => {
      const items = browsing ? (ids || []).map((id) => groupOf.get(id)).filter(Boolean) : [];
      main.querySelector(box).hidden = !items.length;
      main.querySelector(count).textContent = items.length;
      main.querySelector(body).innerHTML = items.map(([g, a]) => line(a, states, `<td class="dl-name" rowspan="${notes(a, states) ? 2 : 1}">${nameOf(g, a)}<div class="small muted dl-aws-name">${esc(extra(a))}</div></td>`, true)).join("");
    };
    section(data.top, "#dl-top-box", "#dl-top", "#dl-top-count", (a) => `Used ${a.uses} time${a.uses === 1 ? "" : "s"}`);
    section(data.recent, "#dl-recent-box", "#dl-recent", "#dl-recent-count", (a) => `Last used ${stamp(a.last_used)}`);
    const progress = main.querySelector("#dl-progress");
    progress.hidden = !downloadingNow(job);
    if (job.accounts) {
      progress.max = job.accounts.length || 1;
      progress.value = job.accounts.filter((a) => a.state === "done" || a.state === "failed").length;
    }
    const count = chosen().length;
    const downloading = job.state === "running";
    const start = main.querySelector("#dl-start"), all = main.querySelector("#dl-test-all");
    const connected = accounts().filter((a) => isConnected(a) && a.session).length;
    start.textContent = downloading ? "Downloading…" : `Download connected (${connected.toLocaleString()})`;
    start.disabled = downloading || !connected;
    start.title = connected ? "Downloads every connected account" : "Test the accounts you want first. Only connected accounts download.";
    all.textContent = running.size ? "Testing…" : `Test selected (${count})`;
    all.disabled = Boolean(running.size) || !count;
    const again = main.querySelector("#dl-test-again");
    const tested = accounts().filter(everTested).length;
    again.textContent = running.size ? `Testing ${running.size} left…` : `Test all (${tested.toLocaleString()})`;
    again.title = "Tests again only accounts tested before. Never-tested accounts are skipped.";
    again.disabled = Boolean(running.size) || !tested;
    main.querySelector("#dl-total").textContent = data.total.toLocaleString();
    main.querySelector("#dl-unique").textContent = data.unique.toLocaleString();
    main.querySelector("#dl-selected").textContent = count.toLocaleString();
    main.querySelector("#dl-job").textContent = jobLabel(job);
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
          if (r.ok && !isConnected(account)) account.connects = (account.connects || 0) + 1;
          account.info = {...account.info, connected: r.ok, arn: r.arn, error: r.error, denied: r.denied, errors: r.errors, checked_at: new Date().toISOString(),
            ...(r.created !== undefined ? {created: r.created, created_error: r.created_error, account_name: r.account_name} : {})};
        } catch (error) {
          account.info = {...account.info, connected: false, error: error.message, checked_at: new Date().toISOString()};
        }
        running.delete(id);
        if (main.querySelector("#dl-list")) list(main);
      }
    }));
    if (!running.size && main.querySelector("#dl-list")) {  // Recent and Most used changed
      const job = data.job;
      data = {...(await api("/download")), job};
      list(main);
    }
  }

  function render(main, app) {
    main.innerHTML = `<div class="dl-head"><h1>AWS download</h1>
        <div class="dl-counts"><span><b id="dl-total"></b> total</span><span><b id="dl-unique"></b> unique</span><span><b id="dl-selected"></b> selected</span></div>
        <div class="dl-actions"><button type="button" id="dl-test-again"></button><button type="button" id="dl-test-all"></button><button type="button" class="primary" id="dl-start"></button></div></div>
      ${data.error ? `<div class="error-box">${esc(data.error)}</div>` : ""}
      <div class="filters dl-filters"><input type="search" id="dl-search" placeholder="Search anything: name, account number, stage, profile" value="${esc(search)}">
        <select id="dl-tag-filter" aria-label="Filter by tag" hidden></select><datalist id="dl-tag-list"></datalist>
        <label class="small"><input type="checkbox" id="dl-only" ${onlySelected ? "checked" : ""}> Selected only</label>
        <span id="dl-job" class="small muted" aria-live="polite"></span></div>
      <progress id="dl-progress" class="dl-progress" hidden></progress>
      <div id="dl-recent-box" hidden><h2 class="dl-section">Recent <span class="muted small">latest <span id="dl-recent-count"></span> tested, downloaded or copied in AWS Accounts</span></h2>
        <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-recent"></tbody></table></div></div>
      <div id="dl-top-box" hidden><h2 class="dl-section">Most used <span class="muted small">top <span id="dl-top-count"></span> by tests, downloads and AWS Accounts copies</span></h2>
        <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-top"></tbody></table></div></div>
      <h2 class="dl-section">All accounts</h2>
      <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-list"></tbody></table></div>
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
    main.querySelector("#dl-tag-filter").addEventListener("change", (e) => {
      tagFilter = e.target.value;
      list(main);
    });
    const saveTags = async (account, tags) => {
      const previous = account.tags;
      account.tags = tags;
      try {
        const saved = await api(`/download/tags/${account.account_id}`, {method: "PUT", body: {tags}});
        account.tags = saved.tags;
        data.all_tags = [...new Set(accounts().flatMap((a) => a.tags || []))].sort((x, y) => x.localeCompare(y));
        if (tagFilter && !data.all_tags.includes(tagFilter)) tagFilter = "";
      } catch (error) {
        account.tags = previous;
        toast(error.message);
      }
      list(main);
    };
    const onTagKey = (e) => {
      const input = e.target.closest("[data-tag-input]");
      if (!input) return;
      if (e.type === "keydown" && e.key === "Escape") {
        editingTag = "";
        return list(main);
      }
      if (e.type === "keydown" && e.key !== "Enter") return;
      const account = accounts().find((a) => a.account_id === input.dataset.tagInput);
      const value = input.value.trim();
      if (!editingTag) return;
      editingTag = "";
      if (value) saveTags(account, [...(account.tags || []), value]);
      else list(main);
    };
    for (const id of ["#dl-list", "#dl-top", "#dl-recent"]) {
      main.querySelector(id).addEventListener("keydown", onTagKey);
      main.querySelector(id).addEventListener("focusout", onTagKey);
    }
    const onTableClick = async (e) => {
      const add = e.target.closest("[data-tag-add]");
      if (add) {
        editingTag = add.dataset.tagAdd;
        list(main);
        main.querySelector(`[data-tag-input="${editingTag}"]`)?.focus();
        return;
      }
      const untag = e.target.closest("[data-untag]");
      if (untag) {
        const account = accounts().find((a) => a.account_id === untag.dataset.account);
        saveTags(account, (account.tags || []).filter((t) => t !== untag.dataset.untag));
        return;
      }
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
      const retry = e.target.closest("[data-retry]");
      if (retry && !running.has(retry.dataset.retry)) {
        const account = find(retry.dataset.retry);
        await api(`/download/denied/${account.account_id}/reset`, {method: "POST"});
        account.info = {...account.info, denied: [], denied_types: []};
        toast("These types will be checked again on the next download.");
        list(main);
      }
    };
    main.querySelector("#dl-list").addEventListener("click", onTableClick);
    main.querySelector("#dl-top").addEventListener("click", onTableClick);
    main.querySelector("#dl-recent").addEventListener("click", onTableClick);
    main.querySelector("#dl-test-again").addEventListener("click", () => runTests(main, accounts().filter(everTested).map((a) => a.account_id)));
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
    clearInterval(ticker);
    ticker = setInterval(() => {
      if (!main.querySelector("#dl-list")) clearInterval(ticker);
      else tick(main);
    }, 1000);
    tick(main);
    if (data.job?.state === "running") poll(main, app);
  };
})();
