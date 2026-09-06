"use strict";
function bookmarkSearchMatches(item, query, fields, mode = "separate", notes = "") {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  const values = {
    title: item.title || "",
    notes,
    url: item.url || "",
    content: item.contentText || item.description || "",
    page_id: String(item.page_id || ""),
  };
  const terms = mode === "together" ? [needle] : needle.split(/\s+/);
  return fields.some(field => typeof values[field] === "string" && terms.some(term => values[field].toLocaleLowerCase().includes(term)));
}
function matchesBookmarkSearch(item) {
  const fields = [...document.querySelectorAll('[name="bookmark-search-field"]:checked')].map(input => input.value);
  const mode = document.querySelector('[name="bookmark-search-mode"]:checked').value;
  return bookmarkSearchMatches(item, query, fields, mode, localPageNotes[item.id] ?? item.notes ?? "");
}
if (typeof module !== "undefined") module.exports = bookmarkSearchMatches;
if (typeof document !== "undefined") {
  const panel = document.querySelector('#advanced-search-panel');
  const toggle = document.querySelector('#advanced-search-toggle');
  const close = () => { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };
  toggle.addEventListener('click', () => { panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden)); });
  panel.addEventListener('change', () => render());
  document.querySelector('#advanced-search-reset').addEventListener('click', () => {
    panel.querySelectorAll('[name="bookmark-search-field"]').forEach(input => input.checked = true);
    panel.querySelector('[value="separate"]').checked = true;
    render();
  });
  document.addEventListener('click', event => { if (!panel.contains(event.target) && !toggle.contains(event.target)) close(); });
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); close(); toggle.focus(); } });
}
