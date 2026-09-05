"use strict";
(() => {
  const key = "owl-bookmark-sidebar-collapsed";
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(key) === "true";
  } catch {}
  function apply() {
    document.documentElement.dataset.bookmarkSidebarCollapsed =
      String(collapsed);
    const button = document.querySelector("#toggle-bookmark-sidebar");
    if (!button) return;
    button.textContent = collapsed ? "›" : "‹";
    button.setAttribute("aria-expanded", String(!collapsed));
    button.title = collapsed
      ? "Expand bookmark sidebar"
      : "Collapse bookmark sidebar";
    button.setAttribute("aria-label", button.title);
  }
  apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document
      .querySelector("#toggle-bookmark-sidebar")
      .addEventListener("click", () => {
        collapsed = !collapsed;
        try {
          localStorage.setItem(key, String(collapsed));
        } catch {}
        apply();
      });
  });
})();
(() => {
  const panels = [
    {
      name: "Page details",
      id: "toggle-bookmark-details",
      attribute: "bookmarkDetailsCollapsed",
      key: "owl-bookmark-details-collapsed",
      icon: "▤",
    },
    {
      name: "People",
      id: "toggle-bookmark-people",
      attribute: "bookmarkPeopleCollapsed",
      key: "owl-bookmark-people-collapsed",
      icon: "♙",
    },
  ];
  for (const panel of panels) {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(panel.key) === "true";
    } catch {}
    function apply() {
      document.documentElement.dataset[panel.attribute] = String(collapsed);
      const button = document.getElementById(panel.id);
      if (!button) return;
      button.textContent = collapsed ? panel.icon : "›";
      button.setAttribute("aria-expanded", String(!collapsed));
      button.title = `${collapsed ? "Expand" : "Collapse"} ${panel.name}`;
      button.setAttribute("aria-label", button.title);
    }
    apply();
    document.addEventListener("DOMContentLoaded", () => {
      apply();
      document.getElementById(panel.id).addEventListener("click", () => {
        collapsed = !collapsed;
        try {
          localStorage.setItem(panel.key, String(collapsed));
        } catch {}
        apply();
      });
    });
  }
})();
