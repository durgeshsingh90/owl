"use strict";

(() => {
  const dialog = document.querySelector("#settings-dialog");
  const form = document.querySelector("#settings-form");
  const baseUrl = document.querySelector("#settings-base-url");
  const username = document.querySelector("#settings-username");
  const token = document.querySelector("#settings-token");
  const feedback = document.querySelector("#settings-feedback");
  const saveButton = document.querySelector("#settings-save");
  const testButton = document.querySelector("#settings-test");
  let workspace = null;
  let pending = null;

  function message(text, error = false) {
    feedback.textContent = text;
    feedback.dataset.error = String(error);
  }

  function busy(value) {
    saveButton.disabled = value;
    testButton.disabled = value;
    baseUrl.disabled = value;
    username.disabled = value;
    token.disabled = value;
  }

  async function loadSettings() {
    if (dialog.open) return;
    form.reset();
    workspace = null;
    busy(true);
    message("Loading settings…");
    dialog.showModal();
    const controller = new AbortController();
    pending = controller;
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      workspace = await connectionJson(
        document.querySelector("#connection-status").dataset.workspaceUrl,
        { signal: controller.signal },
      );
      if (!dialog.open) return;
      const server = workspace.credentials?.[0];
      baseUrl.value = server?.baseUrl || "";
      username.value = server?.username || "";
      token.value = "";
      message(
        server
          ? "Saved settings loaded."
          : "Enter your Bitbucket server details.",
      );
    } catch {
      if (dialog.open)
        message(
          "Cannot reach the OWL backend. Connect the backend, then reopen settings to save or test.",
          true,
        );
    } finally {
      clearTimeout(timer);
      if (pending === controller) {
        pending = null;
        busy(false);
        if (dialog.open) baseUrl.focus();
      }
    }
  }

  async function submitSettings(testOnly) {
    if (pending || !form.reportValidity()) return;
    if (!workspace) {
      message(
        "Connect the OWL backend and reopen settings before saving or testing.",
        true,
      );
      return;
    }
    if (connectionCheckRunning || pullProgress.active) {
      message(
        "A connection check is already running. Try again when it finishes.",
      );
      return;
    }
    const endpoint = new URL(
      testOnly ? workspace.settingsTestUrl : workspace.settingsSaveUrl,
      window.location.href,
    );
    if (endpoint.origin !== window.location.origin) {
      message("The settings endpoint must be on the OWL backend origin.", true);
      return;
    }
    const body = new URLSearchParams(new FormData(form));
    body.set("verify_ssl", "on");
    const controller = new AbortController();
    pending = controller;
    busy(true);
    message(testOnly ? "Testing connection…" : "Saving settings…");
    if (testOnly) {
      connectionCheckRunning = true;
      setConnectionStatus("connecting");
    }
    const timer = setTimeout(() => controller.abort(), 5000);
    let saved = false;
    try {
      await connectionJson(endpoint, {
        method: "POST",
        signal: controller.signal,
        headers: { "X-CSRFToken": workspace.csrfToken },
        body,
      });
      if (testOnly) {
        setConnectionStatus("connected");
        message("Connection successful. Save to keep these settings.");
      } else {
        saved = true;
        token.value = "";
        message("Settings saved. Checking connection…");
      }
    } catch (error) {
      if (testOnly) setConnectionStatus("failed");
      // Never include an entered secret in visible error text.
      const submittedToken = body.get("access_token");
      const safeMessage = submittedToken
        ? String(error.message).split(submittedToken).join("[hidden]")
        : error.message;
      message(
        controller.signal.aborted
          ? "The request timed out after 5 seconds. Please retry."
          : safeMessage,
        true,
      );
    } finally {
      clearTimeout(timer);
      if (testOnly) connectionCheckRunning = false;
      if (pending === controller) {
        pending = null;
        busy(false);
      }
    }
    if (saved) {
      await testConnection();
      if (dialog.open)
        message(
          document.querySelector("#connection-status").dataset.state ===
            "connected"
            ? "Settings saved. Connection successful."
            : "Settings saved. The connection check failed.",
        );
    }
  }

  document
    .querySelector("#settings-button")
    .addEventListener("click", loadSettings);
  document
    .querySelector("#settings-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    pending?.abort();
    token.value = "";
    form.reset();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitSettings(false);
  });
  testButton.addEventListener("click", () => {
    void submitSettings(true);
  });
})();
