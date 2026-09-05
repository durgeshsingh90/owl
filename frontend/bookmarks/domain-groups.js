"use strict";
let domainGroups = [];
let selectedDomainGroup = "";
try {
  const stored = JSON.parse(
    localStorage.getItem("owl-bookmark-domain-groups") || "[]",
  );
  if (Array.isArray(stored))
    domainGroups = stored.filter(
      (group) =>
        typeof group.id === "string" &&
        typeof group.name === "string" &&
        Array.isArray(group.domains) &&
        group.domains.every((domain) => typeof domain === "string"),
    );
} catch {}
function matchesDomainGroup(item) {
  const group = domainGroups.find((group) => group.id === selectedDomainGroup);
  return !group || group.domains.includes(item.domain);
}
function renderDomainGroups() {
  document.querySelector("#domain-groups").innerHTML = domainGroups
    .map(
      (group) =>
        `<div class="domain-group"><div class="domain-group-header"><button class="side-link" data-domain-group="${esc(group.id)}" aria-pressed="${selectedDomainGroup === group.id}" title="${esc(group.name)}"><span aria-hidden="true">▱</span><span class="domain-name">${esc(group.name)}</span><span class="count">${bookmarks.filter((item) => group.domains.includes(item.domain)).length}</span></button><button class="edit-domain-group" data-edit-domain-group="${esc(group.id)}" title="Rename or edit ${esc(group.name)}" aria-label="Rename or edit ${esc(group.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1L20 7l-3-3L5 16l-1 4Z"/></svg></button></div><details><summary>${group.domains.length} domains</summary>${group.domains.map((host) => `<button class="side-link" data-domain="${esc(host)}" aria-pressed="${domain === host}" title="${esc(host)}"><span class="domain-name">${esc(host)}</span><span class="count">${bookmarks.filter((item) => item.domain === host).length}</span></button>`).join("")}</details></div>`,
    )
    .join("");
}
document.addEventListener("DOMContentLoaded", () => {
  const dialog = document.querySelector("#domain-group-dialog"),
    form = document.querySelector("#domain-group-form"),
    name = document.querySelector("#domain-group-name"),
    options = document.querySelector("#domain-group-options"),
    feedback = document.querySelector("#domain-group-feedback");
  let editing = null;
  function open(id = null) {
    editing = id;
    const group = domainGroups.find((item) => item.id === id);
    form.reset();
    name.value = group?.name || "";
    document.querySelector("#domain-group-title").textContent = group
      ? "Edit domain group"
      : "New domain group";
    feedback.textContent = "";
    const domains = [
      ...new Set([
        ...bookmarks.map((item) => item.domain),
        ...(group?.domains || []),
      ]),
    ].sort();
    options.innerHTML = domains
      .map(
        (host) =>
          `<label><input type="checkbox" name="domains" value="${esc(host)}" ${group?.domains.includes(host) ? "checked" : ""}>${esc(host)}</label>`,
      )
      .join("");
    dialog.showModal();
    name.focus();
  }
  document
    .querySelector("#new-domain-group")
    .addEventListener("click", () => open());
  document
    .querySelector("#domain-group-close")
    .addEventListener("click", () => dialog.close());
  document
    .querySelector("#domain-groups")
    .addEventListener("click", (event) => {
      const edit = event.target.closest("[data-edit-domain-group]");
      if (edit) {
        open(edit.dataset.editDomainGroup);
        return;
      }
      const button = event.target.closest("[data-domain-group]");
      if (button) {
        selectedDomainGroup = button.dataset.domainGroup;
        domain = "";
        render();
      }
    });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const title = name.value.trim(),
      domains = [...options.querySelectorAll("input:checked")].map(
        (input) => input.value,
      );
    if (!title || !domains.length) {
      feedback.textContent =
        "Enter a group name and select at least one domain.";
      return;
    }
    if (
      domainGroups.some(
        (group) =>
          group.id !== editing &&
          group.name.toLowerCase() === title.toLowerCase(),
      )
    ) {
      feedback.textContent = "A group already uses that name.";
      return;
    }
    const group = { id: editing || crypto.randomUUID(), name: title, domains };
    const next = editing
      ? domainGroups.map((item) => (item.id === editing ? group : item))
      : [...domainGroups, group];
    try {
      localStorage.setItem("owl-bookmark-domain-groups", JSON.stringify(next));
    } catch {
      feedback.textContent = "Unable to save this group in browser storage.";
      return;
    }
    domainGroups = next;
    selectedDomainGroup = group.id;
    domain = "";
    dialog.close();
    render();
  });
});
