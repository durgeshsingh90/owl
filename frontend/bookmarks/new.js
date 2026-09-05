"use strict";
(() => {
  const dialog = document.querySelector("#new-bookmarks-dialog"),
    form = document.querySelector("#new-bookmarks-form"),
    input = document.querySelector("#new-bookmark-urls"),
    feedback = document.querySelector("#new-bookmarks-feedback");
  document.querySelector("#new-bookmarks").addEventListener("click", () => {
    form.reset();
    feedback.textContent = "";
    dialog.showModal();
    input.focus();
  });
  document
    .querySelector("#new-bookmarks-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => form.reset());
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const lines = input.value
      .split(/\r?\n/)
      .map((value, index) => ({ value: value.trim(), line: index + 1 }))
      .filter((item) => item.value);
    if (!lines.length) {
      feedback.textContent = "Enter at least one URL.";
      return;
    }
    const parsed = lines.map((item) => ({
        ...item,
        url: parseBookmarkUrl(item.value),
      })),
      invalid = parsed.filter((item) => !item.url);
    if (invalid.length) {
      feedback.textContent = `Check line${invalid.length === 1 ? "" : "s"} ${invalid.map((item) => item.line).join(", ")}. Use complete HTTP or HTTPS URLs without embedded credentials. Nothing has been added.`;
      return;
    }
    const known = new Set(bookmarks.map((item) => item.url));
    let duplicates = 0,
      id = nextBookmarkId();
    const added = [];
    for (const { url } of parsed) {
      if (known.has(url.href)) {
        duplicates++;
        continue;
      }
      known.add(url.href);
      let title = url.hostname;
      try {
        const last = url.pathname.split("/").filter(Boolean).at(-1);
        if (last) title = decodeURIComponent(last).replace(/[-_+]/g, " ");
      } catch {}
      added.push({
        id: id++,
        title,
        url: url.href,
        domain: url.hostname,
        description: "Added in this browser · page details not fetched yet",
        views: 0,
        lastViewed: null,
        added: Date.now(),
        updatedInOwlAt: null,
        favorite: false,
        pinned: false,
        custom: true,
      });
    }
    if (!added.length) {
      feedback.textContent =
        "All these bookmarks are already saved. Nothing was added.";
      return;
    }
    try {
      localStorage.setItem(
        "owl-bookmark-added",
        JSON.stringify([...bookmarks.filter((item) => item.custom), ...added]),
      );
    } catch {
      feedback.textContent =
        "Browser storage is full or unavailable. Nothing was added.";
      return;
    }
    bookmarks.push(...added);
    persist();
    view = "all";
    domain = "";
    selectedDomainGroup = "";
    query = "";
    selectedPerson = "";
    selectedBookmarks.clear();
    document.querySelector("#bookmark-search").value = "";
    document.querySelector("#add-bookmark").hidden = true;
    render();
    dialog.close();
    toast(
      `Added ${added.length} bookmarks${duplicates ? `; skipped ${duplicates} duplicates` : ""}.`,
    );
  });
})();
