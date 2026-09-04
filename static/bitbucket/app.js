(() => {
    "use strict";

    const workspace = document.querySelector("[data-workspace]");
    if (!workspace) return;

    const addForm = workspace.querySelector("[data-add-repository-form]");
    const csrfToken = addForm?.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
    const toast = workspace.querySelector("[data-toast]");
    const activeJobs = new Set();
    const initialJobs = document.querySelector("#bitbucket-initial-jobs");
    let authenticationJob = null;
    let reloadScheduled = false;
    let toastTimer = null;

    try {
        for (const jobId of JSON.parse(initialJobs?.textContent || "[]")) {
            activeJobs.add(String(jobId));
        }
    } catch {
        // A malformed optional bootstrap value should not disable the workspace.
    }

    const showToast = (message) => {
        if (!toast) return;
        toast.textContent = message;
        toast.hidden = false;
        window.clearTimeout(toastTimer);
        toastTimer = window.setTimeout(() => {
            toast.hidden = true;
        }, 5000);
    };

    const request = async (url, options = {}) => {
        const headers = new Headers(options.headers || {});
        headers.set("X-CSRFToken", csrfToken);
        headers.set("X-Requested-With", "XMLHttpRequest");
        const response = await fetch(url, {
            credentials: "same-origin",
            ...options,
            headers,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.message || "The request could not be completed.");
            error.data = data;
            throw error;
        }
        return data;
    };

    const post = (url, body = null) => request(url, {method: "POST", body});

    const updateRepository = (repository) => {
        const card = workspace.querySelector(
            '[data-repository-card="' + repository.id + '"]'
        );
        if (!card) return;
        card.dataset.state = repository.state;
        const state = card.querySelector("[data-repository-state]");
        const counts = card.querySelector("[data-repository-counts]");
        const message = card.querySelector("[data-repository-message]");
        if (state) state.textContent = repository.state.replaceAll("_", " ");
        if (counts) {
            counts.textContent = repository.pdfCount + " PDF · " +
                repository.vsdxCount + " VSDX";
        }
        if (message) message.textContent = repository.statusMessage || "";
    };

    const authDialog = workspace.querySelector("[data-auth-dialog]");
    const authRepository = authDialog?.querySelector("[data-auth-repository]");
    const authMessage = authDialog?.querySelector("[data-auth-message]");
    const authLink = authDialog?.querySelector("[data-auth-link]");
    const authRetry = authDialog?.querySelector("[data-auth-retry]");
    const authCancel = authDialog?.querySelector("[data-auth-cancel]");

    const openAuthenticationDialog = (job) => {
        authenticationJob = job;
        if (authRepository) {
            authRepository.textContent = job.repository.project + " / " +
                job.repository.name;
        }
        if (authMessage) {
            authMessage.textContent = job.errorMessage ||
                "Sign in through your organisation's firewall or VPN, then retry.";
        }
        if (authLink) authLink.href = job.authenticationUrl;
        if (authDialog && !authDialog.open) {
            if (typeof authDialog.showModal === "function") authDialog.showModal();
            else authDialog.setAttribute("open", "");
        }
        authRetry?.focus();
    };

    const closeAuthenticationDialog = () => {
        authenticationJob = null;
        if (!authDialog?.open) return;
        if (typeof authDialog.close === "function") authDialog.close();
        else authDialog.removeAttribute("open");
    };

    const handleJob = (job) => {
        updateRepository(job.repository);
        if (job.status === "queued" || job.status === "running") {
            activeJobs.add(job.id);
            return;
        }
        if (job.status === "auth_required") {
            activeJobs.add(job.id);
            openAuthenticationDialog(job);
            return;
        }
        activeJobs.delete(job.id);
        if (authenticationJob?.id === job.id) closeAuthenticationDialog();
        if (job.status === "succeeded" && !reloadScheduled) {
            reloadScheduled = true;
            showToast(job.repository.name + " is ready. Refreshing the PDF list…");
            window.setTimeout(() => window.location.reload(), 650);
        } else if (job.status === "failed") {
            showToast(job.errorMessage || job.repository.name + " could not be updated.");
        }
    };

    const poll = async () => {
        try {
            const url = new URL(workspace.dataset.statusUrl, window.location.href);
            for (const jobId of activeJobs) url.searchParams.append("job", jobId);
            const data = await request(url);
            for (const job of data.jobs || []) handleJob(job);
        } catch {
            // The next bounded poll retries without interrupting user actions.
        } finally {
            window.setTimeout(poll, activeJobs.size ? 1500 : 5000);
        }
    };

    addForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submit = addForm.querySelector("[type=submit]");
        const errorNode = addForm.querySelector("[data-form-error]");
        submit.disabled = true;
        if (errorNode) errorNode.textContent = "";
        try {
            const data = await post(addForm.action, new FormData(addForm));
            activeJobs.add(data.job.id);
            handleJob(data.job);
            addForm.reset();
            showToast(data.job.repository.name + " was added. Testing HTTPS access…");
        } catch (error) {
            const fields = error.data?.errors || {};
            const fieldMessages = Object.values(fields).flatMap((items) =>
                Array.isArray(items) ? items.map((item) => item.message) : []
            );
            if (errorNode) errorNode.textContent = fieldMessages[0] || error.message;
        } finally {
            submit.disabled = false;
        }
    });

    workspace.addEventListener("click", async (event) => {
        const openButton = event.target.closest?.("[data-open-url]");
        if (openButton) {
            openButton.disabled = true;
            try {
                const data = await post(openButton.dataset.openUrl);
                const count = openButton.closest("tr")?.querySelector("[data-open-count]");
                if (count) count.textContent = String(data.openCount);
            } catch (error) {
                showToast(error.message);
            } finally {
                openButton.disabled = false;
            }
            return;
        }

        const revealButton = event.target.closest?.("[data-reveal-url]");
        if (revealButton) {
            revealButton.disabled = true;
            try {
                await post(revealButton.dataset.revealUrl);
                showToast("The containing folder was brought to the front.");
            } catch (error) {
                showToast(error.message);
            } finally {
                revealButton.disabled = false;
            }
        }
    });

    authRetry?.addEventListener("click", async () => {
        if (!authenticationJob) return;
        authRetry.disabled = true;
        try {
            const data = await post(authenticationJob.retryUrl);
            activeJobs.add(data.job.id);
            closeAuthenticationDialog();
            showToast("Retry queued. Running git ls-remote again…");
        } catch (error) {
            showToast(error.message);
        } finally {
            authRetry.disabled = false;
        }
    });

    authCancel?.addEventListener("click", async () => {
        if (!authenticationJob) {
            closeAuthenticationDialog();
            return;
        }
        authCancel.disabled = true;
        try {
            const cancelledId = authenticationJob.id;
            await post(authenticationJob.cancelUrl);
            activeJobs.delete(cancelledId);
            closeAuthenticationDialog();
            showToast("Connection retry cancelled.");
        } catch (error) {
            showToast(error.message);
        } finally {
            authCancel.disabled = false;
        }
    });

    const checkboxes = [...workspace.querySelectorAll("[data-document-select]")];
    const selectPage = workspace.querySelector("[data-select-page]");
    const selectionCount = workspace.querySelector("[data-selection-count]");
    const openSelected = workspace.querySelector("[data-open-selected]");
    const copySelected = workspace.querySelector("[data-copy-selected]");
    const selected = () => checkboxes.filter((checkbox) => checkbox.checked);

    const updateSelection = () => {
        const chosen = selected();
        if (selectionCount) selectionCount.textContent = chosen.length + " selected";
        if (openSelected) openSelected.disabled = chosen.length === 0;
        if (copySelected) copySelected.disabled = chosen.length === 0;
        if (selectPage) {
            selectPage.checked = chosen.length > 0 && chosen.length === checkboxes.length;
            selectPage.indeterminate = chosen.length > 0 &&
                chosen.length < checkboxes.length;
        }
    };

    selectPage?.addEventListener("change", () => {
        for (const checkbox of checkboxes) checkbox.checked = selectPage.checked;
        updateSelection();
    });
    for (const checkbox of checkboxes) checkbox.addEventListener("change", updateSelection);

    openSelected?.addEventListener("click", async () => {
        const ids = selected().map((checkbox) => Number(checkbox.value));
        if (!ids.length) return;
        openSelected.disabled = true;
        try {
            const data = await request(workspace.dataset.openSelectedUrl, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({documentIds: ids}),
            });
            for (const id of data.opened || []) {
                const count = workspace.querySelector(
                    '[data-document-row="' + id + '"] [data-open-count]'
                );
                if (count) count.textContent = String(Number(count.textContent || "0") + 1);
            }
            if (data.skipped?.length) {
                showToast("Opened " + data.opened.length + "; skipped " +
                    data.skipped.length + " unavailable PDFs.");
            } else {
                showToast("Opened " + data.opened.length + " selected PDFs.");
            }
        } catch (error) {
            showToast(error.message);
        } finally {
            updateSelection();
        }
    });

    const copyText = async (value) => {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            return;
        }
        const area = document.createElement("textarea");
        area.value = value;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.append(area);
        area.select();
        const copied = document.execCommand("copy");
        area.remove();
        if (!copied) throw new Error("Clipboard access was denied.");
    };

    copySelected?.addEventListener("click", async () => {
        const paths = selected().map((checkbox) => checkbox.dataset.path);
        if (!paths.length) return;
        copySelected.disabled = true;
        try {
            await copyText(paths.join("\n"));
            showToast("Copied " + paths.length + " local path" +
                (paths.length === 1 ? "." : "s."));
        } catch (error) {
            showToast(error.message);
        } finally {
            updateSelection();
        }
    });

    const schedule = async () => {
        try {
            await post(workspace.dataset.scheduleUrl);
        } catch {
            // run_owl also schedules daily pulls; the next browser tick retries.
        }
    };
    schedule();
    window.setInterval(schedule, 60_000);
    poll();
})();
