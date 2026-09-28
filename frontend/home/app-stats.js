"use strict";
// Statistics for NAAS Update, Network Automation, AWS Accounts and Confluence Tracker,
// plus the sticky "Jump to" bar that links every app's statistics section.
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  const number = value => Number(value || 0).toLocaleString();
  const PLURALS = {category: "categories", copy: "copies", entry: "entries"};
  const plural = (count, word) => `${number(count)} ${count === 1 ? word : PLURALS[word] || `${word}s`}`;
  const when = value => {
    const time = typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
    if (!Number.isFinite(time)) return "Never";
    const minutes = Math.round((Date.now() - time) / 60000);
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 1440) return `${Math.round(minutes / 60)} h ago`;
    const days = Math.round(minutes / 1440);
    return days < 30 ? `${days} day${days === 1 ? "" : "s"} ago` : new Date(time).toLocaleDateString(undefined, {dateStyle: "medium"});
  };
  const day = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleDateString(undefined, {dateStyle: "medium"}) : "—";
  async function json(url) {
    const response = await fetch(url, {cache: "no-store", signal: AbortSignal.timeout(30000)});
    if (!response.ok) throw new Error(`${response.status}`);
    return response.json();
  }
  function figure(key, text) {
    const element = document.querySelector(`[data-stats-figure="${key}"]`);
    if (element) element.textContent = text;
  }

  const metrics = values => `<div class="metrics">${values.map(([label, value, detail]) =>
    `<div class="metric"><p>${esc(label)}</p><strong>${typeof value === "number" ? number(value) : esc(value)}</strong><small>${esc(detail)}</small></div>`).join("")}</div>`;
  const panel = (title, note, body) => `<article class="panel"><div class="panel-heading"><h2>${esc(title)}</h2><span>${esc(note)}</span></div>${body}</article>`;
  const rows = (items, empty) => items.length
    ? `<div class="ranking-list">${items.map(([name, detail, right]) => `<div class="repo"><div><strong>${esc(name)}</strong><small>${esc(detail)}</small></div><span>${esc(right)}</span></div>`).join("")}</div>`
    : `<p class="app-stats-empty">${esc(empty)}</p>`;
  const bars = (items, empty) => {
    const max = Math.max(1, ...items.map(([, value]) => value));
    return items.length
      ? items.map(([name, value, detail]) => `<div class="bar-row"><div class="bar-title"><span>${esc(name)}</span><span>${number(value)}${detail ? ` · ${esc(detail)}` : ""}</span></div><div class="track"><span style="width:${Math.max(2, value / max * 100)}%"></span></div></div>`).join("")
      : `<p class="app-stats-empty">${esc(empty)}</p>`;
  };
  const emptyState = (text, href, action) => `<div class="app-stats-blank"><p>${esc(text)}</p><a href="${esc(href)}">${esc(action)} ↗</a></div>`;

  // NAAS Update and Network Automation share the Bitbucket library API under their own prefix.
  async function renderLibrary(key, prefix, noun) {
    const body = $(`stats-${key}-body`);
    try {
      const [workspace, stats] = await Promise.all([json(`${prefix}/api/workspace?limit=5000`), json(`${prefix}/api/stats`)]);
      figure(key, plural(stats.documents, noun));
      if (!stats.documents && !stats.projects) {
        body.innerHTML = emptyState(`No ${noun}s indexed yet. Add a Bitbucket project in the app to start.`, `..${prefix}/`, "Open app");
        return;
      }
      const documents = workspace.documents || [];
      const month = new Date().toISOString().slice(0, 7);
      const changedThisMonth = documents.filter(doc => String(doc.committedAt || "").slice(0, 7) === month).length;
      const repositories = (workspace.projects || []).flatMap(project => project.repos.map(repo => ({...repo, project: project.name})));
      const people = new Map();
      for (const person of workspace.people || []) people.set(person.name, (people.get(person.name) || 0) + (person.commits || 0));
      body.innerHTML = metrics([
        [`${noun[0].toUpperCase()}${noun.slice(1)}s`, stats.documents, "Indexed files"],
        ["Repositories", stats.repos, plural(stats.projects, "project")],
        ["Total opens", stats.opens, "Files opened from OWL"],
        ["Changed this month", changedThisMonth, "Files with a commit this month"],
        ["Last sync", when(workspace.lastCompletedPull), workspace.lastCompletedPull ? day(workspace.lastCompletedPull) : "No completed sync yet"],
        ["Failed files", stats.failed, stats.failed ? "Retried on the next sync" : "Nothing outstanding"],
      ]) + `<section class="analytics">${[
        panel("Top repositories", "By indexed files", bars(repositories.sort((a, b) => b.pdfCount - a.pdfCount).slice(0, 8).map(repo => [repo.name, repo.pdfCount, repo.project]), "No repositories yet.")),
        panel("Most opened files", "All-time opens", rows(documents.filter(doc => doc.opens > 0).sort((a, b) => b.opens - a.opens).slice(0, 8)
          .map(doc => [doc.name, `${doc.repo} · ${doc.path}`, plural(doc.opens, "open")]), "No files opened yet.")),
        panel("Latest changes", "Newest commits", rows([...documents].sort((a, b) => Date.parse(b.committedAt || 0) - Date.parse(a.committedAt || 0)).slice(0, 8)
          .map(doc => [doc.name, `${doc.repo} · ${doc.commitAuthor || "Unknown author"}`, day(doc.committedAt)]), "No commits yet.")),
        panel("Top contributors", "By commits", rows([...people].sort((a, b) => b[1] - a[1]).slice(0, 8)
          .map(([name, commits]) => [name, "Author", plural(commits, "commit")]), "No contributors yet.")),
      ].join("")}</section>`;
    } catch {
      figure(key, "unavailable");
      body.innerHTML = `<p class="app-stats-empty">Unable to load statistics. Check that OWL is running.</p>`;
    }
  }

  const ENV = /-(prod|production|prd|nonp|nonprod|nonprd|work|mtf|dev|develop|development|test|testing|qa|uat|sit|stage|staging|stg|preprod|pre|perf|sandbox|sbx|demo|poc|lab|dr)(?:-\d+)?$/i;
  async function renderAws() {
    const body = $("stats-aws-body");
    try {
      const [data, connection] = await Promise.all([json("/api/aws-accounts"), json("/api/aws-accounts/connection").catch(() => null)]);
      if (!data.imported) {
        figure("aws", "not imported");
        body.innerHTML = emptyState("No AWS accounts imported yet. Import your accounts JSON to see statistics.", "../aws-accounts/", "Open app");
        return;
      }
      const accounts = [...new Map(Object.values(data.categories).flat().map(account => [account.profile, account])).values()];
      figure("aws", plural(accounts.length, "account"));
      const copies = profile => (data.copies?.profile?.[profile] || 0);
      const environments = new Map();
      for (const account of accounts) {
        const env = ENV.exec(account.profile)?.[1].toLowerCase() || "no env";
        environments.set(env, (environments.get(env) || 0) + 1);
      }
      const totalCopies = Object.values(data.copies || {}).flatMap(Object.values).reduce((sum, value) => sum + value, 0);
      const connected = connection?.status === "connected" && connection.expires_at;
      const left = connected ? Math.max(0, connection.expires_at - Date.now() / 1000) : 0;
      body.innerHTML = metrics([
        ["Accounts", accounts.length, plural(Object.keys(data.categories).length, "category")],
        ["Projects", (data.projects || []).length, `${number((data.projects || []).reduce((sum, project) => sum + project.accounts.length, 0))} accounts grouped`],
        ["Starred accounts", (data.stars || []).length, "Quick access"],
        ["Copies", totalCopies, "Names, IDs and roles copied"],
        ["AWS connection", connected ? "Connected" : connection ? "Disconnected" : "Unknown", connected ? `${Math.floor(left / 3600)}h ${Math.floor(left % 3600 / 60)}m left · ${connection.profile}` : connection?.profile || ""],
        ["Imported", when(data.imported_at), data.generated_at ? `File generated ${day(data.generated_at)}` : ""],
      ]) + `<section class="analytics">${[
        panel("Accounts by category", "Current inventory", bars(Object.entries(data.categories).map(([name, list]) => [name.replace(/_/g, " "), list.length]).sort((a, b) => b[1] - a[1]), "No categories.")),
        panel("Accounts by environment", "From account names", bars([...environments].sort((a, b) => b[1] - a[1]), "No accounts.")),
        panel("Most copied accounts", "Name and ID copies", rows(accounts.filter(account => copies(account.profile)).sort((a, b) => copies(b.profile) - copies(a.profile)).slice(0, 8)
          .map(account => [account.profile, String(account.account_id), plural(copies(account.profile), "copy")]), "Nothing copied yet.")),
        panel("Projects", "Your groupings", rows((data.projects || []).map(project => [project.name, "Project", plural(project.accounts.length, "account")]), "No projects yet. Create one in AWS Accounts.")),
      ].join("")}</section>`;
    } catch {
      figure("aws", "unavailable");
      body.innerHTML = `<p class="app-stats-empty">Unable to load statistics. Check that OWL is running.</p>`;
    }
  }

  async function renderTracker() {
    const body = $("stats-tracker-body");
    try {
      const {roots = []} = await json("/api/confluence-tracker/roots");
      if (!roots.length) {
        figure("tracker", "no roots");
        body.innerHTML = emptyState("No Confluence page trees tracked yet. Add a root page to start daily checks.", "../confluence-tracker/", "Open app");
        return;
      }
      // Downloaded pages, or the discovered total while the first sync is still running.
      const pageCount = root => root.page_count || root.total || 0;
      const pages = roots.reduce((sum, root) => sum + pageCount(root), 0);
      const unread = roots.reduce((sum, root) => sum + (root.unread || 0), 0);
      const failed = roots.reduce((sum, root) => sum + (root.failed || 0), 0);
      const lastSuccess = Math.max(0, ...roots.map(root => Date.parse(root.last_success) || (Number(root.last_success) * 1000) || 0));
      const nextRun = Math.min(...roots.map(root => Number(root.next_run) || Infinity));
      figure("tracker", plural(pages, "page"));
      const labels = {queued: "Queued", discovering: "Discovering", downloading: "Downloading", completed: "Up to date", failed: "Sync incomplete"};
      body.innerHTML = metrics([
        ["Tracked roots", roots.length, "Page trees followed"],
        ["Tracked pages", pages, "Across all roots"],
        ["Unreviewed changes", unread, unread ? "Waiting for review" : "All caught up"],
        ["Download failures", failed, failed ? "Retried automatically" : "None"],
        ["Last successful sync", lastSuccess ? when(lastSuccess) : "Never", lastSuccess ? day(new Date(lastSuccess).toISOString()) : "Waiting for the first sync"],
        ["Next automatic check", Number.isFinite(nextRun) ? (nextRun * 1000 <= Date.now() ? "Due now" : new Date(nextRun * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"})) : "Shortly", "Daily, retry after 2 hours"],
      ]) + `<section class="analytics">${[
        panel("Tracked roots", "Pages and changes", rows(roots.map(root => [root.title, `${labels[root.status] || root.status || ""} · last sync ${root.last_success ? when(Date.parse(root.last_success) || Number(root.last_success)) : "never"}`, `${plural(pageCount(root), "page")}${root.unread ? ` · ${number(root.unread)} new` : ""}`]), "No roots.")),
        panel("Unreviewed changes by root", "Most first", bars(roots.filter(root => root.unread).map(root => [root.title, root.unread]).sort((a, b) => b[1] - a[1]), "No unreviewed changes.")),
      ].join("")}</section>`;
    } catch {
      figure("tracker", "unavailable");
      body.innerHTML = `<p class="app-stats-empty">Unable to load statistics. Check that OWL is running.</p>`;
    }
  }

  // Headline figures for the two apps that already have their own statistics sections.
  json("/api/stats").then(stats => figure("bitbucket", plural(stats.documents, "PDF"))).catch(() => figure("bitbucket", ""));
  json("/api/bookmarks/workspace").then(data => figure("bookmarks", plural((data.bookmarks || []).length, "bookmark"))).catch(() => figure("bookmarks", ""));
  renderLibrary("naas", "/naas", "file");
  renderLibrary("network", "/network-automation", "file");
  renderAws();
  renderTracker();

  // Highlight the app whose statistics are on screen.
  const links = [...document.querySelectorAll("[data-stats-link]")];
  const sections = links.map(link => document.querySelector(link.getAttribute("href"))).filter(Boolean);
  function trackSection() {
    const nav = $("stats-nav");
    const line = (nav?.getBoundingClientRect().bottom || 0) + 40;
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    let current = atBottom ? sections.at(-1) : null;
    if (!current) for (const section of sections) if (section.getBoundingClientRect().top <= line) current = section;
    links.forEach(link => link.toggleAttribute("aria-current", current && link.getAttribute("href") === `#${current.id}`));
    nav?.classList.toggle("stuck", (nav.getBoundingClientRect().top || 0) <= 0.5 && window.scrollY > 0);
  }
  links.forEach(link => link.addEventListener("click", event => {
    const target = document.querySelector(link.getAttribute("href"));
    if (!target) return;
    event.preventDefault();
    const offset = ($("stats-nav")?.offsetHeight || 0) + 16;
    window.scrollTo({top: target.getBoundingClientRect().top + window.scrollY - offset, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
    history.replaceState(null, "", link.getAttribute("href"));
  }));
  window.addEventListener("scroll", trackSection, {passive: true});
  window.addEventListener("resize", trackSection);
  trackSection();
})();
