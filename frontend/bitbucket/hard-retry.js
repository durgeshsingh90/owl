"use strict";
(() => {
  const dialog = document.querySelector("#hard-retry-dialog");
  const input = document.querySelector("#hard-retry-confirmation");
  const start = document.querySelector("#hard-retry-start");
  const error = document.querySelector("#hard-retry-error");
  let ready = false;
  input.oninput = () => {start.disabled = !ready || input.value !== "HARD RETRY";};
  document.querySelector("#hard-retry-cancel").onclick = () => dialog.close();
  document.querySelector("#hard-retry-open").onclick = async () => {
    ready = false; start.disabled = true; input.value = ""; error.textContent = "";
    const scope = document.querySelector("#hard-retry-scope");
    scope.textContent = "Loading the data that will be cleared…";
    dialog.showModal();
    try {
      const data = await crawlJson("/api/crawl/hard-retry/preview");
      scope.textContent = `${data.server}: ${data.projects.map(p => p.project).join(", ") || "No tracked projects"}. ${data.repositories} currently saved repositories, ${data.documents} PDF records and ${data.failed_documents} failed-PDF records. All repositories in these projects will be rescanned, regardless of sidebar selection.`;
      ready = !data.active && data.projects.length > 0;
      if (data.active) error.textContent = "Wait for the active crawl to finish or stop it first.";
      input.oninput();
    } catch (failure) {error.textContent = failure.message;}
  };
  start.onclick = async () => {
    if (!ready || input.value !== "HARD RETRY") return;
    ready = false; start.disabled = true; error.textContent = "Backing up and starting a fresh crawl…";
    try {
      // Backup time depends on database size; do not time out while deletion is starting.
      const response = await fetch("/api/crawl/hard-retry", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({confirmation:input.value})});
      const job = await response.json();
      if (!response.ok) throw new Error(typeof job.detail === "string" ? job.detail : "Hard retry failed.");
      dialog.close();
      watchCrawl(job);
      await loadDatabaseWorkspace();
    } catch (failure) {error.textContent = failure.message + " Close and reopen this dialog to check the current state before trying again.";}
  };
})();
