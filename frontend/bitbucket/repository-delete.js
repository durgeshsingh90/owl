"use strict";
(() => {
  const dialog = document.querySelector("#delete-repo-dialog");
  const form = document.querySelector("#delete-repo-form");
  const unlock = document.querySelector("#delete-repo-unlock");
  const phrase = document.querySelector("#delete-repo-name");
  const confirm = document.querySelector("#delete-repo-confirm");
  const cancel = document.querySelector("#delete-repo-cancel");
  const feedback = document.querySelector("#delete-repo-feedback");
  let ids = [], ready = false, busy = false, generation = 0;
  function update() {
    phrase.disabled = !ready || !unlock.checked || busy;
    confirm.disabled = !ready || !unlock.checked || phrase.value !== "delete all" || busy;
    unlock.disabled = busy || !ready;
    cancel.disabled = busy;
  }
  document.querySelector("#delete-selected-repo").onclick = async () => {
    ids = selectedRepositories().map(repo => Number(repo.id));
    if (!ids.length) return;
    const current = ++generation;
    form.reset(); ready = false; busy = false; update();
    document.querySelector("#delete-repo-description").textContent = "Loading selected repositories…";
    feedback.textContent = "";
    dialog.showModal();
    try {
      const preview = await crawlJson("/api/repositories/delete-preview", {repository_ids:ids});
      if (current !== generation) return;
      document.querySelector("#delete-repo-description").textContent = `${preview.repositories.length} repositories: ${preview.repositories.map(repo => repo.project + "/" + repo.repo).join(", ")}. Deletes ${preview.documents} PDF records and ${preview.failed_documents} failed-PDF records.`;
      ready = true; feedback.textContent = "Unlock and type delete all to confirm this selection."; update();
    } catch (error) {if (current === generation) feedback.textContent = error.message;}
  };
  unlock.onchange = () => {if (!unlock.checked) phrase.value = ""; update(); if (unlock.checked) phrase.focus();};
  phrase.oninput = update;
  cancel.onclick = () => dialog.close();
  document.querySelector("#copy-delete-phrase").onclick = async () => {
    try {await copyText("delete all"); document.querySelector("#delete-phrase-feedback").textContent = "Copied";}
    catch {document.querySelector("#delete-phrase-feedback").textContent = "Type delete all below.";}
  };
  dialog.addEventListener("cancel", event => {if (busy) event.preventDefault();});
  dialog.addEventListener("close", () => {generation++; ready = false; form.reset(); document.querySelector("#delete-phrase-feedback").textContent = "";});
  form.onsubmit = async event => {
    event.preventDefault();
    if (confirm.disabled) return;
    busy = true; update(); feedback.textContent = "Deleting selected repositories and their indexed data…";
    try {
      const result = await crawlJson("/api/repositories/delete", {repository_ids:ids, confirmation:phrase.value});
      state.selectedRepos.clear(); state.selectedPdf = null; state.currentPage = 1;
      await loadDatabaseWorkspace();
      dialog.close(); showToast(`${result.deleted} repositories and their indexed data deleted.`);
    } catch (error) {feedback.textContent = error.message;}
    finally {busy = false; update();}
  };
})();

(() => {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "↶";
  button.title = "Excluded repositories";
  button.setAttribute("aria-label", "Excluded repositories");
  document.querySelector("#delete-selected-repo").after(button);
  const dialog = document.createElement("dialog");
  dialog.innerHTML = '<h2>Excluded repositories</h2><p>Only repository URLs are retained. Restore scans and downloads the entire repository again.</p><div class="excluded-repository-list"></div><p role="status"></p><button type="button">Close</button>';
  document.body.append(dialog);
  dialog.querySelector("button").onclick = () => dialog.close();
  const list = dialog.querySelector(".excluded-repository-list");
  const feedback = dialog.querySelector('[role="status"]');
  button.onclick = async () => {
    list.replaceChildren(); feedback.textContent = "Loading…"; dialog.showModal();
    try {
      const rows = await crawlJson("/api/excluded-repositories");
      feedback.textContent = rows.length ? "" : "No excluded repositories.";
      for (const row of rows) {
        const entry = document.createElement("p");
        const url = document.createElement("span"); url.textContent = row.url;
        url.style.overflowWrap = "anywhere";
        const restore = document.createElement("button");
        restore.type = "button"; restore.textContent = "Restore and crawl";
        restore.onclick = async () => {
          restore.disabled = true; feedback.textContent = "Starting fresh crawl…";
          try {
            const job = await crawlJson("/api/excluded-repositories/restore", {url:row.url});
            dialog.close(); watchCrawl(job); await loadDatabaseWorkspace();
          } catch (error) {feedback.textContent = error.message; restore.disabled = false;}
        };
        entry.append(url, " ", restore); list.append(entry);
      }
    } catch (error) {feedback.textContent = error.message;}
  };
})();
