"use strict";
// "Track in Confluence Tracker" on Confluence bookmarks. The tracker follows the page's
// top-most parent, so a page shows as tracked once that tree is followed.
(() => {
  let tracked = new Map(); // top-most page ID -> tracked root
  let loaded = false;
  const pending = new Set();

  const topPage = item => String(item.ancestors?.[0]?.page_id || item.page_id || "");

  async function load() {
    try {
      const response = await fetch("/api/confluence-tracker/roots", {cache: "no-store"});
      if (!response.ok) return;
      const {roots} = await response.json();
      tracked = new Map(roots.map(root => [String(root.page_id), root]));
      loaded = true;
      if (typeof render === "function") render();
    } catch { /* The button stays available; tracking reports any error itself. */ }
  }

  window.bookmarkTrackButton = item => {
    if (item.sourceType !== "confluence" || !/^\d+$/.test(String(item.page_id || ""))) return "";
    const root = tracked.get(topPage(item));
    const busy = pending.has(item.id);
    const title = root
      ? `Tracked in Confluence Tracker with “${root.title}”. Open the tracker.`
      : "Track this page's tree in Confluence Tracker";
    return `<button type="button" class="bookmark-action bookmark-track${root ? " is-tracked" : ""}" data-track-bookmark="${item.id}" aria-pressed="${!!root}" aria-label="${esc(title)}" title="${esc(title)}" ${busy || !loaded ? "disabled" : ""}><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="3" fill="${root ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.8"/></svg></button>`;
  };

  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-track-bookmark]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const item = bookmarks.find(entry => entry.id === Number(button.dataset.trackBookmark));
    if (!item) return;
    if (tracked.has(topPage(item))) {
      window.open("/confluence-tracker/", "_blank", "noopener");
      return;
    }
    pending.add(item.id);
    render();
    try {
      const response = await fetch("/api/confluence-tracker/roots", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({root: item.url}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not start tracking.");
      toast("Tracking started. Confluence Tracker is reading the page tree in the background.");
      await load();
    } catch (error) {
      toast(error.message);
    } finally {
      pending.delete(item.id);
      render();
    }
  });

  load();
})();
