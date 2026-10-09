"use strict";
// Compare shows what the backend computes in two Monaco editors, the way the monkey
// compare tool does. /api/compare/layout returns both sides already aligned (a line
// missing on one side is an empty filler line there), line numbers, a colour per line,
// changed-word ranges and the difference blocks. This script only turns them into
// Monaco decorations and keeps the editors scrolling together.
(() => {
  const $ = selector => document.querySelector(selector);
  const SIDES = ["original", "modified"];
  const DRAFT_KEY = "owl-compare-draft";
  const LANGUAGES = {yaml: "yaml", yml: "yaml", json: "json", xml: "xml", ini: "ini", cfg: "ini", conf: "ini", properties: "ini", sh: "shell", bash: "shell", md: "markdown", py: "python", sql: "sql", ps1: "powershell", dockerfile: "dockerfile"};
  const pane = {};
  const state = {historyToken: "", historyTimer: 0, view: "all", key: null, layout: null, block: -1, sequence: 0, controller: null, timer: null, syncing: false, applying: false, dirty: false};
  let monaco;

  const number = value => Number(value || 0).toLocaleString();
  const toast = message => {
    const element = $("#toast");
    element.textContent = message;
    element.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { element.hidden = true; }, 2200);
  };
  const showError = message => { $("#error").textContent = message || ""; $("#error").hidden = !message; };
  const theme = () => document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs";

  // Lines typed into a filler. Monaco keeps these marks on their lines through edits;
  // emptied again (by undo or deleting the text), such a line is a filler once more.
  function formerFillers(side) {
    const p = pane[side];
    const lines = new Set();
    for (const range of p.formerFillers.getRanges()) {
      if (p.model.getLineContent(range.startLineNumber) === "") lines.add(range.startLineNumber - 1);
    }
    return lines;
  }

  // The real text of a side: every editor line that is not an alignment filler.
  function realText(side) {
    const p = pane[side];
    const former = formerFillers(side);
    return p.model.getLinesContent().filter((_, index) => !p.filler[index] && !former.has(index)).join("\n");
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
      ignoreBlankLines: $("#ignore-blank-lines").checked,
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
      state.dirty = false;
      showError("");
      apply(layout);
      saveDraft(body.originalText, body.modifiedText);
    } catch (error) {
      if (error.name !== "AbortError") { showError(error.message); $("#status").textContent = ""; }
    }
  }

  async function changeView(view) {
    setViewButtons(view);
    state.block = -1;
    if (!state.key || state.dirty) return compare();
    const sequence = ++state.sequence;
    try {
      const layout = await request(`/api/compare/layout/${state.key}?view=${view}`);
      if (sequence === state.sequence) apply(layout, {top: true});
    } catch (error) {
      if (error.status === 404) compare();
      else showError(error.message);
    }
  }
  function setViewButtons(view) {
    state.view = view;
    document.querySelectorAll("[data-view]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === view)));
  }

  // Show a layout. The editor text is replaced only when the aligned text differs, so an
  // edit inside a line (the usual case) keeps Monaco's undo history and cursor.
  function apply(layout, {top = false} = {}) {
    state.layout = layout;
    state.key = layout.key;
    state.applying = true;
    try {
      for (const side of SIDES) {
        const p = pane[side];
        const data = layout[side];
        const caret = p.model.getValue() !== data.text ? caretLine(side) : null;
        p.numbers = data.numbers;
        p.filler = data.numbers.map(value => value == null);
        if (p.model.getValue() !== data.text) {
          p.model.setValue(data.text);
          p.formerFillers.clear();
          if (caret) restoreCaret(side, caret);
        }
        // A new function makes Monaco redraw the source line numbers.
        p.editor.updateOptions({readOnly: layout.view !== "all", lineNumbers: line => String(p.numbers[line - 1] ?? "")});
      }
    } finally {
      state.applying = false;
    }
    $("#notice").hidden = layout.view === "all";
    $("#notice").textContent = "Filtered view is read-only. Choose Show all to edit.";
    if (top) for (const side of SIDES) pane[side].editor.setScrollTop(0);
    if (state.block >= layout.blocks.length) state.block = layout.blocks.length - 1;
    summarize(layout.summary);
    decorate();
    updateBlockNav();
  }

  // Where the caret is in real lines, so it can return there after fillers change.
  function caretLine(side) {
    const p = pane[side];
    if (!p.editor.hasTextFocus()) return null;
    const position = p.editor.getPosition();
    const top = p.editor.getTopForLineNumber(position.lineNumber) - p.editor.getScrollTop();
    const real = p.filler.slice(0, position.lineNumber - 1).filter(flag => !flag).length;
    return {real, column: position.column, top};
  }
  function restoreCaret(side, caret) {
    const p = pane[side];
    let line = p.numbers.findIndex(value => value === caret.real + 1) + 1;
    if (line < 1) line = p.model.getLineCount();
    p.editor.setPosition({lineNumber: line, column: caret.column});
    // Keep the caret's line where it was on screen.
    p.editor.setScrollTop(p.editor.getTopForLineNumber(line) - caret.top);
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

  const RULER = {c: "rgba(245,158,11,.9)", r: "rgba(244,63,94,.9)", a: "rgba(14,165,233,.9)"};
  function decorate() {
    const layout = state.layout;
    if (!layout) return;
    const current = state.block >= 0 ? layout.blocks[state.block] : null;
    for (const side of SIDES) {
      const p = pane[side];
      const decorations = [];
      const kinds = layout.kinds;
      for (let index = 0; index < kinds.length; index++) {
        const kind = kinds[index];
        const inBlock = current && index >= current[0] && index < current[1];
        if (kind === "s" && !inBlock) continue;
        const filler = p.filler[index];
        const className = [filler ? "cmp-f" : kind === "s" ? "" : `cmp-${kind}`, inBlock ? "cmp-current" : ""].join(" ").trim();
        const ruler = filler || kind === "s" ? null : RULER[kind];
        decorations.push({
          range: new monaco.Range(index + 1, 1, index + 1, 1),
          options: {
            isWholeLine: true,
            className,
            ...(ruler ? {
              overviewRuler: {color: ruler, position: monaco.editor.OverviewRulerLane.Full},
              minimap: {color: ruler, position: monaco.editor.MinimapPosition.Inline},
            } : {}),
          },
        });
      }
      const wordClass = side === "original" ? "cmp-word-removed" : "cmp-word-added";
      for (const [line, ranges] of Object.entries(layout[side].words)) {
        for (const [start, end] of ranges) {
          decorations.push({range: new monaco.Range(Number(line) + 1, start + 1, Number(line) + 1, end + 1), options: {inlineClassName: wordClass}});
        }
      }
      p.decorations.set(decorations);
    }
    paintOverview();
  }

  function paintOverview() {
    const canvas = $("#overview");
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height || !pane.original) return;
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
    }
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const kinds = state.layout?.kinds || "";
    const total = Math.max(1, kinds.length);
    // One mark per run of the same kind, at least 2px so single lines stay visible.
    for (let index = 0; index < kinds.length;) {
      const kind = kinds[index];
      let end = index + 1;
      while (end < kinds.length && kinds[end] === kind) end++;
      if (kind !== "s") {
        context.fillStyle = RULER[kind];
        context.fillRect(3, index / total * height, width - 6, Math.max(2, (end - index) / total * height));
      }
      index = end;
    }
    const editor = pane.original.editor;
    const scrollHeight = Math.max(1, editor.getScrollHeight());
    const viewport = editor.getLayoutInfo().height;
    context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--current").trim() || "#2568b6";
    context.lineWidth = 1.5;
    context.strokeRect(1, editor.getScrollTop() / scrollHeight * height + .75, width - 2, Math.max(6, viewport / scrollHeight * height - 1.5));
  }

  function updateStatus(side) {
    const p = pane[side];
    if (!p?.editor) return;
    const position = p.editor.getPosition() || {lineNumber: 1, column: 1};
    const selection = p.editor.getSelection();
    const selected = selection ? p.model.getValueLengthInRange(selection) : 0;
    p.status.textContent = `Ln ${p.numbers?.[position.lineNumber - 1] ?? "—"}, Col ${position.column}   Sel ${selected}   ${p.stats || ""}`;
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
      const top = pane.original.editor.getVisibleRanges()[0]?.startLineNumber || 1;
      const next = blocks.findIndex(([start]) => start + 1 >= top);
      index = index < 0 ? Math.max(0, (next < 0 ? blocks.length : next) - 1) : (next < 0 ? blocks.length - 1 : next);
    }
    state.block = Math.max(0, Math.min(blocks.length - 1, index));
    const line = blocks[state.block][0] + 1;
    for (const side of SIDES) pane[side].editor.revealLineInCenter(line);
    pane.original.editor.setPosition({lineNumber: line, column: 1});
    decorate();
    updateBlockNav();
  }

  // Keep the filler flags in step with an edit: replaced lines become real text, and
  // lines outside the edit keep their flag.
  function trackEdit(side, changes) {
    const p = pane[side];
    for (const change of [...changes].sort((a, b) => b.range.startLineNumber - a.range.startLineNumber)) {
      const start = change.range.startLineNumber - 1;
      const removed = change.range.endLineNumber - change.range.startLineNumber + 1;
      const added = change.text.split("\n").length;
      if (removed === 1 && added === 1 && p.filler[start]) {
        p.formerFillers.append([{range: new monaco.Range(start + 1, 1, start + 1, 1), options: {stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges}}]);
      }
      p.filler.splice(start, removed, ...Array(added).fill(false));
      p.numbers.splice(start, removed, ...Array(added).fill(null));
    }
  }

  function scheduleCompare() {
    clearTimeout(state.timer);
    state.dirty = true;
    updateBlockNav();
    $("#status").textContent = "Waiting for typing to stop…";
    state.timer = setTimeout(() => compare(), 400);
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
      state.block = Math.min(block, result.blocks.length - 1);
      apply(result);
      saveDraft(result.originalText, result.modifiedText);
      if (state.block >= 0) goToBlock(state.block);
      toast(direction === "right" ? "Copied to the right side" : "Copied to the left side");
    } catch (error) {
      if (error.status === 404) compare();
      toast(error.message);
    }
  }

  // The comparison as saved in Recent and in share links.
  function payload(original = realText("original"), modified = realText("modified")) {
    return {
      originalTitle: pane.original.title.value, modifiedTitle: pane.modified.title.value,
      originalText: original, modifiedText: modified,
      ignoreWhitespace: $("#ignore-whitespace").checked,
      ignoreBlankLines: $("#ignore-blank-lines").checked,
      view: state.view,
    };
  }

  // Each comparison is kept among the 20 most recent (older ones are deleted), updated as
  // it is edited, so it can be reopened from Recent or shared by link.
  async function saveHistory(body = payload()) {
    clearTimeout(state.historyTimer);
    if (!body.originalText.trim() && !body.modifiedText.trim()) return null;
    const saved = await request("/api/compare/history", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...body, token: state.historyToken || ""}),
    });
    state.historyToken = saved.token;
    return saved.token;
  }

  function saveDraft(original, modified) {
    clearTimeout(state.historyTimer);
    state.historyTimer = setTimeout(() => saveHistory(payload(original, modified)).catch(() => {}), 2000);
    try {
      const draft = JSON.stringify({...payload(original, modified), historyToken: state.historyToken || ""});
      // Large inputs are not kept between visits; browser storage is small.
      if (draft.length < 2_000_000) localStorage.setItem(DRAFT_KEY, draft);
      else localStorage.removeItem(DRAFT_KEY);
    } catch { /* Storage may be unavailable. */ }
  }

  function setLanguage() {
    // Syntax colouring follows the file extension in either title.
    const extension = [pane.original.title.value, pane.modified.title.value]
      .map(name => (name.toLowerCase().match(/\.([a-z0-9]+)$/) || [])[1])
      .find(value => LANGUAGES[value]);
    const language = LANGUAGES[extension] || "plaintext";
    for (const side of SIDES) monaco.editor.setModelLanguage(pane[side].model, language);
  }

  function readFile(side, file) {
    if (!file) return;
    if (file.size > 5_000_000) { showError(`${file.name} is larger than 5 MB.`); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const texts = {original: realText("original"), modified: realText("modified")};
      texts[side] = String(reader.result).replace(/\r\n/g, "\n");
      pane[side].title.value = file.name;
      setLanguage();
      if (state.view !== "all") setViewButtons("all");
      compare(texts);
    };
    reader.onerror = () => showError(`Could not read ${file.name}.`);
    reader.readAsText(file);
  }

  function createEditors() {
    monaco.languages.json?.jsonDefaults.setDiagnosticsOptions({validate: false});
    for (const side of SIDES) {
      const root = document.querySelector(`.pane[data-side="${side}"]`);
      const host = $(`#${side}-editor`);
      host.textContent = "";
      const p = pane[side] = {root, host, title: $(`#${side}-title`), status: $(`#${side}-status`), filler: [], numbers: []};
      p.model = monaco.editor.createModel("", "plaintext");
      p.model.setEOL(monaco.editor.EndOfLineSequence.LF);
      p.editor = monaco.editor.create(host, {
        model: p.model,
        theme: theme(),
        automaticLayout: true,
        minimap: {enabled: true, scale: 1, showSlider: "mouseover"},
        lineNumbers: line => String(p.numbers[line - 1] ?? ""),
        lineNumbersMinChars: 4,
        scrollBeyondLastLine: false,
        wordWrap: wrapOn() ? "on" : "off",
        fontSize: 13,
        lineHeight: 22,
        glyphMargin: false,
        folding: false,
        renderLineHighlight: "none",
        smoothScrolling: true,
        unicodeHighlight: {ambiguousCharacters: false, invisibleCharacters: false},
        renderWhitespace: "selection",
        overviewRulerLanes: 1,
        fixedOverflowWidgets: true,
      });
      p.decorations = p.editor.createDecorationsCollection();
      p.formerFillers = p.editor.createDecorationsCollection();
      p.model.onDidChangeContent(event => {
        if (state.applying) return;
        trackEdit(side, event.changes);
        scheduleCompare();
      });
      p.editor.onDidScrollChange(event => {
        if (!event.scrollTopChanged && !event.scrollLeftChanged) return;
        const other = pane[side === "original" ? "modified" : "original"].editor;
        if (!state.syncing) {
          state.syncing = true;
          // Wrapped lines differ in height between the sides, so keep the same top line instead of the same pixels.
          if (wrapOn()) {
            const line = p.editor.getVisibleRanges()[0]?.startLineNumber || 1;
            const offset = p.editor.getScrollTop() - p.editor.getTopForLineNumber(line);
            other.setScrollTop(other.getTopForLineNumber(line) + Math.max(0, offset));
          } else other.setScrollTop(p.editor.getScrollTop());
          other.setScrollLeft(p.editor.getScrollLeft());
          state.syncing = false;
        }
        paintOverview();
      });
      p.editor.onDidChangeCursorSelection(() => updateStatus(side));
      p.title.addEventListener("input", () => { setLanguage(); saveDraft(realText("original"), realText("modified")); });
      root.addEventListener("dragover", event => { event.preventDefault(); root.classList.add("dragging"); });
      root.addEventListener("dragleave", () => root.classList.remove("dragging"));
      root.addEventListener("drop", event => {
        event.preventDefault();
        root.classList.remove("dragging");
        readFile(side, event.dataTransfer.files[0]);
      });
    }
  }

  function bindControls() {
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
      if (state.view !== "all") setViewButtons("all");
      compare(texts);
    }));
    $("#swap").addEventListener("click", () => {
      const texts = {original: realText("modified"), modified: realText("original")};
      [pane.original.title.value, pane.modified.title.value] = [pane.modified.title.value, pane.original.title.value];
      if (state.view !== "all") setViewButtons("all");
      compare(texts);
    });
    document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => changeView(button.dataset.view)));
    $("#ignore-whitespace").addEventListener("change", () => compare());
    $("#block-prev").addEventListener("click", () => goToBlock(state.block - 1));
    $("#block-next").addEventListener("click", () => goToBlock(state.block + 1));
    $("#copy-left").addEventListener("click", () => copyBlock("left"));
    $("#copy-right").addEventListener("click", () => copyBlock("right"));
    // Capture so the shortcuts also work while an editor has focus.
    window.addEventListener("keydown", event => {
      if (event.key === "F7") { event.preventDefault(); event.stopPropagation(); goToBlock(state.block + (event.shiftKey ? -1 : 1)); }
      else if (event.altKey && event.key === "ArrowRight" && !$("#copy-right").disabled) { event.preventDefault(); event.stopPropagation(); copyBlock("right"); }
      else if (event.altKey && event.key === "ArrowLeft" && !$("#copy-left").disabled) { event.preventDefault(); event.stopPropagation(); copyBlock("left"); }
    }, true);
    $("#overview").parentElement.addEventListener("click", event => {
      const rect = event.currentTarget.getBoundingClientRect();
      const lines = state.layout?.lines || 0;
      const line = Math.max(1, Math.floor((event.clientY - rect.top) / rect.height * lines) + 1);
      pane.original.editor.revealLineInCenter(line);
    });
    window.addEventListener("resize", paintOverview);
    new MutationObserver(() => { monaco.editor.setTheme(theme()); paintOverview(); })
      .observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
    $("#share").addEventListener("click", async () => {
      const button = $("#share");
      button.disabled = true;
      try {
        const token = await saveHistory();
        if (!token) throw new Error("Add some text to share this comparison.");
        const url = new URL(location.href);
        url.search = "";
        url.searchParams.set("share", token);
        await navigator.clipboard.writeText(url.toString());
        toast("Link copied · it works while this is among your 20 most recent comparisons");
      } catch (error) {
        toast(error.message || "Could not create a share link.");
      } finally {
        button.disabled = false;
      }
    });
  }

  // Word wrap for both sides together, remembered in this browser.
  const WRAP_KEY = "owl-compare-wrap";
  let wrap = null;
  function wrapOn() {
    if (wrap === null) { try { wrap = localStorage.getItem(WRAP_KEY) === "1"; } catch { wrap = false; } }
    return wrap;
  }

  // Start: a share link wins over the saved draft.
  async function start() {
    createEditors();
    bindControls();
    bindRecent();
    document.querySelectorAll("[data-format]").forEach(button => button.addEventListener("click", () => formatSide(button.dataset.format)));
    $("#ignore-blank-lines").addEventListener("change", () => compare());
    $("#word-wrap").checked = wrapOn();
    $("#word-wrap").addEventListener("change", event => {
      try { localStorage.setItem(WRAP_KEY, event.target.checked ? "1" : "0"); } catch { /* this page only */ }
      wrap = event.target.checked;
      for (const side of SIDES) pane[side].editor.updateOptions({wordWrap: wrap ? "on" : "off"});
    });
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
      if (typeof saved.ignoreBlankLines === "boolean") $("#ignore-blank-lines").checked = saved.ignoreBlankLines;
      if (["all", "differences", "similarities"].includes(saved.view)) setViewButtons(saved.view);
      // Your own draft keeps updating its Recent entry; a link someone shared starts a new one.
      if (!token && typeof saved.historyToken === "string") state.historyToken = saved.historyToken;
    }
    setLanguage();
    compare(texts);
  }

  function openComparison(saved, historyToken = "") {
    const texts = {original: saved.originalText || "", modified: saved.modifiedText || ""};
    for (const side of SIDES) pane[side].title.value = saved[`${side}Title`] || (side === "original" ? "Original" : "Modified");
    if (typeof saved.ignoreWhitespace === "boolean") $("#ignore-whitespace").checked = saved.ignoreWhitespace;
    if (typeof saved.ignoreBlankLines === "boolean") $("#ignore-blank-lines").checked = saved.ignoreBlankLines;
    setViewButtons("all");
    state.historyToken = historyToken;
    state.block = -1;
    setLanguage();
    compare(texts);
  }

  function bindRecent() {
    const toggle = $("#recent-toggle"), menu = $("#recent-menu");
    const ago = seconds => {
      const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
      return minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.round(minutes / 60)}h ago` : `${Math.round(minutes / 1440)}d ago`;
    };
    const escape = value => String(value ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
    async function show() {
      menu.innerHTML = "<p>Loading…</p>";
      try {
        const {items, limit} = await request("/api/compare/history");
        menu.innerHTML = items.length
          ? `<p class="recent-note">Your ${limit} most recent comparisons; older ones are deleted.</p>` + items.map(item => `<div class="recent-item${item.token === state.historyToken ? " current" : ""}"><button type="button" data-recent="${escape(item.token)}" title="Open this comparison"><strong>${escape(item.original_title || "Original")} ⇄ ${escape(item.modified_title || "Modified")}</strong><small>${ago(item.updated_at)} · ${Number(item.original_chars || 0).toLocaleString()} / ${Number(item.modified_chars || 0).toLocaleString()} chars</small></button><button type="button" class="recent-link" data-recent-link="${escape(item.token)}" title="Copy a link to this comparison" aria-label="Copy a link">⧉</button><button type="button" class="recent-delete" data-recent-delete="${escape(item.token)}" title="Remove from Recent" aria-label="Remove">×</button></div>`).join("")
          : "<p>No recent comparisons yet.</p>";
      } catch (error) { menu.innerHTML = `<p>${escape(error.message)}</p>`; }
    }
    toggle.addEventListener("click", () => {
      const opening = menu.hidden;
      menu.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      if (opening) void show();
    });
    document.addEventListener("click", event => {
      if (!menu.hidden && !event.target.closest(".recent")) { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    });
    menu.addEventListener("click", async event => {
      const openButton = event.target.closest("[data-recent]");
      const link = event.target.closest("[data-recent-link]");
      const remove = event.target.closest("[data-recent-delete]");
      try {
        if (openButton) {
          const {payload: saved} = await request(`/api/compare/share/${encodeURIComponent(openButton.dataset.recent)}`);
          menu.hidden = true;
          history.replaceState(null, "", location.pathname);
          openComparison(saved, openButton.dataset.recent);
        } else if (link) {
          const url = new URL(location.href);
          url.search = "";
          url.searchParams.set("share", link.dataset.recentLink);
          await navigator.clipboard.writeText(url.toString());
          toast("Link copied");
        } else if (remove) {
          await request(`/api/compare/history/${encodeURIComponent(remove.dataset.recentDelete)}`, {method: "DELETE"});
          if (remove.dataset.recentDelete === state.historyToken) state.historyToken = "";
          void show();
        }
      } catch (error) { toast(error.message); }
    });
    $("#new-compare").addEventListener("click", async () => {
      await saveHistory().catch(() => {});
      history.replaceState(null, "", location.pathname);
      openComparison({}, "");
    });
  }

  async function formatSide(side) {
    try {
      const {text, kind} = await request("/api/compare/format", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: realText(side)}),
      });
      const texts = {original: realText("original"), modified: realText("modified")};
      texts[side] = text;
      if (state.view !== "all") setViewButtons("all");
      if (!/\.(json5?|jsonl|ndjson)$/i.test(pane[side].title.value)) setLanguageTo("json");
      compare(texts);
      toast(`Formatted as ${kind === "jsonl" ? "JSON Lines" : kind.toUpperCase()}`);
    } catch (error) {
      toast(error.message);
    }
  }
  function setLanguageTo(language) {
    for (const side of SIDES) monaco.editor.setModelLanguage(pane[side].model, language);
  }

  // Monaco is served from OWL itself (frontend/vendor/monaco), so it works offline.
  window.require.config({paths: {vs: "../vendor/monaco/vs"}});
  window.require(["vs/editor/editor.main"], loaded => {
    monaco = loaded || window.monaco;
    start().catch(error => showError(error.message));
  }, () => showError("The editor could not be loaded. Check that OWL is running."));
})();
