"use strict";
// Confluence Tracker additions on top of the Bitbucket explorer UI: the folder and page
// tree under each section, change badges and review, and the Confluence connection settings.

// ---- Folder and page tree -------------------------------------------------------------
const pageTree = {nodes: new Map(), roots: new Map()};
const TREE_LIMIT = 300;

window.buildWorkspaceTree = () => {
  pageTree.nodes.clear();
  for (const item of window.workspaceTree || []) {
    pageTree.nodes.set(`${item.projectId}:${item.pageId}`, {...item, children: []});
  }
  for (const node of pageTree.nodes.values()) {
    const parent = node.parentId && pageTree.nodes.get(`${node.projectId}:${node.parentId}`);
    if (parent && parent !== node) parent.children.push(node);
  }
  const byTitle = (a, b) => (b.children.length > 0) - (a.children.length > 0) ||
    a.title.localeCompare(b.title, undefined, {numeric: true, sensitivity: "base"});
  for (const node of pageTree.nodes.values()) node.children.sort(byTitle);
};

function descendantCount(node) {
  let count = 0;
  const stack = [...node.children];
  while (stack.length) { const next = stack.pop(); count++; stack.push(...next.children); }
  return count;
}

function treeMarkup(nodes, projectId, depth = 1) {
  const shown = nodes.slice(0, TREE_LIMIT);
  const selected = state.selectedFolder;
  return `<ul class="page-tree" role="group">${shown.map(node => {
    const key = `${projectId}:${node.pageId}`;
    const open = expandedTreeNodes.has(key);
    const isFolder = node.children.length > 0;
    const active = selected?.projectId === projectId && selected.pageId === node.pageId;
    return `<li style="--depth:${depth}">
      <div class="page-tree-row${active ? " active" : ""}">
        ${isFolder ? `<button type="button" class="tree-toggle" data-tree-toggle="${escapeHtml(key)}" aria-expanded="${open}" aria-label="${open ? "Collapse" : "Expand"} ${escapeHtml(node.title)}">${open ? "▾" : "▸"}</button>` : '<span class="tree-toggle-space"></span>'}
        <button type="button" class="page-tree-node" data-folder="${escapeHtml(node.pageId)}" data-project-id="${escapeHtml(projectId)}" data-title="${escapeHtml(node.title)}" aria-pressed="${active}" title="${escapeHtml(node.title)} · page ${escapeHtml(node.pageId)}${isFolder ? ` · ${descendantCount(node)} pages below` : ""}">
          <span class="page-tree-icon" aria-hidden="true">${isFolder ? (open ? "📂" : "📁") : "📄"}</span><span class="page-tree-title">${escapeHtml(node.title)}</span>${isFolder ? `<small>${formatNumber(descendantCount(node))}</small>` : ""}
        </button>
      </div>
      ${isFolder && open ? treeMarkup(node.children, projectId, depth + 1) : ""}
    </li>`;
  }).join("")}${nodes.length > TREE_LIMIT ? `<li class="page-tree-more" style="--depth:${depth}">+ ${formatNumber(nodes.length - TREE_LIMIT)} more pages · use search to find them</li>` : ""}</ul>`;
}

// Pages below a section: its sub-pages, or for the tree's own section the loose top-level pages.
window.sectionTree = (project, repo) => {
  const section = pageTree.nodes.get(`${project.id}:${repo.pageId}`);
  if (!section) return "";
  const children = section.pageId === project.rootPageId
    ? section.children.filter(child => child.section === repo.name)
    : section.children;
  return children.length ? treeMarkup(children, project.id) : "";
};

// ---- Change badges and review --------------------------------------------------------
const CHANGE_LABELS = {new: "New", updated: "Updated", returned: "Returned", missing: "Not in tree"};
window.changeBadge = pdf => {
  if (!pdf.unread || !CHANGE_LABELS[pdf.changeKind]) return "";
  return `<span class="change-badge ${escapeHtml(pdf.changeKind)}" title="${pdf.unread} unreviewed ${pdf.unread === 1 ? "change" : "changes"} since you last reviewed">${CHANGE_LABELS[pdf.changeKind]}</span>`;
};

