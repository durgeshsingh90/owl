"use strict";
(() => {
  const dialog = document.querySelector("#failed-pdfs-dialog");
  const status = document.querySelector("#failed-pdfs-status");
  const rows = document.querySelector("#failed-pdfs-rows");
  const retry = document.querySelector("#failed-pdfs-retry");
  let offset = 0, loading = false;
  async function load() {
    if (loading) return;
    loading = true;
    retry.disabled = true;
    status.textContent = "Loading failures from the database…";
    try {
      const failures = await crawlJson(`/api/failed?limit=100&offset=${offset}`);
      rows.replaceChildren();
      const folderList = document.querySelector("#failed-folders-list");
      folderList.replaceChildren();
      const {job} = await crawlJson("/api/jobs/latest");
      for (const failure of job?.folder_failures || []) {
        const item = document.createElement("li");
        item.textContent = `${failure.project} / ${failure.repo} / ${failure.path}: ${failure.error}`;
        folderList.append(item);
      }
      document.querySelector("#failed-folders-section").hidden = !folderList.children.length;
      for (const failure of failures) {
        const row = document.createElement("tr");
        for (const key of ["project", "repo", "pdf_name", "url", "path", "error", "attempts", "last_attempt"]) {
          const cell = document.createElement("td");
          cell.textContent = failure[key] || "Unknown";
          if (key === "url" && /^https?:\/\//i.test(failure.url || "")) {
            const link = document.createElement("a");
            link.href = failure.url;
            link.textContent = failure.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.style.color = "#93c5fd";
            const copy = document.createElement("button");
            copy.type = "button";
            copy.textContent = "Copy URL";
            copy.onclick = async () => {
              try {await copyText(failure.url); showToast("Failed PDF URL copied");}
              catch {showToast("Unable to copy URL", false);}
            };
            cell.replaceChildren(link, document.createElement("br"), copy);
          }
          cell.style.cssText = "padding:8px;overflow-wrap:anywhere;white-space:normal";
          row.append(cell);
        }
        rows.append(row);
      }
      status.textContent = failures.length ? `Showing ${offset + 1}–${offset + failures.length}. ${pullProgress.active ? "Wait for the crawl to finish before retrying." : "Ready to retry."}` : "No failed PDFs on this page.";
      document.querySelector("#failed-pdfs-prev").disabled = offset === 0;
      document.querySelector("#failed-pdfs-next").disabled = failures.length < 100;
      retry.disabled = pullProgress.active || !failures.length;
    } catch (error) { status.textContent = error.message; }
    finally { loading = false; }
  }
  document.querySelector("#show-failed-pdfs").onclick = () => {offset = 0; dialog.showModal(); void load();};
  document.querySelector("#failed-pdfs-close").onclick = () => dialog.close();
  document.querySelector("#failed-pdfs-refresh").onclick = load;
  document.querySelector("#failed-pdfs-prev").onclick = () => {if (!loading) {offset = Math.max(0, offset - 100); void load();}};
  document.querySelector("#failed-pdfs-next").onclick = () => {if (!loading) {offset += 100; void load();}};
  const directRetry = document.querySelector("#retry-failed-pdfs");
  async function retryFailedPdfs() {
    if (pullProgress.active) {showToast("Wait for the current crawl to finish before retrying failed PDFs."); return;}
    retry.disabled = true;
    directRetry.disabled = true;
    try {
      const job = await crawlJson("/api/failed/retry", {});
      dialog.close();
      watchCrawl(job);
    } catch (error) {
      status.textContent = error.message;
      showToast(error.message);
    } finally {
      retry.disabled = pullProgress.active;
      directRetry.disabled = pullProgress.active;
    }
  }
  retry.onclick = retryFailedPdfs;
  directRetry.onclick = retryFailedPdfs;
  window.addEventListener("owl-crawl-finished", () => {if (dialog.open) void load();});
})();
