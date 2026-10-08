"use strict";
// Relevance ranking for bookmark search: the exact phrase first, then bookmarks containing
// every word, then partial matches. Rare words (e.g. "IDE") weigh more than common ones
// (e.g. "AWS"), and title matches beat notes, URL and page content.
const BOOKMARK_STOPWORDS = new Set(["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "the", "to", "with", "via", "vs"]);
const BOOKMARK_FIELD_WEIGHT = {title: 3, notes: 2, url: 1.5, content: 1};
const BOOKMARK_TIER = {PHRASE: 3, ALL: 2, SOME: 1};
const CONTENT_LIMIT = 60000;

// Same rule as search.js: _ - . / + separate words like spaces ("aws_for_ide").
function relevanceWords(value) {
  return String(value || "").toLocaleLowerCase().replace(/[_\-./\\+]+/g, " ").replace(/\s+/g, " ").trim();
}
function bookmarkSearchTerms(query) {
  query = relevanceWords(query);
  const words = [...new Set(query.split(/\s+/).filter(Boolean))];
  const significant = words.filter(word => !BOOKMARK_STOPWORDS.has(word));
  return {
    phrase: query.toLocaleLowerCase().split(/\s+/).filter(Boolean).join(" "),
    words,
    // Filler words like "for" only count when the query has nothing else.
    key: significant.length ? significant : words,
  };
}

const escapeTerm = term => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const boundary = "(^|[^\\p{L}\\p{N}])";
// 1 = whole word, 0.6 = start of a word ("IDEs"), 0.25 = inside a word ("guide"), 0 = absent.
function termQuality(text, term) {
  if (!text.includes(term)) return 0;
  if (new RegExp(`${boundary}${escapeTerm(term)}(?![\\p{L}\\p{N}])`, "u").test(text)) return 1;
  if (new RegExp(`${boundary}${escapeTerm(term)}`, "u").test(text)) return 0.6;
  return 0.25;
}

// Smallest number of consecutive words that contains every term (word-start matches only).
function termWindow(text, terms) {
  if (terms.length < 2) return 1;
  const tokens = text.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const hits = [];
  tokens.forEach((token, position) => terms.forEach((term, index) => {
    if (token.startsWith(term)) hits.push([position, index]);
  }));
  const seen = new Map();
  let best = Infinity, start = 0;
  for (let end = 0; end < hits.length; end++) {
    seen.set(hits[end][1], (seen.get(hits[end][1]) || 0) + 1);
    while (seen.size === terms.length) {
      best = Math.min(best, hits[end][0] - hits[start][0] + 1);
      const index = hits[start++][1];
      if (seen.get(index) === 1) seen.delete(index); else seen.set(index, seen.get(index) - 1);
    }
  }
  return best;
}

function bookmarkFieldTexts(item, fields, notes) {
  const normal = relevanceWords;
  const texts = {};
  if (fields.includes("title")) texts.title = normal(item.title);
  if (fields.includes("notes")) texts.notes = normal(notes);
  if (fields.includes("url")) texts.url = normal(decodeURIComponentSafe(item.url || "").replace(/[-_/.+%?=&#:~]+/g, " "));
  if (fields.includes("content")) texts.content = normal(item.contentText || item.description).slice(0, CONTENT_LIMIT);
  return texts;
}
function decodeURIComponentSafe(value) {
  try { return decodeURIComponent(value); } catch { return value; }
}

/**
 * Rank bookmarks that already passed the search filter.
 * Returns [{item, tier, score, matched, missing, phraseField}] sorted best first.
 */
// Pages you open often rank higher among equally good matches: the bonus grows with
// each doubling of opens (1 open +6, 3 +12, 7 +18, 31 +30), plus a little for a page
// opened in the last two weeks. How well a page matches (its tier) still comes first.
function usageBonus(item) {
  const views = Math.max(0, Number(item.views) || 0);
  const recent = item.lastViewed && Date.now() - Number(item.lastViewed) < 14 * 86400000 ? 5 : 0;
  return 6 * Math.log2(1 + views) + recent;
}
function rankBookmarks(items, query, fields, notesFor = () => "") {
  const terms = bookmarkSearchTerms(query);
  if (!terms.words.length) return [];
  const prepared = items.map(item => ({item, texts: bookmarkFieldTexts(item, fields, notesFor(item))}));
  // Inverse document frequency over the current results: rare words matter more.
  const frequency = new Map(terms.key.map(term => [term, 0]));
  for (const {texts} of prepared) {
    for (const term of terms.key) if (Object.values(texts).some(text => termQuality(text, term) >= 0.6)) frequency.set(term, frequency.get(term) + 1);
  }
  const idf = term => Math.log(1 + prepared.length / (1 + frequency.get(term)));
  const multiWord = terms.words.length > 1;
  const ranked = prepared.map(({item, texts}) => {
    const entries = Object.entries(texts);
    const phraseField = entries.find(([, text]) => multiWord ? termQuality(text, terms.phrase) >= 0.6 : termQuality(text, terms.phrase) === 1)?.[0] || null;
    // Fragments inside other words ("guide" for "ide") still pass the filter but earn no relevance.
    const credit = (text, term) => { const quality = termQuality(text, term); return quality >= 0.6 ? quality : 0; };
    const best = term => Math.max(0, ...entries.map(([field, text]) => BOOKMARK_FIELD_WEIGHT[field] * credit(text, term)));
    const present = term => entries.some(([, text]) => termQuality(text, term) >= 0.6);
    const matched = terms.key.filter(present);
    const missing = terms.key.filter(term => !present(term));
    const tier = phraseField ? BOOKMARK_TIER.PHRASE : !missing.length ? BOOKMARK_TIER.ALL : BOOKMARK_TIER.SOME;
    let score = terms.key.reduce((sum, term) => sum + idf(term) * best(term) * 10, 0);
    if (phraseField === "title") score += 40;
    if (texts.title !== undefined && terms.key.every(term => termQuality(texts.title, term) >= 0.6)) score += 25;
    if (!missing.length && terms.key.length > 1) {
      // Words close together in one field rank above words scattered across the page.
      const window = Math.min(...entries.filter(([, text]) => terms.key.every(term => termQuality(text, term) >= 0.6)).map(([, text]) => termWindow(text, terms.key)));
      if (Number.isFinite(window)) score += 15 + Math.max(0, 20 - window);
    }
    score += usageBonus(item);
    return {item, tier, score, matched, missing, phraseField};
  });
  return ranked.sort((a, b) => b.tier - a.tier || b.score - a.score || String(a.item.title).localeCompare(String(b.item.title)));
}

if (typeof module !== "undefined") {
  module.exports = {rankBookmarks, bookmarkSearchTerms, termQuality, termWindow};
}

if (typeof document !== "undefined") {
  // "Best matches" panel shown above the bookmark tree while searching.
  const PAGE = 8;
  let limit = PAGE, lastQuery = "";
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  function highlightTerms(text, terms) {
    if (!terms.length) return escapeHtml(text);
    const pattern = new RegExp(`${boundary}(${terms.map(escapeTerm).sort((a, b) => b.length - a.length).join("|")})`, "giu");
    let html = "", last = 0;
    for (const match of text.matchAll(pattern)) {
      const start = match.index + match[1].length;
      html += escapeHtml(text.slice(last, start)) + `<mark>${escapeHtml(match[2])}</mark>`;
      last = start + match[2].length;
    }
    return html + escapeHtml(text.slice(last));
  }
  function snippet(item, notes, terms, words) {
    for (const source of [notes, item.contentText || item.description || ""]) {
      const text = String(source || "").replace(/\s+/g, " ");
      const lower = text.toLocaleLowerCase();
      let at = lower.indexOf(terms.phrase);
      if (at < 0) at = Math.min(...terms.key.map(term => lower.search(new RegExp(`${boundary}${escapeTerm(term)}`, "u"))).filter(index => index >= 0));
      if (!Number.isFinite(at) || at < 0) continue;
      const start = Math.max(0, at - 70), end = Math.min(text.length, at + 150);
      return (start ? "…" : "") + highlightTerms(text.slice(start, end), words) + (end < text.length ? "…" : "");
    }
    return "";
  }
  function reason(result, terms) {
    const single = terms.words.length === 1;
    if (result.tier === BOOKMARK_TIER.PHRASE) return `<span class="match-tier phrase">${single ? "Whole word" : "Exact phrase"}${result.phraseField === "title" ? " in title" : ""}</span>`;
    if (result.tier === BOOKMARK_TIER.ALL) return `<span class="match-tier all">${single ? "Word start" : "All words"}</span>`;
    if (!result.matched.length) return `<span class="match-tier some">Partial word</span>`;
    return `<span class="match-tier some">${result.matched.length} of ${terms.key.length} words</span><span class="match-missing">missing: ${result.missing.map(escapeHtml).join(", ")}</span>`;
  }
  const TREE_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 3v14a2 2 0 0 0 2 2h4M5 9h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><rect x="11" y="6" width="9" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="11" y="16" width="9" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';
  window.renderBestMatches = (filtered) => {
    const panel = document.querySelector("#best-matches");
    if (!panel) return;
    const text = query.trim();
    if (text !== lastQuery) { limit = PAGE; lastQuery = text; }
    const fields = [...document.querySelectorAll('[name="bookmark-search-field"]:checked')].map(input => input.value);
    if (!text || filtered.length < 2 || !fields.some(field => field in BOOKMARK_FIELD_WEIGHT)) { panel.hidden = true; panel.innerHTML = ""; return; }
    const notesFor = item => localPageNotes[item.id] ?? item.notes ?? "";
    const ranked = rankBookmarks(filtered.filter(item => !item.searchOnly), text, fields, notesFor);
    const terms = bookmarkSearchTerms(text);
    const counts = [BOOKMARK_TIER.PHRASE, BOOKMARK_TIER.ALL, BOOKMARK_TIER.SOME].map(tier => ranked.filter(result => result.tier === tier).length);
    const summary = [counts[0] && `${counts[0]} exact phrase`, counts[1] && `${counts[1]} all words`, counts[2] && `${counts[2]} partial`].filter(Boolean).join(" · ");
    panel.hidden = false;
    panel.innerHTML = `<div class="best-matches-heading"><h3>Best matches</h3><span>${escapeHtml(summary)} · ranked by relevance</span></div>
      <ol class="best-matches-list">${ranked.slice(0, limit).map((result, index) => {
        const item = result.item;
        const path = bookmarkFolderPath(item, pageHierarchy[item.id]).join(" › ");
        const preview = result.phraseField === "title" ? "" : snippet(item, notesFor(item), terms, terms.words);
        return `<li class="best-match tier-${result.tier}">
          <span class="best-rank tree-number" title="Number in the bookmark tree">${escapeHtml(window.bookmarkSearchNumbers?.pages.get(item.id) ?? index + 1)}</span>
          <div class="best-body">
            <div class="best-line"><a class="best-title" data-open="${item.id}" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${highlightTerms(item.title || item.url, terms.words)}</a>${reason(result, terms)}</div>
            <div class="best-path">${escapeHtml(path)}</div>
            ${preview ? `<p class="best-snippet">${preview}</p>` : ""}
          </div>
          <div class="best-side"><span class="opens-count">${item.views} ${item.views === 1 ? "open" : "opens"}</span><button type="button" class="best-reveal" data-reveal-tree="${item.id}" title="Show in tree" aria-label="Show ${escapeHtml(item.title || item.url)} in the tree">${TREE_ICON}</button></div>
        </li>`;
      }).join("")}</ol>
      ${ranked.length > limit ? `<button type="button" class="best-more" data-best-more>Show ${Math.min(10, ranked.length - limit)} more · ${ranked.length - limit} left</button>` : ""}`;
  };
  document.addEventListener("click", event => {
    if (event.target.closest("[data-best-more]")) {
      limit += 10;
      window.renderBestMatches(visibleBookmarkIds.map(id => bookmarks.find(item => item.id === id)).filter(Boolean));
      return;
    }
    const reveal = event.target.closest("[data-reveal-tree]");
    if (!reveal) return;
    const id = Number(reveal.dataset.revealTree);
    const row = document.querySelector(`#bookmark-tree [data-bookmark-row="${id}"]`);
    if (!row) return;
    for (let parent = row.parentElement; parent; parent = parent.parentElement) {
      if (parent.matches("details[data-branch]")) { collapsedBranches.delete(parent.dataset.branch); parent.open = true; }
    }
    showPageDetails(id);
    row.scrollIntoView({block: "center", behavior: "smooth"});
    row.classList.remove("best-flash"); void row.offsetWidth; row.classList.add("best-flash");
  });
}
