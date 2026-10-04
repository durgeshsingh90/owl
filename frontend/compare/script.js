"use strict";
// Compare draws what the backend computes. /api/compare/layout returns both sides already
// aligned (a line missing on one side is an empty filler line there), line numbers, a
// colour per line, the changed-word ranges and the difference blocks. This script only
// paints them behind two text boxes and keeps the boxes scrolling together.
(() => {
  const $ = selector => document.querySelector(selector);
  const SIDES = ["original", "modified"];
  const LINE = 20;
  const PAD = 12;
  const DRAFT_KEY = "owl-compare-draft";
  const pane = {};
  for (const side of SIDES) {
    const root = document.querySelector(`.pane[data-side="${side}"]`);
    pane[side] = {
      root,
      text: root.querySelector("textarea"),
      layer: root.querySelector(".layer"),
      gutter: root.querySelector(".gutter-lines"),
      title: $(`#${side}-title`),
      status: $(`#${side}-status`),
      // Which displayed lines are fillers the backend added for alignment.
      filler: [],
      numbers: [],
      // The text box value the filler flags describe, and its lines for painting.
      shown: "",
      lines: [""],
      words: {},
    };
  }
  const state = {
    view: "all", key: null, layout: null, block: -1, sequence: 0,
    controller: null, timer: null, syncing: false, frame: 0, charWidth: 7.5,
  };

  const number = value => Number(value || 0).toLocaleString();
  const toast = message => {
    const element = $("#toast");
    element.textContent = message;
    element.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { element.hidden = true; }, 2200);
  };
  const showError = message => { $("#error").textContent = message || ""; $("#error").hidden = !message; };

  function measureCharWidth() {
    const probe = document.createElement("span");
    probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre";
    probe.textContent = "M".repeat(200);
    pane.original.text.parentElement.append(probe);
    state.charWidth = probe.getBoundingClientRect().width / 200 || 7.5;
    probe.remove();
  }

  // The real text of a side is every displayed line that is not an alignment filler.
  function realText(side) {
    const p = pane[side];
    return p.lines.filter((_, index) => !p.filler[index]).join("\n");
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

  async function compare(texts) {
    clearTimeout(state.timer);
    state.controller?.abort();
    state.controller = new AbortController();
    const sequence = ++state.sequence;
    $("#status").textContent = "Comparing…";
    const body = {
      originalText: texts ? texts.original : realText("original"),
      modifiedText: texts ? texts.modified : realText("modified"),
      ignoreWhitespace: $("#ignore-whitespace").checked,
      view: state.view,
    };
    try {
      const layout = await request("/api/compare/layout", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
        signal: state.controller.signal,
      });
      if (sequence !== state.sequence) return;
      showError("");
      apply(layout);
      saveDraft(body.originalText, body.modifiedText);
    } catch (error) {
      if (error.name !== "AbortError") { showError(error.message); $("#status").textContent = ""; }
    }
  }

  async function changeView(view) {
    state.view = view;
    state.block = -1;
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
    if (!state.key) return compare();
    const sequence = ++state.sequence;
    try {
      const layout = await request(`/api/compare/layout/${state.key}?view=${view}`);
      if (sequence === state.sequence) apply(layout, {top: true});
    } catch (error) {
      if (error.status === 404) compare();
      else showError(error.message);
    }
  }

  // Put the aligned texts in the boxes, keeping the caret on the same real line.
  function apply(layout, {top = false} = {}) {
    const active = SIDES.find(side => document.activeElement === pane[side].text);
    let caret = null;
    if (active) caret = caretPosition(active);
    state.layout = layout;
    state.key = layout.key;
    for (const side of SIDES) {
      const p = pane[side];
      const data = layout[side];
      p.numbers = data.numbers;
      p.filler = data.numbers.map(value => value == null);
      p.words = data.words;
      if (p.text.value !== data.text) p.text.value = data.text;
      p.shown = data.text;
      p.lines = data.text.split("\n");
      p.text.readOnly = layout.view !== "all";
    }
    $("#notice").hidden = layout.view === "all";
    $("#notice").textContent = "Filtered view is read-only. Choose Show all to edit.";
    if (top) for (const side of SIDES) { pane[side].text.scrollTop = 0; pane[side].text.scrollLeft = 0; }
    if (caret) restoreCaret(active, caret);
    summarize(layout.summary);
    if (state.block >= layout.blocks.length) state.block = layout.blocks.length - 1;
    updateBlockNav();
    paint();
  }

  function caretPosition(side) {
    const p = pane[side];
    const before = p.text.value.slice(0, p.text.selectionStart);
    const display = before.split("\n").length - 1;
    const real = p.filler.slice(0, display).filter(flag => !flag).length;
    return {display, real, column: before.length - before.lastIndexOf("\n") - 1, scrollTop: p.text.scrollTop};
  }

  function restoreCaret(side, caret) {
    const p = pane[side];
    let display = p.numbers.findIndex(value => value === caret.real + 1);
    if (display < 0) display = Math.max(0, p.numbers.length - 1);
    const lines = p.lines;
    let offset = 0;
    for (let index = 0; index < display; index++) offset += lines[index].length + 1;
    offset += Math.min(caret.column, (lines[display] || "").length);
    p.text.setSelectionRange(offset, offset);
    // Keep the caret's line where it was on screen when fillers above it change.
    p.text.scrollTop = caret.scrollTop + (display - caret.display) * LINE;
  }

  function summarize(summary) {
    $("#count-all").textContent = number(summary.rows);
    $("#count-differences").textContent = number(summary.differences);
    $("#count-similarities").textContent = number(summary.similarities);
    $("#count-changed").textContent = number(summary.changed);
    $("#count-removed").textContent = number(summary.removed);
    $("#count-added").textContent = number(summary.added);
    $("#status").textContent = summary.identical ? "The texts are identical" : `${number(summary.differences)} differing ${summary.differences === 1 ? "line" : "lines"}`;
    for (const side of SIDES) pane[side].stats = `Lines ${number(summary[side].lines)}  Chars ${number(summary[side].chars)}`;
    for (const side of SIDES) updateStatus(side);
  }

  // Visual column of a character column, expanding tabs like the text box does.
  function visualColumn(line, column) {
    let visual = 0;
    for (let index = 0; index < column && index < line.length; index++) visual = line[index] === "\t" ? visual + 4 - (visual % 4) : visual + 1;
    return visual + Math.max(0, column - line.length);
  }

  function paintNow() {
    cancelAnimationFrame(state.frame);
    for (const side of SIDES) paintSide(side);
    paintOverview();
  }
  function paint() {
    cancelAnimationFrame(state.frame);
    state.frame = requestAnimationFrame(paintNow);
  }

  function paintSide(side) {
    const p = pane[side];
    const layout = state.layout;
    const box = p.text;
    const top = box.scrollTop;
    const left = box.scrollLeft;
    const first = Math.max(0, Math.floor(top / LINE) - 2);
    const last = first + Math.ceil(box.clientHeight / LINE) + 4;
    const lines = p.lines;
    const kinds = layout ? layout.kinds : "";
    const current = layout && state.block >= 0 ? layout.blocks[state.block] : null;
    const rows = [];
    const numbers = [];
    for (let index = first; index <= last && index < lines.length; index++) {
      const y = index * LINE - top;
      const kind = kinds[index] || "s";
      const filler = p.filler[index];
      const inBlock = current && index >= current[0] && index < current[1];
      const cls = filler ? "f" : kind === "r" && side === "modified" ? "f" : kind === "a" && side === "original" ? "f" : kind;
      rows.push(`<div class="row ${cls}${inBlock ? " current" : ""}" style="top:${y}px"></div>`);
      for (const [start, end] of p.words[index] || []) {
        const line = lines[index] || "";
        const x = PAD + visualColumn(line, start) * state.charWidth - left;
        const width = (visualColumn(line, end) - visualColumn(line, start)) * state.charWidth;
        rows.push(`<div class="word" style="top:${y}px;left:${x}px;width:${width}px"></div>`);
      }
      numbers.push(`<div style="top:${y}px">${p.numbers[index] ?? ""}</div>`);
    }
    p.layer.innerHTML = rows.join("");
    p.gutter.innerHTML = numbers.join("");
  }

  const OVERVIEW_COLOURS = {c: "rgba(245,158,11,.85)", r: "rgba(244,63,94,.85)", a: "rgba(14,165,233,.85)"};
  const marks = {canvas: document.createElement("canvas"), layout: null, width: 0, height: 0};
  function paintOverview() {
    const canvas = $("#overview");
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const kinds = state.layout?.kinds || "";
    const total = Math.max(1, kinds.length);
    // The difference marks only change with the layout or size; scrolling just moves the frame.
    if (marks.layout !== state.layout || marks.width !== width || marks.height !== height) {
      Object.assign(marks, {layout: state.layout, width, height});
      marks.canvas.width = width * ratio;
      marks.canvas.height = height * ratio;
      const context = marks.canvas.getContext("2d");
      context.scale(ratio, ratio);
      // One mark per run of the same kind, at least 2px so single lines stay visible.
      for (let index = 0; index < kinds.length;) {
        const kind = kinds[index];
        let end = index + 1;
        while (end < kinds.length && kinds[end] === kind) end++;
        if (kind !== "s") {
          context.fillStyle = OVERVIEW_COLOURS[kind];
          context.fillRect(3, index / total * height, width - 6, Math.max(2, (end - index) / total * height));
        }
        index = end;
      }
    }
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
    }
    const context = canvas.getContext("2d");
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(marks.canvas, 0, 0);
    context.scale(ratio, ratio);
    const box = pane.original.text;
    const lines = Math.max(total, box.scrollHeight / LINE);
    context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--current").trim() || "#2568b6";
    context.lineWidth = 1.5;
    context.strokeRect(1, box.scrollTop / LINE / lines * height + .75, width - 2, Math.max(6, box.clientHeight / LINE / lines * height - 1.5));
  }

  function updateStatus(side) {
    const p = pane[side];
    const box = p.text;
    const before = box.value.slice(0, box.selectionStart);
    const display = before.split("\n").length - 1;
    const line = p.numbers[display];
    const column = before.length - before.lastIndexOf("\n");
    const selected = Math.abs(box.selectionEnd - box.selectionStart);
    p.status.textContent = `Ln ${line ?? "—"}, Col ${column}   Sel ${selected}   ${p.stats || ""}`;
  }

  function updateBlockNav() {
    const blocks = state.layout?.blocks || [];
    $("#block-prev").disabled = !blocks.length || state.block === 0;
    $("#block-next").disabled = !blocks.length || state.block >= blocks.length - 1;
    $("#block-position").textContent = blocks.length
      ? (state.block >= 0 ? `${state.block + 1} of ${number(blocks.length)}` : `${number(blocks.length)} ${blocks.length === 1 ? "block" : "blocks"}`)
      : "No differences";
    const canCopy = state.view === "all" && state.block >= 0 && !state.dirty;
    $("#copy-left").disabled = !canCopy;
    $("#copy-right").disabled = !canCopy;
  }

  function goToBlock(index) {
    const blocks = state.layout?.blocks || [];
    if (!blocks.length) return;
    if (state.block < 0) {
      // From nothing selected, start at the first block below (or above) the view.
      const topLine = Math.floor(pane.original.text.scrollTop / LINE);
      const next = blocks.findIndex(([start]) => start >= topLine);
      index = index < 0 ? Math.max(0, (next < 0 ? blocks.length : next) - 1) : (next < 0 ? blocks.length - 1 : next);
    }
    state.block = Math.max(0, Math.min(blocks.length - 1, index));
    scrollToLine(blocks[state.block][0]);
    updateBlockNav();
    paint();
  }

  function scrollToLine(line) {
    const box = pane.original.text;
    box.scrollTop = Math.max(0, line * LINE - box.clientHeight / 3);
    syncFrom("original");
  }

  function syncFrom(side) {
    if (state.syncing) return;
    const source = pane[side].text;
    const target = pane[side === "original" ? "modified" : "original"].text;
    state.syncing = true;
    target.scrollTop = source.scrollTop;
    target.scrollLeft = source.scrollLeft;
    requestAnimationFrame(() => { state.syncing = false; });
    // Scroll events arrive once per frame; painting now keeps colours under the text.
    paintNow();
  }

  // Track which lines are still alignment fillers after an edit: unchanged lines before
  // and after the edited region keep their flag, edited lines are real text.
  function trackEdit(side, previous) {
    const p = pane[side];
    const before = previous.split("\n");
    const after = p.text.value.split("\n");
    let head = 0;
    while (head < before.length && head < after.length && before[head] === after[head]) head++;
    let tail = 0;
    while (tail < before.length - head && tail < after.length - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
    const flags = p.filler;
    p.lines = after;
    p.filler = [
      ...flags.slice(0, head),
      ...Array(after.length - head - tail).fill(false),
      ...flags.slice(before.length - tail),
    ];
    p.numbers = [
      ...p.numbers.slice(0, head),
      ...Array(after.length - head - tail).fill(null),
      ...p.numbers.slice(before.length - tail),
    ];
  }

  function scheduleCompare() {
    clearTimeout(state.timer);
    state.dirty = true;
    updateBlockNav();
    $("#status").textContent = "Waiting for typing to stop…";
    state.timer = setTimeout(() => { state.dirty = false; compare(); }, 400);
  }

  async function copyBlock(direction) {
    if (state.block < 0 || !state.key) return;
    const block = state.block;
    try {
      const result = await request("/api/compare/copy", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({key: state.key, block, direction}),
      });
      apply(result);
      saveDraft(result.originalText, result.modifiedText);
      state.block = Math.min(block, result.blocks.length - 1);
      updateBlockNav();
      if (state.block >= 0) scrollToLine(result.blocks[state.block][0]);
      paint();
      toast(direction === "right" ? "Copied to the right side" : "Copied to the left side");
    } catch (error) {
      if (error.status === 404) compare();
      toast(error.message);
    }
  }

  function saveDraft(original, modified) {
    try {
      const draft = JSON.stringify({originalTitle: pane.original.title.value, modifiedTitle: pane.modified.title.value, originalText: original, modifiedText: modified});
      // Large inputs are not kept between visits; browser storage is small.
      if (draft.length < 2_000_000) localStorage.setItem(DRAFT_KEY, draft);
      else localStorage.removeItem(DRAFT_KEY);
    } catch { /* Storage may be unavailable. */ }
  }

  function readFile(side, file) {
    if (!file) return;
    if (file.size > 5_000_000) { showError(`${file.name} is larger than 5 MB.`); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const texts = {original: realText("original"), modified: realText("modified")};
      texts[side] = String(reader.result);
      pane[side].title.value = file.name;
      if (state.view !== "all") changeViewButtons("all");
      compare(texts);
    };
    reader.onerror = () => showError(`Could not read ${file.name}.`);
    reader.readAsText(file);
  }
  function changeViewButtons(view) {
    state.view = view;
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
  }

  // Events
  for (const side of SIDES) {
    const p = pane[side];
    p.text.addEventListener("input", () => {
      trackEdit(side, p.shown);
      p.shown = p.text.value;
      paint();
      scheduleCompare();
    });
    p.text.addEventListener("scroll", () => syncFrom(side));
    for (const type of ["keyup", "click", "select", "focus"]) p.text.addEventListener(type, () => updateStatus(side));
    p.text.addEventListener("keydown", event => {
      if (event.key === "Tab" && !event.shiftKey && !p.text.readOnly) {
        event.preventDefault();
        document.execCommand("insertText", false, "\t");
      }
    });
    p.title.addEventListener("input", () => saveDraft(realText("original"), realText("modified")));
    p.root.addEventListener("dragover", event => { event.preventDefault(); p.root.classList.add("dragging"); });
    p.root.addEventListener("dragleave", () => p.root.classList.remove("dragging"));
    p.root.addEventListener("drop", event => {
      event.preventDefault();
      p.root.classList.remove("dragging");
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
    const texts = {original: realText("original"), modified: realText("modified")};
    texts[button.dataset.clear] = "";
    if (state.view !== "all") changeViewButtons("all");
    compare(texts);
  }));
  $("#swap").addEventListener("click", () => {
    const texts = {original: realText("modified"), modified: realText("original")};
    [pane.original.title.value, pane.modified.title.value] = [pane.modified.title.value, pane.original.title.value];
    if (state.view !== "all") changeViewButtons("all");
    compare(texts);
  });
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => changeView(button.dataset.view)));
  $("#ignore-whitespace").addEventListener("change", () => compare());
  $("#block-prev").addEventListener("click", () => goToBlock(state.block - 1));
  $("#block-next").addEventListener("click", () => goToBlock(state.block + 1));
  $("#copy-left").addEventListener("click", () => copyBlock("left"));
  $("#copy-right").addEventListener("click", () => copyBlock("right"));
  document.addEventListener("keydown", event => {
    if (event.key === "F7") { event.preventDefault(); goToBlock(state.block + (event.shiftKey ? -1 : 1)); }
    else if (event.altKey && event.key === "ArrowRight" && !$("#copy-right").disabled) { event.preventDefault(); copyBlock("right"); }
    else if (event.altKey && event.key === "ArrowLeft" && !$("#copy-left").disabled) { event.preventDefault(); copyBlock("left"); }
  });
  $("#overview").parentElement.addEventListener("click", event => {
    const rect = event.currentTarget.getBoundingClientRect();
    const lines = state.layout?.lines || 0;
    scrollToLine(Math.floor((event.clientY - rect.top) / rect.height * lines));
  });
  window.addEventListener("resize", paint);
  new MutationObserver(paint).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});

  $("#share").addEventListener("click", async () => {
    const button = $("#share");
    button.disabled = true;
    try {
      const data = await request("/api/compare/share", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          originalTitle: pane.original.title.value, modifiedTitle: pane.modified.title.value,
          originalText: realText("original"), modifiedText: realText("modified"),
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
    measureCharWidth();
    let saved = null;
    const token = new URLSearchParams(location.search).get("share");
    if (token) {
      try { saved = (await request(`/api/compare/share/${encodeURIComponent(token)}`)).payload; }
      catch (error) { showError(error.message); }
    } else {
      try { saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch { /* Ignore an unreadable draft. */ }
    }
    const texts = {original: "", modified: ""};
    if (saved && typeof saved === "object") {
      for (const side of SIDES) {
        if (typeof saved[`${side}Text`] === "string") texts[side] = saved[`${side}Text`];
        if (typeof saved[`${side}Title`] === "string" && saved[`${side}Title`].trim()) pane[side].title.value = saved[`${side}Title`];
      }
      if (typeof saved.ignoreWhitespace === "boolean") $("#ignore-whitespace").checked = saved.ignoreWhitespace;
      if (["all", "differences", "similarities"].includes(saved.view)) changeViewButtons(saved.view);
    }
    compare(texts);
  })();
})();
