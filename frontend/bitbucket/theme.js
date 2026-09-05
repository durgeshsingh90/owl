"use strict";
(() => {
  const key = "owl-bitbucket-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = null;
  try {
    preference = localStorage.getItem(key);
  } catch {
    /* Storage may be unavailable. */
  }
  if (!["light", "dark"].includes(preference)) preference = null;
  function applyTheme() {
    const theme = preference || (system.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const button = document.querySelector("#theme-toggle");
    if (button) {
      const label = `Switch to ${theme === "dark" ? "light" : "dark"} mode`;
      button.setAttribute("aria-label", label);
      button.title = label;
      button.querySelector("span").textContent = theme === "dark" ? "☀" : "☾";
    }
  }
  applyTheme();
  system.addEventListener("change", () => {
    if (!preference) applyTheme();
  });
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme();
    document.querySelector("#theme-toggle").addEventListener("click", () => {
      preference =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(key, preference);
      } catch {
        /* Keep the theme for this page. */
      }
      applyTheme();
    });
  });
})();
