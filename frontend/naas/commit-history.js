"use strict";
(() => {
  const dialog = document.createElement("dialog");
  dialog.className = "commit-history-dialog";
  dialog.innerHTML = '<header><h2>Commit history</h2><button type="button" aria-label="Close commit history">✕</button></header><p class="history-status" role="status"></p><button type="button" class="history-download-all" hidden>Download all (ZIP)</button><ol class="history-list"></ol><section class="history-prs"></section>';
  document.body.append(dialog);
  const heading = dialog.querySelector("h2");
  const status = dialog.querySelector(".history-status");
  const list = dialog.querySelector("ol");
  const prs = dialog.querySelector(".history-prs");
  const dateText = value => typeof value === "number" ? new Date(value).toLocaleString() : "Unavailable";
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
  function textNode(tag, text, parent) {
    const node = document.createElement(tag);
    node.textContent = text;
    parent.append(node);
    return node;
  }
  async function loadPrs(docId, controller, commitRows) {
    textNode("h3", "Unique PR details", prs);
    const progress = textNode("p", "Loading commit-to-PR associations and review activity…", prs);
    try {
      const response = await fetch('/naas/api/document/' + encodeURIComponent(docId) + '/pull-requests', {signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to load PR details.");
      if (request !== controller) return;
      progress.textContent = data.pull_requests.length + " unique PRs";
      const byKey = new Map(data.pull_requests.map(pr => [pr.key, pr]));
      for (const [id, row] of commitRows) {
        const keys = data.commits[id];
        row.textContent = keys == null ? "PR lookup unavailable" : keys.length ? "PR(s): " : "PR(s): None";
        for (const key of keys || []) {
          const pr = byKey.get(key);
          const link = textNode("a", "#" + (pr?.id || key) + " ", row);
          link.href = "#history-pr-" + encodeURIComponent(key);
          link.onclick = event => {
            event.preventDefault();
            document.getElementById("history-pr-" + encodeURIComponent(key))?.scrollIntoView({block:"start", behavior:"smooth"});
          };
        }
      }
      const groups = new Map();
      for (const [id, row] of commitRows) {
        const keys = data.commits[id];
        const memberships = keys == null ? ["__unavailable"] : keys.length ? keys : ["__none"];
        for (const key of memberships) {
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push(row.parentElement);
        }
      }
      const grouped = document.createDocumentFragment();
      for (const [key, items] of groups) {
        const group = document.createElement("li");
        group.className = "history-pr-group";
        const section = document.createElement("details");
        section.open = true;
        group.append(section);
        const pr = byKey.get(key);
        const label = key === "__none" ? "No associated PR" : key === "__unavailable" ? "PR lookup unavailable" :
          "PR #" + (pr?.id || key) + (pr?.title ? " — " + pr.title : "");
        textNode("summary", label + " · " + items.length + (items.length === 1 ? " commit" : " commits"), section);
        const commits = document.createElement("ol");
        section.append(commits);
        for (const original of items) {
          const copy = original.cloneNode(true);
          // A commit can belong to multiple PRs; preserve actions on each copy.
          const originals = original.querySelectorAll("button, a");
          copy.querySelectorAll("button, a").forEach((action, index) => {action.onclick = originals[index].onclick;});
          commits.append(copy);
        }
        grouped.append(group);
      }
      list.replaceChildren(grouped);
      list.classList.add("is-grouped");
      for (const error of data.errors) textNode("p", error, prs);
      for (const pr of data.pull_requests) {
        const card = document.createElement("article");
        card.id = "history-pr-" + encodeURIComponent(pr.key);
        prs.append(card);
        textNode("h4", "PR #" + pr.id + (pr.title ? " — " + pr.title : ""), card);
        const link = textNode("a", pr.url, card);
        if (/^https?:\/\//i.test(pr.url)) {
          link.href = pr.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        }
        if (pr.error) {textNode("p", "Details unavailable: " + pr.error, card); continue;}
        textNode("p", "State: " + pr.state + " · Author: " + pr.author, card);
        textNode("p", "Created: " + dateText(pr.created) + " · Updated: " + dateText(pr.updated) + " · Merged: " + dateText(pr.merged), card);
        if (pr.description) textNode("p", pr.description, card).className = "history-message";
        for (const [title, matches] of [
          ["Approved reviewers", r => r.status === "APPROVED"],
          ["Changes requested", r => r.status === "NEEDS_WORK"],
          ["Not approved / pending reviewers", r => !["APPROVED", "NEEDS_WORK"].includes(r.status)]
        ]) {
          textNode("h5", title, card);
          const group = pr.reviewers.filter(matches);
          textNode("p", group.length ? group.map(r => r.name).join(", ") : "None", card);
        }
        textNode("h5", "Activity history — dates and times", card);
        if (pr.activity_error) textNode("p", "Activity unavailable: " + pr.activity_error, card);
        else if (!pr.activities.length) textNode("p", "No activity returned.", card);
        for (const activity of pr.activities) {
          textNode("p", dateText(activity.timestamp) + " · " + activity.user + " · " + activity.action +
            (activity.message ? "\n" + activity.message : ""), card).className = "history-message";
        }
      }
    } catch (error) {
      if (error.name !== "AbortError" && request === controller) {
        progress.textContent = error.message;
        for (const row of commitRows.values()) row.textContent = "PR lookup unavailable";
        const retry = textNode("button", "Retry PR lookup", prs);
        retry.type = "button";
        retry.onclick = () => {prs.replaceChildren(); loadPrs(docId, controller, commitRows);};
      }
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
    list.classList.remove("is-grouped");
    prs.replaceChildren();
    downloadAll.hidden = true;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch('/naas/api/document/' + encodeURIComponent(button.dataset.commitHistory) + '/commits', {signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to load commit history.");
      if (request !== controller) return;
      heading.textContent = data.name + " — Commit history";
      status.textContent = data.commits.length + " commits · Messages shown below are commit messages.";
      if (!data.commits.length) status.textContent = "No commits found for this file path.";
      const downloadUrl = '/naas/api/document/' + encodeURIComponent(button.dataset.commitHistory) + '/commits/download';
      downloadAll.hidden = !data.commits.length;
      downloadAll.onclick = () => download(downloadAll, downloadUrl);
      const commitRows = new Map();
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
        downloadButton.textContent = "Download File";
        downloadButton.onclick = () => download(downloadButton, downloadUrl + '?commit_id=' + encodeURIComponent(commit.id));
        item.append(id, metadata, message, downloadButton);
        const prRow = textNode("p", "PR(s): Loading…", item);
        commitRows.set(commit.id, prRow);
        list.append(item);
      }
      loadPrs(button.dataset.commitHistory, controller, commitRows);
    } catch (error) {
      if (error.name !== "AbortError" && request === controller) status.textContent = error.message;
    }
  });
})();
