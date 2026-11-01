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
  let testPolling, searchTyping, editingTag = "", editingHost = "", tagDraft = "", tagChoice = -1, tagFilter = "";
  const TAG_SUGGESTIONS = 8;
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

  const isConnected = (a) => a.info?.connected === true && (!a.session || new Date(a.session.expires_at).getTime() > Date.now());
  const deniedOf = (a, s) => [...new Set([...(a.info?.denied_types || []), ...(a.info?.denied || []), ...(s?.denied || [])])].sort();

  // Each account counts as its completed steps; failed and finished accounts count in full.
  const shareOf = (a) => (a.state === "done" || a.state === "failed" ? 1 : a.steps ? (a.step || 0) / a.steps : 0);
  const percent = (share) => `${Math.floor(share * 100)}%`;

  function lastDownload(job) {
    if (!job.finished) return "";
    const failed = job.accounts.filter((a) => a.state === "failed").length;
    const changed = job.accounts.reduce((sum, a) => sum + (a.changed || 0), 0);
    const total = changed + job.accounts.reduce((sum, a) => sum + (a.unchanged || 0), 0);
    return `Last download ${new Date(job.finished * 1000).toLocaleString()} · ${job.accounts.length - failed}/${job.accounts.length} downloaded${failed ? ` · ${failed} failed` : ""}`
      + (total ? ` · ${changed} of ${total} files changed` : "") + (job.snapshot_unchanged ? " · nothing new to analyse" : "") + (job.error ? ` · ${job.error}` : "");
  }

  function drawProgress(main, job) {
    const box = main.querySelector("#dl-progress-box");
    main.querySelector("#dl-job").textContent = job.state === "running" ? "" : lastDownload(job);
    box.hidden = job.state !== "running";
    if (box.hidden) return;
    const items = job.accounts || [];
    const share = items.reduce((sum, a) => sum + shareOf(a), 0) / (items.length || 1);
    const finished = items.filter((a) => a.state === "done" || a.state === "failed");
    const failed = finished.filter((a) => a.state === "failed").length;
    const names = new Map(data.groups.flatMap((g) => g.accounts.map((a) => [a.account_id, `${g.name} ${a.stage}`])));
    const current = items.filter((a) => a.state === "running");
    box.innerHTML = `<div class="dl-bar-row"><b>Overall</b><progress max="1000" value="${Math.floor(share * 1000)}"></progress><b class="dl-pct">${percent(share)}</b>
        <span class="small muted">${finished.length}/${items.length} accounts${failed ? ` · ${failed} failed` : ""}</span></div>
      ${current.map((a) => `<div class="dl-bar-row current"><span class="dl-bar-name" title="${esc(a.account_id)}">${esc(names.get(a.account_id) || a.account_id)}</span><progress max="1000" value="${Math.floor(shareOf(a) * 1000)}"></progress><span class="dl-pct">${percent(shareOf(a))}</span>
        <span class="small muted dl-bar-step">${a.steps ? `Step ${a.step}/${a.steps}` : "Starting"}${a.message ? ` · ${esc(a.message.replace(`${a.account_id}${" "}`, "").replace(`${a.account_id}: `, ""))}` : ""}</span></div>`).join("")}`;
  }


  function notes(a, states) {
    const s = states[a.account_id], i = a.info || {};
    const denied = deniedOf(a, s);
    return [
      ...(s?.state === "failed" || s?.error ? [`<div class="dl-problem">${esc(s.error)}</div>`] : i.connected === false && i.error ? [`<div class="dl-problem">${esc(i.error)}</div>`] : []),
      ...Object.entries(i.errors || {}).slice(0, 5).map(([k, v]) => `<div class="dl-denied">${esc(k)}: ${esc(v)}</div>`),
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
      <td><div class="dl-tags">${(a.tags || []).map((t) => `<span class="dl-tag">${esc(t)}<button type="button" data-untag="${esc(t)}" data-account="${esc(a.account_id)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("")}${editingTag === a.account_id ? `<input class="dl-tag-input" data-tag-input="${esc(a.account_id)}" placeholder="Type a tag" aria-label="Add a tag" maxlength="40" autocomplete="off" spellcheck="false">` : `<button type="button" class="dl-tag-add" data-tag-add="${esc(a.account_id)}" title="Add a tag" aria-label="Add a tag">+</button>`}</div></td>
      <td class="dl-count" title="New connections made: testing again during a live session doesn't count">${a.connects || 0}</td>
      <td><span class="dl-status ${state}" title="${esc(tip)}"><img src="${ICONS[state]}" alt="">${esc(label)}</span>${denied.length ? `<div><button type="button" class="link dl-retry" data-retry="${esc(a.account_id)}" title="${esc(`No access: ${denied.join(", ")}. Not called again. Click to try them on the next download.`)}">${denied.length} skipped</button></div>` : ""}</td>
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
    drawProgress(main, job);
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
    showTagEditor(main);
  }

  // Tag box: suggestions come from tags already used, most used first; empty shows the top ones.
  function tagCounts() {
    const counts = new Map();
    accounts().flatMap((a) => a.tags || []).forEach((t) => counts.set(t, (counts.get(t) || 0) + 1));
    return counts;
  }

  function tagSuggestions(account, text) {
    const typed = text.trim().toLowerCase();
    const mine = new Set((account.tags || []).map((t) => t.toLowerCase()));
    return [...tagCounts()].filter(([t]) => !mine.has(t.toLowerCase()) && t.toLowerCase().includes(typed))
      .sort((a, b) => (typed ? b[0].toLowerCase().startsWith(typed) - a[0].toLowerCase().startsWith(typed) : 0) || b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, TAG_SUGGESTIONS);
  }

  function drawTagMenu(main) {
    const menu = main.querySelector("#dl-tag-menu");
    const input = editingHost && main.querySelector(`#${editingHost} [data-tag-input]`);
    const account = input && accounts().find((a) => a.account_id === input.dataset.tagInput);
    if (!account) {
      menu.hidden = true;
      return;
    }
    const text = input.value.trim();
    const items = tagSuggestions(account, text);
    tagChoice = Math.min(tagChoice, items.length - 1);
    const exists = [...tagCounts().keys(), ...(account.tags || [])].some((t) => t.toLowerCase() === text.toLowerCase());
    const hint = text && !exists ? `<div class="dl-tag-hint">Enter creates “${esc(text)}”</div>` : "";
    const heading = !text && items.length ? `<div class="dl-tag-hint">Most used</div>` : "";
    menu.innerHTML = heading + items.map(([tag, count], i) => `<button type="button" class="dl-tag-option ${i === tagChoice ? "active" : ""}" data-tag-choice="${esc(tag)}" tabindex="-1"><span>${esc(tag)}</span><span class="muted">${count}</span></button>`).join("") + hint;
    menu.hidden = !(heading || items.length || hint);
    const box = input.getBoundingClientRect();
    menu.style.left = `${box.left}px`;
    menu.style.top = `${box.bottom + 4}px`;
  }

  function showTagEditor(main) {
    if (!editingTag) return drawTagMenu(main);
    main.querySelectorAll("[data-tag-input]").forEach((input) => {
      if (input.closest("tbody").id !== editingHost) input.closest(".dl-tags").querySelector("[data-tag-input]").hidden = true;
    });
    const input = main.querySelector(`#${editingHost} [data-tag-input]`);
    if (!input) {
      editingTag = "";
      return drawTagMenu(main);
    }
    input.value = tagDraft;
    input.focus();
    input.setSelectionRange(tagDraft.length, tagDraft.length);
    drawTagMenu(main);
  }

  function closeTagEditor(main) {
    editingTag = "";
    editingHost = "";
    tagDraft = "";
    tagChoice = -1;
    list(main);
  }

  // Runs in the backend, so closing or leaving the page doesn't stop it; this only follows along.
  async function runTests(main, ids) {
    ids.forEach((id) => running.add(id));
    list(main);
    try {
      await api("/download/tests", {method: "POST", body: {account_ids: ids}});
    } catch (error) {
      ids.forEach((id) => running.delete(id));
      list(main);
      return toast(error.message);
    }
    followTests(main);
  }

  async function followTests(main) {
    clearTimeout(testPolling);
    const job = await api("/download/tests");
    if (!main.querySelector("#dl-list")) return;
    running.clear();
    (job.accounts || []).filter((a) => a.state !== "done").forEach((a) => running.add(a.account_id));
    if (job.state === "running") {
      list(main);
      testPolling = setTimeout(() => followTests(main), 1000);
      return;
    }
    const downloadJob = data.job;
    data = {...(await api("/download")), job: downloadJob};  // saved results, Recent and Most used
    list(main);
  }

  function render(main, app) {
    main.innerHTML = `<div class="dl-head"><h1>AWS download</h1>
        <div class="dl-counts"><span><b id="dl-total"></b> total</span><span><b id="dl-unique"></b> unique</span><span><b id="dl-selected"></b> selected</span></div>
        <div class="dl-actions"><button type="button" id="dl-test-again"></button><button type="button" id="dl-test-all"></button><button type="button" class="primary" id="dl-start"></button></div></div>
      ${data.error ? `<div class="error-box">${esc(data.error)}</div>` : ""}
      <div class="filters dl-filters"><input type="search" id="dl-search" placeholder="Search anything: name, account number, stage, profile" value="${esc(search)}">
        <select id="dl-tag-filter" aria-label="Filter by tag" hidden></select>
        <label class="small"><input type="checkbox" id="dl-only" ${onlySelected ? "checked" : ""}> Selected only</label>
        <span id="dl-job" class="small muted" aria-live="polite"></span></div>
      <div id="dl-progress-box" class="dl-progress-box" role="status" hidden></div>
      <div id="dl-recent-box" hidden><h2 class="dl-section">Recent <span class="muted small">latest <span id="dl-recent-count"></span> tested, downloaded or copied in AWS Accounts</span></h2>
        <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-recent"></tbody></table></div></div>
      <div id="dl-top-box" hidden><h2 class="dl-section">Most used <span class="muted small">top <span id="dl-top-count"></span> by tests, downloads and AWS Accounts copies</span></h2>
        <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-top"></tbody></table></div></div>
      <h2 class="dl-section">All accounts</h2>
      <div class="table-wrap"><table class="data dl-table"><thead><tr><th>Name</th><th>Stage</th><th>Account number</th><th>Account created</th><th>Last download</th><th>Tags</th><th>Times connected</th><th>Status</th><th>Test</th></tr></thead><tbody id="dl-list"></tbody></table></div>
      <div id="dl-tag-menu" class="dl-tag-menu" hidden></div>
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
    // Use the existing spelling when a tag is typed in another case, so "Prod" and "prod" stay one tag.
    const assignTag = (account, text) => {
      const value = text.trim();
      const existing = [...tagCounts().keys()].find((t) => t.toLowerCase() === value.toLowerCase());
      tagDraft = "";
      tagChoice = -1;
      if (!value) return list(main);
      saveTags(account, [...(account.tags || []), existing || value]);
    };
    const onTagKey = (e) => {
      const input = e.target.closest("[data-tag-input]");
      if (!input) return;
      const account = accounts().find((a) => a.account_id === input.dataset.tagInput);
      const items = tagSuggestions(account, input.value);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (items.length) tagChoice = e.key === "ArrowDown" ? (tagChoice + 1) % items.length : (tagChoice <= 0 ? items.length : tagChoice) - 1;
        drawTagMenu(main);
      } else if (e.key === "Escape") {
        closeTagEditor(main);
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (!input.value.trim() && tagChoice < 0) return closeTagEditor(main);
        assignTag(account, tagChoice >= 0 ? items[tagChoice][0] : input.value);
      }
    };
    const onTagInput = (e) => {
      if (!e.target.closest("[data-tag-input]")) return;
      tagDraft = e.target.value;
      tagChoice = -1;
      drawTagMenu(main);
    };
    const onTagLeave = (e) => {
      if (!e.target.closest("[data-tag-input]") || !editingTag) return;
      const account = accounts().find((a) => a.account_id === e.target.dataset.tagInput);
      const value = e.target.value;
      editingTag = "";
      editingHost = "";
      tagDraft = "";
      tagChoice = -1;
      if (value.trim()) assignTag(account, value);
      else list(main);
    };
    for (const id of ["#dl-list", "#dl-top", "#dl-recent"]) {
      main.querySelector(id).addEventListener("keydown", onTagKey);
      main.querySelector(id).addEventListener("input", onTagInput);
      main.querySelector(id).addEventListener("focusout", onTagLeave);
    }
    const menu = main.querySelector("#dl-tag-menu");
    // mousedown, so choosing a suggestion doesn't first blur the box.
    menu.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const option = e.target.closest("[data-tag-choice]");
      if (option) assignTag(accounts().find((a) => a.account_id === editingTag), option.dataset.tagChoice);
    });
    main.addEventListener("scroll", () => drawTagMenu(main), {passive: true});
    const onTableClick = async (e) => {
      const add = e.target.closest("[data-tag-add]");
      if (add) {
        editingTag = add.dataset.tagAdd;
        editingHost = add.closest("tbody").id;
        tagDraft = "";
        tagChoice = -1;
        list(main);
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
      polling = setTimeout(() => poll(main, app), 1000);
    } else if (before === "running" && job.snapshot) {
      const newly = job.accounts.filter((a) => a.new_denied?.length);
      toast((job.state === "done" ? "Download finished." : "Download finished; some accounts failed.")
        + (newly.length ? ` No access to some types in ${newly.length} account${newly.length === 1 ? "" : "s"}; they'll be skipped from now on.` : ""));
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
    if (data.tests?.state === "running") followTests(main);
  };
})();
