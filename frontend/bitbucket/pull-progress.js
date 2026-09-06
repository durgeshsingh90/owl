"use strict";
const pullProgress = {active: false, completed: new Set(), failed: new Set(), timer: null, repositories: new Map(), found: new Map()};
function pullRepoMark(projectId, repoName) {
  const status = pullProgress.repositories.get(JSON.stringify([String(projectId), repoName]));
  const marks = {
    queued: ["◷", "Queued"], scanning: ["↻", "Scanning"],
    processing: ["↻", "Processing"], retrying: ["↻", "Retrying"],
    succeeded: ["✓", "Completed"], failed: ["!", "Has failures"], cancelled: ["–", "Stopped"],
  };
  if (!marks[status]) return "";
  const [icon, baseLabel] = marks[status];
  const found = pullProgress.found.get(JSON.stringify([String(projectId), repoName]));
  const label = found == null || status === "queued" ? baseLabel : `${baseLabel} · ${found} PDFs found`;
  return `<span class="repo-job-status repo-job-${status}" title="${label}" aria-label="${label}"><span aria-hidden="true">${icon}</span><span class="repo-job-label">${label}</span></span>`;
}
function formatEta(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const remainingSeconds = String(total % 60).padStart(2, "0");
  return `${hours}h ${minutes}m ${remainingSeconds}s`;
}
async function crawlJson(url, body) {
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Backend request failed.");
  return data;
}
function updatePullSummary(message) {
  document.querySelector("#repository-pull-status").textContent = message;
}
function watchCrawl(job) {
  pullProgress.active = true;
  clearTimeout(pullProgress.timer);
  pullProgress.jobId = job.id;
  const stop = document.querySelector("#pull-stop");
  document.querySelector("#pull-progress").hidden = false;
  document.querySelector(".repository-pull-summary").hidden = false;
  stop.hidden = false;
  stop.textContent = "Stop";
  stop.onclick = async () => {
    if (!pullProgress.active) {
      pullProgress.dismissedId = job.id;
      document.querySelector("#pull-progress").hidden = true;
      return;
    }
    stop.disabled = true;
    try { await crawlJson(`/api/jobs/${job.id}/cancel`, {}); }
    catch (error) { showToast(error.message); }
    finally { stop.disabled = false; }
  };
  let lastStatuses = "";
  let lastProcessed = -1;
  let lastRepositories = -1;
  let lastRecovered = -1;
  async function poll() {
    try {
      const current = await crawlJson(`/api/jobs/${job.id}`);
      if (current.bitbucket_connected && (["queued", "running"].includes(current.status) || ["queued", "running"].includes(job.status))) {
        setConnectionStatus("connected", "Bitbucket responded successfully. Background indexing is running.");
      }
      document.querySelector("#pull-progress-state").textContent = current.detail;
      document.querySelector("#pull-elapsed").textContent = `${Math.round(current.elapsed_seconds)}s`;
      document.querySelector("#pull-progress-counts").textContent = `Repositories ${current.repositories_succeeded ?? Math.max(0, current.repositories_done - current.repositories_failed)}/${current.repositories} successful · PDFs ${current.processed}/${current.found} processed${current.discovery_complete ? "" : " (found so far)"}${current.discovery_failed ? " (known files)" : ""}${current.retry_active ? ` · Retry ${current.retry_processed}/${current.retry_total}` : ""}`;
      document.querySelector("#pull-eta").textContent = !current.discovery_complete ? "Total ETA: discovering PDFs…" : current.eta_seconds == null ? "Total ETA: calculating…" : `Total ETA: ~${formatEta(current.eta_seconds)} remaining`;
      updatePullSummary(current.status.replaceAll("_", " "));
      document.querySelector("#repository-pull-new").textContent = current.new;
      document.querySelector("#repository-pull-unchanged").textContent = current.unchanged;
      document.querySelector("#repository-pull-failed").textContent = current.failed + current.repositories_failed;
      document.querySelector("#repository-pull-updating").textContent = current.updated;
      const statuses = JSON.stringify(current.repository_statuses || {});
      const statusesChanged = statuses !== lastStatuses;
      if (statusesChanged) {
        lastStatuses = statuses;
        pullProgress.found = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.found]));
        pullProgress.repositories = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.status]));
      }
      if (statusesChanged || current.processed !== lastProcessed || current.repositories !== lastRepositories || (current.retry_recovered || 0) !== lastRecovered) {
        lastRecovered = current.retry_recovered || 0;
        lastProcessed = current.processed;
        lastRepositories = current.repositories;
        await loadDatabaseWorkspace();
      }
      if (!["queued", "running"].includes(current.status)) {
        pullProgress.active = false;
        window.dispatchEvent(new Event("owl-crawl-finished"));
        pullProgress.jobId = null;
        stop.textContent = "Dismiss";
        document.querySelector("#pull-eta").textContent = current.status.replaceAll("_", " ");
        await loadDatabaseWorkspace();
        updateSelectionHeader();
        return;
      }
    } catch (error) {
      updatePullSummary(`Progress unavailable: ${error.message}. Retrying…`);
    }
    pullProgress.timer = setTimeout(poll, 1000);
  }
  updateSelectionHeader();
  void poll();
}
async function startPullPreview(targetProjects = projects) {
  if (pullProgress.active) return;
  pullProgress.active = true;
  try {
    const job = await crawlJson("/api/crawl", {project_ids: targetProjects.map(project => Number(project.id))});
    watchCrawl(job);
  } catch (error) { pullProgress.active = false; showToast(error.message); }
}
async function reconnectCrawl() {
  if (pullProgress.active) return;
  try {
    const {job} = await crawlJson("/api/jobs/latest");
    if (job && job.id !== pullProgress.dismissedId) watchCrawl(job);
  } catch (error) { updatePullSummary(`Cannot load crawl status: ${error.message}`); }
}
window.addEventListener("load", reconnectCrawl);
window.addEventListener("focus", reconnectCrawl);
