"use strict";
(() => {
  let ready = false,
    revision = 0,
    saving = false,
    dirty = false, pending = null;
  window.saveBookmarkDatabase = () => {
    if (!ready) return Promise.resolve(false);
    dirty = true;
    if (saving) return pending;
    saving = true;
    pending = (async () => {
    try {
      while (dirty) {
        dirty = false;
        const response = await fetch("/api/bookmarks/workspace", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            revision,
            bookmarks,
            groups: domainGroups,
            notes: localPageNotes,
            starred_people: [...starredConfluencePeople],
            starred_folders: [...starredBookmarkFolders],
          }),
        });
        if (!response.ok)
          throw new Error(
            response.status === 409
              ? "Bookmarks changed in another tab. Reload before editing."
              : "Database save failed. Keep this page open and retry.",
          );
        revision = (await response.json()).revision;
      }
      return true;
    } catch (error) {
      toast(error.message);
      return false;
    } finally {
      saving = false;
    }
    })();
    return pending;
  };
  async function load() {
    try {
      const response = await fetch("/api/bookmarks/workspace", {
        cache: "no-store",
      });
      if (!response.ok) throw new Error();
      let data = await response.json();
      // Import only user-created browser records once into a new database.
      if (data.revision === 0 && data.bookmarks.length === 0) {
        let existing = [];
        try {
          existing = JSON.parse(
            localStorage.getItem("owl-bookmark-added") || "[]",
          ).filter(
            (item) => item.custom === true && parseBookmarkUrl(item.url),
          );
        } catch {}
        if (existing.length) {
          let notes = {},
            groups = [];
          try {
            notes = JSON.parse(
              localStorage.getItem("owl-bookmark-notes") || "{}",
            );
            groups = JSON.parse(
              localStorage.getItem("owl-bookmark-domain-groups") || "[]",
            );
          } catch {}
          const imported = {
            ...data,
            bookmarks: existing,
            notes: Object.fromEntries(
              existing.map((item) => [item.id, notes[item.id] || ""]),
            ),
            groups,
          };
          const saved = await fetch("/api/bookmarks/workspace", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(imported),
          });
          if (!saved.ok) throw new Error();
          data = { ...imported, revision: (await saved.json()).revision };
        }
      }
      bookmarks.splice(0, bookmarks.length, ...data.bookmarks);
      domainGroups = data.groups;
      Object.assign(localPageNotes, data.notes);
      starredBookmarkFolders = new Set(data.starred_folders || []);
      starredConfluencePeople = new Set((data.starred_people || []).map(confluencePersonKey));
      revision = data.revision;
      ready = true;
      window.bookmarkDatabaseReady = true;
      render();
    } catch {
      toast("Unable to load bookmarks from the database. Refresh to retry.");
    }
  }
  document.addEventListener(
    "submit",
    (event) => {
      if (
        !ready &&
        event.target.closest(
          "#bookmark-search-form, #import-bookmarks-form, #delete-bookmarks-form",
        )
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
        toast("Wait for the database to load.");
      }
    },
    true,
  );
  void load();
})();
