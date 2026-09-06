"use strict";
(() => {
  const dialog = document.querySelector("#bookmark-import-dialog"),
    file = document.querySelector("#bookmark-import-file"),
    feedback = document.querySelector("#import-feedback"),
    confirm = document.querySelector("#import-confirm");
  let candidates = [],
    version = 0,
    jsonImport = false;
  document.querySelector("#import-bookmarks").addEventListener("click", () => {
    file.value = "";
    candidates = [];
    confirm.disabled = true;
    feedback.textContent = "Imported bookmarks are saved in the database.";
    dialog.showModal();
  });
  document
    .querySelector("#import-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    version++;
    candidates = [];
    file.value = "";
  });
  file.addEventListener("change", async () => {
    const selected = file.files[0],
      current = ++version;
    candidates = [];
    confirm.disabled = true;
    if (!selected) return;
    if (selected.size > 10 * 1024 * 1024) {
      feedback.textContent = "Choose a bookmark export smaller than 10 MB.";
      return;
    }
    feedback.textContent = "Reading bookmarks…";
    try {
      const text = await selected.text();
      if (current !== version || !dialog.open) return;
      jsonImport = /\.json$/i.test(selected.name) || /^[\s\uFEFF]*[\[{]/.test(text);
      if (jsonImport) {
        const result = bookmarkTransfer.parse(text, bookmarks);
        candidates = result.items;
        feedback.textContent = `${candidates.length} bookmarks ready. ${result.skipped} deleted, duplicate or invalid entries skipped. Saved details are preserved; use Update to fetch current content.`;
        confirm.disabled = !candidates.length;
        return;
      }
      const parsed = new DOMParser().parseFromString(text, "text/html");
      const existing = new Set(bookmarks.map((item) => item.url));
      let skipped = 0;
      for (const link of parsed.querySelectorAll("a[href]")) {
        const url = parseBookmarkUrl(link.getAttribute("href"));
        if (!url || existing.has(url.href)) {
          skipped++;
          continue;
        }
        const folderPath = [];
        let list = link.closest("dl");
        while (list) {
          const parent = list.parentElement;
          let heading =
            parent?.tagName === "DT"
              ? [...parent.children].find((child) => child.tagName === "H3")
              : null;
          if (!heading) {
            const previous = list.previousElementSibling;
            heading =
              previous?.tagName === "H3"
                ? previous
                : previous?.tagName === "DT"
                  ? [...previous.children].find(
                      (child) => child.tagName === "H3",
                    )
                  : null;
          }
          if (heading?.textContent.trim())
            folderPath.unshift(heading.textContent.trim());
          list = parent?.closest("dl");
        }
        existing.add(url.href);
        candidates.push({
          url: url.href,
          title: link.textContent.trim() || url.hostname,
          domain: url.hostname,
          folderPath,
        });
      }
      feedback.textContent = `${candidates.length} new bookmarks ready. ${skipped} duplicate or unsupported links skipped.`;
      confirm.disabled = !candidates.length;
    } catch {
      feedback.textContent =
        "Unable to read this file. Choose a valid bookmark JSON or HTML export.";
    }
  });
  confirm.addEventListener("click", async () => {
    if (!candidates.length || !window.bookmarkDatabaseReady) return;
    confirm.disabled = true;
    const existing = new Set(bookmarks.map((item) => item.url));
    let id = nextBookmarkId() - 1;
    const added = candidates
      .filter((item) => !existing.has(item.url))
      .map((item) => ({
        id: ++id,
        description: "Imported in the database · page details not fetched yet",
        views: 0,
        lastViewed: null,
        added: Date.now(),
        updatedInOwlAt: null,
        favorite: false,
        pinned: false,
        custom: true,
        ...item,
      }));
    let failed = 0;
    for (const item of jsonImport ? [] : added) {
      feedback.textContent = "Fetching details: " + item.url;
      try { Object.assign(item, await resolveBookmark(item.url)); }
      catch (error) { item.fetchError = error.message; failed++; }
    }
    // Allocate IDs after network requests, since another save may have completed.
    for (const item of added) { item.id = nextBookmarkId(); bookmarks.push(item); if (typeof item.notes === "string") localPageNotes[item.id] = item.notes; }
    if (!await persist()) {
      feedback.textContent = "Database save failed. Reload before importing again.";
      confirm.disabled = false; return;
    }
    view = "all";
    domain = "";
    selectedDomainGroup = "";
    query = "";
    selectedPerson = "";
    document.querySelector("#bookmark-search").value = "";
    document.querySelector("#add-bookmark").hidden = true;
    render();
    dialog.close();
    toast(`Imported ${added.length} bookmarks into the database${failed ? "; " + failed + " page fetches failed. Use Update to retry" : ""}.`);
  });
  document.querySelector("#export-bookmarks").addEventListener("click", async () => {
    if (!window.bookmarkDatabaseReady) { toast("Wait for the database to load."); return; }
    if (!await persist()) return;
    try {
      const response = await fetch("/api/bookmarks/workspace", { cache: "no-store" });
      if (!response.ok) throw new Error("Unable to export bookmarks.");
      const data = await response.json();
      const records = data.bookmarks.map(item => ({
        ...bookmarkTransfer.normalize(item),
        notes: data.notes[item.id] ?? item.notes ?? "",
        space_key: item.spaceKey || "",
        saved_at: item.saved_at || new Date(item.added).toISOString(),
        modified: item.confluenceUpdatedAt || null,
        last_refreshed: item.lastRefreshed || null,
        deleted: false,
      }));
      const blob = new Blob([JSON.stringify(records, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `owl-bookmarks-${new Date().toISOString().slice(0, 10)}.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`Exported ${records.length} bookmarks.`);
    } catch (error) { toast(error.message); }
  });
})();
