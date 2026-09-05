"use strict";

const day = 86400000,
  now = Date.now();
let saved = {};
try {
  saved = JSON.parse(localStorage.getItem("owl-bookmark-demo") || "{}");
} catch {}
const bookmarks = bookmarkSeed.map(
  ([title, url, description, views, last], index) => ({
    id: index + 1,
    title,
    url,
    description,
    domain: new URL(url).hostname,
    views,
    lastViewed: last === null ? null : now - last * day,
    added: now - (index + 1) * day,
    updatedInOwlAt:
      index === 1 ? now - day : index === 4 ? now - 3 * day : null,
    favorite: [0, 2, 6].includes(index),
    pinned: [0, 1].includes(index),
    ...(saved[index + 1] || {}),
  }),
);
// Explicit illustrative page attribution; never inferred from domain membership.
const confluenceAttribution = {
  1: ["Sarah Wilson", "John Smith"],
  2: ["John Smith", "Sarah Wilson"],
  3: ["Priya Patel", "Priya Patel"],
  4: ["Michael Chen", "John Smith"],
  5: ["Sarah Wilson", "Priya Patel"],
  6: ["Emma Brown", "Emma Brown"],
  12: ["John Smith", "Michael Chen"],
};
bookmarks.forEach((item) => {
  const attribution = confluenceAttribution[item.id];
  if (attribution) {
    item.author = attribution[0];
    item.lastEditor = attribution[1];
    // Illustrative Confluence dates, distinct from the OWL bookmark timestamps.
    item.writtenAt = Date.UTC(2026, 6, 1 + item.id);
    item.confluenceUpdatedAt = Date.UTC(2026, 7, 18 + item.id);
  }
});
let deletedBookmarkIds = new Set();
try {
  deletedBookmarkIds = new Set(
    JSON.parse(localStorage.getItem("owl-bookmark-deleted") || "[]"),
  );
} catch {}
const selectedBookmarks = new Set();
let visibleBookmarkIds = [];
function nextBookmarkId() {
  return (
    Math.max(0, ...bookmarks.map((item) => item.id), ...deletedBookmarkIds) + 1
  );
}
function updateBookmarkSelection() {
  const count = selectedBookmarks.size;
  document.querySelector("#bookmark-selection-count").textContent =
    `${count} selected`;
  const all = document.querySelector("#select-all-bookmarks");
  all.disabled = !visibleBookmarkIds.length;
  all.checked =
    visibleBookmarkIds.length > 0 && count === visibleBookmarkIds.length;
  all.indeterminate = count > 0 && count < visibleBookmarkIds.length;
  document.querySelector("#open-selected-bookmarks").disabled = !count;
  document.querySelector("#delete-selected-bookmarks").disabled = !count;
}
let view = "all",
  domain = "",
  query = "",
  selectedPerson = "",
  personRole = "any",
  peopleQuery = "";
function matchesPerson(item) {
  return (
    !selectedPerson ||
    (personRole !== "updated" && item.author === selectedPerson) ||
    (personRole !== "written" && item.lastEditor === selectedPerson)
  );
}
function renderConfluencePeople(scoped) {
  const counts = new Map();
  for (const item of scoped) {
    if (item.author) {
      const person = counts.get(item.author) || { written: 0, updated: 0 };
      person.written++;
      counts.set(item.author, person);
    }
    if (item.lastEditor) {
      const person = counts.get(item.lastEditor) || { written: 0, updated: 0 };
      person.updated++;
      counts.set(item.lastEditor, person);
    }
  }
  const people = [...counts]
    .filter(([name]) => name.toLowerCase().includes(peopleQuery))
    .sort(([a], [b]) => a.localeCompare(b));
  document.querySelector("#confluence-people-count").textContent =
    people.length;
  document
    .querySelector("#all-confluence-people")
    .setAttribute("aria-pressed", String(!selectedPerson));
  document.querySelector("#confluence-people").innerHTML = people
    .map(
      ([name, counts]) =>
        `<article class="confluence-person ${selectedPerson === name ? "active" : ""}" data-person="${esc(name)}" data-role="any"><button class="person-name" data-person="${esc(name)}" data-role="any" aria-pressed="${selectedPerson === name && personRole === "any"}"><span class="person-initials">${esc(
          name
            .split(" ")
            .map((part) => part[0])
            .join(""),
        )}</span><strong>${esc(name)}</strong></button><div class="person-page-counts"><button data-person="${esc(name)}" data-role="written" aria-pressed="${selectedPerson === name && personRole === "written"}" title="Filter pages written by ${esc(name)}">${counts.written} written</button><button data-person="${esc(name)}" data-role="updated" aria-pressed="${selectedPerson === name && personRole === "updated"}" title="Filter pages whose latest editor is ${esc(name)}">${counts.updated} updated</button></div></article>`,
    )
    .join("");
  document.querySelector("#confluence-people-empty").hidden = people.length > 0;
}

