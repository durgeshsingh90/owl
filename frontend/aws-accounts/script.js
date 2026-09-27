"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const displayDate = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—';
  const PIN_KEY = 'owl-aws-accounts-pinned';
  // Trailing environment token, optionally followed by an account-number suffix (mc-x-nonp-216989139306).
  const ENV_PATTERN = /-(prod|production|nonp|nonprod|work|mtf|dev|test|qa|uat|stage|staging|preprod|sandbox)(?:-\d+)?$/i;
  const ENV_CLASS = {prod:'prod',production:'prod',nonp:'nonp',nonprod:'nonp',work:'work'};
  const ENV_ORDER = ['prod','nonp','work','other'];
  const ENV_LABEL = {all:'All',prod:'prod',nonp:'nonp',work:'work',other:'other'};
  let data = null, envFilter = 'all', pinned = new Set(), toastTimer = 0;
  try { pinned = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]')); } catch { /* Pins stay for this page only. */ }

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
    if (!match) return {base: profile, env: '', kind: 'other'};
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
    return [...map.values()].map(items => items.sort((a, b) => ENV_ORDER.indexOf(a.kind) - ENV_ORDER.indexOf(b.kind)));
  }

  const copyCount = (kind, value) => data?.copies?.[kind]?.[value] || 0;
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

  const slug = name => 'category-' + encodeURIComponent(name).replace(/%/g, '_');
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
    $('roles').innerHTML = roles.length ? '<span class="eyebrow">COMMON ROLES · CLICK TO COPY</span><div class="role-list">' + roles.map(([name, value]) =>
      `<button class="role" type="button" data-copy="${esc(value)}" data-kind="role" data-label="${esc(name.replace(/_/g, ' '))}" title="Copy ${esc(name.replace(/_/g, ' '))}"><span>${esc(name.replace(/_/g, ' '))} ${badge('role', value)}</span><code>${esc(value)}</code></button>`).join('') + '</div>' : '';
    $('roles').hidden = !roles.length;
    const counts = {all: count, prod: 0, nonp: 0, work: 0, other: 0};
    for (const account of all) counts[environment(account.profile).kind] += 1;
    $('env-filter').innerHTML = ['all', ...ENV_ORDER].filter(key => key === 'all' || counts[key]).map(key =>
      `<button type="button" data-env="${key}" aria-pressed="${key === envFilter}">${ENV_LABEL[key]}<small>${counts[key]}</small></button>`).join('');
  }

  function renderFrequent() {
    const ids = new Map(Object.values(data.categories).flat().map(account => [account.profile, String(account.account_id)]));
    const top = Object.entries(data.copies?.profile || {}).filter(([profile]) => ids.has(profile))
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 10);
    $('frequent').hidden = !top.length;
    $('frequent-list').innerHTML = top.map(([profile]) =>
      `<div class="chip"><button class="profile" type="button" data-copy="${esc(profile)}" data-kind="profile" data-label="Account name" title="Copy account name">${esc(profile)} ${badge('profile', profile)}</button>` +
      `<button class="account-id" type="button" data-copy="${esc(ids.get(profile))}" data-kind="account_id" data-label="Account ID" title="Copy account ID">${esc(ids.get(profile))}</button></div>`).join('');
  }

  function render() {
    const query = $('search').value.trim().toLowerCase();
    const nav = [], cards = [];
    let shown = 0;
    for (const name of orderedCategories()) {
      const sets = groups(data.categories[name]).map(items => items.filter(item =>
        (envFilter === 'all' || item.kind === envFilter) &&
        (!query || item.profile.toLowerCase().includes(query) || String(item.account_id).toLowerCase().includes(query))
      )).filter(items => items.length);
      const total = sets.reduce((sum, items) => sum + items.length, 0);
      if (!total) continue;
      shown += total;
      const isPinned = pinned.has(name);
      nav.push(`<a href="#${slug(name)}" class="${isPinned ? 'pinned' : ''}">${isPinned ? '★ ' : ''}${esc(name)}<small>${total}</small></a>`);
      cards.push(`<article class="category${isPinned ? ' pinned' : ''}" id="${slug(name)}">
        <header><h2>${esc(name)}</h2><span class="count">${total} account${total === 1 ? '' : 's'}</span>
          <button class="pin" type="button" data-pin="${esc(name)}" aria-pressed="${isPinned}" title="${isPinned ? 'Unstar' : 'Star'} ${esc(name)}">${isPinned ? '★' : '☆'}</button></header>
        ${sets.map(items => `<div class="group">${items.map(item => `<div class="account">
          <button class="profile" type="button" data-copy="${esc(item.profile)}" data-kind="profile" data-label="Account name" title="Copy account name">${highlight(item.profile, query)} ${badge('profile', item.profile)}</button>
          ${item.env ? `<span class="env ${item.kind}">${esc(item.env)}</span>` : ''}
          <button class="account-id" type="button" data-copy="${esc(item.account_id)}" data-kind="account_id" data-label="Account ID" title="Copy account ID">${highlight(String(item.account_id), query)} ${badge('account_id', String(item.account_id))}</button>
        </div>`).join('')}</div>`).join('')}
      </article>`);
    }
    $('category-nav').innerHTML = nav.join('');
    $('categories').innerHTML = cards.join('');
    $('no-results').hidden = shown > 0;
  }

  function show() {
    $('empty-state').hidden = Boolean(data);
    $('content').hidden = !data;
    $('file-note').textContent = '';
    $('export-button').hidden = !data;
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

  async function copy(text, label, kind) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement('textarea'), {value: text});
      document.body.append(area); area.select(); document.execCommand('copy'); area.remove();
    }
    toast(`${label} copied: ${text}`);
    if (!kind) return;
    try {
      const {count} = await api('/api/aws-accounts/copies', {method:'POST', body:JSON.stringify({kind, value:text})});
      ((data.copies ||= {})[kind] ||= {})[text] = count;
      document.querySelectorAll('[data-count-kind]').forEach(element => {
        if (element.dataset.countKind !== kind || element.dataset.countValue !== text) return;
        element.textContent = `⧉ ${count}`;
        element.title = `Copied ${count} time${count === 1 ? '' : 's'}`;
        element.hidden = false;
      });
      if (kind === 'profile') renderFrequent();
    } catch { /* The copy still worked; only the counter failed to save. */ }
  }

  function toast(message) {
    $('toast').textContent = message;
    $('toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('show'), 1800);
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('[data-copy],[data-pin],[data-env],.category-nav a');
    if (!target) return;
    if (target.dataset.copy !== undefined) return copy(target.dataset.copy, target.dataset.label || 'Value', target.dataset.kind);
    if (target.dataset.pin !== undefined) {
      const name = target.dataset.pin;
      pinned.has(name) ? pinned.delete(name) : pinned.add(name);
      try { localStorage.setItem(PIN_KEY, JSON.stringify([...pinned])); } catch { /* Keep pins for this page. */ }
      return render();
    }
    if (target.dataset.env) {
      envFilter = target.dataset.env;
      document.querySelectorAll('[data-env]').forEach(button => button.setAttribute('aria-pressed', button.dataset.env === envFilter));
      return render();
    }
    const card = document.getElementById(target.hash.slice(1));
    if (card) {
      event.preventDefault();
      card.scrollIntoView({behavior:'smooth', block:'start'});
      card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');
    }
  });
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
