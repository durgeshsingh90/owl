(() => {
    "use strict";

    const workspace = document.querySelector("[data-bitbucket-workspace]");
    const form = document.querySelector("[data-pdf-star-form]");
    if (!workspace || !form || typeof window.fetch !== "function") return;

    const status = workspace.querySelector("[data-pdf-star-status]");
    let requestPending = false;
    const starButtons = () =>
        Array.from(workspace.querySelectorAll("[data-pdf-star-toggle]"));
    const buttonsForDocument = (documentId) =>
        starButtons().filter((button) => button.dataset.documentId === documentId);
    const announce = (message) => {
        if (status) status.textContent = message;
    };
    const starredFirstActive = () => {
        try {
            return (
                new window.URL(window.location.href).searchParams.get("sort") ===
                "starred_first"
            );
        } catch {
            return false;
        }
    };
    const setPending = (buttons, pending) => {
        buttons.forEach((button) => {
            button.disabled = pending;
            if (pending) button.setAttribute("aria-busy", "true");
            else button.removeAttribute("aria-busy");
        });
    };
    const actionForState = (action, starred) => {
        const url = new window.URL(action, window.location.href);
        url.searchParams.set("starred", String(starred));
        return url.href;
    };
    const renderStar = (button, { documentId, filename, starred }) => {
        button.dataset.documentId = documentId;
        button.dataset.pdfStarName = filename;
        button.setAttribute("aria-label", `Star PDF: ${filename}`);
        button.setAttribute("aria-pressed", String(starred));
        button.title = starred
            ? `Remove star from ${filename} in OWL`
            : `Star ${filename} in OWL`;
        button.dataset.tooltip = starred ? "Remove star" : "Star PDF";
        button.formAction = actionForState(button.formAction, !starred);
        const icon = button.querySelector("[data-pdf-star-icon]");
        if (icon) icon.textContent = starred ? "★" : "☆";
        const row = button.closest("[data-pdf-row]");
        if (row) row.dataset.pdfStarred = String(starred);
    };

    form.addEventListener("submit", async (event) => {
        const submitter = event.submitter;
        if (!submitter?.matches?.("[data-pdf-star-toggle]")) return;
        if (starredFirstActive()) return;
        event.preventDefault();

        const requestedDocumentId = submitter.dataset.documentId || "";
        if (!/^[1-9]\d*$/.test(requestedDocumentId) || requestPending) return;
        const desiredStarred = submitter.getAttribute("aria-pressed") !== "true";
        const action = submitter.formAction;

        requestPending = true;
        const initialButtons = buttonsForDocument(requestedDocumentId);
        setPending(starButtons(), true);

        try {
            const requestBody = new window.FormData(form);
            requestBody.set("starred", String(desiredStarred));
            const csrf = form.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";
            const response = await window.fetch(action, {
                method: "POST",
                body: requestBody,
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            let payload = {};
            try {
                payload = await response.json();
            } catch {
                // The validation below supplies one stable message for non-JSON responses.
            }
            if (!response.ok) {
                const detail =
                    payload && typeof payload === "object" ? payload.detail : "";
                throw new Error(detail || "The PDF star could not be changed.");
            }

            const responseDocumentId = String(payload?.documentId ?? "");
            if (
                responseDocumentId !== requestedDocumentId ||
                typeof payload.filename !== "string" ||
                !payload.filename ||
                typeof payload.starred !== "boolean"
            ) {
                throw new Error("OWL returned an incomplete PDF-star response.");
            }

            const matchingButtons = new Set([
                ...initialButtons,
                ...buttonsForDocument(responseDocumentId),
            ]);
            matchingButtons.forEach((button) =>
                renderStar(button, {
                    documentId: responseDocumentId,
                    filename: payload.filename,
                    starred: payload.starred,
                }),
            );
            announce(
                payload.detail ||
                    payload.label ||
                    `${payload.filename} ${payload.starred ? "starred" : "unstarred"}.`,
            );
        } catch (error) {
            announce(error?.message || "The PDF star could not be changed.");
        } finally {
            requestPending = false;
            setPending(starButtons(), false);
        }
    });
})();
