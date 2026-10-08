"use strict";
// Home: refresh every app at once, and how much space each app takes in the database.
(() => {
  const size = bytes => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = Number(bytes) || 0, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
  };
  window.owlToast = window.owlToast || (message => {
    let element = document.querySelector("#owl-toast");
    if (!element) {
      element = Object.assign(document.createElement("div"), {id: "owl-toast", role: "status"});
      element.style.cssText = "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#14263e;color:#fff;padding:9px 16px;border-radius:8px;font-size:13px;box-shadow:0 4px 14px #0004;z-index:50";
      document.body.append(element);
    }
    element.textContent = message;
    element.hidden = false;
    clearTimeout(element.timer);
    element.timer = setTimeout(() => { element.hidden = true; }, 3000);
  });
  const escape = value => String(value ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);

  async function load(fresh = false) {
    const bars = document.querySelector("#storage-bars");
    try {
      const response = await fetch(`/api/home/storage${fresh ? "?fresh=true" : ""}`, {cache: "no-store"});
      if (!response.ok) throw new Error();
      const data = await response.json();
      const largest = Math.max(1, ...data.apps.map(app => app.bytes));
      document.querySelector("#storage-total").textContent = `Total ${size(data.total_bytes)} on disk`;
      bars.innerHTML = data.apps
        .slice()
        .sort((a, b) => b.bytes - a.bytes)
        .map(app => `<div class="bar-row"><div class="bar-title"><span>${escape(app.name)}</span><strong>${size(app.bytes)}</strong></div><div class="track" role="img" aria-label="${escape(app.name)}: ${size(app.bytes)}"><span style="width:${app.bytes / largest * 100}%"></span></div></div>`)
        .join("");
      document.querySelector("#storage-foot").textContent =
        `${data.files.map(file => `${file.name} ${size(file.bytes)}`).join(" · ")}${data.free_bytes ? ` · ${size(data.free_bytes)} free inside the files, reused for new data` : ""} · in ${data.folder} · measured ${new Date(data.measured_at * 1000).toLocaleTimeString()}`;
    } catch {
      bars.innerHTML = "<p>Unable to measure the database. Check that OWL is running.</p>";
      document.querySelector("#storage-total").textContent = "";
    }
  }

  const button = document.querySelector("#refresh-all");
  button?.addEventListener("click", async () => {
    button.disabled = true;
    button.classList.add("spinning");
    try {
      const response = await fetch("/api/home/refresh-all", {method: "POST"});
      if (!response.ok) throw new Error();
      const {started} = await response.json();
      const count = Object.values(started).filter(Boolean).length;
      window.owlToast?.(count ? `Refreshing ${count} ${count === 1 ? "app" : "apps"} in the background` : "Every app is already refreshing or not set up");
      // The schedulers pick it up within a minute; the labels follow.
      for (const delay of [1500, 6000, 20000, 65000]) setTimeout(() => window.dispatchEvent(new CustomEvent("owl-auto-refresh-changed")), delay);
    } catch {
      window.owlToast?.("Could not start the refresh. Check that OWL is running.");
    } finally {
      setTimeout(() => { button.disabled = false; button.classList.remove("spinning"); }, 2000);
    }
  });

  // How many times each app was opened, small and faded in its card's corner.
  async function loadOpens() {
    try {
      const response = await fetch("/api/home/opens", {cache: "no-store"});
      if (!response.ok) return;
      const {opens} = await response.json();
      document.querySelectorAll("[data-app-opens]").forEach(element => {
        const entry = opens[element.dataset.appOpens];
        element.hidden = !entry;
        if (!entry) return;
        element.textContent = `opened ${entry.count.toLocaleString()}×`;
        element.title = `Opened ${entry.count.toLocaleString()} times · last ${new Date(entry.last_opened * 1000).toLocaleString()}`;
      });
    } catch { /* The counts are a nicety; the page works without them. */ }
  }

  void load();
  void loadOpens();
})();
