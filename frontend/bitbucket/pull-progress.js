"use strict";
const pullProgress = {active: false, completed: new Set(), failed: new Set(), timer: null, repositories: new Map(), found: new Map(), processed: new Map(), failedCounts: new Map()};
pullProgress.timings = new Map();
function pullRepoMark(projectId, repoName) {
  const status = pullProgress.repositories.get(JSON.stringify([String(projectId), repoName]));
  if (!pullProgress.active || status !== "succeeded") return "";
  return '<span class="repo-job-status repo-job-succeeded" title="Completed in this sync" aria-label="Completed in this sync">✓</span>';
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
function canResumeCrawl(job) {
  const checkpoint = job.checkpoint;
  if (!checkpoint || job.resumed_by || !["cancelled", "interrupted", "failed", "paused"].includes(job.status)) return false;
  if (!checkpoint.enumeration_complete) return true;
  return (checkpoint.repos || []).some(([project, repo, id]) => {
    if (checkpoint.finished.includes(String(id))) return false;
    const inventory = checkpoint.inventories[String(id)];
    return inventory == null || inventory.some(path => !(checkpoint.successful[String(id)] || []).includes(path)
      || JSON.stringify(checkpoint.active_pdf) === JSON.stringify([project, repo, path]));
  });
}
function watchCrawl(job) {
  let lastWorkspaceRefresh = 0;
  pullProgress.active = ["queued", "running", "paused"].includes(job.status);
  clearTimeout(pullProgress.timer);
  pullProgress.jobId = job.id;
  const controls = document.querySelector("#crawl-controls");
  const stop = document.querySelector("#crawl-stop");
  const pause = document.querySelector("#crawl-pause");
  pause.onclick = async () => {
    pause.disabled = true;
    try { await crawlJson(`/api/jobs/${job.id}/pause`, {}); }
    catch(error) { showToast(error.message); }
    finally { pause.disabled = false; }
  };
  const resume = document.querySelector("#crawl-resume");
  resume.hidden = !canResumeCrawl(job);
  resume.disabled = false;
  resume.onclick = async () => {
    resume.disabled = true;
    try {
      const resumed = await crawlJson(`/api/jobs/${job.id}/resume`, {});
      watchCrawl(resumed);
    } catch (error) { resume.disabled = false; showToast(error.message); }
  };
  controls.hidden = false;
  stop.hidden = !["queued", "running"].includes(job.status);
  stop.disabled = false;
  stop.onclick = async () => {
    stop.disabled = true;
    try { await crawlJson(`/api/jobs/${job.id}/cancel`, {}); }
    catch (error) { showToast(error.message); }
    finally { stop.disabled = false; }
  };
  document.querySelector(".repository-pull-summary").hidden = false;
  let lastStatuses = "";
  let lastProcessed = -1;
  let lastRepositories = -1;
  let lastRecovered = -1;
  async function poll() {
    try {
      const current = await crawlJson(`/api/jobs/${job.id}`);
      const running = ["queued", "running"].includes(current.status);
      const paused = current.status === "paused";
      pullProgress.active = running || paused;
      pause.hidden = !running;
      const elapsed = running && current.started_at ? Math.max(current.elapsed_seconds || 0, (Date.now() - Date.parse(current.started_at)) / 1000 - (current.paused_seconds || 0)) : current.elapsed_seconds;
      const eta = current.eta_seconds == null ? null : Math.max(current.eta_seconds ? 1 : 0, current.eta_seconds - (current.eta_updated_at ? (Date.now() - Date.parse(current.eta_updated_at)) / 1000 : 0));
      document.querySelector("#crawl-eta").textContent = paused ? "ETA paused" : current.eta_seconds == null ? (running ? "ETA calculating…" : "") : `ETA ${formatEta(eta)}`;
      document.querySelector("#crawl-elapsed").textContent = formatEta(elapsed);
      const totalRepos = Math.max(0, Number(current.repositories) || 0);
      const doneRepos = Math.min(totalRepos, Math.max(0, Number(current.repositories_done) || 0));
      const percentage = totalRepos ? Math.floor(doneRepos / totalRepos * 100) : 0;
      const progress = document.querySelector("#crawl-percentage");
      document.querySelector("#crawl-repository-count").textContent = `${doneRepos}/${totalRepos}`;
      progress.textContent = `${percentage}%`;
      progress.title = `${doneRepos}/${totalRepos} repositories finished (including failures and empty repositories)`;
      progress.setAttribute("aria-label", `${percentage}% complete: ${doneRepos} of ${totalRepos} repositories finished`);
      stop.hidden = !running && !paused;
      resume.hidden = !paused && !canResumeCrawl(current);
      resume.title = paused ? "Resume sync" : "Resume interrupted sync";
      resume.setAttribute("aria-label", resume.title);
      if (current.bitbucket_connected && (["queued", "running"].includes(current.status) || ["queued", "running"].includes(job.status))) {
        setConnectionStatus("connected", "Bitbucket responded successfully. Background indexing is running.");
      }
      updatePullSummary(current.status.replaceAll("_", " "));
      document.querySelector("#repository-pull-new").textContent = current.new;
      const unchangedCount = Number(current.unchanged) || 0;
      const failedCount = (Number(current.failed) || 0) + (Number(current.repositories_failed) || 0);
      for (const [selector, count] of [["#repository-pull-unchanged", unchangedCount], ["#repository-pull-failed", failedCount]]) {
        const element = document.querySelector(selector);
        element.textContent = count;
        element.parentElement.hidden = count <= 1;
      }
      document.querySelector("#repository-pull-updating").textContent = current.updated;
      const statuses = JSON.stringify(current.repository_statuses || {});
      const statusesChanged = statuses !== lastStatuses;
      if (statusesChanged) {
        lastStatuses = statuses;
        pullProgress.timings = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo]));
        pullProgress.failedCounts = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.failed]));
        pullProgress.processed = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.processed]));
        pullProgress.found = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.found]));
        pullProgress.repositories = new Map(Object.values(current.repository_statuses || {}).map(repo =>
          [JSON.stringify([String(repo.project_id), repo.repo]), repo.status]));
      }
      if (!current.background && (!lastWorkspaceRefresh || Date.now() - lastWorkspaceRefresh >= 5000) && (statusesChanged || current.processed !== lastProcessed || current.repositories !== lastRepositories || (current.retry_recovered || 0) !== lastRecovered)) {
        lastWorkspaceRefresh = Date.now();
        lastRecovered = current.retry_recovered || 0;
        lastProcessed = current.processed;
        lastRepositories = current.repositories;
        await loadDatabaseWorkspace();
      }
      if (!["queued", "running", "paused"].includes(current.status)) {
        pullProgress.active = false;
        window.dispatchEvent(new Event("owl-crawl-finished"));
        pullProgress.jobId = null;
        if (!current.background) await loadDatabaseWorkspace();
        else {
          if (current.status === "succeeded") {
            window.workspaceLastPull = current.completed_at;
            const label = document.querySelector("#shared-last-pull");
            label.hidden = false;
            label.textContent = `Last Git pull: ${formatLastPull(current.completed_at)} · 0 days ago`;
          }
          updatePullSummary(current.status === "succeeded" ? "Background sync complete · refresh to see updates" : "Background sync will retry in two hours");
        }
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
  updateSelectionHeader();
  try {
    const selected = selectedRepositories();
    const scope = selected.length ? {repository_ids: selected.map(repo => Number(repo.id))} : {project_ids: targetProjects.map(project => Number(project.id))};
    const job = await crawlJson("/api/crawl", scope);
    watchCrawl(job);
  } catch (error) { pullProgress.active = false; updateSelectionHeader(); showToast(error.message); }
}
async function reconnectCrawl() {
  if (pullProgress.active) return;
  try {
    const {job} = await crawlJson("/api/jobs/latest");
    if (job && job.id !== pullProgress.dismissedId && job.id !== pullProgress.lastSeenId) {
      pullProgress.lastSeenId = job.id;
      watchCrawl(job);
    }
  } catch (error) { updatePullSummary(`Cannot load crawl status: ${error.message}`); }
}
window.addEventListener("load", reconnectCrawl);
window.addEventListener("focus", reconnectCrawl);

// Discover scheduled work quietly while this tab stays open.
setInterval(reconnectCrawl, 60000);
