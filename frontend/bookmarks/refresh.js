"use strict";
(() => {
  const button = document.querySelector("#update-all-bookmarks"),
    status = document.querySelector("#bookmark-refresh-status");
  let busy = false;
  const lastUpdate = document.querySelector("#bookmark-last-update");
  const cacheKey = "owl-bookmarks-last-update-all";
  let lastTimestamp = 0;
  function showLastUpdate(record, cached = false) {
    const date = new Date(record?.at);
    if (
      !record?.at ||
      !Number.isFinite(date.getTime()) ||
      date.getTime() < lastTimestamp
    )
      return;
    lastTimestamp = date.getTime();
    const formatted = date.toLocaleString("en-GB", {
      timeZone: "Europe/Dublin",
      dateStyle: "medium",
      timeStyle: "short",
    });
    lastUpdate.textContent = `Last update all: ${formatted}${record.status === "succeeded_with_errors" ? " · with errors" : ""}`;
    lastUpdate.title = cached
      ? "Last known completed update. Backend verification pending."
      : "Last completed backend update.";
    if (!cached)
      try {
        localStorage.setItem(cacheKey, JSON.stringify(record));
      } catch {}
  }
  try {
    showLastUpdate(JSON.parse(localStorage.getItem(cacheKey) || "null"), true);
  } catch {}
  function recordRun(run) {
    if (!run) return;
    const completed =
      ["succeeded", "succeeded_with_errors"].includes(run.status) &&
      run.completed_at;
    showLastUpdate({
      at: completed ? run.completed_at : run.last_completed_at,
      status: completed ? run.status : run.last_completed_status,
    });
  }
  function message(text) {
    status.hidden = false;
    status.textContent = text;
    status.title = text;
  }
  async function request(path, options = {}) {
    const target = new URL(path, location.href);
    if (target.origin !== location.origin)
      throw Error("Refresh requires the OWL backend on this origin.");
    const controller = new AbortController(),
      timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(target, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
          ...options.headers,
        },
      });
      if (!response.headers.get("content-type")?.includes("application/json"))
        throw Error(
          "Backend unavailable. Connect OWL to update saved bookmarks.",
        );
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || "Bookmark refresh failed.");
      return data;
    } finally {
      clearTimeout(timer);
    }
  }
  async function reloadMetadata() {
    const data = await request("/bookmarks/workspace/"),
      pages = new Map();
    function collect(value) {
      if (!value || typeof value !== "object") return;
      if (
        typeof value.url === "string" &&
        typeof value.title === "string" &&
        "sourceType" in value
      )
        pages.set(value.url, value);
      for (const child of Object.values(value))
        if (typeof child === "object") collect(child);
    }
    collect(data);
    for (const item of bookmarks) {
      const remote = pages.get(item.url);
      if (!remote) continue;
      const changed =
        item.title !== remote.title || item.version !== remote.version;
      item.title = remote.title;
      item.version = remote.version;
      if (remote.sourceType === "confluence") {
        item.author = remote.createdBy || remote.author || "Unknown";
        item.lastEditor = remote.modifiedBy || "Unknown";
        item.writtenAt = remote.createdAt;
        item.confluenceUpdatedAt = remote.updatedAt;
      }
      if (changed) item.updatedInOwlAt = Date.now();
      item.pageTextSizeBytes = remote.pageTextSizeBytes;
    }
    persist();
    render();
    if (selectedBookmarkId !== null) showPageDetails(selectedBookmarkId);
  }
  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.classList.add("refreshing");
    message("Starting bookmark update…");
    try {
      const workspace = await request("/bookmarks/workspace/");
      if (!workspace.csrfToken)
        throw Error("Unable to obtain the backend refresh token.");
      const started = await request(
        workspace.urls?.refreshStart || "/bookmarks/refresh/start/",
        { method: "POST", headers: { "X-CSRFToken": workspace.csrfToken } },
      );
      let run = started.refresh;
      if (!run?.run_id) throw Error("Backend did not return a refresh job.");
      const job = run.run_id;
      while (true) {
        message(
          `${run.status_label || run.status} · ${run.processed}/${run.total} · ${run.succeeded} successful · ${run.failed} failed`,
        );
        recordRun(run);
        if (!run.active) break;
        await new Promise((resolve) => setTimeout(resolve, 3000));
        const result = await request(
          workspace.urls?.refreshStatus || "/bookmarks/refresh/status/",
        );
        run = result.refresh;
        if (!run || run.run_id !== job)
          throw Error("Refresh status changed. Check backend refresh history.");
      }
      if (!["succeeded", "succeeded_with_errors"].includes(run.status))
        throw Error(run.detail || "Bookmark update did not complete.");
      try {
        await reloadMetadata();
        message(`Updated · ${run.succeeded} successful · ${run.failed} failed`);
      } catch {
        message(
          `Backend update finished · ${run.failed} failed. Reload to retrieve latest page details.`,
        );
      }
    } catch (error) {
      message(
        error.name === "AbortError"
          ? "Status request timed out. A queued backend update may still be running."
          : error.message,
      );
    } finally {
      busy = false;
      button.disabled = false;
      button.classList.remove("refreshing");
    }
  });
  void request("/bookmarks/refresh/status/")
    .then((data) => {
      recordRun(data.refresh);
      if (!lastTimestamp) lastUpdate.textContent = "Last update all: never";
    })
    .catch(() => {
      lastUpdate.title = lastTimestamp
        ? "Last known completed update; backend unavailable."
        : "Connect the backend to retrieve the last update time.";
    });
})();
