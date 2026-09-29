"use strict";
// Version history for a Confluence page, loaded live from Confluence (newest first).
(() => {
  const dialog = document.createElement("dialog");
  dialog.className = "commit-history-dialog";
  dialog.innerHTML = '<header><h2>Version history</h2><button type="button" aria-label="Close version history">✕</button></header><p class="history-status" role="status"></p><ol class="history-list"></ol>';
  document.body.append(dialog);
  const heading = dialog.querySelector("h2");
  const status = dialog.querySelector(".history-status");
  const list = dialog.querySelector("ol");
  dialog.querySelector("header button").onclick = () => dialog.close();
  let sequence = 0;
  document.querySelector("#pdf-table-body").addEventListener("click", async event => {
    const button = event.target.closest("[data-commit-history]");
    if (!button) return;
    const current = ++sequence;
    heading.textContent = "Version history";
    status.textContent = "Loading versions from Confluence…";
    list.replaceChildren();
    dialog.showModal();
    try {
      const response = await fetch(`/api/confluence-library/document/${encodeURIComponent(button.dataset.commitHistory)}/versions`, {cache: "no-store"});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Unable to load version history.");
      if (current !== sequence) return;
      heading.textContent = `${data.name} — Version history`;
      status.textContent = `${data.versions.length} ${data.versions.length === 1 ? "version" : "versions"} · page ${data.pageId}`;
      for (const version of data.versions) {
        const item = document.createElement("li");
        const title = document.createElement("strong");
        title.textContent = `v${version.number ?? "?"}`;
        const meta = document.createElement("span");
        const when = Number.isFinite(Date.parse(version.when)) ? new Date(version.when).toLocaleString() : "Date unavailable";
        meta.textContent = ` · ${version.author || "Unknown"} · ${when}`;
        const note = document.createElement("p");
        note.textContent = version.message || "No version comment.";
        const link = document.createElement("a");
        link.href = version.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Open this version ↗";
        item.append(title, meta, note, link);
        list.append(item);
      }
    } catch (error) {
      if (current === sequence) status.textContent = error.message;
    }
  });
})();