const views = [
  ["all", "All bookmarks", "▤"],
  ["favorite", "Favourites", "☆"],
  ["pinned", "Pinned", "♧"],
  ["recent", "Recent", "◷"],
  ["frequent", "Frequently viewed", "↗"],
  ["never", "Never viewed", "○"],
];
const esc = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
function matches(item, key) {
  return (
    key === "all" ||
    (key === "favorite" && item.favorite) ||
    (key === "pinned" && item.pinned) ||
    (key === "recent" &&
      item.lastViewed &&
      item.lastViewed >= Date.now() - 7 * day) ||
    (key === "frequent" && item.views >= 5) ||
    (key === "never" && item.views === 0)
  );
}
function persist() {
  try {
    localStorage.setItem(
      "owl-bookmark-overview",
      JSON.stringify({ at: Date.now(), bookmarks }),
    );
  } catch {}
  try {
    localStorage.setItem(
      "owl-bookmark-added",
      JSON.stringify(bookmarks.filter((item) => item.custom)),
    );
  } catch {}
  const data = {};
  bookmarks.forEach(
    (item) =>
      (data[item.id] = {
        favorite: item.favorite,
        pinned: item.pinned,
        views: item.views,
        lastViewed: item.lastViewed,
        added: item.added,
        updatedInOwlAt: item.updatedInOwlAt ?? null,
      }),
  );
  try {
    localStorage.setItem("owl-bookmark-demo", JSON.stringify(data));
  } catch {}
}
function date(value) {
  return value
    ? new Date(value).toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      })
    : "Never";
}
// These timestamps describe OWL records, independently of Confluence edits.
function owlCalendarDay(value) {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return NaN;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Dublin",
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).formatToParts(parsed);
  const part = (type) => Number(parts.find((item) => item.type === type).value);
  return Date.UTC(part("year"), part("month") - 1, part("day"));
}
function confluenceAge(value, asOf = Date.now()) {
  if (value === null || value === undefined) return "date unavailable";
  const then = owlCalendarDay(value),
    today = owlCalendarDay(asOf);
  if (!Number.isFinite(then) || then > today) return "date unavailable";
  const days = Math.floor((today - then) / day);
  return days === 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`;
}
function bookmarkAgeTag(value, kind, asOf = Date.now()) {
  if (value === null || value === undefined) return "";
  const created = owlCalendarDay(value),
    today = owlCalendarDay(asOf);
  if (!Number.isFinite(created) || created > today) return "";
  const start = new Date(created),
    expiry = new Date(
      Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1),
    );
  const monthEnd = new Date(
    Date.UTC(expiry.getUTCFullYear(), expiry.getUTCMonth() + 1, 0),
  ).getUTCDate();
  expiry.setUTCDate(Math.min(start.getUTCDate(), monthEnd));
  if (today > expiry.getTime()) return "";
  const days = Math.floor((today - created) / day);
  const age =
    today === expiry.getTime()
      ? "1 month ago"
      : days === 0
        ? "today"
        : days === 1
          ? "1 day ago"
          : `${days} days ago`;
  const label = kind === "updated" ? "Updated" : "New";
  return `<span class="bookmark-age-tag ${kind}" title="${label === "New" ? "Added to" : "Updated in"} OWL on ${esc(date(value))}">${label} · ${age}</span>`;
}
// Sample hierarchy is explicit. Live pages must supply their actual parent IDs.
const pageHierarchy = {
  1: { space: "Engineering", parent: null },
  2: { space: "Engineering", parent: 1 },
  5: { space: "Engineering", parent: 2 },
  12: { space: "Engineering", parent: 2 },
  3: { space: "Operations", parent: null },
  4: { space: "Operations", parent: 3 },
  6: { space: "Product", parent: null },
};
const collapsedBranches = new Set();
let treeFilterKey = "";
function renderBookmarkTree(filtered) {
  const key = JSON.stringify([
    view,
    domain,
    selectedDomainGroup,
    query,
    selectedPerson,
    personRole,
  ]);
  if (key !== treeFilterKey) {
    collapsedBranches.clear();
    treeFilterKey = key;
  }
  const matching = new Set(filtered.map((item) => item.id)),
    included = new Set(matching);
  for (const item of filtered) {
    let parent = pageHierarchy[item.id]?.parent;
    const seen = new Set();
    while (
      parent &&
      !seen.has(parent) &&
      bookmarks.some((item) => item.id === parent)
    ) {
      seen.add(parent);
      included.add(parent);
      parent = pageHierarchy[parent]?.parent;
    }
  }
  const groups = new Map();
  for (const item of bookmarks) {
    if (!included.has(item.id)) continue;
    const hierarchy = pageHierarchy[item.id];
    const path = hierarchy
      ? ["Confluence", hierarchy.space]
      : Array.isArray(item.folderPath) && item.folderPath.length
        ? item.folderPath.filter(
            (part) => typeof part === "string" && part.trim(),
          )
        : [item.domain];
    const group = JSON.stringify(path);
    const list = groups.get(group) || [];
    list.push(item);
    groups.set(group, list);
  }
  const order = new Map(filtered.map((item, index) => [item.id, index]));
  function entry(item, number, list, visited = new Set()) {
    if (visited.has(item.id)) return "";
    const next = new Set(visited);
    next.add(item.id);
    const children = list
      .filter((child) => pageHierarchy[child.id]?.parent === item.id)
      .sort((a, b) => (order.get(a.id) ?? -1) - (order.get(b.id) ?? -1));
    const context = !matching.has(item.id),
      branch = "page-" + item.id;
    const title = `<span class="tree-row-heading"><span class="tree-name-group"><input class="bookmark-checkbox" type="checkbox" data-select-bookmark="${item.id}" aria-label="Select ${esc(item.title)}" ${selectedBookmarks.has(item.id) ? "checked" : ""} ${context ? "disabled" : ""}><span class="tree-number">${number}</span><span class="tree-page-icon" aria-hidden="true">▤</span><a class="tree-title tree-select" data-open="${item.id}" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>${context ? '<span class="tree-context">Parent context</span>' : ""}</span><span class="tree-heading-meta"><span class="bookmark-open-count">${item.views} ${item.views === 1 ? "open" : "opens"}</span>${bookmarkAgeTag(item.added, "new")}${bookmarkAgeTag(item.updatedInOwlAt, "updated")}</span></span>`;
    const metadata = `<div class="tree-page-content" tabindex="0" aria-label="Show details for ${esc(item.title)}"><div class="bookmark-actions"><button class="bookmark-action" data-favorite="${item.id}" aria-label="${item.favorite ? "Unfavourite" : "Favourite"} ${esc(item.title)}" aria-pressed="${item.favorite}" title="${item.favorite ? "Remove from favourites" : "Add to favourites"}"><svg class="bookmark-star" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3L3 9.6l6.2-.9L12 3Z"/></svg></button><button class="bookmark-action" data-pin="${item.id}" aria-label="${item.pinned ? "Unpin" : "Pin"} ${esc(item.title)}" aria-pressed="${item.pinned}" title="Pin">♧</button><button class="bookmark-action" data-copy="${item.id}" aria-label="Copy URL for ${esc(item.title)}" title="Copy URL">⧉</button></div>${item.author ? `<div class="tree-authors"><span title="Confluence page created: ${esc(date(item.writtenAt))}">Written by ${esc(item.author)}</span> · <span title="Confluence page last edited: ${esc(date(item.confluenceUpdatedAt))}">Updated by ${esc(item.lastEditor)}</span></div>` : ""}</div>`;
    return `<li data-bookmark-row="${item.id}" class="tree-node ${selectedBookmarkId === item.id ? "details-selected" : ""} ${context ? "context-node" : ""}">${children.length ? `<details data-branch="${branch}" ${collapsedBranches.has(branch) ? "" : "open"}><summary>${title}</summary>${metadata}<ul>${children.map((child, index) => entry(child, number + "." + (index + 1), list, next)).join("")}</ul></details>` : `<div class="tree-leaf">${title}</div>${metadata}`}</li>`;
  }
  const folders = { children: new Map(), pages: [] };
  for (const [group, list] of groups) {
    let folder = folders;
    for (const name of JSON.parse(group)) {
      if (!folder.children.has(name))
        folder.children.set(name, { children: new Map(), pages: [] });
      folder = folder.children.get(name);
    }
    folder.pages.push(...list);
  }
  function folderCount(folder) {
    return (
      folder.pages.filter((item) => matching.has(item.id)).length +
      [...folder.children.values()].reduce(
        (sum, child) => sum + folderCount(child),
        0,
      )
    );
  }
  function folderOpens(folder) {
    return (
      folder.pages.reduce((sum, item) => sum + (Number(item.views) || 0), 0) +
      [...folder.children.values()].reduce(
        (sum, child) => sum + folderOpens(child),
        0,
      )
    );
  }
  function folderMarkup(folder, name, path, number) {
    const branch = "folder-" + JSON.stringify(path);
    const roots = folder.pages
      .filter(
        (item) =>
          !pageHierarchy[item.id]?.parent ||
          !included.has(pageHierarchy[item.id].parent),
      )
      .sort((a, b) => (order.get(a.id) ?? -1) - (order.get(b.id) ?? -1));
    const children = [...folder.children]
      .map(
        ([childName, child], index) =>
          `<li class="nested-bookmark-folder">${folderMarkup(child, childName, [...path, childName], number + "." + (index + 1))}</li>`,
      )
      .join("");
    return `<details class="tree-space" data-branch="${esc(branch)}" ${collapsedBranches.has(branch) ? "" : "open"}><summary><span class="tree-folder" aria-hidden="true">▱</span><strong>${esc(name)}</strong><span class="tree-count">${folderCount(folder)} bookmarks</span><span class="tree-folder-opens">${folderOpens(folder)} opens</span><span class="tree-folder-label">Folder</span></summary><ul>${children}${roots.map((item, index) => entry(item, number + "." + (folder.children.size + index + 1), folder.pages)).join("")}</ul></details>`;
  }
  document.querySelector("#bookmark-tree").innerHTML = [...folders.children]
    .map(([name, folder], index) =>
      folderMarkup(folder, name, [name], String(index + 1)),
    )
    .join("");
}
let selectedBookmarkId = null;
const localPageNotes = {};
try {
  const notes = JSON.parse(localStorage.getItem("owl-bookmark-notes") || "{}");
  if (notes && typeof notes === "object" && !Array.isArray(notes))
    Object.assign(localPageNotes, notes);
} catch {}
function showPageDetails(id) {
  const item = bookmarks.find((item) => item.id === id);
  if (!item) return;
  selectedBookmarkId = id;
  document.querySelector("#page-details-empty").hidden = true;
  document.querySelector("#page-details-content").hidden = false;
  document.querySelector("#page-note").value =
    typeof localPageNotes[id] === "string" ? localPageNotes[id] : "";
  document.querySelector("#page-note-status").textContent =
    "Notes are saved on this browser.";
  document.querySelector("#detail-title").textContent = item.title;
  document.querySelector("#detail-description").textContent = item.description;
  const exact = (value) =>
    new Date(value).toLocaleString("en-GB", {
      timeZone: "Europe/Dublin",
      dateStyle: "medium",
      timeStyle: "short",
    });
  const authorship = document.querySelector("#detail-authorship");
  authorship.hidden = !item.author;
  authorship.innerHTML = item.author
    ? `<div><span>Written by</span><strong>${esc(item.author)}</strong><small>${confluenceAge(item.writtenAt)} · ${esc(exact(item.writtenAt))}</small></div><div><span>Updated by</span><strong>${esc(item.lastEditor)}</strong><small>${confluenceAge(item.confluenceUpdatedAt)} · ${esc(exact(item.confluenceUpdatedAt))}</small></div>`
    : "";
  document
    .querySelector("#page-note")
    .setAttribute("aria-label", `Notes for ${item.title}`);
  const hierarchy = pageHierarchy[item.id];
  const parent = hierarchy?.parent
    ? bookmarks.find((page) => page.id === hierarchy.parent)
    : null;
  const fields = [
    ["URL", item.url],
    ["Domain", item.domain],
    ["Source", item.author ? "Confluence · sample metadata" : "Web bookmark"],
    ["Space", hierarchy?.space || "Not available"],
    [
      "Parent page",
      parent?.title || (hierarchy ? "Top-level page" : "Not available"),
    ],
    ["Opens", item.views],
    ["Favourite", item.favorite ? "Yes" : "No"],
    ["Pinned", item.pinned ? "Yes" : "No"],
    ["Added to OWL", `${confluenceAge(item.added)} · ${exact(item.added)}`],
    [
      "Updated in OWL",
      item.updatedInOwlAt
        ? `${confluenceAge(item.updatedInOwlAt)} · ${exact(item.updatedInOwlAt)}`
        : "Not updated",
    ],
    [
      "Last opened",
      item.lastViewed
        ? `${confluenceAge(item.lastViewed)} · ${exact(item.lastViewed)}`
        : "Never",
    ],
  ];
  document.querySelector("#detail-metadata").innerHTML = fields
    .map(
      ([label, value]) =>
        `<dt>${esc(label)}</dt><dd class="${label === "URL" ? "detail-url-value" : ""}"><span>${esc(value)}</span>${label === "URL" ? `<button class="detail-copy-open" type="button" data-copy-open="${item.id}" aria-label="Copy URL and open page in a new tab" title="Copy URL and open in new tab"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8h11v13H9V8ZM15 8V3H4v13h5"/></svg></button>` : ""}</dd>`,
    )
    .join("");
  const link = document.querySelector("#detail-open");
  link.href = item.url;
  link.dataset.open = item.id;

  document
    .querySelectorAll("[data-bookmark-row]")
    .forEach((row) =>
      row.classList.toggle(
        "details-selected",
        Number(row.dataset.bookmarkRow) === id,
      ),
    );
}
document
  .querySelector("#detail-metadata")
  .addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-open]");
    if (!button) return;
    const item = bookmarks.find(
      (item) => item.id === Number(button.dataset.copyOpen),
    );
    if (!item) return;
    // Open synchronously within the click gesture so clipboard permission cannot delay it.
    window.open(item.url, "_blank", "noopener,noreferrer");
    item.views++;
    item.lastViewed = Date.now();
    persist();
    render();
    try {
      await navigator.clipboard.writeText(item.url);
      button.classList.add("copied");
      button.innerHTML = '<span aria-hidden="true">✓</span>';
      button.title = "Copied";
      toast("URL copied.");
      setTimeout(() => {
        if (selectedBookmarkId === item.id) showPageDetails(item.id);
      }, 1000);
    } catch {
      toast("Unable to copy URL. The page was requested in a new tab.");
    }
  });
