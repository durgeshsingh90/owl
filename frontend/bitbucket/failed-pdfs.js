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
      for (const failure of failures) {
        const row = document.createElement("tr");
        for (const key of ["project", "repo", "path", "error", "attempts", "last_attempt"]) {
          const cell = document.createElement("td");
          cell.textContent = failure[key] ?? "Unknown";
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
  retry.onclick = async () => {
    retry.disabled = true;
    try {
      const job = await crawlJson("/api/failed/retry", {});
      dialog.close();
      watchCrawl(job);
    } catch (error) {status.textContent = error.message; retry.disabled = pullProgress.active;}
  };
  window.addEventListener("owl-crawl-finished", () => {if (dialog.open) void load();});
})();
