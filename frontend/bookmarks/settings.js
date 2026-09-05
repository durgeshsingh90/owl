"use strict";
(() => {
  const dialog = document.querySelector("#confluence-settings"),
    form = document.querySelector("#confluence-form"),
    pat = document.querySelector("#confluence-pat"),
    url = document.querySelector("#confluence-url"),
    receipt = document.querySelector("#verification-receipt"),
    feedback = document.querySelector("#settings-feedback"),
    test = document.querySelector("#confluence-test"),
    save = document.querySelector("#confluence-save");
  let workspace = null,
    controller = null;
  function message(text, error = false) {
    feedback.textContent = text;
    feedback.dataset.error = String(error);
  }
  function busy(value) {
    test.disabled = value;
    save.disabled = value;
    url.disabled = value;
    pat.disabled = value;
  }
  async function request(path, options = {}) {
    const target = new URL(path, location.href);
    if (target.origin !== location.origin)
      throw Error("Settings must use the same OWL backend origin.");
    const response = await fetch(target, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: {
        Accept: "application/json",
        "X-Requested-With": "XMLHttpRequest",
        ...options.headers,
      },
    });
    if (!response.headers.get("content-type")?.includes("application/json"))
      throw Error(
        "OWL backend is unavailable in this frontend preview. Connect the backend to test or save Confluence settings.",
      );
    const result = await response.json();
    if (!response.ok)
      throw Error(result.detail || result.message || "The request failed.");
    return result;
  }
  document
    .querySelector("#settings-button")
    .addEventListener("click", async () => {
      form.reset();
      workspace = null;
      dialog.showModal();
      busy(true);
      message("Loading Confluence settings…");
      const pending = new AbortController();
      controller = pending;
      const timer = setTimeout(() => pending.abort(), 10000);
      try {
        const data = await request("/bookmarks/settings/workspace/", {
          signal: pending.signal,
        });
        if (!dialog.open) return;
        workspace = data;
        url.value = data.configuration?.baseUrl || "";
        applyConfluenceBaseUrl(url.value);
        pat.value = "";
        message(
          data.configuration?.managed_externally
            ? "This connection is managed outside OWL."
            : "Enter your Confluence URL and PAT, then test the connection.",
        );
      } catch (error) {
        if (dialog.open)
          message(
            error.name === "AbortError"
              ? "Settings request timed out."
              : error.message,
            true,
          );
      } finally {
        clearTimeout(timer);
        if (controller === pending) {
          controller = null;
          busy(false);
          if (workspace?.configuration?.managed_externally) busy(true);
        }
      }
    });
  async function submit(testOnly) {
    if (controller || !form.reportValidity()) return;
    if (!workspace) {
      message(
        "Connect the OWL backend and reopen settings before testing or saving.",
        true,
      );
      return;
    }
    const body = new URLSearchParams(new FormData(form));
    const path = testOnly
      ? workspace.urls.confluenceTest
      : workspace.urls.confluenceSave;
    if (!path || !workspace.csrfToken) {
      message("Backend settings response is incomplete.", true);
      return;
    }
    busy(true);
    message(testOnly ? "Testing connection…" : "Saving settings…");
    const pending = new AbortController();
    controller = pending;
    const timer = setTimeout(() => pending.abort(), 10000);
    try {
      const result = await request(path, {
        method: "POST",
        body,
        headers: { "X-CSRFToken": workspace.csrfToken },
        signal: pending.signal,
      });
      if (!dialog.open) return;
      if (testOnly) {
        receipt.value = result.verification_receipt || "";
        message(result.detail || result.label || "Connection test completed.");
      } else {
        if (result.state !== "success")
          throw Error(result.detail || "Settings were not saved.");
        applyConfluenceBaseUrl(body.get("base_url"));
        pat.value = "";
        receipt.value = "";
        message("Confluence settings saved.");
        void confluenceConnection.test();
      }
    } catch (error) {
      if (dialog.open) {
        receipt.value = "";
        message(
          error.name === "AbortError"
            ? "Request timed out after 10 seconds."
            : error.message,
          true,
        );
      }
    } finally {
      clearTimeout(timer);
      if (controller === pending) {
        controller = null;
        busy(false);
      }
    }
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit(false);
  });
  test.addEventListener("click", () => void submit(true));
  [url, pat].forEach((input) =>
    input.addEventListener("input", () => {
      receipt.value = "";
    }),
  );
  document
    .querySelector("#settings-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    controller?.abort();
    controller = null;
    form.reset();
    workspace = null;
    busy(false);
  });
})();
