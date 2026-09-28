"use strict";
const advancedSearch = {ids: new Set(), rank: new Map(), tiers: {}, timer: null, controller: null, version: 0, signature: null, pending: false};
// The server returns matches most relevant first: 3 exact phrase, 2 words close together, 1 all words.
const SEARCH_TIERS = {3: ['phrase', 'Exact phrase'], 2: ['near', 'Words close together'], 1: ['all', 'All words']};
function searchTierBadge(pdf) {
  const words = state.searchQuery.trim().split(/\s+/).filter(word => word && !word.startsWith('-'));
  const mode = document.querySelector('[name="search-mode"]:checked')?.value;
  const tier = SEARCH_TIERS[advancedSearch.tiers[pdf.id]];
  if (!tier || words.length < 2 || mode === 'together') return '';
  return `<span class="match-tier ${tier[0]}" title="Search relevance">${tier[1]}</span>`;
}
function scheduleAdvancedSearch() {
  const status = document.querySelector('#advanced-search-status');
  const q = state.searchQuery.trim();
  const fields = [...document.querySelectorAll('[name="search-field"]:checked')].map(input => input.value);
  const mode = document.querySelector('[name="search-mode"]:checked').value;
  const signature = JSON.stringify([q, fields, mode]);
  const changed = signature !== advancedSearch.signature;
  // Polling must not cancel the same search or blank already displayed matches.
  if (!changed && advancedSearch.pending) return;
  clearTimeout(advancedSearch.timer);
  advancedSearch.controller?.abort();
  const version = ++advancedSearch.version;
  advancedSearch.signature = signature;
  advancedSearch.pending = Boolean(q && fields.length);
  if (changed) { advancedSearch.ids = new Set(); advancedSearch.rank = new Map(); advancedSearch.tiers = {}; }
  status.textContent = !q ? '' : !fields.length ? 'Select a search field' : 'Searching…';
  if (changed) state.currentPage = 1;
  renderCommitChart();
  renderPdfTable();
  if (!q || !fields.length) return;
  advancedSearch.timer = setTimeout(async () => {
    const controller = new AbortController();
    advancedSearch.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/network-automation/api/search/matches', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({q, fields, mode}), signal: controller.signal,
      });
      if (!response.ok) throw new Error('Search unavailable. Please try again.');
      const data = await response.json();
      if (version !== advancedSearch.version) return;
      advancedSearch.ids = new Set(data.ids);
      advancedSearch.rank = new Map(data.ids.map((id, index) => [id, index]));
      advancedSearch.tiers = data.tiers || {};
      status.textContent = '';
      renderCommitChart();
      renderPdfTable();
    } catch (error) {
      if (version === advancedSearch.version) {
        status.textContent = controller.signal.aborted ? 'Search timed out. Try again.' : error.message;
        renderPdfTable();
      }
    } finally {
      clearTimeout(timeout);
      if (version === advancedSearch.version) advancedSearch.pending = false;
    }
  }, 200);
}
const searchPanel = document.querySelector('#advanced-search-panel');
const searchToggle = document.querySelector('#advanced-search-toggle');
function closeAdvancedSearch() {
  searchPanel.hidden = true;
  searchToggle.setAttribute('aria-expanded', 'false');
}
searchToggle.addEventListener('click', () => {
  searchPanel.hidden = !searchPanel.hidden;
  searchToggle.setAttribute('aria-expanded', String(!searchPanel.hidden));
});
searchPanel.addEventListener('change', scheduleAdvancedSearch);
document.querySelector('#advanced-search-reset').addEventListener('click', () => {
  searchPanel.querySelectorAll('[name="search-field"]').forEach(input => input.checked = true);
  searchPanel.querySelector('[value="separate"]').checked = true;
  scheduleAdvancedSearch();
});
document.addEventListener('click', event => {
  if (!searchPanel.contains(event.target) && !searchToggle.contains(event.target)) closeAdvancedSearch();
});
searchPanel.addEventListener('keydown', event => {
  if (event.key === 'Escape') { closeAdvancedSearch(); searchToggle.focus(); }
});
