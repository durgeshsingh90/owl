"use strict";
// Automatic refresh status shared by every OWL app. Each app's backend refreshes once a
// day and retries every two hours after a failure; this label only reports that schedule.
(() => {
  const APPS = {
    bitbucket: {name: "Bitbucket", endpoint: "/api/refresh-schedule", href: "/bitbucket/"},
    naas: {name: "NAAS and Networking", endpoint: "/naas/api/refresh-schedule", href: "/naas/"},
    network: {name: "Network Automation", endpoint: "/network-automation/api/refresh-schedule", href: "/network-automation/"},
    bookmarks: {name: "Bookmarks", endpoint: "/api/bookmarks/refresh-schedule", href: "/bookmarks/"},
    tracker: {name: "Confluence Tracker", endpoint: "/api/confluence-tracker/refresh-schedule", href: "/confluence-tracker/"},
  };

  function duration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    if (minutes) return `${minutes}m`;
    return `${total}s`;
  }
  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
  }
  // "56 min ago", "23 hours ago", "3 days ago".
  function since(epoch) {
    const minutes = Math.max(0, Math.floor((Date.now() / 1000 - Number(epoch)) / 60));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
    const days = Math.floor(hours / 24);
    return `${days} ${days === 1 ? "day" : "days"} ago`;
  }
  function ago(epoch) {
    return `${duration(Date.now() / 1000 - epoch)} ago`;
  }

  // Bookmarks keeps its original "scheduled" status; trackers report a failure as "retrying".
  // Refreshes asked for (refresh-all) but not yet reported running: app -> until when.
  const preparing = new Map();
  window.addEventListener("owl-auto-refresh-preparing", event => {
    // Clocks may differ a little between browser and backend.
    for (const app of event.detail || []) preparing.set(app, {until: Date.now() + 90000, since: Date.now() / 1000 - 5});
  });
  window.addEventListener("owl-auto-refresh-prepared", event => {
    for (const app of event.detail || []) preparing.delete(app);
    window.dispatchEvent(new CustomEvent("owl-auto-refresh-changed"));
  });
  // What every label and the Home table show for an app: one wording everywhere.
  function view(app, state) {
    const asked = preparing.get(app);
    // Done preparing once the refresh runs, or has already been tried since the click.
    if (asked && (state?.status === "running" || Number(state?.last_attempt) >= asked.since || Number(state?.last_success) >= asked.since)) {
      preparing.delete(app);
    }
    if (preparing.has(app) && preparing.get(app).until > Date.now()) {
      return {tone: "running", text: "Preparing to refresh…", detail: "Starting in the background; this takes a few seconds."};
    }
    preparing.delete(app);
    return state ? describe(state, app) : null;
  }

  function describe(state, app = "") {
    const now = Date.now() / 1000;
    const next = Number(state.next_run);
    const eta = Number.isFinite(next) && next > 0
      ? (next <= now ? "due now" : `in ${duration(next - now)}`)
      : "shortly";
    const schedule = `Daily · retry every ${state.retry_hours || 1}h on failure`;
    const last = state.last_success ? `Last updated ${since(state.last_success)}` : "Not updated yet";
    switch (state.status) {
      case "running": {
        // Bookmarks and Confluence Tracker count pages.
        const unit = app === "bookmarks" || app === "tracker" ? " pages" : "";
        const progress = state.total ? ` · updating ${state.completed}/${state.total}${unit}` : "";
        const failed = state.failed ? ` · ${state.failed} failed` : "";
        const remaining = Number(state.eta_seconds) > 0 && state.completed > 0 ? ` · about ${duration(state.eta_seconds)} left` : "";
        // A manual Update all in Bookmarks runs in the browser rather than in the background.
        const what = state.manual ? "Updating now (Update all)" : "Refreshing in background";
        if (state.phase) {
          // Bitbucket-style apps: say which repository and whether it is listing or updating.
          const repos = state.repositories ? ` · ${state.repositories_done}/${state.repositories} repositories done` : "";
          const left = Number(state.current_eta_seconds) > 0 && state.current_processed > 0 ? ` · about ${duration(state.current_eta_seconds)} left` : "";
          const text = state.phase === "finding"
            ? `${what} · finding files in ${state.current} · ${Number(state.current_found).toLocaleString()} found`
            : state.phase === "retrying"
              ? `${what} · retrying failed files in ${state.current}`
              : `${what} · updating ${state.current} ${Number(state.current_processed).toLocaleString()}/${Number(state.current_found).toLocaleString()}${left}`;
          return {tone: "running", text: text + repos, detail: `${schedule}. ${last}.`};
        }
        return {tone: "running", text: `${what}${progress}${failed}${remaining}`, detail: `${schedule}. ${last}.`};
      }
      case "retrying":
        return {tone: "retrying", text: `Refresh failed · retry ${eta}`, detail: `${state.message || "The last refresh failed."} Next attempt ${Number.isFinite(next) && next > 0 ? when(next) : "shortly"}. ${last}.`};
      case "idle":
      case "not_configured":
        return {tone: "idle", text: "Auto refresh waiting for setup", detail: state.message || schedule};
      default:
        return {tone: "ok", text: `Auto refresh ${eta}`, detail: `${schedule}. Next ${Number.isFinite(next) && next > 0 ? when(next) : "shortly"}. ${last}.`};
    }
  }

  async function load(app) {
    const response = await fetch(APPS[app].endpoint, {cache: "no-store", signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  const STYLE = `
    .owl-auto-refresh{display:inline-flex;align-items:center;gap:6px;margin:4px 0 0;font-size:12px;line-height:1.4;color:inherit;opacity:.85}
    .owl-auto-refresh .owl-ar-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#1f9d55}
    .owl-auto-refresh[data-tone="running"] .owl-ar-dot{background:#2f7de1;animation:owl-ar-pulse 1.2s ease-in-out infinite}
    .owl-auto-refresh[data-tone="retrying"] .owl-ar-dot{background:#d97706}
    .owl-auto-refresh[data-tone="idle"] .owl-ar-dot,.owl-auto-refresh[data-tone="unknown"] .owl-ar-dot{background:#8b949e}
    .owl-auto-refresh[data-tone="retrying"] .owl-ar-text{color:#b45309}
    :root[data-theme="dark"] .owl-auto-refresh[data-tone="retrying"] .owl-ar-text{color:#fbbf24}
    @media (prefers-color-scheme: dark){:root:not([data-theme="light"]) .owl-auto-refresh[data-tone="retrying"] .owl-ar-text{color:#fbbf24}}
    .owl-auto-refresh[data-show-last]{flex-wrap:wrap}
    .owl-ar-last{flex-basis:100%;padding-left:14px;font-size:10.5px;opacity:.75}
    .owl-ar-table{width:100%;border-collapse:collapse;font-size:12px}
    .owl-ar-table th,.owl-ar-table td{text-align:left;padding:8px 10px;border-top:1px solid rgba(127,127,127,.2);vertical-align:top}
    .owl-ar-table thead th{border-top:0;font-size:11px;font-weight:600;opacity:.7}
    .owl-ar-table tbody th a{color:inherit;font-weight:600;text-decoration:none}
    .owl-ar-table tbody th a:hover{text-decoration:underline}
    .owl-ar-table .owl-auto-refresh{margin:0}
    @keyframes owl-ar-pulse{50%{opacity:.35}}
    @media (prefers-reduced-motion: reduce){.owl-auto-refresh .owl-ar-dot{animation:none!important}}`;
  function injectStyle() {
    if (document.getElementById("owl-auto-refresh-style")) return;
    const style = document.createElement("style");
    style.id = "owl-auto-refresh-style";
    style.textContent = STYLE;
    document.head.append(style);
  }

  // A page label: <p class="owl-auto-refresh" data-auto-refresh="bitbucket"></p>
  function label(element) {
    const app = element.dataset.autoRefresh;
    element.setAttribute("role", "status");
    element.innerHTML = '<span class="owl-ar-dot" aria-hidden="true"></span><span class="owl-ar-text">Checking automatic refresh…</span>';
    let state = null;
    const render = () => {
      const shown = view(app, state) || {tone: "unknown", text: "Auto refresh status unavailable", detail: "OWL must be running to refresh automatically."};
      element.dataset.tone = shown.tone;
      element.querySelector(".owl-ar-text").textContent = shown.text;
      element.title = shown.detail;
      // Labels say how long ago the last update was; while all is well that is all
      // they show (the next refresh is in Home's Automatic refresh table).
      if (element.hasAttribute("data-show-last") && state) {
        let line = element.querySelector(".owl-ar-last");
        if (!line) element.append(line = Object.assign(document.createElement("small"), {className: "owl-ar-last"}));
        line.textContent = state.last_success ? `Last updated ${since(state.last_success)}` : "Not updated yet";
        line.title = state.last_success ? when(state.last_success) : "";
        const quiet = shown.tone === "ok";
        element.classList.toggle("owl-ar-quiet", quiet);
        if (quiet) element.querySelector(".owl-ar-text").textContent = line.textContent;
        line.hidden = quiet;
      }
    };
    let timer = 0;
    async function poll() {
      clearTimeout(timer);
      try { state = await load(app); }
      catch { state = null; }
      render();
      window.dispatchEvent(new CustomEvent("owl-auto-refresh", {detail: {app, state}}));
      timer = setTimeout(poll, state && state.status === "running" ? 10000 : 30000);
    }
    // A page that starts or ends a refresh itself asks for the label to update now.
    window.addEventListener("owl-auto-refresh-changed", event => {
      if (!event.detail || event.detail === app) void poll();
    });
    setInterval(render, 30000);
    void poll();
  }

  // Home summary: <div data-auto-refresh-table></div>, one row per app.
  function table(element) {
    const states = {};
    const escape = value => String(value ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
    const render = () => {
      element.innerHTML = `<table class="owl-ar-table"><thead><tr><th scope="col">App</th><th scope="col">Status</th><th scope="col">Next refresh</th><th scope="col">Last success</th><th scope="col">Note</th></tr></thead><tbody>${
        Object.entries(APPS).map(([app, config]) => {
          const state = states[app];
          const shown = view(app, state) || {tone: "unknown", text: state === null ? "Status unavailable" : "Checking…", detail: ""};
          const next = Number(state?.next_run);
          const nextText = shown.tone === "running" ? "Now"
            : !state || state.status === "idle" || state.status === "not_configured" ? "—"
            : Number.isFinite(next) && next > 0 ? `${when(next)} (${next <= Date.now() / 1000 ? "due now" : "in " + duration(next - Date.now() / 1000)})` : "Shortly";
          // The status reads exactly as on the app's card; the note only explains a problem.
          const note = shown.tone === "running" ? "" : state?.message || "";
          return `<tr><th scope="row"><a href="${config.href}">${escape(config.name)}</a></th><td><span class="owl-auto-refresh" data-tone="${shown.tone}"><span class="owl-ar-dot" aria-hidden="true"></span><span class="owl-ar-text">${escape(shown.text)}</span></span></td><td>${escape(nextText)}</td><td>${state?.last_success ? `${escape(when(state.last_success))} (${escape(since(state.last_success))})` : "—"}</td><td>${escape(note)}</td></tr>`;
        }).join("")
      }</tbody></table>`;
    };
    for (const app of Object.keys(APPS)) {
      let timer = 0;
      const poll = async () => {
        clearTimeout(timer);
        try { states[app] = await load(app); }
        catch { states[app] = null; }
        render();
        timer = setTimeout(poll, states[app]?.status === "running" ? 10000 : 30000);
      };
      // Refresh-all (or a page) starting or ending a refresh updates the table now.
      window.addEventListener("owl-auto-refresh-changed", event => {
        if (!event.detail || event.detail === app) void poll();
      });
      void poll();
    }
    render();
    setInterval(render, 30000);
  }

  window.owlAutoRefresh = {APPS, describe, load, duration, when, ago};
  injectStyle();
  document.querySelectorAll("[data-auto-refresh]").forEach(label);
  document.querySelectorAll("[data-auto-refresh-table]").forEach(table);
})();
