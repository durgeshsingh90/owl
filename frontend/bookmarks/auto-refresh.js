"use strict";
(() => {
  const label = document.getElementById("bookmark-auto-refresh-status");
  const reload = document.getElementById("bookmark-auto-refresh-reload");
  reload.addEventListener("click", () => location.reload());
  async function poll() {
    try {
      const response = await fetch("/api/bookmarks/refresh-schedule", {cache:"no-store"});
      if (!response.ok) throw Error();
      const state = await response.json();
      window.bookmarkAutoRefreshRunning = state.status === "running";
      const next = state.next_run ? new Date(state.next_run * 1000).toLocaleString() : "shortly";
      label.textContent = state.status === "running"
        ? `Automatic Confluence update: ${state.completed}/${state.total} · ${state.failed} failed`
        : state.status === "retrying"
          ? `${state.message} Next attempt: ${next}`
          : `Automatic Confluence updates: weekly · Next update: ${next}`;
      reload.hidden = !window.bookmarkDatabaseReady || state.workspace_revision === window.bookmarkDatabaseRevision;
    } catch {
      label.textContent = "Automatic update status unavailable. OWL must be running to update pages.";
    } finally {
      setTimeout(poll, 15000);
    }
  }
  void poll();
})();
