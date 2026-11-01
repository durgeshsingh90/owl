"use strict";
// Timeline: every resource creation and change in the snapshot, newest first, under the same
// date headings as the Bitbucket timeline (Today, Yesterday, This week, ... then month by month).
AA.views.timeline = (() => {
  const {esc, api} = AA;
  const PAGE = 100;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const SOURCES = {aws: "AWS API", config: "AWS Config", cloudtrail: "CloudTrail"};
  const filter = {account: "", kind: "", action: "", text: "", page: 1, scope: "all"};
  let data = null, loadedFor = "", chosen = "";

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  }

  // The heading a moment falls under, relative to now (local time).
  function group(iso, now = new Date()) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return "Unknown date";
    const day = startOfDay(at), today = startOfDay(now), dayMs = 86400000;
    const monday = today - ((now.getDay() + 6) % 7) * dayMs;
    const ranges = [
      ["Today", today, today + dayMs], ["Yesterday", today - dayMs, today], ["Day before yesterday", today - 2 * dayMs, today - dayMs],
      ["This week", monday, today + dayMs], ["Last week", monday - 7 * dayMs, monday],
      ["This month", new Date(now.getFullYear(), now.getMonth(), 1).getTime(), today + dayMs],
      ["Last month", new Date(now.getFullYear(), now.getMonth() - 1, 1).getTime(), new Date(now.getFullYear(), now.getMonth(), 1).getTime()],
      ["This year", new Date(now.getFullYear(), 0, 1).getTime(), today + dayMs],
    ];
    const hit = ranges.find(([, start, end]) => day >= start && day < end);
    return hit ? hit[0] : `${MONTHS[at.getMonth()]} ${at.getFullYear()}`;
  }

  function render(main, app) {
    const project = app.project();
    const projectAccounts = new Set(project ? [...Object.values(project.environments).map((b) => b.account_id), ...(project.aws_accounts || []).map((a) => a.account_id)] : []);
    const inScope = data.events.filter((e) => filter.scope === "all" || !projectAccounts.size || projectAccounts.has(e.account));
    const text = filter.text.trim().toLowerCase();
    const rows = inScope.filter((e) => (!filter.account || e.account === filter.account) && (!filter.kind || e.kind === filter.kind)
      && (!filter.action || e.action === filter.action)
      && (!text || `${e.label} ${e.id} ${e.by} ${e.event} ${e.vpc}`.toLowerCase().includes(text)));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    filter.page = Math.min(filter.page, pages);
    const start = (filter.page - 1) * PAGE;
    const now = new Date();
    const counts = new Map();
    rows.forEach((e) => counts.set(group(e.time, now), (counts.get(group(e.time, now)) || 0) + 1));
    const accounts = [...new Map(inScope.map((e) => [e.account, e.account_name])).entries()].sort();
    const kinds = [...new Set(inScope.map((e) => e.kind))].sort();
    let previous = null;
    const body = rows.slice(start, start + PAGE).map((e, i) => {
      const heading = group(e.time, now);
      const separator = heading !== previous ? `<tr class="timeline-date-row"><th colspan="9" scope="rowgroup"><span class="timeline-marker" aria-hidden="true"></span>
        <strong>${esc(heading)}</strong><span class="muted">${counts.get(heading)} ${counts.get(heading) === 1 ? "event" : "events"}</span></th></tr>` : "";
      previous = heading;
      const at = new Date(e.time);
      return `${separator}<tr class="clickable timeline-row" tabindex="0" data-kind="${esc(e.collection)}" data-id="${esc(e.id)}">
        <td class="muted small">${start + i + 1}</td>
        <td class="nowrap">${esc(at.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}))}<br><span class="muted small">${esc(at.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}))}</span></td>
        <td><span class="action ${e.action === "Created" ? "created" : "updated"}">${esc(e.action)}</span></td>
        <td><b>${esc(e.label)}</b>${e.label !== e.id ? `<br><span class="mono muted">${esc(e.id.split("/").pop())}</span>` : ""}</td>
        <td>${esc(e.kind)}</td>
        <td>${esc(e.account_name || e.account)}<br><span class="mono muted">${esc(e.account)}</span></td>
        <td class="mono small">${esc(e.vpc || "")}</td>
        <td class="small">${esc(e.by || "")}${e.event ? `<br><span class="muted">${esc(e.event)}</span>` : ""}${e.changes > 1 ? `<br><span class="muted">${e.changes} changes in window</span>` : ""}</td>
        <td class="small muted">${esc(SOURCES[e.source] || e.source)}</td></tr>`;
    }).join("");
    main.innerHTML = `<div class="page-head"><div><h1>Timeline</h1>
        <p>When each resource was created and last changed, newest first.</p></div>
        <select id="tl-snapshot" aria-label="Snapshot">${app.overview.snapshots.map((s) => `<option value="${esc(s.id)}" ${s.id === loadedFor ? "selected" : ""}>${esc(s.name)} · ${esc(AA.when(s.created))}</option>`).join("")}</select>
        <div class="small muted">${data.events.length.toLocaleString()} events · ${data.without_times.toLocaleString()} of ${data.resources.toLocaleString()} resources have no known times</div></div>
      <div class="filters">
        ${project ? `<div class="segmented small"><button type="button" data-scope="project" aria-pressed="${filter.scope === "project"}">${esc(project.name)} accounts</button><button type="button" data-scope="all" aria-pressed="${filter.scope === "all"}">All accounts</button></div>` : ""}
        <select data-f="account"><option value="">Any account</option>${accounts.map(([id, name]) => `<option value="${esc(id)}" ${id === filter.account ? "selected" : ""}>${esc(name || id)} · ${esc(id)}</option>`).join("")}</select>
        <select data-f="kind"><option value="">Any resource</option>${kinds.map((k) => `<option ${k === filter.kind ? "selected" : ""}>${esc(k)}</option>`).join("")}</select>
        <select data-f="action"><option value="">Created or updated</option>${["Created", "Updated"].map((a) => `<option ${a === filter.action ? "selected" : ""}>${a}</option>`).join("")}</select>
        <input type="search" data-f="text" placeholder="Search name, id, who, event" value="${esc(filter.text)}" style="width:240px">
        <span class="small muted" style="margin-left:auto">${rows.length ? `${start + 1}–${Math.min(start + PAGE, rows.length)} of ${rows.length.toLocaleString()}` : "0"}</span>
        <button type="button" data-page="-1" ${filter.page === 1 ? "disabled" : ""} aria-label="Previous page">‹</button><span class="small">${filter.page} / ${pages}</span>
        <button type="button" data-page="1" ${filter.page === pages ? "disabled" : ""} aria-label="Next page">›</button></div>
      <div class="table-wrap"><table class="data timeline"><thead><tr><th>#</th><th>When</th><th></th><th>Resource</th><th>Type</th><th>Account</th><th>VPC</th><th>By</th><th>Source</th></tr></thead>
      <tbody>${body || `<tr><td colspan="9" class="empty">${data.events.length ? "No events match." : "No created or updated times in this snapshot. Download with AWS Config or CloudTrail times switched on (AWS download page)."}</td></tr>`}</tbody></table></div>`;
    main.querySelectorAll("[data-f]").forEach((input) => input.addEventListener(input.type === "search" ? "input" : "change", () => {
      filter[input.dataset.f] = input.value;
      filter.page = 1;
      if (input.type === "search") {
        clearTimeout(input._t);
        input._t = setTimeout(() => {
          render(main, app);
          const box = main.querySelector('[data-f="text"]');
          box.focus();
          box.setSelectionRange(box.value.length, box.value.length);
        }, 1000);
      } else render(main, app);
    }));
    main.querySelector("#tl-snapshot").addEventListener("change", async (e) => {
      chosen = e.target.value;
      filter.page = 1;
      await timeline(main, app);
    });
    main.querySelectorAll("[data-scope]").forEach((b) => b.addEventListener("click", () => {
      filter.scope = b.dataset.scope;
      filter.page = 1;
      render(main, app);
    }));
    main.querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => {
      filter.page += Number(b.dataset.page);
      render(main, app);
      main.scrollTop = 0;
    }));
    main.querySelectorAll("tr[data-id]").forEach((row) => {
      row.addEventListener("click", () => app.showObject(row.dataset.kind, row.dataset.id));
      row.addEventListener("keydown", (e) => e.key === "Enter" && row.click());
    });
  }

  async function timeline(main, app) {
    // Its own snapshot choice, newest first: the timeline is about the latest download.
    const ids = app.overview.snapshots.map((s) => s.id);
    const snapshot = ids.includes(chosen) ? chosen : ids[0];
    if (!snapshot) {
      main.innerHTML = `<div class="empty">No snapshot yet. Download or import AWS data first.</div>`;
      return;
    }
    if (loadedFor !== snapshot) {
      main.innerHTML = `<div class="loading">Loading…</div>`;
      data = await api(`/snapshots/${encodeURIComponent(snapshot)}/timeline`);
      loadedFor = snapshot;
    }
    render(main, app);
  }

  return timeline;
})();
