"use strict";
function bookmarkPageIdentity(value, pageId = "") {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    const path = url.pathname.match(/^(.*?)(?:\/pages\/|\/spaces\/|\/display\/|\/x\/)/);
    if (!path) return null;
    const queryId = [...url.searchParams].find(([key]) => key.toLowerCase() === "pageid")?.[1];
    const id = String(pageId || queryId || url.pathname.match(/\/pages\/(\d+)(?:\/|$)/)?.[1] || "");
    return /^\d+$/.test(id) ? `${url.origin}${path[1]}:page:${id}` : null;
  } catch { return null; }
}
function bookmarkMatchesUrl(item, value) {
  try {
    if (new URL(item.url).href === new URL(value).href) return true;
  } catch { return false; }
  const identity = bookmarkPageIdentity(value);
  return Boolean(identity && identity === bookmarkPageIdentity(item.url, item.page_id));
}
function bookmarkSearchMatches(item, query, fields, mode = "separate", notes = "") {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  if ((fields.includes("url") || fields.includes("page_id")) && bookmarkMatchesUrl(item, query.trim())) return true;
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
if (typeof module !== "undefined") {
  module.exports = bookmarkSearchMatches;
  module.exports.matchesUrl = bookmarkMatchesUrl;
}
if (typeof document !== "undefined") {
  const panel = document.querySelector('#advanced-search-panel');
  const toggle = document.querySelector('#advanced-search-toggle');
  const storageKey = 'owl-bookmark-search-settings';
  const fields = [...panel.querySelectorAll('[name="bookmark-search-field"]')];
  const modes = [...panel.querySelectorAll('[name="bookmark-search-mode"]')];
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved?.fields) && saved.fields.every(value => fields.some(input => input.value === value))) {
      fields.forEach(input => { input.checked = saved.fields.includes(input.value); });
    }
    if (modes.some(input => input.value === saved?.mode)) {
      modes.forEach(input => { input.checked = input.value === saved.mode; });
    }
  } catch {}
  const save = () => {
    try {
      localStorage.setItem(storageKey, JSON.stringify({
        fields: fields.filter(input => input.checked).map(input => input.value),
        mode: modes.find(input => input.checked)?.value || 'separate',
      }));
    } catch {}
  };
  const close = () => { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };
  toggle.addEventListener('click', () => { panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden)); });
  panel.addEventListener('change', () => { save(); render(); });
  document.querySelector('#advanced-search-reset').addEventListener('click', () => {
    panel.querySelectorAll('[name="bookmark-search-field"]').forEach(input => input.checked = true);
    panel.querySelector('[value="separate"]').checked = true;
    save();
    render();
  });
  document.addEventListener('click', event => { if (!panel.contains(event.target) && !toggle.contains(event.target)) close(); });
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); close(); toggle.focus(); } });
}
