"use strict";
(() => {
  const panel = document.getElementById("bookmark-failures");
  const list = document.getElementById("bookmark-failure-list");
  const entries = new Map();
  window.showBookmarkFailure = (message, key = message) => {
    if (entries.has(key)) return;
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = message;
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Dismiss";
    dismiss.setAttribute("aria-label", "Dismiss failure: " + message);
    dismiss.addEventListener("click", () => {
      row.remove();
      entries.delete(key);
      panel.hidden = entries.size === 0;
    });
    row.append(text, dismiss);
    entries.set(key, row);
    list.append(row);
    panel.hidden = false;
  };
})();
