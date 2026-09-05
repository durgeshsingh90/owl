"use strict";
// Frontend-only preview. Replace timed progress with backend job events when available.
const pullProgress = {
  active: false,
  completed: new Set(),
  failed: new Set(),
  connectionState: null,
  timer: null,
};
function pullRepoMark(projectId, repoName) {
  return pullProgress.completed.has(repositoryKey(projectId, repoName))
    ? '<span class="pull-repo-done" role="img" aria-label="Completed in preview" title="Completed in preview">✓</span>'
    : "";
}
function updatePullSummary(message) {
  document.querySelector("#repository-pull-status").textContent = message;
  document.querySelector("#repository-pull-updating").textContent =
    pullProgress.active ? 1 : 0;
  // The preview has no backend outcomes: do not invent new/unchanged/failure counts.
  document.querySelector("#repository-pull-new").textContent = "—";
  document.querySelector("#repository-pull-unchanged").textContent = "—";
  document.querySelector("#repository-pull-failed").textContent = "—";
}
function startPullPreview(targetProjects = projects, operation = "Pull") {
  if (pullProgress.active) return;
  const repositories = targetProjects.flatMap((project) =>
    project.repos.map((repo) => ({
      key: repositoryKey(project.id, repo.name),
      files: repo.pdfCount,
    })),
  );
  if (!repositories.length) {
    showToast("No repositories to pull.", false);
    return;
  }
  const container = document.querySelector("#connection-status");
  pullProgress.connectionState = {
    status: container.dataset.state || "failed",
    detail: container.title,
  };
  pullProgress.active = true;
  pullProgress.completed.clear();
  pullProgress.failed.clear();
  document.querySelector(".repository-pull-summary").hidden = false;
  updatePullSummary(`${operation} preview · outcome counts await backend`);
  const started = performance.now();
  const durationPerRepository = 2000;
  const duration = repositories.length * durationPerRepository;
  const totalFiles = repositories.reduce((sum, repo) => sum + repo.files, 0);
  const panel = document.querySelector("#pull-progress");
  const stop = document.querySelector("#pull-stop");
  panel.hidden = false;
  stop.hidden = false;
  stop.textContent = "Stop";
  document.querySelector("#pull-progress-state").textContent =
    `${operation} preview · simulated`;
  container.dataset.state = "connecting";
  container.title = `${operation} preview — no backend operation`;
  document.querySelector("#connection-image").src = "assets/no-connection.gif";
  document.querySelector("#connection-label").textContent =
    `${operation} preview running`;
  const connectionButton = document.querySelector("#test-connection");
  connectionButton.disabled = true;
  connectionButton.title = `${operation} preview running`;
  connectionButton.setAttribute("aria-label", `${operation} preview running`);
  updateSelectionHeader();
  renderProjects();
  function formatSeconds(seconds) {
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }
  function finish(cancelled) {
    clearInterval(pullProgress.timer);
    pullProgress.active = false;
    document.querySelector("#pull-progress-state").textContent = cancelled
      ? `${operation} preview stopped`
      : `${operation} preview complete`;
    document.querySelector("#pull-eta").textContent =
      "No backend operation performed";
    stop.textContent = "Dismiss";
    const remaining =
      repositories.length -
      pullProgress.completed.size -
      pullProgress.failed.size;
    updatePullSummary(
      cancelled
        ? `Preview stopped · ${remaining} repositories unfinished`
        : `${operation} preview complete · no backend operation performed`,
    );
    const previous = pullProgress.connectionState;
    setConnectionStatus(
      connectionCheckRunning ? "connecting" : previous.status,
      previous.detail,
    );
    updateSelectionHeader();
  }
  function tick() {
    const elapsed = Math.min(performance.now() - started, duration);
    const done = Math.min(
      repositories.length,
      Math.floor(elapsed / durationPerRepository),
    );
    const completedFiles = repositories
      .slice(0, done)
      .reduce((sum, repo) => sum + repo.files, 0);
    const currentFiles =
      done < repositories.length
        ? Math.floor(
            repositories[done].files *
              ((elapsed % durationPerRepository) / durationPerRepository),
          )
        : 0;
    if (done !== pullProgress.completed.size) {
      repositories
        .slice(0, done)
        .forEach((repo) => pullProgress.completed.add(repo.key));
      renderProjects();
      updatePullSummary(
        `${operation} preview · ${done}/${repositories.length} repositories completed`,
      );
    }
    document.querySelector("#pull-elapsed").textContent = formatSeconds(
      Math.floor(elapsed / 1000),
    );
    document.querySelector("#pull-progress-counts").textContent =
      `${done}/${repositories.length} repos · ${formatNumber(completedFiles + currentFiles)}/${formatNumber(totalFiles)} files`;
    document.querySelector("#pull-eta").textContent =
      `ETA ${formatSeconds(Math.ceil((duration - elapsed) / 1000))}`;
    if (done === repositories.length) finish(false);
  }
  stop.onclick = () => {
    if (pullProgress.active) finish(true);
    else {
      stop.hidden = true;
      document.querySelector("#pull-progress-state").textContent =
        "Git pull · ready";
      document.querySelector("#pull-elapsed").textContent = "00:00";
      document.querySelector("#pull-progress-counts").textContent =
        "0 repositories · 0 files";
      document.querySelector("#pull-eta").textContent = "ETA —";
      pullProgress.completed.clear();
      pullProgress.failed.clear();
      document.querySelector(".repository-pull-summary").hidden = true;
      renderProjects();
    }
  };
  tick();
  pullProgress.timer = setInterval(tick, 250);
}
