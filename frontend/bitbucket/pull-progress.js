"use strict";
const pullProgress = {active: false, completed: new Set(), failed: new Set(), timer: null};
function pullRepoMark() { return ""; }
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
  let lastProcessed = -1;
  let lastRepositories = -1;
  async function poll() {
    try {
      const current = await crawlJson(`/api/jobs/${job.id}`);
      document.querySelector("#pull-progress-state").textContent = current.detail;
      document.querySelector("#pull-elapsed").textContent = `${Math.round(current.elapsed_seconds)}s`;
      document.querySelector("#pull-progress-counts").textContent = `${current.repositories_done}/${current.repositories} repositories · ${current.processed}/${current.found} PDFs processed · ${current.updated} updated`;
      document.querySelector("#pull-eta").textContent = current.eta_seconds == null ? "Discovering files / ETA unavailable" : `ETA ${current.eta_seconds}s`;
      updatePullSummary(current.status.replaceAll("_", " "));
      document.querySelector("#repository-pull-new").textContent = current.new;
      document.querySelector("#repository-pull-unchanged").textContent = current.unchanged;
      document.querySelector("#repository-pull-failed").textContent = current.failed + current.repositories_failed;
      document.querySelector("#repository-pull-updating").textContent = current.updated;
      if (current.processed !== lastProcessed || current.repositories !== lastRepositories) {
        lastProcessed = current.processed;
        lastRepositories = current.repositories;
        await loadDatabaseWorkspace();
      }
      if (!["queued", "running"].includes(current.status)) {
        pullProgress.active = false;
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
