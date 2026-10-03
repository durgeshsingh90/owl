"use strict";
// The shared label (/home/auto-refresh.js) polls the schedule; this offers a reload once
// a background refresh has changed the saved pages.
(() => {
  const reload = document.getElementById("bookmark-auto-refresh-reload");
  reload.addEventListener("click", () => location.reload());
  window.addEventListener("owl-auto-refresh", ({detail}) => {
    if (detail.app !== "bookmarks" || !detail.state) return;
    const state = detail.state;
    window.bookmarkAutoRefreshRunning = state.status === "running";
    reload.hidden = !window.bookmarkDatabaseReady || state.workspace_revision === window.bookmarkDatabaseRevision;
  });
})();
