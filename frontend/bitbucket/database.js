"use strict";
async function loadDatabaseWorkspace() {
  try {
    const response = await fetch("/api/workspace", { cache: "no-store" });
    if (!response.ok) throw new Error("Unable to load saved repository data.");
    const data = await response.json();
    projects.splice(0, projects.length, ...data.projects);
    pdfs.splice(0, pdfs.length, ...data.documents);
    people.splice(0, people.length, ...data.people);
    renderApp();
  } catch (error) {
    showToast(error.message);
  }
}
void loadDatabaseWorkspace();
window.addEventListener("focus", loadDatabaseWorkspace);
document
  .querySelector("#pdf-table-body")
  .addEventListener("click", async (event) => {
    const link = event.target.closest("a");
    if (!link) return;
    const pdf = pdfs.find((item) => item.pdfUrl === link.href);
    if (!pdf) return;
    const response = await fetch(`/api/document/${pdf.id}/open`, {
      method: "POST",
    });
    if (response.ok) {
      pdf.openCount = (await response.json()).open_count;
      renderPdfTable();
    }
  });

(() => {
  const dialog = document.querySelector("#pdf-details-dialog");
  const status = document.querySelector("#pdf-details-status");
  const fields = document.querySelector("#pdf-details-fields");
  const text = document.querySelector("#pdf-details-text");
  let sequence = 0;
  document.querySelector("#pdf-details-close").onclick = () => dialog.close();
  dialog.addEventListener("close", () => { sequence++; });
  document.querySelector("#pdf-table-body").addEventListener("click", async event => {
    const button = event.target.closest("[data-pdf-details]");
    if (!button) return;
    const current = ++sequence;
    fields.replaceChildren();
    text.textContent = "";
    status.textContent = "Loading saved record…";
    dialog.showModal();
    try {
      const response = await fetch(`/api/document/${button.dataset.pdfDetails}`, {cache: "no-store"});
      if (!response.ok) throw new Error("Could not load the saved PDF record.");
      const record = await response.json();
      if (current !== sequence) return;
      const labels = {
        pdf_name: "PDF name", project: "Project", repo: "Repository", path: "Path",
        url: "Bitbucket URL", file_size: "File size (bytes)", page_count: "Pages",
        author: "Commit author", commit_id: "Commit ID", commit_message: "Commit message",
        commit_date: "Commit date", added_at: "Saved at", updated_at: "Updated at",
        last_scanned: "Last scanned", pdf_hash: "SHA-256", open_count: "Open count", notes: "Notes",
      };
      for (const [key, label] of Object.entries(labels)) {
        const title = document.createElement("dt");
        const value = document.createElement("dd");
        title.textContent = label;
        value.textContent = record[key] ?? "Not available";
        value.style.margin = "0";
        fields.append(title, value);
      }
      text.textContent = record.pdf_text || "No text was extracted from this PDF.";
      status.textContent = "Loaded from the database.";
    } catch (error) { if (current === sequence) status.textContent = error.message; }
  });
})();
