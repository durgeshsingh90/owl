"use strict";
// Search AWS: everything downloaded, ranked by how well it matches. Opens any result's properties.
AA.views.explore = (() => {
  const {esc, api} = AA;
  const PAGE = 100;
  const TIER = {Exact: "PASS", "All words": "PASS", "Most words": "WARNING", "Some words": "WARNING", "Near match": "UNKNOWN"};
  const state = {q: "", type: "", account: "", offset: 0};
  let data = null, typing, sequence = 0, viewerReady = false;
  const pending = [];

  function mark(value, words) {
    let html = esc(value);
    for (const word of words) {
      if (word.length < 2) continue;
      html = html.replace(new RegExp(esc(word).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
    }
    return html;
  }

  function when(iso) {
    return iso ? new Date(iso).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "";
  }

  function resultsHtml() {
    if (!data) return `<div class="loading">Loading…</div>`;
    if (!data.resources) return `<div class="empty">Nothing downloaded yet. Test accounts and download them on the <a href="#download">AWS download</a> page.</div>`;
    if (!state.q.trim()) return `<div class="empty">Search ${data.resources.toLocaleString()} resources across ${data.accounts.toLocaleString()} account${data.accounts === 1 ? "" : "s"}: names, ids, IPs, CIDRs, ARNs, tags, DNS names, anything.</div>`;
    if (!data.total) return `<div class="empty">Nothing matches “${esc(state.q)}”, even loosely.</div>`;
    const words = [...state.q.toLowerCase().split(/\s+/).filter(Boolean), ...Object.values(data.near_words || {}).flat()];
    const pages = Math.ceil(data.total / PAGE), page = Math.floor(state.offset / PAGE) + 1;
    const near = Object.entries(data.near_words || {});
    return `<div class="ex-summary"><b>${data.total.toLocaleString()}</b> result${data.total === 1 ? "" : "s"}
        ${Object.entries(data.tiers).map(([t, n]) => `<span class="status ${TIER[t]}">${esc(t)} ${n.toLocaleString()}</span>`).join("")}
        ${near.length ? `<span class="small muted">Did you mean ${near.map(([, list]) => esc(list.slice(0, 3).join(", "))).join("; ")}?</span>` : ""}</div>
      <div class="ex-results">${data.results.map((r, i) => `<button type="button" class="ex-result" data-i="${i}">
        <div class="ex-title"><span class="status ${TIER[r.tier]}">${esc(r.tier)}</span><span class="ex-type">${esc(r.type_title)}</span>
          <b>${mark(r.name || r.id, words)}</b>${r.name && r.name !== r.id ? `<span class="mono muted">${mark(String(r.id).split("/").pop(), words)}</span>` : ""}
          ${r.of > 1 ? `<span class="small muted">${r.matched}/${r.of} words</span>` : ""}</div>
        <div class="small muted">${esc(r.account)}${r.account_name ? ` · ${esc(r.account_name)}` : ""}${r.env ? ` · ${esc(r.env)}` : ""}${r.region ? ` · ${esc(r.region)}` : ""}${r.created_at ? ` · created ${esc(when(r.created_at))}` : ""}
          ${r.type === "aws_account" ? ` · <span class="link-like" role="link" tabindex="0" data-state="${esc(r.account)}">Open all its resources</span>` : ""}</div>
        ${r.tags?.length ? `<div class="dl-tags">${r.tags.map((t) => `<span class="dl-tag">${mark(t, words)}</span>`).join("")}</div>` : ""}
        ${r.contains ? `<div class="small">Range <span class="mono">${esc(r.contains)}</span> contains this address</div>` : ""}
        ${r.snippets.map((s) => `<div class="ex-snippet"><span class="muted">${esc(s.field)}</span> <span class="mono">${mark(s.value, words)}</span></div>`).join("")}
      </button>`).join("")}</div>
      ${pages > 1 ? `<div class="filters ex-pages"><button type="button" data-page="-1" ${page === 1 ? "disabled" : ""}>Previous</button><span class="small">Page ${page} of ${pages}</span><button type="button" data-page="1" ${page === pages ? "disabled" : ""}>Next</button></div>` : ""}`;
  }

  function draw(main) {
    const body = main.querySelector("#ex-body");
    body.innerHTML = resultsHtml();
    const type = main.querySelector("#ex-type");
    if (data?.types) {
      type.innerHTML = `<option value="">All types</option>` + data.types.map((t) => `<option value="${esc(t.type)}" ${t.type === state.type ? "selected" : ""}>${esc(t.title)} (${t.count.toLocaleString()})</option>`).join("");
    }
    body.querySelectorAll(".ex-result").forEach((button) => button.addEventListener("click", () => {
      body.querySelector(".ex-result.active")?.classList.remove("active");
      button.classList.add("active");
      open(main, data.results[+button.dataset.i]);
    }));
    body.querySelectorAll("[data-state]").forEach((link) => link.addEventListener("click", (event) => {
      event.stopPropagation();
      show(main, `/api/architecture/download/state/${link.dataset.state}`, `${link.dataset.state} all resources`);
    }));
    body.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => {
      state.offset = Math.max(0, state.offset + PAGE * Number(button.dataset.page));
      load(main);
      main.scrollTop = 0;
    }));
  }

  async function load(main) {
    const mine = ++sequence;
    const query = new URLSearchParams({q: state.q, type: state.type, limit: PAGE, offset: state.offset});
    try {
      const answer = await api(`/explore/search?${query}`);
      if (mine !== sequence || !main.querySelector("#ex-body")) return;
      data = answer;
      draw(main);
    } catch (error) {
      if (main.querySelector("#ex-body")) main.querySelector("#ex-body").innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
    }
  }

  // The right side is the JSON Visualizer itself, so details get all its views and tools.
  function open(main, result) {
    const path = `/api/architecture/explore/resource?${new URLSearchParams({file: result.file, index: result.index})}`;
    show(main, path, `${result.type_title} ${result.name || String(result.id).split("/").pop()}`);
  }

  function show(main, path, name) {
    main.querySelector(".ex-split").classList.add("open");
    const frame = main.querySelector("#ex-viewer");
    if (viewerReady) {
      frame.contentWindow.postMessage({type: "owl-open", path, name}, location.origin);
    } else if (!frame.getAttribute("src")) {
      frame.src = `../json-visualizer/?embed=1&${new URLSearchParams({open: path, name})}`;
    } else {
      pending.push({path, name});
    }
  }

  function onViewerReady(event) {
    if (event.origin !== location.origin || event.data?.type !== "owl-viewer-ready") return;
    viewerReady = true;
    const frame = document.querySelector("#ex-viewer");
    pending.splice(0).forEach((item) => frame?.contentWindow.postMessage({type: "owl-open", ...item}, location.origin));
  }

  return function explore(main) {
    main.innerHTML = `<div class="page-head"><div><h1>Search AWS</h1><p>Everything in your downloads, best matches first.</p></div></div>
      <div class="filters ex-bar"><input type="search" id="ex-q" placeholder="Search anything: name, id, IP, CIDR, ARN, tag, DNS name…" value="${esc(state.q)}" autocomplete="off" spellcheck="false">
        <select id="ex-type" aria-label="Resource type"><option value="">All types</option></select></div>
      <div class="ex-split"><div id="ex-body"></div><iframe id="ex-viewer" title="Details in the JSON Visualizer"></iframe></div>`;
    viewerReady = false;
    pending.length = 0;
    window.removeEventListener("message", onViewerReady);
    window.addEventListener("message", onViewerReady);
    const box = main.querySelector("#ex-q");
    const run = () => {
      clearTimeout(typing);
      state.q = box.value;
      state.offset = 0;
      load(main);
    };
    box.addEventListener("input", () => {
      clearTimeout(typing);
      typing = setTimeout(run, box.value.trim() ? 1000 : 0);
    });
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter") run();
    });
    main.querySelector("#ex-type").addEventListener("change", (event) => {
      state.type = event.target.value;
      state.offset = 0;
      load(main);
    });
    load(main);
    box.focus();
  };
})();
