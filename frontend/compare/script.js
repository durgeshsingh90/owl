"use strict";
// Compare renders what the backend computes: alignment, highlights, filters, counts and
// pages all come from /api/compare. This script only collects input and draws rows.
(() => {
  const $ = selector => document.querySelector(selector);
  const sides = ["original", "modified"];
  const text = {original: $("#original-text"), modified: $("#modified-text")};
  const title = {original: $("#original-title"), modified: $("#modified-title")};
  const DRAFT_KEY = "owl-compare-draft";
  const state = {view: "all", key: null, result: null, hunk: -1, sequence: 0, controller: null, timer: null};

  const escape = value => String(value).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
  const number = value => Number(value || 0).toLocaleString();

  function toast(message) {
    const element = $("#toast");
    element.textContent = message;
    element.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { element.hidden = true; }, 2200);
  }
  function showError(message) {
    $("#error").textContent = message || "";
    $("#error").hidden = !message;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "Comparison failed.");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  // Send both texts once; later pages and views are fetched by the comparison key.
  async function analyze(offset = 0) {
    clearTimeout(state.timer);
    state.controller?.abort();
    state.controller = new AbortController();
    const sequence = ++state.sequence;
    $("#status").textContent = "Comparing…";
    try {
      const result = await request("/api/compare/analyze", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          originalText: text.original.value,
          modifiedText: text.modified.value,
          ignoreWhitespace: $("#ignore-whitespace").checked,
          view: state.view,
          offset,
        }),
        signal: state.controller.signal,
      });
      if (sequence !== state.sequence) return null;
      showError("");
      render(result);
      return result;
    } catch (error) {
      if (error.name === "AbortError") return null;
      showError(error.message);
      $("#status").textContent = "";
      return null;
    }
  }

  async function load(offset) {
    if (!state.key) return analyze(offset);
    const sequence = ++state.sequence;
    $("#status").textContent = "Loading…";
    try {
      const params = new URLSearchParams({view: state.view, offset});
      const result = await request(`/api/compare/results/${state.key}?${params}`);
      if (sequence !== state.sequence) return null;
      render(result);
      return result;
    } catch (error) {
      // The backend keeps recent comparisons only; recompute when one has expired.
      if (error.status === 404) return analyze(offset);
      showError(error.message);
      return null;
    }
  }

  function scheduleAnalyze() {
    clearTimeout(state.timer);
    state.hunk = -1;
    $("#status").textContent = "Waiting for typing to stop…";
    state.timer = setTimeout(() => analyze(0), 400);
    saveDraft();
  }

  function segments(row, side) {
    const parts = row[`${side}Segments`];
    if (!parts) return escape(row[side]);
    return parts.map(part => part.changed ? `<mark>${escape(part.text)}</mark>` : escape(part.text)).join("");
  }

  function render(result) {
    state.result = result;
    state.key = result.key;
    const summary = result.summary;
    $("#count-all").textContent = number(summary.rows);
    $("#count-differences").textContent = number(summary.differences);
    $("#count-similarities").textContent = number(summary.similarities);
    $("#count-changed").textContent = number(summary.changed);
    $("#count-removed").textContent = number(summary.removed);
    $("#count-added").textContent = number(summary.added);
    for (const side of sides) {
      const stats = summary[side];
      $(`#${side}-status`).dataset.stats = `Lines ${number(stats.lines)}   Chars ${number(stats.chars)}`;
      updateCursor(side);
    }
    $("#status").textContent = summary.identical ? "The texts are identical" : `${number(summary.differences)} differing ${summary.differences === 1 ? "line" : "lines"}`;

    const hunks = new Set(result.hunks);
    $("#rows").innerHTML = result.rows.map(row => {
      const classes = [row.kind, hunks.has(row.position) ? "hunk-start" : ""].join(" ");
      return `<tr class="${classes}" data-position="${row.position}">`
        + `<td class="ln">${row.originalLine ?? ""}</td><td class="text left">${segments(row, "original")}</td>`
        + `<td class="ln right">${row.modifiedLine ?? ""}</td><td class="text right">${segments(row, "modified")}</td></tr>`;
    }).join("");
    const empty = $("#empty");
    empty.hidden = result.total > 0;
    empty.textContent = state.view === "differences" ? "No differences. The texts match." : state.view === "similarities" ? "No matching lines." : "Nothing to compare yet.";

    const end = Math.min(result.offset + result.rows.length, result.total);
    $("#page-label").textContent = result.total ? `Rows ${number(result.offset + 1)}–${number(end)} of ${number(result.total)}` : "";
    $("#page-prev").disabled = result.offset <= 0;
    $("#page-next").disabled = end >= result.total;
    $(".pager").hidden = result.total <= result.limit;
    updateHunkNav();
  }

  function updateHunkNav() {
    const hunks = state.result?.hunks || [];
    $("#hunk-prev").disabled = !hunks.length || state.hunk <= 0;
    $("#hunk-next").disabled = !hunks.length || state.hunk >= hunks.length - 1;
    $("#hunk-position").textContent = hunks.length
      ? (state.hunk >= 0 ? `${state.hunk + 1} of ${number(hunks.length)}` : `${number(hunks.length)} ${hunks.length === 1 ? "block" : "blocks"}`)
      : "";
  }

  async function goToHunk(index) {
    const hunks = state.result?.hunks || [];
    if (index < 0 || index >= hunks.length) return;
    state.hunk = index;
    const position = hunks[index];
    const {offset, limit} = state.result;
    if (position < offset || position >= offset + limit) {
      const loaded = await load(Math.floor(position / limit) * limit);
      if (!loaded) return;
    }
    updateHunkNav();
    const row = $(`#rows tr[data-position="${position}"]`);
    if (!row) return;
    const container = $("#diff");
    container.scrollTop = row.offsetTop - container.clientHeight / 3;
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
  }

  function updateCursor(side) {
    const area = text[side];
    const before = area.value.slice(0, area.selectionStart);
    const line = before.split("\n").length;
    const col = before.length - before.lastIndexOf("\n");
    const selected = Math.abs(area.selectionEnd - area.selectionStart);
    const status = $(`#${side}-status`);
    status.innerHTML = `<span>Ln ${line}, Col ${col}</span><span>Sel ${selected}</span><span>${escape(status.dataset.stats || "")}</span>`;
  }

  function setView(view) {
    state.view = view;
    state.hunk = -1;
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
    $("#diff").scrollTop = 0;
    load(0);
  }

  function syncTitles() {
    $("#head-original").textContent = title.original.value || "Original";
    $("#head-modified").textContent = title.modified.value || "Modified";
  }

  function saveDraft() {
    try {
      const draft = {originalTitle: title.original.value, modifiedTitle: title.modified.value, originalText: text.original.value, modifiedText: text.modified.value};
      const json = JSON.stringify(draft);
      // Large inputs are not kept between visits; browser storage is small.
      if (json.length < 2_000_000) localStorage.setItem(DRAFT_KEY, json);
      else localStorage.removeItem(DRAFT_KEY);
    } catch { /* Storage may be unavailable. */ }
  }
  function restore(payload) {
    if (!payload || typeof payload !== "object") return;
    for (const side of sides) {
      if (typeof payload[`${side}Text`] === "string") text[side].value = payload[`${side}Text`];
      if (typeof payload[`${side}Title`] === "string" && payload[`${side}Title`].trim()) title[side].value = payload[`${side}Title`];
    }
    if (typeof payload.ignoreWhitespace === "boolean") $("#ignore-whitespace").checked = payload.ignoreWhitespace;
    if (["all", "differences", "similarities"].includes(payload.view)) {
      state.view = payload.view;
      document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === state.view)));
    }
    syncTitles();
  }

  function readFile(side, file) {
    if (!file) return;
    if (file.size > 5_000_000) { showError(`${file.name} is larger than 5 MB.`); return; }
    const reader = new FileReader();
    reader.onload = () => {
      text[side].value = String(reader.result);
      title[side].value = file.name;
      syncTitles();
      scheduleAnalyze();
    };
    reader.onerror = () => showError(`Could not read ${file.name}.`);
    reader.readAsText(file);
  }

  // Events
  for (const side of sides) {
    text[side].addEventListener("input", scheduleAnalyze);
    for (const type of ["keyup", "click", "select"]) text[side].addEventListener(type, () => updateCursor(side));
    title[side].addEventListener("input", () => { syncTitles(); saveDraft(); });
    const pane = text[side].closest(".pane");
    pane.addEventListener("dragover", event => { event.preventDefault(); pane.classList.add("dragging"); });
    pane.addEventListener("dragleave", () => pane.classList.remove("dragging"));
    pane.addEventListener("drop", event => {
      event.preventDefault();
      pane.classList.remove("dragging");
      readFile(side, event.dataTransfer.files[0]);
    });
  }
  let openSide = "original";
  document.querySelectorAll("[data-open]").forEach(button => button.addEventListener("click", () => {
    openSide = button.dataset.open;
    $("#file-input").value = "";
    $("#file-input").click();
  }));
  $("#file-input").addEventListener("change", event => readFile(openSide, event.target.files[0]));
  document.querySelectorAll("[data-clear]").forEach(button => button.addEventListener("click", () => {
    text[button.dataset.clear].value = "";
    scheduleAnalyze();
  }));
  $("#swap").addEventListener("click", () => {
    [text.original.value, text.modified.value] = [text.modified.value, text.original.value];
    [title.original.value, title.modified.value] = [title.modified.value, title.original.value];
    syncTitles();
    scheduleAnalyze();
  });
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
  $("#ignore-whitespace").addEventListener("change", scheduleAnalyze);
  $("#wrap").addEventListener("change", event => $("#diff").classList.toggle("wrap", event.target.checked));
  $("#hunk-prev").addEventListener("click", () => goToHunk(state.hunk - 1));
  $("#hunk-next").addEventListener("click", () => goToHunk(state.hunk + 1));
  $("#page-prev").addEventListener("click", () => { state.hunk = -1; load(Math.max(0, state.result.offset - state.result.limit)); $("#diff").scrollTop = 0; });
  $("#page-next").addEventListener("click", () => { state.hunk = -1; load(state.result.offset + state.result.limit); $("#diff").scrollTop = 0; });
  document.addEventListener("keydown", event => {
    if (event.key !== "F7") return;
    event.preventDefault();
    goToHunk(state.hunk + (event.shiftKey ? -1 : 1));
  });

  $("#share").addEventListener("click", async () => {
    const button = $("#share");
    button.disabled = true;
    try {
      const data = await request("/api/compare/share", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          originalTitle: title.original.value, modifiedTitle: title.modified.value,
          originalText: text.original.value, modifiedText: text.modified.value,
          ignoreWhitespace: $("#ignore-whitespace").checked, view: state.view,
        }),
      });
      const url = new URL(location.href);
      url.search = "";
      url.searchParams.set("share", data.token);
      await navigator.clipboard.writeText(url.toString());
      toast("Link copied · valid for 24 hours");
    } catch (error) {
      toast(error.message || "Could not create a share link.");
    } finally {
      button.disabled = false;
    }
  });

  // Start: a share link wins over the saved draft.
  (async () => {
    const token = new URLSearchParams(location.search).get("share");
    if (token) {
      try { restore((await request(`/api/compare/share/${encodeURIComponent(token)}`)).payload); }
      catch (error) { showError(error.message); }
    } else {
      try { restore(JSON.parse(localStorage.getItem(DRAFT_KEY) || "null")); } catch { /* Ignore an unreadable draft. */ }
    }
    syncTitles();
    analyze(0);
  })();
})();
