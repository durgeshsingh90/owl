"use strict";
(() => {
  const dialog = document.createElement("dialog");
  dialog.className = "commit-history-dialog";
  dialog.innerHTML = '<header><h2>Commit history</h2><button type="button" aria-label="Close commit history">✕</button></header><p class="history-status" role="status"></p><button type="button" class="history-download-all" hidden>Download all (ZIP)</button><ol class="history-list"></ol>';
  document.body.append(dialog);
  const heading = dialog.querySelector("h2");
  const status = dialog.querySelector(".history-status");
  const list = dialog.querySelector("ol");
  const downloadAll = dialog.querySelector(".history-download-all");
  async function download(button, url) {
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "Preparing download…";
    try {
      const response = await fetch(url);
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Download failed.");
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      const name = response.headers.get("Content-Disposition")?.split("filename*=UTF-8''")[1];
      link.download = name ? decodeURIComponent(name) : "versions.zip";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }
  let request;
  dialog.querySelector("button").onclick = () => dialog.close();
  dialog.addEventListener("close", () => request?.abort());
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-commit-history]");
    if (!button) return;
    event.preventDefault();
    request?.abort();
    const controller = new AbortController();
    request = controller;
    heading.textContent = "Commit history";
    status.textContent = "Loading commit history…";
    list.replaceChildren();
    downloadAll.hidden = true;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch('/api/document/' + encodeURIComponent(button.dataset.commitHistory) + '/commits', {signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to load commit history.");
      if (request !== controller) return;
      heading.textContent = data.name + " — Commit history";
      status.textContent = data.commits.length + " commits · Messages shown below are commit messages.";
      if (!data.commits.length) status.textContent = "No commits found for this file path.";
      const downloadUrl = '/api/document/' + encodeURIComponent(button.dataset.commitHistory) + '/commits/download';
      downloadAll.hidden = !data.commits.length;
      downloadAll.onclick = () => download(downloadAll, downloadUrl);
      for (const commit of data.commits) {
        const item = document.createElement("li");
        const id = document.createElement("code");
        id.textContent = commit.id;
        const metadata = document.createElement("p");
        const date = typeof commit.timestamp === "number" ? new Date(commit.timestamp).toLocaleString() : "Date unavailable";
        metadata.textContent = commit.author + " · " + date;
        const message = document.createElement("p");
        message.className = "history-message";
        message.textContent = commit.message || "No commit message.";
        const downloadButton = document.createElement("button");
        downloadButton.type = "button";
        downloadButton.textContent = "Download PDF";
        downloadButton.onclick = () => download(downloadButton, downloadUrl + '?commit_id=' + encodeURIComponent(commit.id));
        item.append(id, metadata, message, downloadButton);
        list.append(item);
      }
    } catch (error) {
      if (error.name !== "AbortError" && request === controller) status.textContent = error.message;
    }
  });
})();
