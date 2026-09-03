(() => {
    "use strict";

    const workspace = document.querySelector("[data-bitbucket-workspace]");
    const form = document.querySelector("[data-people-star-form]");
    if (!workspace || !form || typeof window.fetch !== "function") return;

    const status = workspace.querySelector("[data-people-star-status]");
    let requestPending = false;
    const starButtons = () =>
        Array.from(workspace.querySelectorAll("[data-people-star-toggle]"));
    const buttonsForIdentity = (identityKey) =>
        starButtons().filter(
            (button) => button.dataset.peopleIdentityKey === identityKey,
        );
    const announce = (message) => {
        if (status) status.textContent = message;
    };
    const setPending = (buttons, pending) => {
        buttons.forEach((button) => {
            button.disabled = pending;
            if (pending) button.setAttribute("aria-busy", "true");
            else button.removeAttribute("aria-busy");
        });
    };
    const renderStar = (button, { identityKey, personName, starred }) => {
        button.value = identityKey;
        button.dataset.peopleIdentityKey = identityKey;
        button.dataset.peopleStarName = personName;
        button.setAttribute("aria-pressed", String(starred));
        button.setAttribute("aria-label", `Star ${personName}`);
        button.title = starred
            ? `Remove star from ${personName} locally in OWL`
            : `Star ${personName} locally in OWL`;
        button.formAction = `${form.action}?starred=${String(!starred)}`;
        const icon = button.querySelector("[data-people-star-icon]");
        if (icon) icon.textContent = starred ? "★" : "☆";
        const row = button.closest("[data-people-filter-entry]");
        if (row) row.dataset.peopleStarred = String(starred);
    };

    form.addEventListener("submit", async (event) => {
        const submitter = event.submitter;
        if (!submitter?.matches?.("[data-people-star-toggle]")) return;
        event.preventDefault();

        const requestedIdentity = submitter.dataset.peopleIdentityKey || submitter.value;
        if (!requestedIdentity || requestPending) return;
        const desiredStarred = submitter.getAttribute("aria-pressed") !== "true";

        requestPending = true;
        const initialButtons = buttonsForIdentity(requestedIdentity);
        setPending(starButtons(), true);

        try {
            const requestBody = new window.FormData(form);
            requestBody.set("person", requestedIdentity);
            requestBody.set("starred", String(desiredStarred));
            const csrf = form.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";
            const response = await window.fetch(form.action, {
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
                // The error below gives a stable message when a response is not JSON.
            }
            if (!response.ok) {
                const detail =
                    payload && typeof payload === "object" ? payload.detail : "";
                throw new Error(detail || "The person star could not be changed.");
            }
            if (
                payload === null ||
                typeof payload !== "object" ||
                typeof payload.identity_key !== "string" ||
                !payload.identity_key ||
                typeof payload.person !== "string" ||
                !payload.person ||
                typeof payload.starred !== "boolean"
            ) {
                throw new Error("OWL returned an incomplete person-star response.");
            }

            const matchingButtons = new Set([
                ...initialButtons,
                ...buttonsForIdentity(payload.identity_key),
            ]);
            matchingButtons.forEach((button) =>
                renderStar(button, {
                    identityKey: payload.identity_key,
                    personName: payload.person,
                    starred: payload.starred,
                }),
            );
            announce(
                payload.detail ||
                    payload.label ||
                    `${payload.person} ${payload.starred ? "starred" : "unstarred"}.`,
            );
        } catch (error) {
            announce(error?.message || "The person star could not be changed.");
        } finally {
            requestPending = false;
            setPending(starButtons(), false);
        }
    });
})();
