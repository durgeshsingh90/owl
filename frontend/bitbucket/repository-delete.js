"use strict";
(() => {
  const dialog = document.querySelector("#delete-repo-dialog");
  const form = document.querySelector("#delete-repo-form");
  const unlock = document.querySelector("#delete-repo-unlock");
  const name = document.querySelector("#delete-repo-name");
  const confirm = document.querySelector("#delete-repo-confirm");
  const feedback = document.querySelector("#delete-repo-feedback");
  let copyFeedbackTimer;
  let target = null;
  let busy = false;
  let generation = 0;

  function updateLock() {
    name.disabled = !unlock.checked || busy;
    confirm.disabled =
      !target || !unlock.checked || name.value !== "delete all" || busy;
  }

  document
    .querySelector("#delete-selected-repo")
    .addEventListener("click", () => {
      if (state.selectedRepos.size !== 1) return;
      const [projectId, repoName] = JSON.parse([...state.selectedRepos][0]);
      const project = findProject(projectId);
      const repo = findRepository(projectId, repoName);
      if (!project || !repo) return;
      target = { project, repo };
      form.reset();
      busy = false;
      generation += 1;
      feedback.textContent = "Deletion is locked.";
      document.querySelector("#delete-repo-description").textContent =
        `${repo.name} — ${repo.baseUrl}`;
      updateLock();
      dialog.showModal();
    });
  document
    .querySelector("#copy-delete-phrase")
    .addEventListener("click", async () => {
      const status = document.querySelector("#delete-phrase-feedback");
      clearTimeout(copyFeedbackTimer);
      try {
        await copyText("delete all");
        status.textContent = "Copied";
        copyFeedbackTimer = setTimeout(() => {
          status.textContent = "";
        }, 1000);
      } catch {
        status.textContent = "Unable to copy. Type delete all below.";
      }
    });
  unlock.addEventListener("change", () => {
    if (!unlock.checked) name.value = "";
    feedback.textContent = unlock.checked
      ? "Type delete all in lowercase to enable deletion for this repository."
      : "Deletion is locked.";
    updateLock();
    if (unlock.checked) name.focus();
  });
  name.addEventListener("input", updateLock);
  document
    .querySelector("#delete-repo-cancel")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => {
    if (busy) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    clearTimeout(copyFeedbackTimer);
    document.querySelector("#delete-phrase-feedback").textContent = "";
    form.reset();
    target = null;
    generation += 1;
    updateLock();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (confirm.disabled || !target) return;
    const selected = target;
    const currentGeneration = generation;
    busy = true;
    updateLock();
    unlock.disabled = true;
    document.querySelector("#delete-repo-cancel").disabled = true;
    feedback.textContent = "Checking saved repository…";
    try {
      const workspace = await connectionJson(
        document.querySelector("#connection-status").dataset.workspaceUrl,
        { signal: AbortSignal.timeout(10000) },
      );
      const normalize = (url) => url.replace(/\/$/, "").replace(/\.git$/, "");
      const saved = workspace.repositories?.find(
        (repo) => normalize(repo.url) === normalize(selected.repo.baseUrl),
      );
      if (!saved)
        throw new Error(
          "This sample repository is not saved in OWL. No repository was deleted.",
        );
      if (!saved.deleteUrl || !saved.canonicalUrl)
        throw new Error(
          "Restart the OWL backend to enable repository deletion.",
        );
      const endpoint = new URL(saved.deleteUrl, window.location.href);
      if (endpoint.origin !== window.location.origin)
        throw new Error("Invalid repository deletion endpoint.");
      feedback.textContent = "Deleting repository…";
      await connectionJson(endpoint, {
        method: "POST",
        signal: AbortSignal.timeout(10000),
        headers: { "X-CSRFToken": workspace.csrfToken },
        body: new URLSearchParams({
          unlocked: "yes",
          confirm_phrase: name.value,
          confirm_url: saved.canonicalUrl,
        }),
      });
      selected.project.repos = selected.project.repos.filter(
        (repo) => repo !== selected.repo,
      );
      state.selectedRepos.delete(
        repositoryKey(selected.project.id, selected.repo.name),
      );
      for (const collection of [pdfs, people]) {
        for (let index = collection.length - 1; index >= 0; index -= 1) {
          if (
            collection[index].projectId === selected.project.id &&
            collection[index].repo === selected.repo.name
          )
            collection.splice(index, 1);
        }
      }
      state.selectedPdf = null;
      state.currentPage = 1;
      renderApp({ resetScroll: true });
      dialog.close();
      showToast(`${selected.repo.name} removed from OWL.`);
    } catch (error) {
      if (generation === currentGeneration) {
        feedback.textContent = error.message;
        unlock.checked = false;
        name.value = "";
      }
    } finally {
      busy = false;
      unlock.disabled = false;
      document.querySelector("#delete-repo-cancel").disabled = false;
      updateLock();
    }
  });
})();
