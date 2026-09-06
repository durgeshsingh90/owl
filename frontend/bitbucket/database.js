"use strict";
let workspaceLoadVersion = 0;
async function loadDatabaseWorkspace() {
  const version = ++workspaceLoadVersion;
  try {
    const response = await fetch("/api/workspace", { cache: "no-store" });
    if (!response.ok) throw new Error("Unable to load saved repository data.");
    const data = await response.json();
    if (version !== workspaceLoadVersion) return;
    if (!Array.isArray(data.projects) || !Array.isArray(data.documents) || !Array.isArray(data.people)) {
      throw new Error("Invalid workspace response; keeping the last loaded data.");
    }
    // Avoid spreading large PDF libraries into function arguments (browser limit).
    for (const [target, source] of [[projects, data.projects], [pdfs, data.documents], [people, data.people]]) {
      target.length = 0;
      for (const item of source) target.push(item);
    }
    authorLookup = null;
    renderApp();
    if (state.searchQuery.trim()) scheduleAdvancedSearch();
  } catch (error) {
    if (version === workspaceLoadVersion) showToast(error.message);
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
  let savedDetails = "", extractedText = "";
  const copyDetails = document.querySelector("#pdf-details-copy");
  const copyExtracted = document.querySelector("#pdf-text-copy");
  async function copySaved(value, label) {
    const current = sequence;
    try {
      await copyText(value);
      if (sequence === current) status.textContent = `${label} copied.`;
    } catch {
      if (sequence === current) status.textContent = "Could not copy. Please select and copy the text manually.";
    }
  }
  copyDetails.onclick = () => copySaved(savedDetails, "Saved PDF details");
  copyExtracted.onclick = () => copySaved(extractedText, "Extracted text");
  document.querySelector("#pdf-details-close").onclick = () => dialog.close();
  dialog.addEventListener("close", () => { sequence++; });
  document.querySelector("#pdf-table-body").addEventListener("click", async event => {
    const button = event.target.closest("[data-pdf-details]");
    if (!button) return;
    const current = ++sequence;
    copyDetails.disabled = copyExtracted.disabled = true;
    savedDetails = extractedText = "";
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
      savedDetails = Object.entries(labels).map(([key, label]) => `${label}: ${record[key] ?? "Not available"}`).join("\n");
      extractedText = record.pdf_text || "";
      copyDetails.disabled = false;
      copyExtracted.disabled = !extractedText;
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
