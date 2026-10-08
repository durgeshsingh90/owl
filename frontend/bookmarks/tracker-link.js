"use strict";
// Track Confluence trees from Bookmarks. Any page link works: the tracker follows the
// page's top-most parent and, from today, records only pages that are new or updated.
(() => {
  let tracked = new Map(); // top-most page ID -> tracked root
  let loaded = false;
  const pending = new Set();

  const pageIdFromUrl = url => {
    try { return new URL(url).searchParams.get("pageId") || (url.match(/\/pages\/(\d+)/) || [])[1] || ""; }
    catch { return ""; }
  };
  const isConfluence = item => item.sourceType === "confluence" || !!item.confluenceBaseUrl
    || /\/(pages|display|spaces|x)\/|pageId=/.test(item.url || "");
  const topPage = item => String(item.ancestors?.[0]?.page_id || item.page_id || pageIdFromUrl(item.url));

  async function load() {
    try {
      const response = await fetch("/api/confluence-tracker/roots", {cache: "no-store"});
      if (!response.ok) return;
      const {roots} = await response.json();
      tracked = new Map(roots.map(root => [String(root.page_id), root]));
      loaded = true;
      if (typeof render === "function") render();
    } catch { /* The buttons stay available; tracking reports any error itself. */ }
  }

  function button(key, root, label) {
    const busy = pending.has(key);
    const title = root
      ? `Tracked in Confluence Tracker with “${root.title}”. Open the tracker.`
      : `Track ${label} in Confluence Tracker (its top-most parent tree, new and updated pages from today)`;
    return `<button type="button" class="bookmark-action bookmark-track${root ? " is-tracked" : ""}" data-track-key="${esc(key)}" aria-pressed="${!!root}" aria-label="${esc(title)}" title="${esc(title)}" ${busy || !loaded ? "disabled" : ""}><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="3" fill="${root ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.8"/></svg></button>`;
  }

  // What each button tracks: a page link, remembered by key until clicked.
  const targets = new Map();

  window.bookmarkTrackButton = item => {
    if (!isConfluence(item)) return "";
    const key = "page-" + item.id;
    targets.set(key, item);
    return button(key, tracked.get(topPage(item)), "this page's tree");
  };

  window.bookmarkFolderTrackButton = path => {
    const items = bookmarks.filter(item => isConfluence(item) &&
      JSON.stringify([item.space || "Pages", ...(item.breadcrumb || [])].slice(0, path.length)) === JSON.stringify(path));
    if (!items.length) return "";
    const key = "folder-" + JSON.stringify(path);
    targets.set(key, items[0]);
    return button(key, items.map(item => tracked.get(topPage(item))).find(Boolean), "this folder's tree");
  };

  // Navigation list: a small eye only on trees tracked in Confluence Tracker, like the star.
  window.bookmarkTrackedMark = path => {
    const root = bookmarks
      .filter(item => isConfluence(item) &&
        JSON.stringify([item.space || "Pages", ...(item.breadcrumb || [])].slice(0, path.length)) === JSON.stringify(path))
      .map(item => tracked.get(topPage(item)))
      .find(Boolean);
    if (!root) return "";
    const title = `Tracked in Confluence Tracker (${root.title})`;
    return `<span class="bookmark-root-tracked" role="img" aria-label="${esc(title)}" title="${esc(title)}"><svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" fill="none" stroke="currentColor" stroke-width="2.2"/><circle cx="12" cy="12" r="3.2" fill="currentColor"/></svg></span>`;
  };

  async function track(url, key) {
    if (key) pending.add(key);
    render();
    try {
      const before = new Set(tracked.keys());
      const response = await fetch("/api/confluence-tracker/roots", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({root: url}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not start tracking.");
      await load();
      const added = [...tracked.keys()].find(id => !before.has(id));
      toast(added
        ? `Tracking “${tracked.get(added).title}”. From today, new and updated pages appear in Confluence Tracker.`
        : "That tree is already tracked in Confluence Tracker.");
    } catch (error) {
      toast(error.message);
    } finally {
      if (key) pending.delete(key);
      render();
    }
  }

  document.addEventListener("click", event => {
    const element = event.target.closest("[data-track-key]");
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    if (element.getAttribute("aria-pressed") === "true") {
      window.open("/confluence-tracker/", "_blank", "noopener");
      return;
    }
    const item = targets.get(element.dataset.trackKey);
    if (item) void track(item.url, element.dataset.trackKey);
  });

  document.querySelector("#track-confluence-tree")?.addEventListener("click", () => {
    const url = window.prompt("Paste any Confluence page link. The tracker follows its top-most parent and, from today, records new and updated pages.");
    if (url && url.trim()) void track(url.trim());
  });

  load();
})();
