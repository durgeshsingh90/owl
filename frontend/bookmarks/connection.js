"use strict";
const confluenceConnection = (() => {
  const container = document.querySelector("#connection-status"),
    button = document.querySelector("#test-connection"),
    image = document.querySelector("#connection-image"),
    label = document.querySelector("#connection-label");
  let current = null;
  function status(state, detail) {
    container.dataset.state = state;
    image.src =
      "../bitbucket/assets/" +
      (state === "connected"
        ? "connected.png"
        : state === "connecting"
          ? "no-connection.gif"
          : "disconnected.png");
    button.disabled = state === "connecting";
    const text =
      state === "connecting"
        ? "Checking Confluence connection…"
        : state === "connected"
          ? "Confluence connected. Click to retest."
          : "Confluence disconnected. Click to retest.";
    button.title = detail ? text + " " + detail : text;
    button.setAttribute("aria-label", text);
    label.textContent = text;
  }
  async function json(path, options) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: {
        Accept: "application/json",
        "X-Requested-With": "XMLHttpRequest",
        ...options?.headers,
      },
    });
    if (!response.headers.get("content-type")?.includes("application/json"))
      throw Error("OWL backend is unavailable in this frontend preview.");
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || "Connection test failed.");
    return data;
  }
  async function test() {
    current?.abort();
    const controller = new AbortController();
    current = controller;
    status("connecting");
    let outcome = "failed",
      detail = "Connection check timed out after 10 seconds.";
    const windowFinished = new Promise((resolve) =>
      setTimeout(() => {
        controller.abort();
        resolve();
      }, 10000),
    );
    try {
      const workspace = await json("/bookmarks/settings/workspace/", {
        signal: controller.signal,
      });
      if (!workspace.csrfToken)
        throw Error("Backend did not provide a connection-test token.");
      applyConfluenceBaseUrl(workspace.configuration?.baseUrl);
      const result = await json("/bookmarks/connection/test/", {
        method: "POST",
        headers: { "X-CSRFToken": workspace.csrfToken },
        signal: controller.signal,
      });
      outcome = "connected";
      detail = result.detail || "Saved Confluence connection verified.";
    } catch (error) {
      detail =
        error.name === "AbortError"
          ? "Connection check timed out after 10 seconds."
          : error.message;
    } finally {
      await windowFinished;
      if (current === controller) {
        current = null;
        status(outcome, detail);
      }
    }
  }
  button.addEventListener("click", () => void test());
  void test();
  return { test };
})();