document.querySelector("#bookmark-tree").addEventListener("click", (event) => {
  if (event.target.closest("a,button,input")) return;
  const row = event.target.closest("[data-bookmark-row]");
  if (row) showPageDetails(Number(row.dataset.bookmarkRow));
});
document
  .querySelector("#bookmark-tree")
  .addEventListener("keydown", (event) => {
    if (
      !event.target.matches(".tree-page-content") ||
      !["Enter", " "].includes(event.key)
    )
      return;
    event.preventDefault();
    showPageDetails(
      Number(event.target.closest("[data-bookmark-row]").dataset.bookmarkRow),
    );
  });
document.querySelector("#page-note").addEventListener("input", (event) => {
  if (selectedBookmarkId === null) return;
  const value = event.target.value;
  localPageNotes[selectedBookmarkId] = value;
  try {
    localStorage.setItem("owl-bookmark-notes", JSON.stringify(localPageNotes));
    const item = bookmarks.find((item) => item.id === selectedBookmarkId);
    item.updatedInOwlAt = Date.now();
    persist();
    document.querySelector("#page-note-status").textContent =
      "Saved on this browser";
  } catch {
    document.querySelector("#page-note-status").textContent =
      "Unable to save. Keep this page open and copy your notes.";
  }
});
document.querySelector("#page-note").addEventListener("blur", () => {
  render();
  if (selectedBookmarkId !== null) showPageDetails(selectedBookmarkId);
});
function render() {
  document.querySelector("#bookmark-views").innerHTML = views
    .map(
      ([key, label, icon]) =>
        `<button class="side-link" data-view="${key}" aria-label="${label}" title="${label}" aria-pressed="${view === key}"><span class="nav-symbol" aria-hidden="true">${icon}</span><span class="view-name">${label}</span><span class="count">${bookmarks.filter((item) => matches(item, key)).length}</span></button>`,
    )
    .join("");
  renderDomainGroups();
  const grouped = new Set(domainGroups.flatMap((group) => group.domains));
  const domains = [...new Set(bookmarks.map((item) => item.domain))]
    .filter((host) => !grouped.has(host))
    .sort();
  document.querySelector("#bookmark-domains").innerHTML = domains
    .map(
      (host) =>
        `<button class="side-link" data-domain="${host}" aria-label="${host}" aria-pressed="${domain === host}" title="${host}"><span class="domain-initial">${host.split(".")[0].slice(0, 2).toUpperCase()}</span><span class="domain-name">${host}</span><span class="count">${bookmarks.filter((item) => item.domain === host).length}</span></button>`,
    )
    .join("");
  document.querySelector("#clear-domain").hidden =
    !domain && !selectedDomainGroup;
  const label = views.find((item) => item[0] === view)[1];
  document.querySelector("#bookmark-title").textContent = "Bookmark Tree";
  document.querySelector("#view-breadcrumb").textContent = label;
  const searchedUrl = parseBookmarkUrl(
    document.querySelector("#bookmark-search").value,
  );
  const scoped = bookmarks.filter(
    (item) =>
      matches(item, view) &&
      (!domain || item.domain === domain) &&
      matchesDomainGroup(item) &&
      (searchedUrl
        ? parseBookmarkUrl(item.url)?.href === searchedUrl.href
        : `${item.title} ${item.description} ${item.url}`
            .toLowerCase()
            .includes(query)),
  );
  renderConfluencePeople(scoped);
  const filtered = scoped.filter(matchesPerson);
  visibleBookmarkIds = filtered.map((item) => item.id);
  const visible = new Set(visibleBookmarkIds);
  selectedBookmarks.forEach((id) => {
    if (!visible.has(id)) selectedBookmarks.delete(id);
  });
  updateBookmarkSelection();
  const sort = document.querySelector("#bookmark-sort").value;
  filtered.sort((a, b) =>
    sort === "title"
      ? a.title.localeCompare(b.title)
      : sort === "opens"
        ? b.views - a.views
        : sort === "viewed"
          ? (b.lastViewed || 0) - (a.lastViewed || 0)
          : b.added - a.added,
  );
  document.querySelector("#bookmark-summary").textContent =
    `${filtered.length} bookmarks${domain ? " · " + domain : ""}${selectedDomainGroup ? " · " + (domainGroups.find((group) => group.id === selectedDomainGroup)?.name || "") : ""}${selectedPerson ? " · " + selectedPerson + (personRole === "any" ? "" : " · " + personRole) : ""}`;
  document.querySelector("#filter-description").textContent =
    view === "recent"
      ? "Viewed in the last 7 days"
      : view === "frequent"
        ? "Viewed at least 5 times"
        : view === "never"
          ? "Bookmarks you have not opened yet"
          : "Your saved pages, in one place.";
  renderBookmarkTree(filtered);
  document.querySelector("#bookmark-empty").hidden = filtered.length > 0;
  document.querySelector("#bookmark-total").textContent =
    `Showing ${filtered.length} of ${bookmarks.length} sample bookmarks`;
}
document.querySelector("#bookmark-tree").addEventListener(
  "toggle",
  (event) => {
    const branch = event.target.dataset.branch;
    if (!branch) return;
    if (event.target.open) collapsedBranches.delete(branch);
    else collapsedBranches.add(branch);
  },
  true,
);
for (const [id, open] of [
  ["expand-bookmark-tree", true],
  ["collapse-bookmark-tree", false],
])
  document.querySelector("#" + id).addEventListener("click", () => {
    document.querySelectorAll("#bookmark-tree details").forEach((node) => {
      node.open = open;
      if (open) collapsedBranches.delete(node.dataset.branch);
      else collapsedBranches.add(node.dataset.branch);
    });
  });
