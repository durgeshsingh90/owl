"use strict";
(() => {
  const input = document.querySelector('#search-input');
  const list = document.createElement('div');
  list.id = 'recent-searches';
  list.className = 'recent-searches';
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'Recent searches');
  list.hidden = true;
  input.parentElement.append(list);
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', list.id);
  input.setAttribute('aria-expanded', 'false');
  const key = 'owl-bitbucket-recent-searches';
  let history = [], matches = [], active = -1;
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    if (Array.isArray(saved)) history = saved.filter(x => typeof x === 'string' && x.trim()).slice(0, 8);
  } catch {}
  function remember() {
    const value = input.value.trim();
    if (!value) return;
    history = [value, ...history.filter(x => x.toLowerCase() !== value.toLowerCase())].slice(0, 8);
    try { localStorage.setItem(key, JSON.stringify(history)); } catch {}
  }
  function close() {
    list.hidden = true;
    active = -1;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  function open() {
    if (!document.querySelector('#advanced-search-panel').hidden) return;
    const query = input.value.trim().toLowerCase();
    matches = history.filter(x => x.toLowerCase().includes(query));
    active = -1;
    input.removeAttribute('aria-activedescendant');
    list.replaceChildren();
    matches.forEach((value, index) => {
      const option = document.createElement('div');
      option.id = `recent-search-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      option.textContent = value;
      option.addEventListener('mousedown', event => event.preventDefault());
      option.addEventListener('click', () => select(index));
      list.append(option);
    });
    list.hidden = !matches.length;
    input.setAttribute('aria-expanded', String(matches.length > 0));
  }
  function select(index) {
    input.value = matches[index];
    input.dispatchEvent(new Event('input', {bubbles: true}));
    remember();
    close();
    input.focus();
  }
  input.addEventListener('focus', open);
  input.addEventListener('click', open);
  input.addEventListener('input', open);
  input.addEventListener('blur', () => { remember(); close(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') { close(); return; }
    if (event.key === 'Enter') {
      if (!list.hidden && active >= 0) { event.preventDefault(); select(active); }
      else { remember(); close(); }
      return;
    }
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    if (list.hidden) open();
    if (list.hidden) return;
    event.preventDefault();
    active = active < 0 ? (event.key === 'ArrowDown' ? 0 : matches.length - 1)
      : (active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
    [...list.children].forEach((option, index) => option.setAttribute('aria-selected', String(index === active)));
    input.setAttribute('aria-activedescendant', list.children[active].id);
    list.children[active].scrollIntoView({block: 'nearest'});
  });
  document.querySelector('#advanced-search-toggle').addEventListener('click', close);
})();
