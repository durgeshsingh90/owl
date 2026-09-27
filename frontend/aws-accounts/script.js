"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const displayDate = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—';
  const PIN_KEY = 'owl-aws-accounts-pinned', SELECTED_KEY = 'owl-aws-accounts-category';
  // Trailing environment token, optionally followed by an account-number suffix (mc-x-nonp-216989139306).
  const ENV_PATTERN = /-(prod|production|prd|nonp|nonprod|nonprd|work|mtf|dev|develop|development|test|testing|qa|uat|sit|stage|staging|stg|preprod|pre|perf|sandbox|sbx|demo|poc|lab|dr)(?:-\d+)?$/i;
  const ENV_CLASS = {prod:'prod',production:'prod',prd:'prod',nonp:'nonp',nonprod:'nonp',nonprd:'nonp',work:'work'};
  const ENV_FIRST = ['prod','production','prd','nonp','nonprod','nonprd','work'];
  // Known environments first, then any other environment alphabetically, then accounts without one.
  const envRank = env => env === '' ? 1e6 : ENV_FIRST.includes(env) ? ENV_FIRST.indexOf(env) : 100;
  const envSort = (a, b) => envRank(a) - envRank(b) || a.localeCompare(b);
  let data = null, envFilter = 'all', pinned = new Set(), selected = null, toastTimer = 0;
  try {
    pinned = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]'));
    selected = localStorage.getItem(SELECTED_KEY);
  } catch { /* Pins and the selected category stay for this page only. */ }
  const store = (key, value) => { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* Per-page only. */ } };
  const label = name => name.replace(/_/g, ' ');
  // Categories this small sit side by side as boxes; larger ones get a full-width section.
  const COMPACT = 45;
  const STARRED = '__starred__';
  const starredSet = () => new Set(data?.stars || []);

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

  function environment(profile) {
    const match = ENV_PATTERN.exec(profile);
    if (!match) return {base: profile, env: '', kind: 'none'};
    const env = match[1].toLowerCase();
    return {base: profile.slice(0, match.index), env, kind: ENV_CLASS[env] || 'other'};
  }

  // Group sibling accounts (x-prod, x-nonp, x-work) together, keeping the file's order of first appearance.
  function groups(accounts) {
    const map = new Map();
    for (const account of accounts) {
      const info = environment(account.profile);
      if (!map.has(info.base)) map.set(info.base, []);
      map.get(info.base).push({...account, ...info});
    }
    return [...map.values()].flatMap(items => items
      .sort((a, b) => envSort(a.env, b.env))
      .map((item, index) => ({...item, last: index === items.length - 1})));
  }

  const copyCount = (kind, value) => data?.copies?.[kind]?.[value] || 0;
  // One counter per account: copying the name or the ID both count. Older ID-only counts are added in.
  const accountCount = (profile, id) => copyCount('profile', profile) + copyCount('account_id', String(id));
  const accountBadge = (profile, id) => {
    const count = accountCount(profile, id);
    return `<span class="copies" data-count-account="${esc(profile)}" data-count-id="${esc(id)}"${count ? '' : ' hidden'} title="Copied ${count} time${count === 1 ? '' : 's'}">⧉ ${count}</span>`;
  };
  const badge = (kind, value) => {
    const count = copyCount(kind, value);
    return `<span class="copies" data-count-kind="${kind}" data-count-value="${esc(value)}"${count ? '' : ' hidden'} title="Copied ${count} time${count === 1 ? '' : 's'}">⧉ ${count}</span>`;
  };

  function highlight(text, query) {
    if (!query) return esc(text);
    const index = text.toLowerCase().indexOf(query);
    if (index < 0) return esc(text);
    return esc(text.slice(0, index)) + '<mark>' + esc(text.slice(index, index + query.length)) + '</mark>' + esc(text.slice(index + query.length));
  }

  function orderedCategories() {
    const names = Object.keys(data.categories);
    return [...names.filter(name => pinned.has(name)), ...names.filter(name => !pinned.has(name))];
  }

  function renderHeader() {
    const all = Object.values(data.categories).flat();
    const count = all.length;
    $('total').textContent = count.toLocaleString();
    const categories = Object.keys(data.categories).length;
    const mismatch = Number.isInteger(data.total_accounts) && data.total_accounts !== count ? ` · file reports ${data.total_accounts.toLocaleString()}` : '';
    $('summary').textContent = `${categories.toLocaleString()} categories · generated ${displayDate(data.generated_at)} · imported ${displayDate(data.imported_at)}${mismatch}`;
    $('file-note').textContent = `${count.toLocaleString()} accounts · imported ${displayDate(data.imported_at)}`;
    const roles = Object.entries(data.common_roles || {});
    $('roles').innerHTML = roles.length ? '<span class="eyebrow">COMMON ROLES</span>' + roles.map(([name, value]) =>
      `<button class="role" type="button" data-copy="${esc(value)}" data-kind="role" data-label="${esc(label(name))}" title="Copy ${esc(label(name))}"><span class="role-name">${esc(label(name))}</span><code>${esc(value)}</code>${badge('role', value)}<span class="copy-icon" aria-hidden="true">⧉</span></button>`).join('') : '';
    $('roles').hidden = !roles.length;
    const counts = new Map();
    for (const account of all) {
      const env = environment(account.profile).env;
      counts.set(env, (counts.get(env) || 0) + 1);
    }
    if (envFilter !== 'all' && !counts.has(envFilter)) envFilter = 'all';
    const chip = (key, text, count, kind) => `<button type="button" data-env="${esc(key)}" class="${kind}" aria-pressed="${key === envFilter}">${esc(text)}<small>${count}</small></button>`;
    $('env-filter').innerHTML = chip('all', 'All', count, '') +
      [...counts.keys()].sort(envSort).map(env => chip(env, env || 'no env', counts.get(env), env ? ENV_CLASS[env] || 'other' : 'none')).join('');
  }

  function renderFrequent() {
    const ids = new Map(Object.values(data.categories).flat().map(account => [account.profile, String(account.account_id)]));
    const top = [...ids].map(([profile, id]) => [profile, accountCount(profile, id)]).filter(([, count]) => count)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 10);
    $('frequent').hidden = !top.length;
    $('frequent-list').innerHTML = top.map(([profile]) =>
      `<div class="chip"><button class="name" type="button" data-copy="${esc(profile)}" data-account="${esc(profile)}" data-label="Account name" title="Copy account name"><span class="text">${esc(profile)}</span></button>` +
      `<button class="id" type="button" data-copy="${esc(ids.get(profile))}" data-account="${esc(profile)}" data-label="Account ID" title="Copy account ID"><span class="text">${esc(ids.get(profile))}</span></button>${accountBadge(profile, ids.get(profile))}</div>`).join('');
  }

  const row = (item, query, next, stars) => `<div class="row${!next || next.base !== item.base ? ' group-end' : ''}">
    <button class="star-account" type="button" data-star="${esc(item.profile)}" aria-pressed="${stars.has(item.profile)}" title="${stars.has(item.profile) ? 'Unstar' : 'Star'} ${esc(item.profile)}">${stars.has(item.profile) ? '★' : '☆'}</button>
    <button class="name" type="button" data-copy="${esc(item.profile)}" data-account="${esc(item.profile)}" data-label="Account name" title="Copy account name · ${esc(item.profile)}"><span class="text">${highlight(item.profile, query)}</span></button>
    <span class="env ${ENV_CLASS[item.env] || 'other'}">${esc(item.env)}</span>
    <button class="id" type="button" data-copy="${esc(item.account_id)}" data-account="${esc(item.profile)}" data-label="Account ID" title="Copy account ID"><span class="text">${highlight(String(item.account_id), query)}</span></button>
    <span class="count-cell">${accountBadge(item.profile, item.account_id)}</span>
  </div>`;

  function render() {
    const query = $('search').value.trim().toLowerCase();
    const stars = starredSet();
    if (selected && !(selected in data.categories) && !(selected === STARRED && stars.size)) selected = null;
    const keep = item => (envFilter === 'all' || item.env === envFilter) &&
      (!query || item.profile.toLowerCase().includes(query) || String(item.account_id).toLowerCase().includes(query));
    const matches = new Map();
    if (stars.size) {
      // Starred accounts, once each, even when a profile appears in several categories.
      const seen = new Set();
      const starred = Object.values(data.categories).flat().filter(account =>
        stars.has(account.profile) && !seen.has(account.profile) && seen.add(account.profile));
      matches.set(STARRED, groups(starred).filter(keep));
    }
    for (const name of orderedCategories()) matches.set(name, groups(data.categories[name]).filter(keep));
    const everything = [...matches].filter(([name]) => name !== STARRED).reduce((sum, [, items]) => sum + items.length, 0);
    const navItem = (name, count, text, isPinned) => `<button type="button" data-select="${esc(name)}" aria-current="${(name || null) === selected}" class="${isPinned ? 'pinned' : ''}${count ? '' : ' empty'}">
      <span class="nav-name">${isPinned ? '<span class="star">★</span>' : ''}${esc(text)}</span><span class="nav-count">${count}</span></button>`;
    lastInView = null;
    $('category-nav').innerHTML = navItem('', everything, 'All accounts', false) +
      [...matches].map(([name, items]) => name === STARRED
        ? navItem(name, items.length, 'Starred accounts', true).replace('class="pinned', 'class="starred-nav pinned')
        : navItem(name, items.length, label(name), pinned.has(name))).join('');
    const sections = [];
    let shown = 0;
    for (const [name, items] of matches) {
      if (selected && name !== selected) continue;
      if (!items.length) continue;
      if (name !== STARRED || selected === STARRED) shown += items.length;
      const isStarred = name === STARRED;
      const isPinned = isStarred || pinned.has(name);
      const compact = !selected && items.length <= COMPACT;
      sections.push({compact, html: `<section class="category${isPinned ? ' pinned' : ''}${isStarred ? ' starred' : ''}${compact ? ' compact' : ''}" data-category="${esc(name)}">
        <header><h2>${isStarred ? '★ Starred accounts' : esc(label(name))}</h2><span class="count">${items.length} account${items.length === 1 ? '' : 's'}</span>
          ${isStarred ? '' : `<button class="pin" type="button" data-pin="${esc(name)}" aria-pressed="${isPinned}" title="${isPinned ? 'Unpin' : 'Pin'} category ${esc(label(name))} to the top">${isPinned ? '★' : '☆'}</button>`}</header>
        <div class="rows">${items.map((item, index) => row(item, query, items[index + 1], stars)).join('')}</div>
      </section>`});
    }
    // Consecutive small categories share one row of boxes.
    const html = [];
    for (let index = 0; index < sections.length;) {
      if (!sections[index].compact) { html.push(sections[index++].html); continue; }
      const boxes = [];
      while (index < sections.length && sections[index].compact) boxes.push(sections[index++].html);
      html.push(`<div class="compact-grid">${boxes.join('')}</div>`);
    }
    $('title').firstChild.textContent = selected === STARRED ? 'Starred accounts ' : selected ? `${label(selected)} ` : 'AWS accounts ';
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
    document.querySelectorAll('#category-nav [data-select]').forEach(button => {
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
    $('delete-button').hidden = !data;
    if (!data) return;
    renderHeader();
    renderFrequent();
    render();
  }

  async function load() {
    try {
      const result = await api('/api/aws-accounts');
      data = result.imported ? result : null;
      error('');
    } catch (failure) {
      error(`Could not load saved accounts: ${failure.message}`);
      data = null;
    }
    show();
  }

  async function importFile(file) {
    if (!file) return;
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      error(`${file.name} is not valid JSON.`);
      return;
    }
    if (!parsed || typeof parsed.categories !== 'object' || Array.isArray(parsed.categories)) {
      error(`${file.name} has no "categories" object. Expected {"categories": {"Name": [{"profile": "…", "account_id": "…"}]}}.`);
      return;
    }
    try {
      const result = await api('/api/aws-accounts', {method:'PUT', body:JSON.stringify(parsed)});
      error('');
      toast(`Imported ${result.accounts.toLocaleString()} accounts from ${file.name}`);
      await load();
    } catch (failure) {
      error(`Could not import ${file.name}:\n${failure.message}`);
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
        element.textContent = `⧉ ${total}`;
        element.title = `Copied ${total} time${total === 1 ? '' : 's'}`;
        element.hidden = false;
      };
      if (account) {
        document.querySelectorAll('[data-count-account]').forEach(element => {
          if (element.dataset.countAccount === account) show(element, accountCount(account, element.dataset.countId));
        });
        renderFrequent();
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

  function toast(message) {
    $('toast').textContent = message;
    $('toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('show'), 1800);
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('[data-copy],[data-pin],[data-star],[data-env],[data-select]');
    if (!target) return;
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
    render();
    window.scrollTo({top: 0});
  });
  // Delete all is locked until the exact phrase is typed; the backend checks it again.
  const PHRASE = 'delete all';
  const unlocked = () => $('delete-phrase').value.trim().toLowerCase() === PHRASE;
  function lockState() {
    $('delete-confirm').disabled = !unlocked();
    $('delete-confirm').textContent = unlocked() ? '🔓 Delete all' : '🔒 Delete all';
  }
  $('delete-button').addEventListener('click', () => {
    $('delete-count').textContent = Object.values(data.categories).flat().length.toLocaleString();
    $('delete-phrase').value = '';
    $('delete-error').hidden = true;
    lockState();
    $('delete-dialog').showModal();
    $('delete-phrase').focus();
  });
  $('delete-phrase').addEventListener('input', lockState);
  document.querySelectorAll('[data-close-delete]').forEach(button => button.addEventListener('click', () => $('delete-dialog').close()));
  $('delete-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!unlocked()) return;
    $('delete-confirm').disabled = true;
    try {
      await api('/api/aws-accounts', {method:'DELETE', body:JSON.stringify({confirmation:PHRASE})});
      pinned.clear(); selected = null; store(PIN_KEY, null); store(SELECTED_KEY, null);
      $('delete-dialog').close();
      $('search').value = '';
      toast('All AWS Accounts data deleted');
      await load();
      window.owlRefreshConnection?.();
    } catch (failure) {
      $('delete-error').textContent = failure.message;
      $('delete-error').hidden = false;
      lockState();
    }
  });
  window.addEventListener('scroll', trackScroll, {passive: true});
  window.addEventListener('resize', trackScroll);
  $('search').addEventListener('input', render);
  $('import-button').addEventListener('click', () => $('file-input').click());
  $('empty-import').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', event => { importFile(event.target.files[0]); event.target.value = ''; });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName) && data) { event.preventDefault(); $('search').focus(); }
    if (event.key === 'Escape' && document.activeElement === $('search')) { $('search').value = ''; render(); }
  });
  let dragDepth = 0;
  const hasFile = event => [...(event.dataTransfer?.types || [])].includes('Files');
  document.addEventListener('dragenter', event => { if (hasFile(event)) { dragDepth += 1; $('drop-overlay').hidden = false; } });
  document.addEventListener('dragleave', event => { if (hasFile(event) && --dragDepth <= 0) { dragDepth = 0; $('drop-overlay').hidden = true; } });
  document.addEventListener('dragover', event => { if (hasFile(event)) event.preventDefault(); });
  document.addEventListener('drop', event => {
    if (!hasFile(event)) return;
    event.preventDefault(); dragDepth = 0; $('drop-overlay').hidden = true;
    importFile(event.dataTransfer.files[0]);
  });
  window.owlToast = toast;
  load();
})();