function updateReviewControls() {
  const scope = state.selectedProject ? pdfs.filter(pdf => pdf.projectId === state.selectedProject) : pdfs;
  const unread = scope.filter(pdf => pdf.unread > 0).length;
  const button = document.querySelector("#review-changes");
  button.disabled = unread === 0;
  button.textContent = unread ? `Mark ${formatNumber(unread)} ${unread === 1 ? "change" : "changes"} reviewed` : "No unreviewed changes";
  button.title = state.selectedProject ? `Only ${findProject(state.selectedProject)?.name || "this tree"}` : "All tracked trees";
  document.querySelector("#unreviewed-only").checked = state.unreviewedOnly;
}
const baseRenderPdfTable = renderPdfTable;
renderPdfTable = function renderPdfTableWithReview() {
  baseRenderPdfTable();
  updateReviewControls();
};
document.querySelector("#unreviewed-only").addEventListener("change", event => {
  state.unreviewedOnly = event.target.checked;
  state.currentPage = 1;
  renderApp({resetScroll: true});
});
document.querySelector("#review-changes").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch("/api/confluence-library/review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({project_id: state.selectedProject ? Number(state.selectedProject) : null}),
    });
    if (!response.ok) throw new Error();
    const {reviewed} = await response.json();
    await loadDatabaseWorkspace();
    showToast(`${formatNumber(reviewed)} ${reviewed === 1 ? "change" : "changes"} marked reviewed`);
  } catch {
    showToast("Could not mark changes reviewed. Try again.", false);
    updateReviewControls();
  }
});

// ---- Confluence connection settings (shared with Bookmarks) ---------------------------
(() => {
  const dialog = document.querySelector("#settings-dialog");
  const form = document.querySelector("#settings-form");
  const baseUrl = document.querySelector("#settings-base-url");
  const verify = document.querySelector("#settings-verify-ssl");
  const token = document.querySelector("#settings-token");
  const feedback = document.querySelector("#settings-feedback");
  const buttons = [document.querySelector("#settings-save"), document.querySelector("#settings-test")];
  const message = (text, error = false) => { feedback.textContent = text; feedback.dataset.error = String(error); };
  const busy = value => buttons.forEach(button => { button.disabled = value; });
  async function send(url) {
    const body = new URLSearchParams({base_url: baseUrl.value.trim(), personal_access_token: token.value});
    if (verify.checked) body.set("verify_ssl", "on");
    const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "The Confluence server did not accept these settings.");
    return result;
  }
  document.querySelector("#settings-button").addEventListener("click", async () => {
    form.reset();
    message("Loading settings…");
    dialog.showModal();
    try {
      const response = await fetch("/bookmarks/settings/workspace/", {cache: "no-store"});
      const {configuration} = await response.json();
      baseUrl.value = configuration.baseUrl || "";
      verify.checked = configuration.verifySsl !== false;
      message(configuration.hasToken ? "Saved settings loaded. Leave the token blank to keep it." : "Enter your Confluence base URL and personal access token.");
    } catch {
      message("Cannot reach the OWL backend.", true);
    }
  });
  document.querySelector("#settings-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { token.value = ""; });
  document.querySelector("#settings-test").addEventListener("click", async () => {
    busy(true);
    message("Testing connection…");
    try { message((await send("/bookmarks/settings/test/")).detail || "Connection works."); }
    catch (error) { message(error.message, true); }
    finally { busy(false); }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    busy(true);
    message("Testing and saving…");
    try {
      await send("/bookmarks/settings/save/");
      token.value = "";
      message("Saved. Confluence Tracker and Bookmarks now use this connection.");
      void testConnection();
    } catch (error) { message(error.message, true); }
    finally { busy(false); }
  });
})();
if (window.workspaceTree?.length) { window.buildWorkspaceTree(); renderProjects(); }
