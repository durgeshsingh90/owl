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
