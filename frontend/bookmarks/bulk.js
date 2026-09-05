"use strict";
(() => {
  const tree = document.querySelector("#bookmark-tree"),
    dialog = document.querySelector("#delete-bookmarks-dialog"),
    form = document.querySelector("#delete-bookmarks-form"),
    unlock = document.querySelector("#delete-bookmarks-unlock"),
    phrase = document.querySelector("#delete-bookmarks-phrase"),
    confirm = document.querySelector("#delete-bookmarks-confirm"),
    feedback = document.querySelector("#delete-bookmarks-feedback");
  let targets = [];
  tree.addEventListener("click", (event) => {
    if (event.target.matches("[data-select-bookmark]")) event.stopPropagation();
  });
  tree.addEventListener("change", (event) => {
    const box = event.target.closest("[data-select-bookmark]");
    if (!box) return;
    const id = Number(box.dataset.selectBookmark);
    if (box.checked) selectedBookmarks.add(id);
    else selectedBookmarks.delete(id);
    updateBookmarkSelection();
  });
  document
    .querySelector("#select-all-bookmarks")
    .addEventListener("change", (event) => {
      selectedBookmarks.clear();
      if (event.target.checked)
        visibleBookmarkIds.forEach((id) => selectedBookmarks.add(id));
      render();
    });
  document
    .querySelector("#open-selected-bookmarks")
    .addEventListener("click", () => {
      const selected = bookmarks.filter((item) =>
        selectedBookmarks.has(item.id),
      );
      for (const item of selected)
        window.open(item.url, "_blank", "noopener,noreferrer");
      if (selected.length)
        toast(
          "Tabs requested. Allow pop-ups for this site if some do not open.",
        );
    });
  function gate() {
    phrase.disabled = !unlock.checked;
    confirm.disabled =
      !targets.length || !unlock.checked || phrase.value !== "delete all";
  }
  document
    .querySelector("#delete-selected-bookmarks")
    .addEventListener("click", () => {
      targets = bookmarks
        .filter((item) => selectedBookmarks.has(item.id))
        .map((item) => item.id);
      if (!targets.length) return;
      form.reset();
      feedback.textContent = "Deletion is locked.";
      document.querySelector("#delete-bookmark-list").innerHTML = bookmarks
        .filter((item) => targets.includes(item.id))
        .map((item) => `<li>${esc(item.title)}</li>`)
        .join("");
      gate();
      dialog.showModal();
    });
  unlock.addEventListener("change", () => {
    if (!unlock.checked) phrase.value = "";
    gate();
    if (unlock.checked) phrase.focus();
  });
  phrase.addEventListener("input", gate);
  document
    .querySelector("#copy-bookmark-delete-phrase")
    .addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText("delete all");
        feedback.textContent = "Copied";
      } catch {
        feedback.textContent = "Type delete all below.";
      }
    });
  document
    .querySelector("#delete-bookmarks-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    targets = [];
    form.reset();
    gate();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!targets.length || !unlock.checked || phrase.value !== "delete all")
      return;
    const deleted = new Set([...deletedBookmarkIds, ...targets]);
    try {
      localStorage.setItem(
        "owl-bookmark-deleted",
        JSON.stringify([...deleted]),
      );
    } catch {
      feedback.textContent =
        "Unable to save deletion. No bookmarks were removed.";
      return;
    }
    deletedBookmarkIds = deleted;
    for (let i = bookmarks.length - 1; i >= 0; i--)
      if (deleted.has(bookmarks[i].id)) bookmarks.splice(i, 1);
    if (targets.includes(selectedBookmarkId)) {
      selectedBookmarkId = null;
      document.querySelector("#page-details-content").hidden = true;
      document.querySelector("#page-details-empty").hidden = false;
    }
    const count = targets.length;
    selectedBookmarks.clear();
    persist();
    dialog.close();
    render();
    toast(`Deleted ${count} selected bookmarks from this browser.`);
  });
})();
