"use strict";
function bookmarkFolderPath(item, hierarchy) {
  return hierarchy
    ? [hierarchy.space || "Pages", ...(item.breadcrumb || [])]
    : Array.isArray(item.folderPath) && item.folderPath.length
      ? item.folderPath.filter(part => typeof part === "string" && part.trim())
      : [item.domain];
}
function compareBookmarkOrder(a, b, sort) {
  return sort === "title" ? a.title.localeCompare(b.title)
    : sort === "opens" ? b.views - a.views
      : sort === "viewed" ? (b.lastViewed || 0) - (a.lastViewed || 0)
        : b.added - a.added;
}
function bookmarkTreeNumbers(items, hierarchy, sort) {
  const folders = {children: new Map(), pages: []};
  const pageNumbers = new Map(), folderNumbers = new Map();
  const ids = new Set(items.map(item => item.id));
  const ordered = [...items].sort((a, b) => compareBookmarkOrder(a, b, sort));
  const order = new Map(ordered.map((item, index) => [item.id, index]));
  const compare = (a, b) => order.get(a.id) - order.get(b.id);
  // Folder insertion and page ordering mirror the unfiltered bookmark tree.
  for (const item of items) {
    let folder = folders;
    for (const name of bookmarkFolderPath(item, hierarchy[item.id])) {
      if (!folder.children.has(name)) folder.children.set(name, {children: new Map(), pages: []});
      folder = folder.children.get(name);
    }
    folder.pages.push(item);
  }
  function page(item, number, children, visited = new Set()) {
    if (visited.has(item.id)) return;
    pageNumbers.set(item.id, number);
    const next = new Set(visited);
    next.add(item.id);
    (children.get(item.id) || []).forEach((child, index) => page(child, `${number}.${index + 1}`, children, next));
  }
  function folder(node, path, number) {
    folderNumbers.set(JSON.stringify(path), number);
    [...node.children].forEach(([name, child], index) => folder(child, [...path, name], `${number}.${index + 1}`));
    const children = new Map();
    for (const item of [...node.pages].sort(compare)) {
      const parent = hierarchy[item.id]?.parent;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(item);
    }
    node.pages.filter(item => !hierarchy[item.id]?.parent || !ids.has(hierarchy[item.id].parent))
      .sort(compare).forEach((item, index) => page(item, `${number}.${node.children.size + index + 1}`, children));
  }
  [...folders.children].forEach(([name, node], index) => folder(node, [name], String(index + 1)));
  return {pages: pageNumbers, folders: folderNumbers};
}
function bookmarkBranchContext(items, matches, hierarchy) {
  const branches = new Set(matches.map(item => JSON.stringify(bookmarkFolderPath(item, hierarchy[item.id]))));
  return new Set(items.filter(item => {
    const path = bookmarkFolderPath(item, hierarchy[item.id]);
    return path.some((_, index) => branches.has(JSON.stringify(path.slice(0, index + 1))));
  }).map(item => item.id));
}
function extendBookmarkTreeNumbers(numbers, items, hierarchy) {
  const next = new Map();
  for (const number of [...numbers.pages.values(), ...numbers.folders.values()]) {
    const parts = number.split('.'), last = Number(parts.pop()), parent = parts.join('.');
    next.set(parent, Math.max(next.get(parent) || 0, last));
  }
  function allocate(parent) {
    const value = (next.get(parent) || 0) + 1;
    next.set(parent, value);
    return parent ? `${parent}.${value}` : String(value);
  }
  for (const item of items) {
    const path = bookmarkFolderPath(item, hierarchy[item.id]);
    let parent = '';
    path.forEach((_, index) => {
      const key = JSON.stringify(path.slice(0, index + 1));
      if (!numbers.folders.has(key)) numbers.folders.set(key, allocate(parent));
      parent = numbers.folders.get(key);
    });
    if (!numbers.pages.has(item.id)) numbers.pages.set(item.id, allocate(parent));
  }
}
if (typeof module !== "undefined") module.exports = {bookmarkTreeNumbers, bookmarkFolderPath, compareBookmarkOrder, bookmarkBranchContext, extendBookmarkTreeNumbers};
