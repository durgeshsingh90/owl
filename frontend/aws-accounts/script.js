"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const displayDate = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—';
  const PIN_KEY = 'owl-aws-accounts-pinned', SELECTED_KEY = 'owl-aws-accounts-category', SORT_KEY = 'owl-aws-accounts-sort', SORT_DIR_KEY = 'owl-aws-accounts-sort-direction';
  // Each sort starts in its natural direction; clicking the active one again reverses it.
  const SORT_DEFAULT = {az: 'asc', count: 'desc', file: 'asc'};
  const SORT_LABEL = {
    az: {asc: ['A–Z', 'Category name A to Z'], desc: ['Z–A', 'Category name Z to A']},
    count: {desc: ['Count ↓', 'Most accounts first'], asc: ['Count ↑', 'Fewest accounts first']},
    file: {asc: ['File ↓', 'Order in the JSON file'], desc: ['File ↑', 'Reverse of the JSON file order']},
  };
  // Trailing environment token, optionally followed by an account-number suffix (mc-x-nonp-216989139306).
  const ENV_PATTERN = /-(prod|production|prd|nonp|nonprod|nonprd|work|mtf|dev|develop|development|test|testing|qa|uat|sit|stage|staging|stg|preprod|pre|perf|sandbox|sbx|demo|poc|lab|dr)(?:-\d+)?$/i;
  const ENV_CLASS = {prod:'prod',production:'prod',prd:'prod',nonp:'nonp',nonprod:'nonp',nonprd:'nonp',work:'work',
    // Environments as grouped from the AWS config file.
    'Prod':'prod','Non-prod':'nonp','Stage':'stage','Test':'test','Dev':'work','Sandbox':'other','DR':'other','Other':'none'};
  const ENV_FIRST = ['Prod','Non-prod','Stage','Test','Dev','Sandbox','DR','prod','production','prd','nonp','nonprod','nonprd','work'];
  // Known environments first, then any other environment alphabetically, then accounts without one.
  const envRank = env => env === '' || env === 'Other' ? 1e6 : ENV_FIRST.includes(env) ? ENV_FIRST.indexOf(env) : 100;
  const envSort = (a, b) => envRank(a) - envRank(b) || a.localeCompare(b);
  const EXPANDED_KEY = 'owl-aws-accounts-expanded', CLOSED_ENVS_KEY = 'owl-aws-accounts-closed-envs';
  let expanded = new Set(), closedEnvs = new Set();
  try {
    expanded = new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]'));
    closedEnvs = new Set(JSON.parse(localStorage.getItem(CLOSED_ENVS_KEY) || '[]'));
  } catch { /* Open and closed groups stay for this page only. */ }
  let data = null, envFilter = 'all', pinned = new Set(), selected = null, sortMode = 'az', sortDir = 'asc', toastTimer = 0;
  try {
    pinned = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]'));
    selected = localStorage.getItem(SELECTED_KEY);
    sortMode = ['az', 'count', 'file'].includes(localStorage.getItem(SORT_KEY)) ? localStorage.getItem(SORT_KEY) : 'az';
    sortDir = ['asc', 'desc'].includes(localStorage.getItem(SORT_DIR_KEY)) ? localStorage.getItem(SORT_DIR_KEY) : SORT_DEFAULT[sortMode];
  } catch { /* Pins and the selected category stay for this page only. */ }
  const store = (key, value) => { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* Per-page only. */ } };
  const label = name => name.replace(/_/g, ' ');
  const STARRED = '__starred__';
  const starredSet = () => new Set(data?.stars || []);
  const PROJECT = 'project:';
  const projectKey = project => PROJECT + project.id;
  const projectFor = key => key?.startsWith(PROJECT) ? (data?.projects || []).find(project => projectKey(project) === key) : null;

  function error(message) { $('error').textContent = message; $('error').hidden = !message; }
  async function api(path, options = {}) {
    const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...options.headers}});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(body.detail) ? body.detail.map(item => `${item.loc.slice(1).join(' › ')}: ${item.msg}`).join('\n') : body.detail;
      throw Error(detail || 'The request could not be completed.');
    }
    return body;
  }

  function environment(profile, account = null) {
    const match = ENV_PATTERN.exec(profile);
    const base = match ? profile.slice(0, match.index) : profile;
    // Accounts from the AWS config file carry their grouped environment (Prod, Non-prod…).
    const env = account?.environment || (match ? match[1].toLowerCase() : '');
    return {base, env, kind: env ? ENV_CLASS[env] || 'other' : 'none'};
  }

  // Group sibling accounts (x-prod, x-nonp, x-work) together, keeping the file's order of first appearance.
  function groups(accounts) {
    const map = new Map();
    for (const account of accounts) {
      const info = environment(account.profile, account);
      if (!map.has(info.base)) map.set(info.base, []);
      map.get(info.base).push({...account, ...info});
    }
    return [...map.values()].flatMap(items => items
      .sort((a, b) => envSort(a.env, b.env))
      .map((item, index) => ({...item, last: index === items.length - 1})));
  }

  const short = count => (window.shortCount || String)(count);
  const copyCount = (kind, value) => data?.copies?.[kind]?.[value] || 0;
  // One counter per account: copying the name or the ID both count. Older ID-only counts are added in.
  const accountCount = (profile, id) => copyCount('profile', profile) + copyCount('account_id', String(id));
  const accountBadge = (profile, id) => {
    const count = accountCount(profile, id);
    return `<span class="copies" data-count-account="${esc(profile)}" data-count-id="${esc(id)}"${count ? '' : ' hidden'} title="Copied ${count} time${count === 1 ? '' : 's'}">⧉ ${short(count)}</span>`;
  };
  const badge = (kind, value) => {
    const count = copyCount(kind, value);
    return `<span class="copies" data-count-kind="${kind}" data-count-value="${esc(value)}"${count ? '' : ' hidden'} title="Copied ${count} time${count === 1 ? '' : 's'}">⧉ ${short(count)}</span>`;
  };

  // Highlight every search word (longest first so "stablecoin" wins over "stable").
  function highlight(text, query) {
    const words = [...new Set(query.split(/\s+/).filter(Boolean))].sort((a, b) => b.length - a.length);
    if (!words.length) return esc(text);
    const lower = text.toLowerCase(), marked = new Array(text.length).fill(false);
    for (const word of words) for (let at = lower.indexOf(word); at >= 0; at = lower.indexOf(word, at + 1)) marked.fill(true, at, at + word.length);
    let html = '', open = false;
    for (let index = 0; index < text.length; index++) {
      if (marked[index] !== open) { html += open ? '</mark>' : '<mark>'; open = marked[index]; }
      html += esc(text[index]);
    }
    return html + (open ? '</mark>' : '');
  }

  // Search relevance: 4 exact name or ID, 3 the phrase in order, 2 every word as a whole
  // name part (or its start), 1 every word somewhere. Every word must appear to match.
  const compactText = value => value.replace(/[^a-z0-9]/g, '');
  const searchWords = query => [...new Set(query.split(/\s+/).filter(Boolean))];
  function accountMatches(item, words) {
    const profile = item.profile.toLowerCase(), id = String(item.account_id).toLowerCase();
    return words.every(word => profile.includes(word) || id.includes(word));
  }
  function accountRelevance(item, query, words) {
    const profile = item.profile.toLowerCase(), id = String(item.account_id).toLowerCase();
    const tokens = profile.split(/[^a-z0-9]+/).filter(Boolean);
    const quality = word => tokens.includes(word) || id === word ? 1 : tokens.some(token => token.startsWith(word)) || id.startsWith(word) ? 0.7 : 0.3;
    const qualities = words.map(quality);
    const tier = profile === query || id === query || compactText(profile) === compactText(query) ? 4
      : words.length > 1 ? (compactText(profile).includes(compactText(query)) ? 3 : qualities.every(value => value >= 0.7) ? 2 : 1)
        : qualities[0] === 1 ? 3 : qualities[0] >= 0.7 ? 2 : 1;
    const score = qualities.reduce((sum, value) => sum + value, 0) + (profile.startsWith(words[0]) || tokens[1]?.startsWith(words[0]) ? 0.5 : 0)
      - profile.length / 200 + Math.log1p(accountCount(item.profile, item.account_id)) * 0.3;
    return {tier, score};
  }
  const BEST = '__best__', BEST_LIMIT = 10;

  // Strictly by category name, account count or file order, in the chosen direction.
  // Names compare without leading spaces, emoji or punctuation ("☁ Platform" sorts under P).
  const sortName = name => label(name).replace(/^[^\p{L}\p{N}]+/u, '') || label(name);
  function orderedCategories() {
    const byName = (a, b) => sortName(a).localeCompare(sortName(b), undefined, {sensitivity: 'base', numeric: true});
    const direction = sortDir === 'asc' ? 1 : -1;
    const names = Object.keys(data.categories);
    if (sortMode === 'az') names.sort((a, b) => direction * byName(a, b));
    if (sortMode === 'count') names.sort((a, b) => direction * (data.categories[a].length - data.categories[b].length) || byName(a, b));
    if (sortMode === 'file' && sortDir === 'desc') names.reverse();
    return names;
  }

  function renderHeader() {
    const all = Object.values(data.categories).flat();
    const count = all.length;
    $('total').textContent = count.toLocaleString();
    const categories = Object.keys(data.categories).length;
    const mismatch = Number.isInteger(data.total_accounts) && data.total_accounts !== count ? ` · file reports ${data.total_accounts.toLocaleString()}` : '';
    $('summary').textContent = `${categories.toLocaleString()} categories · read from ${data.config?.path || 'the AWS config file'} · ${displayDate(data.imported_at)}${mismatch}`;
    $('file-note').textContent = `${count.toLocaleString()} accounts · read ${displayDate(data.imported_at)}`;
    const roles = Object.entries(data.common_roles || {});
    $('roles').innerHTML = roles.length ? '<span class="eyebrow">COMMON ROLES</span>' + roles.map(([name, value]) =>
      `<button class="role" type="button" data-copy="${esc(value)}" data-kind="role" data-label="${esc(label(name))}" title="Copy ${esc(label(name))}"><span class="role-name">${esc(label(name))}</span><code>${esc(value)}</code>${badge('role', value)}<span class="copy-icon" aria-hidden="true">⧉</span></button>`).join('') : '';
    $('roles').hidden = !roles.length;
    const counts = new Map();
    for (const account of all) {
      const env = environment(account.profile, account).env;
      counts.set(env, (counts.get(env) || 0) + 1);
    }
    if (envFilter !== 'all' && !counts.has(envFilter)) envFilter = 'all';
    const chip = (key, text, count, kind) => `<button type="button" data-env="${esc(key)}" class="${kind}" aria-pressed="${key === envFilter}">${esc(text)}<small>${count}</small></button>`;
    $('env-filter').innerHTML = chip('all', 'All', count, '') +
      [...counts.keys()].sort(envSort).map(env => chip(env, env || 'no env', counts.get(env), env ? ENV_CLASS[env] || 'other' : 'none')).join('');
  }

  const row = (item, query, next, stars, project) => `<div class="row${!next || next.base !== item.base ? ' group-end' : ''}" draggable="true" data-profile="${esc(item.profile)}">
    <button class="star-account" type="button" data-star="${esc(item.profile)}" aria-pressed="${stars.has(item.profile)}" title="${stars.has(item.profile) ? 'Unstar' : 'Star'} ${esc(item.profile)}">${stars.has(item.profile) ? '★' : '☆'}</button>
    <button class="name" type="button" data-copy="${esc(item.profile)}" data-account="${esc(item.profile)}" data-label="Account name" title="Copy account name · ${esc(item.profile)}"><span class="text">${highlight(item.profile, query)}</span></button>
    <span class="env ${ENV_CLASS[item.env] || 'other'}">${esc(item.env)}</span>
    <button class="id" type="button" data-copy="${esc(item.account_id)}" data-account="${esc(item.profile)}" data-label="Account ID" title="Copy account ID"><span class="text">${highlight(String(item.account_id), query)}</span></button>
    <span class="count-cell">${accountBadge(item.profile, item.account_id)}<button class="row-action" type="button" data-add-menu="${esc(item.profile)}" title="Add ${esc(item.profile)} to a project" aria-label="Add to a project">⊕</button>${project ? `<button class="row-action remove" type="button" data-remove="${esc(item.profile)}" data-project="${project.id}" title="Remove from ${esc(project.name)}" aria-label="Remove from project">×</button>` : ''}</span>
  </div>`;

  // Inside a category, accounts sit under their environment, each part opening and closing.
  function envGroups(name, items, query, stars) {
    const byEnv = new Map();
    for (const item of items) {
      if (!byEnv.has(item.env)) byEnv.set(item.env, []);
      byEnv.get(item.env).push(item);
    }
    if (byEnv.size < 2 && !byEnv.has('') ) {
      const [[env, list]] = [...byEnv];
      return envBlock(name, env, list, query, stars);
    }
    return [...byEnv.keys()].sort(envSort).map(env => envBlock(name, env, byEnv.get(env), query, stars)).join('');
  }
  function envBlock(name, env, list, query, stars) {
    const key = `${name}\u0000${env}`;
    list = list.slice().sort((a, b) => a.profile.localeCompare(b.profile));
    return `<details class="env-group" data-env-group="${esc(key)}" ${closedEnvs.has(key) ? '' : 'open'}>
      <summary><span class="env ${ENV_CLASS[env] || 'other'}">${esc(env || 'no env')}</span><span class="count">${list.length} account${list.length === 1 ? '' : 's'}</span></summary>
      <div class="rows">${list.map(item => row({...item, last: true}, query, null, stars)).join('')}</div>
    </details>`;
  }

  function render() {
    const query = $('search').value.trim().toLowerCase();
    const stars = starredSet();
    if (selected && !(selected in data.categories) && !(selected === STARRED && stars.size) && !projectFor(selected)) selected = null;
    const words = searchWords(query);
    const keep = item => (envFilter === 'all' || item.env === envFilter) && (!words.length || accountMatches(item, words));
    const matches = new Map();
    const byProfile = new Map();
    for (const account of Object.values(data.categories).flat()) if (!byProfile.has(account.profile)) byProfile.set(account.profile, account);
    for (const project of data.projects || []) {
      matches.set(projectKey(project), groups(project.accounts.map(profile => byProfile.get(profile)).filter(Boolean)).filter(keep));
    }
    if (stars.size) {
      // Starred accounts, once each, even when a profile appears in several categories.
      const seen = new Set();
      const starred = Object.values(data.categories).flat().filter(account =>
        stars.has(account.profile) && !seen.has(account.profile) && seen.add(account.profile));
      matches.set(STARRED, groups(starred).filter(keep));
    }
    for (const name of orderedCategories()) matches.set(name, groups(data.categories[name]).filter(keep));
    const extra = name => name === STARRED || name.startsWith(PROJECT);
    const everything = [...matches].filter(([name]) => !extra(name)).reduce((sum, [, items]) => sum + items.length, 0);
    const navItem = (name, count, text, isPinned) => `<button type="button" data-select="${esc(name)}" aria-current="${(name || null) === selected}" class="${isPinned ? 'pinned' : ''}${count ? '' : ' empty'}">
      <span class="nav-name">${isPinned ? '<span class="star">★</span>' : ''}${esc(text)}</span><span class="nav-count">${count}</span></button>`;
    lastInView = null;
    const projects = data.projects || [];
    $('project-nav').innerHTML = projects.map(project => navItem(projectKey(project), matches.get(projectKey(project)).length, project.name, false)
      .replace('<button ', `<button data-drop-project="${project.id}" `).replace('<span class="nav-name">', '<span class="nav-name"><span class="folder">▣</span>')).join('');
    $('project-hint').hidden = projects.length > 0;
    // Each category opens to its environments (Prod, Non-prod, Stage…); one click shows that part.
    const envList = (name, items) => {
      const counts = new Map();
      for (const item of items) counts.set(item.env, (counts.get(item.env) || 0) + 1);
      if (!counts.size || !expanded.has(name)) return '';
      return `<div class="env-nav">${[...counts.keys()].sort(envSort).map(env => `<button type="button" data-select="${esc(name)}" data-select-env="${esc(env)}" aria-current="${selected === name && envFilter === env}"><span class="env ${ENV_CLASS[env] || 'other'}">${esc(env || 'no env')}</span><span class="nav-count">${counts.get(env)}</span></button>`).join('')}</div>`;
    };
    const toggle = name => `<button type="button" class="nav-expand" data-expand="${esc(name)}" aria-expanded="${expanded.has(name)}" title="${expanded.has(name) ? 'Hide' : 'Show'} environments">${expanded.has(name) ? '▾' : '▸'}</button>`;
    $('category-nav').innerHTML = navItem('', everything, 'All accounts', false) +
      [...matches].filter(([name]) => !name.startsWith(PROJECT)).map(([name, items]) => name === STARRED
        ? navItem(name, items.length, 'Starred accounts', true).replace('class="pinned', 'class="starred-nav pinned')
        : `<div class="nav-group">${toggle(name)}${navItem(name, items.length, label(name), pinned.has(name))}</div>${envList(name, items)}`).join('');
    const sections = [];
    let shown = 0;
    const filtering = Boolean(query) || envFilter !== 'all';
    for (const [name, items] of matches) {
      if (selected && name !== selected) continue;
      const project = projectFor(name);
      // Empty projects stay visible as drop targets unless a search or filter is active.
      if (!items.length && !(project && !filtering)) continue;
      if (!extra(name) || selected === name) shown += items.length;
      if (project) {
        sections.push({compact: !selected, html: `<section class="category project${!selected ? ' compact' : ''}" data-category="${esc(name)}" data-drop-project="${project.id}">
          <header><h2><span class="folder">▣</span>${esc(project.name)}</h2><span class="count">${items.length} account${items.length === 1 ? '' : 's'}</span>
            <button class="pin" type="button" data-rename-project="${project.id}" title="Rename ${esc(project.name)}">✎</button>
            <button class="pin" type="button" data-delete-project="${project.id}" title="Delete project ${esc(project.name)}">🗑</button></header>
          ${items.length ? `<div class="rows">${items.map((item, index) => row(item, query, items[index + 1], stars, project)).join('')}</div>`
            : '<div class="drop-zone">Drag accounts here, or use ⊕ on any account row</div>'}
        </section>`});
        continue;
      }
      if (!items.length) continue;
      const isStarred = name === STARRED;
      const isPinned = isStarred || pinned.has(name);
      // Every category is a card in one column flow; one chosen category gets the full width.
      const compact = !selected;
      sections.push({compact, html: `<section class="category${isPinned ? ' pinned' : ''}${isStarred ? ' starred' : ''}${compact ? ' compact' : ''}" data-category="${esc(name)}">
        <header><h2>${isStarred ? '★ Starred accounts' : esc(label(name))}</h2><span class="count">${items.length} account${items.length === 1 ? '' : 's'}</span>
          ${isStarred ? '' : `<button class="pin" type="button" data-pin="${esc(name)}" aria-pressed="${isPinned}" title="${isPinned ? 'Remove highlight from' : 'Highlight'} category ${esc(label(name))}">${isPinned ? '★' : '☆'}</button>`}</header>
        ${isStarred ? `<div class="rows">${items.map((item, index) => row(item, query, items[index + 1], stars)).join('')}</div>` : envGroups(name, items, query, stars)}
      </section>`});
    }
    // Consecutive small categories share one row of boxes.
    const html = [];
    if (words.length && !selected) {
      // Best matches across every category, most relevant first.
      const seen = new Set();
      const ranked = Object.values(data.categories).flat()
        .filter(account => !seen.has(account.profile) && seen.add(account.profile))
        .map(account => ({...account, ...environment(account.profile, account)}))
        .filter(keep)
        .map(item => ({item, ...accountRelevance(item, query, words)}))
        .sort((a, b) => b.tier - a.tier || b.score - a.score || a.item.profile.localeCompare(b.item.profile));
      if (ranked.length > 1) {
        const top = ranked.slice(0, BEST_LIMIT).map(result => result.item);
        const labels = {4: 'exact', 3: 'phrase', 2: 'whole words', 1: 'partial words'};
        const summary = [4, 3, 2, 1].map(tier => [tier, ranked.filter(result => result.tier === tier).length]).filter(([, count]) => count)
          .map(([tier, count]) => `${count} ${labels[tier]}`).join(' · ');
        html.push(`<section class="category best" data-category="${BEST}">
          <header><h2>Best matches</h2><span class="count">${esc(summary)} · most relevant first${ranked.length > BEST_LIMIT ? ` · top ${BEST_LIMIT} of ${ranked.length}` : ''}</span></header>
          <div class="rows">${top.map(item => row({...item, last: true}, query, null, stars)).join('')}</div>
        </section>`);
      }
    }
    // Most used: every account copied at least once, most copied first, above the categories.
    if (!selected) {
      const seen = new Set();
      const used = Object.values(data.categories).flat()
        .filter(account => !seen.has(account.profile) && seen.add(account.profile))
        .map(account => ({...account, ...environment(account.profile, account), uses: accountCount(account.profile, account.account_id)}))
        .filter(item => item.uses && keep(item))
        .sort((a, b) => b.uses - a.uses || a.profile.localeCompare(b.profile));
      if (used.length) html.push(`<section class="category most-used" data-category="__most_used__">
        <header><h2>Most used</h2><span class="count">${used.length} account${used.length === 1 ? '' : 's'} · most copied first</span></header>
        <div class="rows">${used.map(item => row({...item, last: true}, query, null, stars)).join('')}</div>
      </section>`);
    }
    // All categories flow down balanced columns (masonry), so no box leaves a gap.
    const cards = sections.filter(section => section.compact).map(section => section.html);
    if (cards.length) html.push(`<div class="compact-grid">${cards.join('')}</div>`);
    html.push(...sections.filter(section => !section.compact).map(section => section.html));
    $('title').firstChild.textContent = selected === STARRED ? 'Starred accounts ' : projectFor(selected) ? `${projectFor(selected).name} ` : selected ? `${label(selected)} ` : 'AWS accounts ';
    document.querySelectorAll('[data-sort]').forEach(button => {
      const active = button.dataset.sort === sortMode;
      const [text, title] = SORT_LABEL[button.dataset.sort][active ? sortDir : SORT_DEFAULT[button.dataset.sort]];
      button.setAttribute('aria-pressed', active);
      button.textContent = text;
      button.title = active ? `${title} · click to reverse` : title;
    });
    $('total').textContent = shown.toLocaleString();
    $('categories').innerHTML = html.join('');
    $('no-results').hidden = shown > 0;
    trackScroll();
  }

  // Mark the categories under the sticky top bar in the sidebar as the page scrolls.
  let lastInView = '';
  function trackScroll() {
    const line = 68 + 60;
    const sections = [...document.querySelectorAll('#categories .category')];
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    let active = sections.filter(section => {
      const box = section.getBoundingClientRect();
      return box.top <= line && box.bottom > line;
    });
    if (atBottom) active = sections.filter(section => section.getBoundingClientRect().bottom <= window.innerHeight + 4 && section.getBoundingClientRect().bottom > line);
    if (!active.length && sections.length && sections[0].getBoundingClientRect().top > line) active = [sections[0]];
    const names = new Set(active.map(section => section.dataset.category));
    const key = [...names].join('\n');
    if (key === lastInView) return;
    lastInView = key;
    let first = null;
    document.querySelectorAll('.sidebar [data-select]').forEach(button => {
      const inView = names.has(button.dataset.select);
      button.classList.toggle('in-view', inView);
      if (inView && !first) first = button;
    });
    first?.scrollIntoView({block: 'nearest'});
  }

  function show() {
    $('empty-state').hidden = Boolean(data);
    $('content').hidden = !data;
    $('file-note').textContent = '';
    $('export-button').hidden = !data;
    if (!data) return;
    renderHeader();
    render();
  }

  let configState = null;
  async function load() {
    try {
      const result = await api('/api/aws-accounts');
      data = result.imported ? result : null;
      configState = result.config;
      // A config problem shows on the page; the last accounts read stay visible.
      error(result.config?.error || '');
      if (!data && result.config) $('empty-detail').textContent = result.config.error || `Reading ${result.config.path}.`;
    } catch (failure) {
      error(`Could not load saved accounts: ${failure.message}`);
      data = null;
    }
    show();
  }

  // Where the AWS config file is. OWL reads it again by itself whenever it changes.
  function openConfig() {
    const config = data?.config || configState || {};
    // The default is ~/.aws/config (C:\Users\you\.aws\config on Windows); it is filled in.
    $('config-path').value = config.custom ? config.path : '~/.aws/config';
    $('config-path').placeholder = '~/.aws/config';
    $('config-note').textContent = config.loaded_at
      ? `Reading ${config.path} · last read ${displayDate(config.loaded_at)}. Leave empty for the default: ${config.default_path}.`
      : `Leave empty for the default: ${config.default_path || '~/.aws/config'}.`;
    $('config-error').hidden = !config.error;
    $('config-error').textContent = config.error || '';
    $('config-dialog').showModal();
    $('config-path').focus();
  }
  async function saveConfig(path) {
    // ~/.aws/config is the default itself, so it follows the user's home folder.
    if (path.replace(/\\/g, '/').replace(/^"|"$/g, '') === '~/.aws/config') path = '';
    try {
      const state = await api('/api/aws-accounts/config', {method: 'PUT', body: JSON.stringify({path})});
      $('config-dialog').close();
      toast(`Reading AWS accounts from ${state.path}`);
      await load();
    } catch (failure) {
      // "Not Found" means the running backend predates this setting.
      $('config-error').textContent = failure.message === 'Not Found'
        ? 'This OWL backend is older than the page. Restart OWL (python dev.py restart), then try again.'
        : failure.message;
      $('config-error').hidden = false;
    }
  }

  async function copy(text, label, kind, account) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement('textarea'), {value: text});
      document.body.append(area); area.select(); document.execCommand('copy'); area.remove();
    }
    toast(`${label} copied: ${text}`);
    if (account) { kind = 'profile'; text = account; }
    if (!kind) return;
    try {
      const {count} = await api('/api/aws-accounts/copies', {method:'POST', body:JSON.stringify({kind, value:text})});
      ((data.copies ||= {})[kind] ||= {})[text] = count;
      const show = (element, total) => {
        element.textContent = `⧉ ${short(total)}`;
        element.title = `Copied ${total} time${total === 1 ? '' : 's'}`;
        element.hidden = false;
      };
      if (account) {
        document.querySelectorAll('[data-count-account]').forEach(element => {
          if (element.dataset.countAccount === account) show(element, accountCount(account, element.dataset.countId));
        });
        render();
      } else {
        document.querySelectorAll('[data-count-kind]').forEach(element => {
          if (element.dataset.countKind === kind && element.dataset.countValue === text) show(element, count);
        });
      }
    } catch { /* The copy still worked; only the counter failed to save. */ }
  }

  async function toggleStar(profile) {
    const starred = !starredSet().has(profile);
    const before = data.stars || [];
    data.stars = starred ? [...before, profile] : before.filter(value => value !== profile);
    render();
    try {
      await api('/api/aws-accounts/stars', {method:'PUT', body:JSON.stringify({profile, starred})});
      toast(`${starred ? 'Starred' : 'Unstarred'} ${profile}`);
    } catch (failure) {
      data.stars = before;
      render();
      error(`Could not save the star: ${failure.message}`);
    }
  }

  // Projects: personal groupings of accounts. Accounts stay in their categories too.
  let dialogProject = null, dialogProfile = null;
  function openProjectDialog(project = null, profile = null) {
    dialogProject = project; dialogProfile = profile;
    $('project-dialog-title').textContent = project ? 'Rename project' : 'New project';
    $('project-save').textContent = project ? 'Save' : profile ? 'Create and add' : 'Create project';
    $('project-name').value = project?.name || '';
    $('project-dialog-note').textContent = profile ? `${profile} will be added to the new project.` : '';
    $('project-dialog-note').hidden = !profile;
    $('project-error').hidden = true;
    $('project-dialog').showModal();
    $('project-name').select();
  }
  $('project-form').addEventListener('submit', async event => {
    event.preventDefault();
    const name = $('project-name').value.trim();
    if (!name) return;
    try {
      if (dialogProject) {
        await api(`/api/aws-accounts/projects/${dialogProject.id}`, {method:'PATCH', body:JSON.stringify({name})});
        dialogProject.name = name.replace(/\s+/g, ' ');
        toast(`Renamed to ${dialogProject.name}`);
      } else {
        const project = await api('/api/aws-accounts/projects', {method:'POST', body:JSON.stringify({name})});
        (data.projects ||= []).push(project);
        if (dialogProfile) await addToProject(project.id, dialogProfile, false);
        toast(`Created ${project.name}${dialogProfile ? ` with ${dialogProfile}` : ''}`);
      }
      $('project-dialog').close();
      render();
    } catch (failure) {
      $('project-error').textContent = failure.message;
      $('project-error').hidden = false;
    }
  });
  document.querySelectorAll('[data-close-project]').forEach(button => button.addEventListener('click', () => $('project-dialog').close()));

  async function addToProject(id, profile, announce = true) {
    const project = data.projects.find(item => item.id === id);
    if (!project) return;
    if (project.accounts.includes(profile)) { if (announce) toast(`${profile} is already in ${project.name}`); return; }
    try {
      await api(`/api/aws-accounts/projects/${id}/accounts`, {method:'PUT', body:JSON.stringify({profile})});
      project.accounts.push(profile);
      if (announce) { toast(`Added ${profile} to ${project.name}`); render(); }
    } catch (failure) { error(`Could not add to ${project.name}: ${failure.message}`); }
  }
  async function removeFromProject(id, profile) {
    const project = data.projects.find(item => item.id === id);
    try {
      await api(`/api/aws-accounts/projects/${id}/accounts?profile=${encodeURIComponent(profile)}`, {method:'DELETE'});
      project.accounts = project.accounts.filter(value => value !== profile);
      toast(`Removed ${profile} from ${project.name}`);
      render();
    } catch (failure) { error(`Could not remove from ${project.name}: ${failure.message}`); }
  }
  async function deleteProject(id) {
    const project = data.projects.find(item => item.id === id);
    if (!confirm(`Delete project "${project.name}"? Its ${project.accounts.length} account(s) stay in their categories.`)) return;
    try {
      await api(`/api/aws-accounts/projects/${id}`, {method:'DELETE'});
      data.projects = data.projects.filter(item => item.id !== id);
      if (selected === projectKey(project)) { selected = null; store(SELECTED_KEY, null); }
      toast(`Deleted project ${project.name}`);
      render();
    } catch (failure) { error(`Could not delete ${project.name}: ${failure.message}`); }
  }

  function openMenu(anchor, profile) {
    const menu = $('project-menu');
    const projects = data.projects || [];
    menu.innerHTML = `<div class="menu-title">Add ${esc(profile)} to</div>` + projects.map(project => {
      const inside = project.accounts.includes(profile);
      return `<button type="button" role="menuitem" data-add-to="${project.id}" data-profile="${esc(profile)}"${inside ? ' disabled' : ''}>▣ ${esc(project.name)}${inside ? ' <small>added</small>' : ''}</button>`;
    }).join('') + `<button type="button" role="menuitem" class="menu-new" data-add-to="new" data-profile="${esc(profile)}">＋ New project…</button>`;
    menu.hidden = false;
    const box = anchor.getBoundingClientRect();
    menu.style.top = `${Math.min(box.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
    menu.style.left = `${Math.max(8, Math.min(box.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.querySelector('button:not([disabled])')?.focus();
  }
  function closeMenu() { $('project-menu').hidden = true; }
  window.addEventListener('scroll', closeMenu, {passive: true});

  // Drag an account row onto a project (sidebar item or project box), or onto "New".
  const ACCOUNT_TYPE = 'application/x-owl-aws-account';
  const isAccountDrag = event => [...(event.dataTransfer?.types || [])].includes(ACCOUNT_TYPE);
  let dropTarget = null;
  const setDropTarget = element => {
    if (dropTarget === element) return;
    dropTarget?.classList.remove('drop-over');
    dropTarget = element;
    dropTarget?.classList.add('drop-over');
  };
  document.addEventListener('dragstart', event => {
    const source = event.target.closest?.('.row[data-profile]');
    if (!source) return;
    event.dataTransfer.setData(ACCOUNT_TYPE, source.dataset.profile);
    event.dataTransfer.setData('text/plain', source.dataset.profile);
    event.dataTransfer.effectAllowed = 'copy';
    source.classList.add('dragging');
    document.body.classList.add('dragging-account');
    closeMenu();
  });
  document.addEventListener('dragend', event => {
    event.target.closest?.('.row')?.classList.remove('dragging');
    document.body.classList.remove('dragging-account');
    setDropTarget(null);
  });
  document.addEventListener('dragover', event => {
    if (!isAccountDrag(event)) return;
    const target = event.target.closest('[data-drop-project],[data-drop-new]');
    setDropTarget(target);
    if (target) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }
  });
  document.addEventListener('drop', event => {
    if (!isAccountDrag(event)) return;
    const target = event.target.closest('[data-drop-project],[data-drop-new]');
    setDropTarget(null);
    document.body.classList.remove('dragging-account');
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const profile = event.dataTransfer.getData(ACCOUNT_TYPE);
    if (target.dataset.dropNew !== undefined) openProjectDialog(null, profile);
    else addToProject(Number(target.dataset.dropProject), profile);
  }, true);

  function toast(message) {
    $('toast').textContent = message;
    $('toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('show'), 1800);
  }

  document.addEventListener('click', event => {
    if (!event.target.closest('#project-menu,[data-add-menu]')) closeMenu();
    const target = event.target.closest('[data-copy],[data-pin],[data-star],[data-env],[data-select],[data-expand],[data-sort],[data-add-menu],[data-add-to],[data-remove],[data-rename-project],[data-delete-project],#new-project');
    if (!target) return;
    if (target.dataset.expand !== undefined) {
      const name = target.dataset.expand;
      expanded.has(name) ? expanded.delete(name) : expanded.add(name);
      store(EXPANDED_KEY, JSON.stringify([...expanded]));
      return render();
    }
    if (target.id === 'new-project') return openProjectDialog();
    if (target.dataset.sort) {
      sortDir = target.dataset.sort === sortMode ? (sortDir === 'asc' ? 'desc' : 'asc') : SORT_DEFAULT[target.dataset.sort];
      sortMode = target.dataset.sort;
      store(SORT_KEY, sortMode);
      store(SORT_DIR_KEY, sortDir);
      return render();
    }
    if (target.dataset.addMenu !== undefined) return openMenu(target, target.dataset.addMenu);
    if (target.dataset.addTo !== undefined) {
      closeMenu();
      return target.dataset.addTo === 'new' ? openProjectDialog(null, target.dataset.profile) : addToProject(Number(target.dataset.addTo), target.dataset.profile);
    }
    if (target.dataset.remove !== undefined) return removeFromProject(Number(target.dataset.project), target.dataset.remove);
    if (target.dataset.renameProject) return openProjectDialog(data.projects.find(project => project.id === Number(target.dataset.renameProject)));
    if (target.dataset.deleteProject) return deleteProject(Number(target.dataset.deleteProject));
    if (target.dataset.copy !== undefined) return copy(target.dataset.copy, target.dataset.label || 'Value', target.dataset.kind, target.dataset.account);
    if (target.dataset.star !== undefined) return toggleStar(target.dataset.star);
    if (target.dataset.pin !== undefined) {
      const name = target.dataset.pin;
      pinned.has(name) ? pinned.delete(name) : pinned.add(name);
      store(PIN_KEY, JSON.stringify([...pinned]));
      return render();
    }
    if (target.dataset.env !== undefined) {
      envFilter = target.dataset.env;
      document.querySelectorAll('[data-env]').forEach(button => button.setAttribute('aria-pressed', button.dataset.env === envFilter));
      return render();
    }
    selected = target.dataset.select || null;
    store(SELECTED_KEY, selected);
    // An environment under a category shows only that part of it.
    if (target.dataset.selectEnv !== undefined) {
      envFilter = target.dataset.selectEnv;
      if (data) renderHeader();
    }
    render();
    window.scrollTo({top: 0});
  });
  window.addEventListener('scroll', trackScroll, {passive: true});
  window.addEventListener('resize', trackScroll);
  $('search').addEventListener('input', render);
  $('settings-button').addEventListener('click', openConfig);
  $('empty-settings').addEventListener('click', openConfig);
  $('config-form').addEventListener('submit', event => { event.preventDefault(); saveConfig($('config-path').value.trim()); });
  $('config-default').addEventListener('click', () => saveConfig(''));
  document.querySelectorAll('[data-close-config]').forEach(button => button.addEventListener('click', () => $('config-dialog').close()));
  // The file is read again when it changes; look for changes when the page is shown again.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  document.addEventListener('toggle', event => {
    const group = event.target.closest?.('[data-env-group]');
    if (!group) return;
    if (group.open) closedEnvs.delete(group.dataset.envGroup); else closedEnvs.add(group.dataset.envGroup);
    store(CLOSED_ENVS_KEY, JSON.stringify([...closedEnvs]));
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName) && data) { event.preventDefault(); $('search').focus(); }
    if (event.key === 'Escape' && document.activeElement === $('search')) { $('search').value = ''; render(); }
    if (event.key === 'Escape') closeMenu();
  });
  window.owlToast = toast;
  load();
})();
