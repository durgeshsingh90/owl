"use strict";
(() => {
  const dialog = document.querySelector("#bookmark-import-dialog"),
    file = document.querySelector("#bookmark-import-file"),
    feedback = document.querySelector("#import-feedback"),
    confirm = document.querySelector("#import-confirm");
  let candidates = [],
    version = 0;
  document.querySelector("#import-bookmarks").addEventListener("click", () => {
    file.value = "";
    candidates = [];
    confirm.disabled = true;
    feedback.textContent = "Imported bookmarks are saved in this browser.";
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
        "Unable to read this file. Choose a bookmark HTML export.";
    }
  });
  confirm.addEventListener("click", () => {
    if (!candidates.length) return;
    confirm.disabled = true;
    const existing = new Set(bookmarks.map((item) => item.url));
    let id = nextBookmarkId() - 1;
    const added = candidates
      .filter((item) => !existing.has(item.url))
      .map((item) => ({
        ...item,
        id: ++id,
        description: "Imported in this browser · page details not fetched yet",
        views: 0,
        lastViewed: null,
        added: Date.now(),
        updatedInOwlAt: null,
        favorite: false,
        pinned: false,
        custom: true,
      }));
    try {
      localStorage.setItem(
        "owl-bookmark-added",
        JSON.stringify([...bookmarks.filter((item) => item.custom), ...added]),
      );
    } catch {
      feedback.textContent =
        "Browser storage is full or unavailable. Nothing was imported.";
      confirm.disabled = false;
      return;
    }
    bookmarks.push(...added);
    persist();
    view = "all";
    domain = "";
    selectedDomainGroup = "";
    query = "";
    selectedPerson = "";
    document.querySelector("#bookmark-search").value = "";
    document.querySelector("#add-bookmark").hidden = true;
    render();
    dialog.close();
    toast(`Imported ${added.length} bookmarks on this browser.`);
  });
})();