document
  .querySelector("#confluence-people-search")
  .addEventListener("input", (event) => {
    peopleQuery = event.target.value.trim().toLowerCase();
    render();
  });
document
  .querySelector("#all-confluence-people")
  .addEventListener("click", () => {
    selectedPerson = "";
    personRole = "any";
    render();
  });
document
  .querySelector("#confluence-people")
  .addEventListener("click", (event) => {
    const button = event.target.closest("[data-person]");
    if (!button) return;
    selectedPerson = button.dataset.person;
    personRole = button.dataset.role;
    render();
  });
document
  .querySelector("#bookmark-search")
  .addEventListener("input", (event) => {
    query = event.target.value.trim().toLowerCase();
    const url = parseBookmarkUrl(event.target.value);
    if (url) {
      view = "all";
      domain = "";
      selectedDomainGroup = "";
      selectedPerson = "";
    }
    document.querySelector("#add-bookmark").hidden =
      !url ||
      bookmarks.some((item) => parseBookmarkUrl(item.url)?.href === url.href);
    render();
  });
document.querySelector("#bookmark-sort").addEventListener("change", render);
document.querySelector("#clear-domain").addEventListener("click", () => {
  domain = "";
  selectedDomainGroup = "";
  render();
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest(
    "[data-view],[data-domain],[data-favorite],[data-pin],[data-copy],[data-open]",
  );
  if (!button) return;
  if (button.dataset.view) {
    view = button.dataset.view;
    document.querySelector("#bookmark-sort").value =
      view === "frequent" ? "opens" : view === "recent" ? "viewed" : "added";
    render();
    return;
  }
  if (button.dataset.domain) {
    selectedDomainGroup = "";
    domain = domain === button.dataset.domain ? "" : button.dataset.domain;
    render();
    return;
  }
  const action = ["favorite", "pin", "copy", "open"].find(
    (key) => button.dataset[key],
  );
  const item = bookmarks.find(
    (item) => item.id === Number(button.dataset[action]),
  );
  if (!item) return;
  if (action === "favorite") item.favorite = !item.favorite;
  if (action === "pin") item.pinned = !item.pinned;
  if (action === "open") {
    item.views++;
    item.lastViewed = Date.now();
    persist();
    setTimeout(render, 0);
    return;
  }
  if (action === "copy") {
    try {
      await navigator.clipboard.writeText(item.url);
      toast("URL copied");
    } catch {
      toast("Unable to copy URL");
    }
    return;
  }
  persist();
  render();
});
let toastTimer;
function toast(message) {
  const node = document.querySelector("#bookmark-toast");
  node.textContent = message;
  node.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("visible"), 1800);
}
function parseBookmarkUrl(value) {
  try {
    const result = new URL(value.trim());
    return ["http:", "https:"].includes(result.protocol) &&
      !result.username &&
      !result.password
      ? result
      : null;
  } catch {
    return null;
  }
}
function applyConfluenceBaseUrl(value) {
  const base = parseBookmarkUrl(value);
  if (!base || base.protocol !== "https:") return;
  for (const item of bookmarks) {
    if (!confluenceAttribution[item.id] || item.custom) continue;
    const oldDomain = item.domain;
    const original = new URL(bookmarkSeed[item.id - 1][1]);
    item.url =
      base.origin +
      base.pathname.replace(/\/$/, "") +
      original.pathname.replace(/^\/wiki/, "");
    item.domain = base.hostname;
    if (domain === oldDomain) domain = base.hostname;
  }
  render();
}
try {
  const added = JSON.parse(localStorage.getItem("owl-bookmark-added") || "[]");
  if (Array.isArray(added))
    for (const item of added) {
      const url = parseBookmarkUrl(item.url);
      if (
        url &&
        item.custom &&
        Number.isInteger(item.id) &&
        item.id > bookmarkSeed.length &&
        !bookmarks.some((existing) => existing.id === item.id)
      )
        bookmarks.push({ ...item, url: url.href, domain: url.hostname });
    }
} catch {}
document
  .querySelector("#bookmark-search-form")
  .addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.querySelector("#bookmark-search");
    const url = parseBookmarkUrl(input.value);
    if (!url) return;
    if (
      bookmarks.some((item) => parseBookmarkUrl(item.url)?.href === url.href)
    ) {
      view = "all";
      domain = "";
      selectedDomainGroup = "";
      selectedPerson = "";
      query = input.value.trim().toLowerCase();
      document.querySelector("#add-bookmark").hidden = true;
      render();
      toast("This bookmark is already saved.");
      return;
    }
    const slug = url.pathname.split("/").filter(Boolean).at(-1);
    let title = url.hostname;
    try {
      title = slug
        ? decodeURIComponent(slug).replace(/[-_+]/g, " ")
        : url.hostname;
    } catch {}
    bookmarks.push({
      id: nextBookmarkId(),
      title,
      url: url.href,
      domain: url.hostname,
      description: "Added in this browser · page details not fetched yet",
      views: 0,
      lastViewed: null,
      added: Date.now(),
      favorite: false,
      pinned: false,
      custom: true,
    });
    persist();
    view = "all";
    domain = "";
    selectedDomainGroup = "";
    selectedPerson = "";
    query = input.value.trim().toLowerCase();
    document.querySelector("#add-bookmark").hidden = true;
    render();
    toast("Bookmark added on this browser.");
  });
for (let i = bookmarks.length - 1; i >= 0; i--)
  if (deletedBookmarkIds.has(bookmarks[i].id)) bookmarks.splice(i, 1);
applyConfluenceBaseUrl(sampleConfluenceBaseUrl);
// Keep local sample record dates stable across reloads.
persist();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) render();
});
