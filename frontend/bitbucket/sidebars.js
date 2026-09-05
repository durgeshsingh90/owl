"use strict";
(() => {
  const key = "owl-bitbucket-sidebar-layout";
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(key) || "{}") || {};
  } catch {
    /* Default to expanded. */
  }
  const collapsed = {
    projects: saved.projects === true,
    people: saved.people === true,
  };
  function apply(panel) {
    document.documentElement.dataset[`${panel}Collapsed`] = String(
      collapsed[panel],
    );
    const button = document.getElementById(`toggle-${panel}`);
    if (!button) return;
    const label = `${collapsed[panel] ? "Expand" : "Collapse"} ${panel === "projects" ? "Projects" : "People"}`;
    button.setAttribute("aria-expanded", String(!collapsed[panel]));
    button.setAttribute("aria-label", label);
    button.title = label;
    button.querySelector(".sidebar-chevron").textContent =
      (panel === "projects") === collapsed[panel] ? "›" : "‹";
    document.getElementById(`${panel}-content`).hidden = collapsed[panel];
    if (panel === "projects") {
      const deleteButton = document.getElementById("delete-selected-repo");
      const destination = document.querySelector(
        collapsed.projects ? ".left-sidebar" : ".repository-toolbar",
      );
      destination.appendChild(deleteButton);
    }
  }
  Object.keys(collapsed).forEach(apply);
  document.addEventListener("DOMContentLoaded", () => {
    for (const panel of Object.keys(collapsed)) {
      apply(panel);
      document
        .getElementById(`toggle-${panel}`)
        .addEventListener("click", () => {
          collapsed[panel] = !collapsed[panel];
          apply(panel);
          try {
            localStorage.setItem(key, JSON.stringify(collapsed));
          } catch {
            /* Still usable for this session. */
          }
        });
    }
  });
})();
