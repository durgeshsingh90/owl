"use strict";
(() => {
  const panel = document.getElementById("bookmark-failures");
  const list = document.getElementById("bookmark-failure-list");
  const entries = new Map();
  const storageKey = "owl-dismissed-bookmark-failures";
  let dismissed = new Set();
  try { dismissed = new Set(JSON.parse(localStorage.getItem(storageKey) || "[]")); } catch {}
  function close(key, dismiss = true) {
    const entry = entries.get(key);
    if (!entry) return;
    if (dismiss && entry.persistent) {
      dismissed.add(key);
      try { localStorage.setItem(storageKey, JSON.stringify([...dismissed].slice(-1000))); } catch {}
    }
    entry.row.remove();
    entries.delete(key);
    panel.hidden = entries.size === 0;
    if (dismiss) entry.onDismiss?.();
  }
  document.getElementById("dismiss-bookmark-failures").onclick = () => {
    for (const key of [...entries.keys()]) close(key);
  };
  window.removeBookmarkFailure = key => close(key, false);
  window.showBookmarkFailure = (message, key = message, persistent = false, onDismiss = null) => {
    if (entries.has(key) || (persistent && dismissed.has(key))) return;
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = message;
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Dismiss";
    dismiss.setAttribute("aria-label", "Dismiss failure: " + message);
    dismiss.addEventListener("click", () => close(key));
    row.append(text, dismiss);
    entries.set(key, {row, persistent, onDismiss});
    list.append(row);
    panel.hidden = false;
  };
})();
