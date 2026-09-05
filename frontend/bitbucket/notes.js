"use strict";

// Use the stable PDF URL, not a display name or temporary numeric sample ID.
function pdfNoteKey(pdf) {
  return `owl-bitbucket-pdf-note:${pdf.pdfUrl}`;
}

function readPdfNote(pdf) {
  try {
    return localStorage.getItem(pdfNoteKey(pdf)) || "";
  } catch {
    return "";
  }
}

(() => {
  const dialog = document.querySelector("#notes-dialog");
  const input = document.querySelector("#pdf-notes");
  const feedback = document.querySelector("#notes-feedback");
  let target = null;
  document
    .querySelector("#pdf-table-body")
    .addEventListener("click", (event) => {
      const button = event.target.closest("[data-pdf-notes]");
      if (!button) return;
      target = pdfs.find((pdf) => pdf.id === Number(button.dataset.pdfNotes));
      if (!target) return;
      document.querySelector("#notes-filename").textContent = target.name;
      feedback.textContent = "";
      feedback.dataset.error = "false";
      try {
        input.value = localStorage.getItem(pdfNoteKey(target)) || "";
      } catch {
        input.value = "";
        feedback.textContent =
          "Browser storage is unavailable. Notes cannot be loaded or saved.";
        feedback.dataset.error = "true";
      }
      dialog.showModal();
      input.focus();
    });
  for (const id of ["notes-close", "notes-cancel"]) {
    document.getElementById(id).addEventListener("click", () => dialog.close());
  }
  dialog.addEventListener("close", () => {
    target = null;
    input.value = "";
  });
  document.querySelector("#notes-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!target) return;
    try {
      if (input.value.trim())
        localStorage.setItem(pdfNoteKey(target), input.value);
      else localStorage.removeItem(pdfNoteKey(target));
    } catch {
      feedback.textContent =
        "Could not save notes. Browser storage may be full or disabled. Your text is still here.";
      feedback.dataset.error = "true";
      return;
    }
    dialog.close();
    renderPdfTable();
    showToast("Notes saved");
  });
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key.startsWith("owl-bitbucket-pdf-note:"))
      renderPdfTable();
  });
})();
