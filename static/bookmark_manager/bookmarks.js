(() => {
    "use strict";

    const settingsDialog = document.querySelector("[data-settings-dialog]");
    const settingsButtons = document.querySelectorAll("[data-settings-open]");
    const settingsHeading = document.querySelector("[data-settings-heading]");
    let activeSettingsButton = null;

    const csrfToken = () =>
        document.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";

    const openSettings = (trigger = settingsButtons[0]) => {
        activeSettingsButton = trigger || settingsButtons[0] || null;
        if (!settingsDialog || typeof settingsDialog.showModal !== "function") {
            const fallbackUrl = activeSettingsButton?.dataset.settingsFallback;
            if (fallbackUrl) {
                window.location.assign(fallbackUrl);
            }
            return;
        }
        settingsDialog.showModal();
        settingsHeading?.focus();
    };

    settingsButtons.forEach((button) => {
        button.addEventListener("click", () => openSettings(button));
    });

    const settingsForm = document.querySelector("[data-confluence-settings-form]");
    const patInput = settingsForm?.querySelector("input[type='password'], input[data-pat-input]");
    const showPatButton = settingsForm?.querySelector("[data-show-pat]");
    const verificationReceipt = settingsForm?.querySelector("[name='verification_receipt']");
    const testButton = settingsForm?.querySelector("[data-test-connection]");
    const saveSettingsButton = settingsForm?.querySelector("[data-save-settings]");
    const testResult = settingsForm?.querySelector("[data-connection-test-result]");
    const bitbucketCredentialForm = document.querySelector("[data-bitbucket-credential-form]");
    const bitbucketTokenInput = bitbucketCredentialForm?.querySelector(
        "input[type='password'], input[data-bitbucket-token-input]",
    );
    const showBitbucketTokenButton = bitbucketCredentialForm?.querySelector(
        "[data-show-bitbucket-token]",
    );

    const resetSettingsForm = () => {
        settingsForm?.reset();
        if (patInput) {
            patInput.type = "password";
        }
        if (showPatButton) {
            showPatButton.textContent = "Show";
            showPatButton.setAttribute("aria-pressed", "false");
        }
        if (verificationReceipt) {
            verificationReceipt.value = "";
        }
        if (testResult) {
            testResult.textContent = "No connection test has run for the current values.";
            testResult.dataset.state = "idle";
        }
        bitbucketCredentialForm?.reset();
        if (bitbucketTokenInput) {
            bitbucketTokenInput.type = "password";
        }
        if (showBitbucketTokenButton) {
            showBitbucketTokenButton.textContent = "Show";
            showBitbucketTokenButton.setAttribute("aria-pressed", "false");
        }
    };

    document.querySelectorAll("[data-settings-close]").forEach((button) => {
        button.addEventListener("click", () => {
            resetSettingsForm();
            settingsDialog?.close();
        });
    });
    settingsDialog?.addEventListener("cancel", resetSettingsForm);
    settingsDialog?.addEventListener("close", () => activeSettingsButton?.focus());
    if (settingsDialog?.dataset.openOnLoad === "true") {
        openSettings();
    }

    showPatButton?.addEventListener("click", () => {
        if (!patInput) {
            return;
        }
        const showing = patInput.type === "text";
        patInput.type = showing ? "password" : "text";
        showPatButton.textContent = showing ? "Show" : "Hide";
        showPatButton.setAttribute("aria-pressed", String(!showing));
        patInput.focus();
    });

    showBitbucketTokenButton?.addEventListener("click", () => {
        if (!bitbucketTokenInput) {
            return;
        }
        const showing = bitbucketTokenInput.type === "text";
        bitbucketTokenInput.type = showing ? "password" : "text";
        showBitbucketTokenButton.textContent = showing ? "Show" : "Hide";
        showBitbucketTokenButton.setAttribute("aria-pressed", String(!showing));
        bitbucketTokenInput.focus();
    });

    document.querySelectorAll("[data-settings-cancel]").forEach((button) => {
        button.addEventListener("click", () => {
            resetSettingsForm();
            settingsDialog?.close();
        });
    });

    testButton?.addEventListener("click", async (event) => {
        if (!settingsForm || !testResult) {
            return;
        }
        event.preventDefault();
        if (!settingsForm.reportValidity()) {
            return;
        }

        testButton.disabled = true;
        testButton.setAttribute("aria-busy", "true");
        testResult.textContent = "Testing one read-only Confluence request…";
        testResult.dataset.state = "progress";
        if (verificationReceipt) {
            verificationReceipt.value = "";
        }

        try {
            const response = await fetch(settingsForm.dataset.testUrl, {
                method: "POST",
                body: new FormData(settingsForm),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            const payload = await response.json();
            testResult.textContent = `${payload.label} — ${payload.detail}`;
            testResult.dataset.state = payload.state || "error";
            if (response.ok && payload.verification_receipt && verificationReceipt) {
                verificationReceipt.value = payload.verification_receipt;
            }
        } catch (_error) {
            testResult.textContent = "Unreachable — OWL could not complete the connection test.";
            testResult.dataset.state = "unreachable";
        } finally {
            testButton.disabled = false;
            testButton.removeAttribute("aria-busy");
        }
    });

    const submitLocalForm = async (
        form,
        statusTarget,
        busyButton,
        actionUrl = form.action,
        formData = new FormData(form),
        redirectDelay = 0,
    ) => {
        busyButton?.setAttribute("aria-busy", "true");
        if (busyButton) {
            busyButton.disabled = true;
        }
        let redirectAccepted = false;
        try {
            const response = await fetch(actionUrl, {
                method: "POST",
                body: formData,
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (response.redirected) {
                const target = new URL(response.url, window.location.href);
                if (target.origin === window.location.origin) {
                    redirectAccepted = true;
                    window.location.assign(target.href);
                    return null;
                }
            }
            let payload = null;
            if ((response.headers.get("content-type") || "").includes("application/json")) {
                payload = await response.json();
            }
            if (payload?.redirect) {
                const target = new URL(payload.redirect, window.location.href);
                if (target.origin === window.location.origin) {
                    redirectAccepted = true;
                    if (statusTarget) {
                        statusTarget.hidden = false;
                        statusTarget.textContent = `${payload.label} — ${payload.detail}`;
                        statusTarget.dataset.state = payload.state || "success";
                    }
                    if (redirectDelay > 0) {
                        window.setTimeout(() => window.location.assign(target.href), redirectDelay);
                    } else {
                        window.location.assign(target.href);
                    }
                    return payload;
                }
            }
            if (statusTarget) {
                statusTarget.hidden = false;
                statusTarget.textContent = payload
                    ? `${payload.label} — ${payload.detail}`
                    : "The action could not be completed. Review the form and try again.";
                statusTarget.dataset.state = payload?.state || "error";
            }
            return payload;
        } catch (_error) {
            if (statusTarget) {
                statusTarget.hidden = false;
                statusTarget.textContent = "The local action could not be completed.";
                statusTarget.dataset.state = "unreachable";
            }
            return null;
        } finally {
            if (!redirectAccepted) {
                busyButton?.removeAttribute("aria-busy");
                if (busyButton) {
                    busyButton.disabled = false;
                }
            }
        }
    };

    settingsForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!settingsForm.reportValidity()) {
            return;
        }
        await submitLocalForm(settingsForm, testResult, saveSettingsButton);
    });

    const bookmarkUnifiedForm = document.querySelector("[data-bookmark-unified-form]");
    const bookmarkSaveButton = bookmarkUnifiedForm?.querySelector("[data-bookmark-save]");
    const bookmarkSaveResult = document.querySelector("[data-bookmark-save-result]");
    let bookmarkSaveInFlight = false;
    bookmarkUnifiedForm?.addEventListener("submit", async (event) => {
        const submitter = event.submitter;
        if (!(submitter instanceof HTMLElement) || !submitter.matches("[data-bookmark-save]")) {
            return;
        }
        event.preventDefault();
        if (bookmarkSaveInFlight) {
            return;
        }
        window.clearTimeout(searchTimer);
        if (!bookmarkUnifiedForm.reportValidity()) {
            return;
        }
        const saveAction = submitter.getAttribute("formaction") || bookmarkUnifiedForm.action;
        const formData = new FormData(bookmarkUnifiedForm);
        formData.set("csrfmiddlewaretoken", csrfToken());
        if (bookmarkSaveResult) {
            const value = String(formData.get("q") || "");
            const confluenceLike = /(?:confluence|\/spaces\/[^/]+\/pages\/|pageid=)/i.test(
                value,
            );
            bookmarkSaveResult.hidden = false;
            bookmarkSaveResult.dataset.state = "progress";
            bookmarkSaveResult.textContent = confluenceLike
                ? "Retrieving the Confluence page, hierarchy, and searchable text…"
                : "Adding bookmark…";
        }
        const defaultSaveLabel = submitter.textContent.trim() || "Add bookmark";
        bookmarkSaveInFlight = true;
        submitter.disabled = true;
        submitter.setAttribute("aria-busy", "true");
        submitter.setAttribute("aria-label", "Adding bookmark");
        submitter.textContent = "Adding…";
        const payload = await submitLocalForm(
            bookmarkUnifiedForm,
            bookmarkSaveResult,
            submitter,
            saveAction,
            formData,
            1250,
        );
        let redirectPending = false;
        if (payload?.redirect) {
            try {
                redirectPending =
                    new URL(payload.redirect, window.location.href).origin ===
                    window.location.origin;
            } catch (_error) {
                redirectPending = false;
            }
        }
        submitter.removeAttribute("aria-busy");
        if (payload?.created) {
            submitter.classList.add("is-added");
            submitter.textContent = "✓ Added";
            submitter.setAttribute("aria-label", "Bookmark added");
            submitter.disabled = true;
            announce(payload.detail || "Bookmark added");
            window.setTimeout(() => {
                submitter.classList.remove("is-added");
                submitter.textContent = defaultSaveLabel;
                submitter.removeAttribute("aria-label");
                submitter.disabled = redirectPending;
                if (!redirectPending) {
                    bookmarkSaveInFlight = false;
                }
            }, 1000);
        } else {
            submitter.textContent = defaultSaveLabel;
            submitter.removeAttribute("aria-label");
            submitter.disabled = redirectPending;
            if (!redirectPending) {
                bookmarkSaveInFlight = false;
            }
        }
    });

    document.querySelectorAll("[data-bookmark-import-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) {
                return;
            }
            const button = form.querySelector("[data-import-submit]");
            const progress = form.querySelector("[data-import-progress]");
            const label = form.querySelector("[data-import-progress-label]");
            const status = form.querySelector("[data-import-status]");
            const file = form.querySelector("input[type='file']")?.files?.[0];
            if (progress) {
                progress.hidden = false;
            }
            if (status) {
                status.hidden = true;
                status.textContent = "";
                status.dataset.state = "progress";
            }
            if (label) {
                label.textContent = file?.name?.toLowerCase().endsWith(".txt")
                    ? "Extracting URLs, checking Confluence Page IDs, and retrieving pages…"
                    : "Validating records and saving bookmarks…";
            }
            await submitLocalForm(form, status, button);
            if (progress) {
                progress.hidden = true;
            }
        });
    });

    document.querySelectorAll("[data-remove-credential-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            const confirmed = window.confirm(
                "Remove the securely stored Confluence PAT? Local bookmarks will remain available.",
            );
            if (!confirmed) {
                event.preventDefault();
                return;
            }
            event.preventDefault();
            await submitLocalForm(
                form,
                testResult,
                form.querySelector("button[type='submit']"),
            );
        });
    });

    document.querySelectorAll("[data-remove-bitbucket-credential-form]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const confirmed = window.confirm(
                "Remove this saved Bitbucket HTTPS credential? Repositories, downloaded files, and indexes will remain available.",
            );
            if (!confirmed) {
                event.preventDefault();
            }
        });
    });

    const bookmarkSearch = document.querySelector("[data-bookmark-search]");
    document.addEventListener("keydown", (event) => {
        const target = event.target;
        const isTyping =
            target instanceof HTMLInputElement ||
            target instanceof HTMLTextAreaElement ||
            target instanceof HTMLSelectElement ||
            target?.isContentEditable;
        if (event.key === "/" && !isTyping && bookmarkSearch) {
            event.preventDefault();
            bookmarkSearch.focus();
        } else if (event.key === "Escape" && document.activeElement === bookmarkSearch) {
            bookmarkSearch.blur();
        }
    });

    const peopleSearchToggle = document.querySelector("[data-people-search-toggle]");
    const peopleSearchPanel = document.querySelector("[data-people-search-panel]");
    const peopleSearchInput = document.querySelector("[data-people-search-input]");
    const peopleSearchStatus = document.querySelector("[data-people-search-status]");
    const peopleEntries = Array.from(document.querySelectorAll("[data-people-entry]"));
    const peopleNoResults = document.querySelector("[data-people-no-results]");
    const peopleSelectionInputs = Array.from(document.querySelectorAll("[data-person-select]"));
    const peopleSelectionStatus = document.querySelector("[data-people-selection-status]");
    const peopleFilterSubmit = document.querySelector("[data-people-filter-submit]");

    const normalizedPersonName = (value) =>
        String(value || "")
            .normalize("NFKD")
            .replace(/[\u0300-\u036f]/g, "")
            .toLocaleLowerCase()
            .trim();

    const filterPeople = () => {
        const terms = normalizedPersonName(peopleSearchInput?.value).split(/\s+/).filter(Boolean);
        let visibleCount = 0;
        peopleEntries.forEach((entry) => {
            const personName = normalizedPersonName(entry.dataset.personName);
            const matches = terms.every((term) => personName.includes(term));
            entry.hidden = !matches;
            if (matches) {
                visibleCount += 1;
            }
        });
        if (peopleNoResults) {
            peopleNoResults.hidden = visibleCount !== 0 || terms.length === 0;
        }
        if (peopleSearchStatus) {
            if (terms.length === 0) {
                peopleSearchStatus.textContent = `${peopleEntries.length} ${peopleEntries.length === 1 ? "person" : "people"} available`;
            } else if (visibleCount === 0) {
                peopleSearchStatus.textContent = "No people found";
            } else {
                peopleSearchStatus.textContent = `${visibleCount} ${visibleCount === 1 ? "person" : "people"} found`;
            }
        }
    };

    const renderPeopleSelection = () => {
        const selectedCount = peopleSelectionInputs.filter((input) => input.checked).length;
        peopleSelectionInputs.forEach((input) => {
            input.closest(".people-filter-option")?.classList.toggle("is-active", input.checked);
        });
        if (peopleSelectionStatus) {
            peopleSelectionStatus.textContent = `${selectedCount} selected`;
        }
        if (peopleFilterSubmit) {
            peopleFilterSubmit.textContent = selectedCount === 0 ? "Show all" : "Show pages";
        }
    };

    const closePeopleSearch = () => {
        if (!peopleSearchPanel || !peopleSearchToggle) {
            return;
        }
        peopleSearchPanel.hidden = true;
        peopleSearchToggle.setAttribute("aria-expanded", "false");
        if (peopleSearchInput) {
            peopleSearchInput.value = "";
        }
        filterPeople();
        peopleSearchToggle.focus();
    };

    peopleSearchToggle?.addEventListener("click", () => {
        if (!peopleSearchPanel) {
            return;
        }
        const opening = peopleSearchPanel.hidden;
        if (!opening) {
            closePeopleSearch();
            return;
        }
        peopleSearchPanel.hidden = false;
        peopleSearchToggle.setAttribute("aria-expanded", "true");
        peopleSearchInput?.focus();
    });
    peopleSearchInput?.addEventListener("input", filterPeople);
    peopleSearchInput?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            closePeopleSearch();
        }
    });
    peopleSelectionInputs.forEach((input) => {
        input.addEventListener("change", renderPeopleSelection);
    });
    renderPeopleSelection();

    const statusText = document.querySelector("[data-global-status-text]");
    const announce = (message) => {
        if (statusText && message) {
            statusText.textContent = message;
        }
    };

    const removeQueryParameter = (name) => {
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.delete(name);
        const nextLocation = `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
        window.history.replaceState(window.history.state, "", nextLocation);
    };

    document.querySelectorAll("[data-dismiss-import-result]").forEach((button) => {
        button.addEventListener("click", () => {
            document.querySelectorAll("[data-import-result]").forEach((result) => result.remove());
            removeQueryParameter("import_run");
            announce("Import result dismissed");
            document
                .querySelector("[data-bookmark-search], [data-import-submit], [data-settings-heading]")
                ?.focus();
        });
    });

    const refreshResult = document.querySelector("[data-refresh-result]");
    const refreshFailureList = refreshResult?.querySelector("[data-refresh-failure-list]");
    let refreshResultHistoryLocked = refreshResult?.dataset.historyLocked === "true";
    refreshResult?.querySelector("[data-dismiss-refresh-result]")?.addEventListener("click", () => {
        refreshResult.hidden = true;
        refreshResultHistoryLocked = false;
        removeQueryParameter("refresh_run");
        announce("Refresh issues dismissed");
        document.querySelector("[data-global-refresh-button]")?.focus();
    });

    const renderRefreshFailures = (failures) => {
        if (!refreshResult || !refreshFailureList || !Array.isArray(failures)) {
            return;
        }
        if (refreshResultHistoryLocked) {
            return;
        }
        refreshFailureList.replaceChildren();
        failures.forEach((failure) => {
            const item = document.createElement("li");
            const label = document.createElement("strong");
            label.textContent = failure.page_id
                ? `Page ID ${failure.page_id}`
                : failure.bookmark_id
                  ? `Bookmark #${failure.bookmark_id}`
                  : "Saved bookmark";
            item.append(label);
            if (failure.url) {
                const url = document.createElement("code");
                url.textContent = failure.url;
                item.append(url);
            }
            const reason = document.createElement("span");
            reason.textContent = failure.reason || "The page could not be refreshed.";
            item.append(reason);
            refreshFailureList.append(item);
        });
        refreshResult.hidden = failures.length === 0;
    };

    const globalRefresh = document.querySelector("[data-global-refresh]");
    const globalRefreshButton = globalRefresh?.querySelector("[data-global-refresh-button]");
    const globalRefreshSpinner = globalRefresh?.querySelector("[data-global-refresh-spinner]");
    const globalRefreshLabel = globalRefresh?.querySelector("[data-global-refresh-label]");
    const globalRefreshTime = globalRefresh?.querySelector("[data-global-refresh-time]");
    const globalRefreshProgress = globalRefresh?.querySelector("[data-global-refresh-progress]");
    const globalRefreshTerminalStatuses = new Set([
        "succeeded",
        "succeeded_with_errors",
        "failed",
        "interrupted",
    ]);
    let globalRefreshPollTimer = null;
    let globalRefreshReloadPending = globalRefresh?.dataset.active === "true";
    let globalRefreshReloadScheduled = false;
    let globalRefreshObservedRunId = Number(globalRefresh?.dataset.runId) || 0;

    const reloadAfterGlobalRefresh = () => {
        if (globalRefreshReloadScheduled) {
            return;
        }
        globalRefreshReloadScheduled = true;
        if (globalRefreshPollTimer) {
            window.clearTimeout(globalRefreshPollTimer);
            globalRefreshPollTimer = null;
        }
        const reloadWhenPanelsClose = () => {
            const panelOpen = Array.from(
                document.querySelectorAll(
                    "[data-repository-status-panel], [data-notification-panel]",
                ),
            ).some((panel) => !panel.hidden);
            if (panelOpen) {
                // Preserve open worker logs and alert history without another
                // server request; keep the completed refresh pending locally.
                window.setTimeout(reloadWhenPanelsClose, 1000);
                return;
            }
            window.location.reload();
        };
        window.setTimeout(reloadWhenPanelsClose, 250);
    };

    const formatRefreshTimestamp = (value) => {
        if (!value) {
            return "";
        }
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) {
            return "";
        }
        return new Intl.DateTimeFormat(undefined, {
            dateStyle: "medium",
            timeStyle: "medium",
        }).format(parsed);
    };

    const renderGlobalRefresh = (refresh) => {
        if (!globalRefresh || !refresh) {
            return;
        }
        const active = Boolean(refresh.active);
        const status = refresh.status || "idle";
        const runId = Number(refresh.run_id) || 0;
        if (runId && runId !== globalRefreshObservedRunId) {
            globalRefreshObservedRunId = runId;
            globalRefreshReloadPending = true;
        }
        if (active) {
            globalRefreshReloadPending = true;
        }
        const total = Number(refresh.total) || 0;
        const processed = Number(refresh.processed) || 0;
        const progress = Math.max(0, Math.min(100, Number(refresh.progress) || 0));
        globalRefresh.dataset.active = String(active);
        globalRefresh.dataset.status = status;
        globalRefreshButton.disabled = active;
        globalRefreshButton.toggleAttribute("aria-busy", active);
        globalRefreshSpinner.hidden = !active;
        globalRefreshProgress.style.width = `${progress}%`;

        if (globalRefreshLabel) {
            if (active) {
                globalRefreshLabel.textContent =
                    refresh.status === "queued"
                        ? "Refresh queued"
                        : `Refreshing ${processed} / ${total}`;
            } else if (refresh.status === "succeeded") {
                globalRefreshLabel.textContent = `${refresh.succeeded} refreshed`;
            } else if (refresh.status === "succeeded_with_errors") {
                globalRefreshLabel.textContent = `${refresh.succeeded} refreshed · ${refresh.failed} failed`;
            } else if (["failed", "interrupted"].includes(refresh.status)) {
                globalRefreshLabel.textContent = "Refresh needs attention";
            } else {
                globalRefreshLabel.textContent = "Refresh Confluence";
            }
        }

        const completedAt = refresh.last_completed_at || refresh.completed_at;
        if (globalRefreshTime) {
            const formatted = formatRefreshTimestamp(completedAt);
            globalRefreshTime.textContent = formatted
                ? `Last completed ${formatted}`
                : refresh.detail || "Never refreshed globally";
            if (completedAt) {
                const time = document.createElement("time");
                time.dateTime = completedAt;
                time.textContent = formatted;
                globalRefreshTime.textContent = "Last completed ";
                globalRefreshTime.append(time);
            }
        }
        globalRefreshButton.title = active
            ? `Confluence refresh is running: ${processed} of ${total} processed`
            : refresh.detail ||
              "Refresh every saved Confluence page, hierarchy, metadata, and searchable text";
        renderRefreshFailures(refresh.failures || []);

        if (
            globalRefreshReloadPending &&
            !active &&
            globalRefreshTerminalStatuses.has(status)
        ) {
            globalRefreshReloadPending = false;
            announce(
                status === "succeeded"
                    ? `Confluence refresh completed: ${refresh.succeeded} bookmarks updated.`
                    : refresh.detail || "Confluence refresh completed with errors.",
            );
            reloadAfterGlobalRefresh();
        }
    };

    const pollGlobalRefresh = async () => {
        if (!globalRefresh?.dataset.statusUrl) {
            return;
        }
        try {
            const response = await fetch(globalRefresh.dataset.statusUrl, {
                credentials: "same-origin",
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || "Refresh status is unavailable.");
            }
            renderGlobalRefresh(payload.refresh);
            if (payload.refresh?.active) {
                globalRefreshPollTimer = window.setTimeout(pollGlobalRefresh, 1500);
            }
        } catch (_error) {
            if (globalRefreshLabel) {
                globalRefreshLabel.textContent = "Refresh status unavailable";
            }
            globalRefreshButton.disabled = false;
            globalRefreshButton.removeAttribute("aria-busy");
            globalRefreshSpinner.hidden = true;
        }
    };

    globalRefresh?.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (globalRefreshButton.disabled) {
            return;
        }
        globalRefreshButton.disabled = true;
        globalRefreshButton.setAttribute("aria-busy", "true");
        globalRefreshSpinner.hidden = false;
        refreshResultHistoryLocked = false;
        if (globalRefreshLabel) {
            globalRefreshLabel.textContent = "Starting refresh…";
        }
        try {
            const response = await fetch(globalRefresh.action, {
                method: "POST",
                body: new FormData(globalRefresh),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || "The refresh could not start.");
            }
            globalRefreshReloadPending = true;
            renderGlobalRefresh(payload.refresh);
            announce(payload.detail || "Confluence refresh started in the background.");
            if (!globalRefreshReloadScheduled) {
                if (globalRefreshPollTimer) {
                    window.clearTimeout(globalRefreshPollTimer);
                }
                globalRefreshPollTimer = window.setTimeout(pollGlobalRefresh, 500);
            }
        } catch (error) {
            const detail = error instanceof Error ? error.message : "The refresh could not start.";
            if (globalRefreshLabel) {
                globalRefreshLabel.textContent = "Refresh could not start";
            }
            if (globalRefreshTime) {
                globalRefreshTime.textContent = detail;
            }
            globalRefreshButton.disabled = false;
            globalRefreshButton.removeAttribute("aria-busy");
            globalRefreshSpinner.hidden = true;
            announce(detail);
        }
    });

    if (globalRefresh?.dataset.active === "true") {
        globalRefreshPollTimer = window.setTimeout(pollGlobalRefresh, 300);
    } else if (globalRefreshTime) {
        const formatted = formatRefreshTimestamp(globalRefresh.dataset.lastCompletedAt);
        if (formatted) {
            globalRefreshTime.textContent = `Last completed ${formatted}`;
        }
    }

    window.addEventListener("owl:refresh-status", (event) => {
        renderGlobalRefresh(event.detail || {});
    });

    const safeStorage = {
        get(key, fallback = null) {
            try {
                const value = window.localStorage.getItem(key);
                return value === null ? fallback : value;
            } catch (_error) {
                return fallback;
            }
        },
        set(key, value) {
            try {
                window.localStorage.setItem(key, value);
            } catch (_error) {
                // The core interface remains usable when browser storage is disabled.
            }
        },
    };

    const expansionStorageKey = "owl.bookmark-manager.tree-expansion.v1";
    const selectionStorageKey = "owl.bookmark-manager.selection.v1";
    const checkedStorageKey = "owl.bookmark-manager.checked.v1";
    const treeScrollStorageKey = "owl.bookmark-manager.tree-scroll.v1";

    const readJsonObject = (key) => {
        try {
            return JSON.parse(safeStorage.get(key, "{}")) || {};
        } catch (_error) {
            return {};
        }
    };

    const setTreeExpanded = (button, expanded, persist = true) => {
        const groupId = button.getAttribute("aria-controls");
        const group = groupId ? document.getElementById(groupId) : null;
        button.setAttribute("aria-expanded", String(expanded));
        button.closest("[role='treeitem']")?.setAttribute("aria-expanded", String(expanded));
        if (group) {
            group.hidden = !expanded;
        }
        if (persist) {
            const states = readJsonObject(expansionStorageKey);
            states[button.dataset.nodeKey] = expanded;
            safeStorage.set(expansionStorageKey, JSON.stringify(states));
        }
    };

    const storedExpansion = readJsonObject(expansionStorageKey);
    document.querySelectorAll("[data-tree-toggle]").forEach((button) => {
        const key = button.dataset.nodeKey;
        const expanded = Object.hasOwn(storedExpansion, key) ? storedExpansion[key] : true;
        setTreeExpanded(button, Boolean(expanded), false);
        button.addEventListener("click", () => {
            setTreeExpanded(button, button.getAttribute("aria-expanded") !== "true");
        });
    });

    const locatedBookmark = document.querySelector("[data-located-bookmark]");
    if (locatedBookmark) {
        let ancestorGroup = locatedBookmark.closest("[role='group']");
        while (ancestorGroup) {
            const owner = document.querySelector(
                `[data-tree-toggle][aria-controls="${CSS.escape(ancestorGroup.id)}"]`,
            );
            if (!owner) {
                break;
            }
            setTreeExpanded(owner, true);
            ancestorGroup = owner.closest("[role='group']");
        }
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        locatedBookmark.scrollIntoView({
            block: "center",
            behavior: reduceMotion ? "auto" : "smooth",
        });
        locatedBookmark.focus({ preventScroll: true });
    }

    document.querySelectorAll("[data-tree-expand-all]").forEach((button) => {
        button.addEventListener("click", () => {
            document
                .querySelectorAll("[data-tree-toggle]")
                .forEach((toggle) => setTreeExpanded(toggle, true));
            announce("All bookmark branches expanded");
        });
    });
    document.querySelectorAll("[data-tree-collapse-all]").forEach((button) => {
        button.addEventListener("click", () => {
            document
                .querySelectorAll("[data-tree-toggle]")
                .forEach((toggle) => setTreeExpanded(toggle, false));
            announce("All bookmark branches collapsed");
        });
    });

    const treeScroll = document.querySelector("[data-tree-scroll]");
    if (treeScroll) {
        const previousScroll = Number.parseInt(safeStorage.get(treeScrollStorageKey, "0"), 10);
        if (Number.isFinite(previousScroll)) {
            treeScroll.scrollTop = previousScroll;
        }
        treeScroll.addEventListener("scroll", () => {
            safeStorage.set(treeScrollStorageKey, String(treeScroll.scrollTop));
        });
    }

    const treeRoot = document.querySelector("[data-bookmark-tree]");
    if (treeRoot && !treeRoot.querySelector('[data-tree-row][tabindex="0"]')) {
        treeRoot.querySelector("[data-tree-row]")?.setAttribute("tabindex", "0");
    }
    const storedSelection = safeStorage.get(selectionStorageKey, "");
    if (
        treeRoot &&
        !treeRoot.dataset.selectedBookmark &&
        storedSelection &&
        !window.location.search
    ) {
        const storedCheckbox = treeRoot.querySelector(
            `[data-tree-check][data-bookmark-id="${CSS.escape(storedSelection)}"]`,
        );
        if (storedCheckbox?.dataset.detailsUrl) {
            window.location.replace(storedCheckbox.dataset.detailsUrl);
        }
    }

    const treeChecks = Array.from(document.querySelectorAll("[data-tree-check]"));
    const checkedBookmarks = readJsonObject(checkedStorageKey);
    const selectAllCheckbox = document.querySelector("[data-tree-select-all]");
    const selectionCount = document.querySelector("[data-tree-selection-count]");
    const deleteSelectedButton = document.querySelector("[data-delete-selected]");
    const openSelectedButton = document.querySelector("[data-open-selected-button]");
    const detailsOpenButton = document.querySelector("[data-details-open-button]");
    let deleteRelockTimer = null;

    const showTreeRowDetails = (row) => {
        const detailsUrl = row?.dataset.detailsUrl;
        const bookmarkId = row?.dataset.bookmarkId;
        if (!detailsUrl || !bookmarkId) {
            return;
        }
        safeStorage.set(selectionStorageKey, bookmarkId);
        window.location.assign(detailsUrl);
    };

    treeRoot?.addEventListener("click", (event) => {
        const row = event.target.closest("[data-tree-row]");
        if (
            !row?.dataset.detailsUrl ||
            event.target.closest("button, input, label, a, form, select, textarea")
        ) {
            return;
        }
        showTreeRowDetails(row);
    });

    const directTreeCheck = (item) =>
        item?.querySelector(":scope > [data-tree-row] [data-tree-check]") || null;
    const descendantTreeChecks = (item) =>
        Array.from(item?.querySelectorAll(":scope > ul [data-tree-check]") || []);
    const detailsCheckboxForItem = (item) => {
        const direct = directTreeCheck(item);
        if (direct?.dataset.detailsUrl) {
            return direct;
        }
        return (
            descendantTreeChecks(item).find(
                (candidate) => candidate.checked && candidate.dataset.detailsUrl,
            ) || null
        );
    };

    const refreshItemAggregate = (item) => {
        const checkbox = directTreeCheck(item);
        const descendants = descendantTreeChecks(item);
        if (!checkbox || !descendants.length) {
            if (checkbox) {
                checkbox.indeterminate = false;
            }
            return;
        }
        const allDescendants = descendants.every(
            (candidate) => candidate.checked && !candidate.indeterminate,
        );
        const anyDescendants = descendants.some(
            (candidate) => candidate.checked || candidate.indeterminate,
        );
        if (checkbox.dataset.bookmarkId) {
            const ownBookmarkSelected = checkbox.checked;
            checkbox.indeterminate =
                (ownBookmarkSelected || anyDescendants) &&
                !(ownBookmarkSelected && allDescendants);
        } else {
            checkbox.checked = allDescendants;
            checkbox.indeterminate = anyDescendants && !allDescendants;
        }
    };

    const refreshAncestorAggregates = (item) => {
        let parentItem = item?.parentElement?.closest("[data-tree-item]");
        while (parentItem) {
            refreshItemAggregate(parentItem);
            parentItem = parentItem.parentElement?.closest("[data-tree-item]");
        }
    };

    const bookmarkTreeChecks = () =>
        treeChecks.filter((checkbox) => checkbox.dataset.bookmarkId);
    const selectedBookmarkChecks = () =>
        bookmarkTreeChecks().filter((checkbox) => checkbox.checked);

    const lockDeleteSelected = () => {
        if (!deleteSelectedButton) {
            return;
        }
        deleteSelectedButton.dataset.deleteLocked = "true";
        const icon = deleteSelectedButton.querySelector("[data-delete-lock-icon]");
        const label = deleteSelectedButton.querySelector("[data-delete-label]");
        if (icon) {
            icon.textContent = "🔒";
        }
        if (label) {
            const count = selectedBookmarkChecks().length;
            label.textContent = count ? `Delete selected (${count})` : "Delete selected";
        }
        if (deleteRelockTimer) {
            window.clearTimeout(deleteRelockTimer);
            deleteRelockTimer = null;
        }
    };

    const persistCheckedBookmarks = () => {
        const nextChecked = {};
        selectedBookmarkChecks().forEach((checkbox) => {
            nextChecked[checkbox.dataset.bookmarkId] = true;
        });
        safeStorage.set(checkedStorageKey, JSON.stringify(nextChecked));
    };

    const refreshSelectionControls = () => {
        const bookmarkChecks = bookmarkTreeChecks();
        const selectedChecks = selectedBookmarkChecks();
        const count = selectedChecks.length;
        const openableCount = selectedChecks.filter(
            (checkbox) => checkbox.dataset.openUrl,
        ).length;
        if (selectAllCheckbox) {
            selectAllCheckbox.disabled = bookmarkChecks.length === 0;
            selectAllCheckbox.checked =
                bookmarkChecks.length > 0 && count === bookmarkChecks.length;
            selectAllCheckbox.indeterminate = count > 0 && count < bookmarkChecks.length;
        }
        if (selectionCount) {
            selectionCount.textContent = `${count} selected`;
        }
        if (deleteSelectedButton) {
            deleteSelectedButton.disabled = count === 0;
            lockDeleteSelected();
        }
        if (openSelectedButton) {
            const openSelectedLabel = count
                ? openableCount === 1
                    ? "Open 1 selected live bookmark in a new tab"
                    : `Open ${openableCount} selected live bookmarks in separate tabs`
                : "Select bookmarks to open";
            openSelectedButton.disabled = openableCount === 0;
            openSelectedButton.setAttribute("aria-label", openSelectedLabel);
            openSelectedButton.title = openSelectedLabel;
        }
        if (detailsOpenButton) {
            const defaultLabel =
                detailsOpenButton.dataset.defaultAriaLabel || "Open saved bookmark";
            if (count > 1) {
                const selectedLabel = `Open ${openableCount} selected live bookmark${
                    openableCount === 1 ? "" : "s"
                } in separate tabs`;
                detailsOpenButton.setAttribute("aria-label", selectedLabel);
                detailsOpenButton.title = selectedLabel;
                detailsOpenButton.disabled = openableCount === 0;
            } else {
                detailsOpenButton.setAttribute("aria-label", defaultLabel);
                detailsOpenButton.title = defaultLabel;
                detailsOpenButton.disabled = false;
            }
        }
    };

    deleteSelectedButton?.addEventListener("click", (event) => {
        if (deleteSelectedButton.dataset.deleteLocked !== "true") {
            return;
        }
        event.preventDefault();
        deleteSelectedButton.dataset.deleteLocked = "false";
        const icon = deleteSelectedButton.querySelector("[data-delete-lock-icon]");
        const label = deleteSelectedButton.querySelector("[data-delete-label]");
        if (icon) {
            icon.textContent = "🔓";
        }
        if (label) {
            label.textContent = "Click again to delete";
        }
        announce("Delete unlocked. Click again to remove the selected bookmarks.");
        deleteRelockTimer = window.setTimeout(lockDeleteSelected, 10000);
    });

    treeChecks.forEach((checkbox) => {
        checkbox.checked = Boolean(
            checkbox.dataset.bookmarkId && checkedBookmarks[checkbox.dataset.bookmarkId],
        );
        checkbox.indeterminate = false;
    });
    Array.from(document.querySelectorAll("[data-tree-item]"))
        .reverse()
        .forEach(refreshItemAggregate);
    persistCheckedBookmarks();
    refreshSelectionControls();

    selectAllCheckbox?.addEventListener("change", () => {
        treeChecks.forEach((checkbox) => {
            checkbox.checked = selectAllCheckbox.checked;
            checkbox.indeterminate = false;
        });
        Array.from(document.querySelectorAll("[data-tree-item]"))
            .reverse()
            .forEach(refreshItemAggregate);
        persistCheckedBookmarks();
        refreshSelectionControls();
        const count = selectedBookmarkChecks().length;
        announce(
            count
                ? `All ${count} shown bookmark${count === 1 ? "" : "s"} selected`
                : "All shown bookmarks cleared",
        );
    });

    treeChecks.forEach((checkbox) => {
        checkbox.addEventListener("change", () => {
            const item = checkbox.closest("[data-tree-item]");
            checkbox.indeterminate = false;
            descendantTreeChecks(item).forEach((descendant) => {
                descendant.checked = checkbox.checked;
                descendant.indeterminate = false;
            });
            refreshAncestorAggregates(item);
            persistCheckedBookmarks();
            refreshSelectionControls();
            const count = selectedBookmarkChecks().length;
            announce(`${count} bookmark${count === 1 ? "" : "s"} selected`);

            // A checked row is also the page the user is working with. Folder-only
            // rows open the first bookmarked child selected by the cascade.
            if (checkbox.checked) {
                const detailsCheckbox = detailsCheckboxForItem(item);
                if (
                    detailsCheckbox &&
                    detailsCheckbox.dataset.bookmarkId !== treeRoot?.dataset.selectedBookmark
                ) {
                    showTreeRowDetails(detailsCheckbox.closest("[data-tree-row]"));
                }
            }
        });
    });

    const folderMoveUrl = treeRoot?.dataset.folderMoveUrl || "";
    const folderReturnUrl =
        treeRoot?.dataset.folderReturnUrl || `${window.location.pathname}${window.location.search}`;
    const folderDropTargets = Array.from(document.querySelectorAll("[data-folder-drop-target]"));
    const bookmarkDragHandles = Array.from(document.querySelectorAll("[data-bookmark-drag]"));
    let draggedBookmarkId = "";
    let draggedBookmarkTitle = "";

    const folderActionPayload = async (response) => {
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {};
        }
        if (!response.ok) {
            throw new Error(payload.detail || "The personal-folder change could not be saved.");
        }
        return payload;
    };

    const selectedIdsForFolderMove = (fallbackId) => {
        const selected = selectedBookmarkChecks().map((checkbox) => checkbox.dataset.bookmarkId);
        return selected.includes(String(fallbackId)) && selected.length > 1
            ? selected
            : [String(fallbackId)];
    };

    const moveBookmarksToFolder = async (bookmarkIds, folderId) => {
        if (!folderMoveUrl || !bookmarkIds.length) {
            return;
        }
        const body = new FormData();
        bookmarkIds.forEach((bookmarkId) => body.append("bookmark_ids", bookmarkId));
        body.append("folder_id", folderId || "");
        body.append("return_to", folderReturnUrl);
        const response = await fetch(folderMoveUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest",
            },
            body,
        });
        const payload = await folderActionPayload(response);
        announce(payload.detail || "Bookmark folder updated");
        window.location.assign(payload.redirect || folderReturnUrl);
    };

    bookmarkDragHandles.forEach((handle) => {
        handle.addEventListener("dragstart", (event) => {
            draggedBookmarkId = handle.dataset.bookmarkId || "";
            draggedBookmarkTitle = handle.dataset.bookmarkTitle || "bookmark";
            if (!draggedBookmarkId || !event.dataTransfer) {
                event.preventDefault();
                return;
            }
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", draggedBookmarkId);
            handle.closest("[data-tree-row]")?.classList.add("is-dragging");
            announce(`Moving ${draggedBookmarkTitle}. Drop it on a personal folder.`);
        });
        handle.addEventListener("dragend", () => {
            handle.closest("[data-tree-row]")?.classList.remove("is-dragging");
            folderDropTargets.forEach((target) => target.classList.remove("is-drop-target"));
            draggedBookmarkId = "";
            draggedBookmarkTitle = "";
        });
    });

    folderDropTargets.forEach((target) => {
        target.addEventListener("dragenter", (event) => {
            if (!draggedBookmarkId) {
                return;
            }
            event.preventDefault();
            target.classList.add("is-drop-target");
        });
        target.addEventListener("dragover", (event) => {
            if (!draggedBookmarkId) {
                return;
            }
            event.preventDefault();
            if (event.dataTransfer) {
                event.dataTransfer.dropEffect = "move";
            }
        });
        target.addEventListener("dragleave", (event) => {
            const relatedTarget = event.relatedTarget;
            if (!(relatedTarget instanceof Node) || !target.contains(relatedTarget)) {
                target.classList.remove("is-drop-target");
            }
        });
        target.addEventListener("drop", async (event) => {
            if (!draggedBookmarkId) {
                return;
            }
            event.preventDefault();
            target.classList.remove("is-drop-target");
            const folderId = target.dataset.folderDropTarget || "";
            const bookmarkIds = selectedIdsForFolderMove(draggedBookmarkId);
            try {
                await moveBookmarksToFolder(bookmarkIds, folderId);
            } catch (error) {
                announce(error instanceof Error ? error.message : "The bookmark could not be moved.");
            }
        });
    });

    const folderCreateForm = document.querySelector("[data-folder-create-form]");
    const folderCreateToggle = document.querySelector("[data-folder-create-toggle]");
    const folderCreatePanel = document.querySelector("[data-folder-create-panel]");
    const folderNameInput = folderCreateForm?.querySelector("[name='name']");
    const closeFolderCreate = () => {
        if (folderCreatePanel) {
            folderCreatePanel.hidden = true;
        }
        folderCreateToggle?.setAttribute("aria-expanded", "false");
    };
    folderCreateToggle?.addEventListener("click", () => {
        const opening = Boolean(folderCreatePanel?.hidden);
        if (folderCreatePanel) {
            folderCreatePanel.hidden = !opening;
        }
        folderCreateToggle.setAttribute("aria-expanded", String(opening));
        if (opening) {
            folderNameInput?.focus();
        }
    });
    folderCreateForm?.querySelector("[data-folder-create-cancel]")?.addEventListener("click", () => {
        closeFolderCreate();
        folderCreateToggle?.focus();
    });
    folderCreateForm?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            closeFolderCreate();
            folderCreateToggle?.focus();
        }
    });
    folderCreateForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submitButton = folderCreateForm.querySelector("button[type='submit']");
        if (submitButton) {
            submitButton.disabled = true;
        }
        try {
            const response = await fetch(folderCreateForm.action, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-CSRFToken": csrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: new FormData(folderCreateForm),
            });
            const payload = await folderActionPayload(response);
            announce(payload.detail || "Personal folder created");
            window.location.assign(payload.redirect || folderReturnUrl);
        } catch (error) {
            announce(error instanceof Error ? error.message : "The folder could not be created.");
            folderNameInput?.focus();
        } finally {
            if (submitButton) {
                submitButton.disabled = false;
            }
        }
    });

    document.querySelectorAll("[data-folder-move-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const fallbackId = form.dataset.bookmarkId || "";
            const folderSelect = form.querySelector("[name='folder_id']");
            const bookmarkIds = selectedIdsForFolderMove(fallbackId);
            const submitButton = form.querySelector("button[type='submit']");
            if (submitButton) {
                submitButton.disabled = true;
            }
            try {
                await moveBookmarksToFolder(bookmarkIds, folderSelect?.value || "");
            } catch (error) {
                announce(error instanceof Error ? error.message : "The bookmark could not be moved.");
                if (submitButton) {
                    submitButton.disabled = false;
                }
            }
        });
    });

    const tagAutocomplete = document.querySelector("[data-tag-autocomplete]");
    const tagInput = tagAutocomplete?.querySelector("#bookmark-organisation-tags");
    const tagSuggestionList = tagAutocomplete?.querySelector("[data-tag-suggestion-list]");
    const tagSuggestionData = tagAutocomplete?.querySelector("#bookmark-tag-suggestions-data");
    let tagSuggestions = [];
    let activeTagSuggestion = -1;

    if (tagSuggestionData) {
        try {
            const parsedSuggestions = JSON.parse(tagSuggestionData.textContent || "[]");
            tagSuggestions = Array.isArray(parsedSuggestions)
                ? parsedSuggestions.filter((value) => typeof value === "string")
                : [];
        } catch (_error) {
            tagSuggestions = [];
        }
    }

    const normalizedTag = (value) =>
        String(value || "")
            .normalize("NFKC")
            .replace(/\s+/g, " ")
            .trim()
            .toLocaleLowerCase();

    const activeTagToken = () => {
        if (!(tagInput instanceof HTMLInputElement)) {
            return null;
        }
        const value = tagInput.value;
        const cursor = tagInput.selectionStart ?? value.length;
        const start = value.lastIndexOf(",", cursor - 1) + 1;
        const nextComma = value.indexOf(",", cursor);
        const end = nextComma === -1 ? value.length : nextComma;
        const segment = value.slice(start, end);
        return {
            end,
            query: segment.trim(),
            start,
            leadingSpace: segment.match(/^\s*/)?.[0] || "",
            trailingSpace: segment.match(/\s*$/)?.[0] || "",
        };
    };

    const tagSuggestionButtons = () =>
        Array.from(tagSuggestionList?.querySelectorAll("[data-tag-suggestion]") || []);

    const hideTagSuggestions = () => {
        if (tagSuggestionList) {
            tagSuggestionList.hidden = true;
            tagSuggestionList.replaceChildren();
        }
        activeTagSuggestion = -1;
        tagInput?.setAttribute("aria-expanded", "false");
        tagInput?.removeAttribute("aria-activedescendant");
    };

    const activateTagSuggestion = (index) => {
        const buttons = tagSuggestionButtons();
        if (!buttons.length || !(tagInput instanceof HTMLInputElement)) {
            return;
        }
        activeTagSuggestion = (index + buttons.length) % buttons.length;
        buttons.forEach((button, buttonIndex) => {
            const active = buttonIndex === activeTagSuggestion;
            button.setAttribute("aria-selected", String(active));
            button.classList.toggle("is-active", active);
        });
        const activeButton = buttons[activeTagSuggestion];
        tagInput.setAttribute("aria-activedescendant", activeButton.id);
        activeButton.scrollIntoView({ block: "nearest" });
    };

    const chooseTagSuggestion = (tagName) => {
        const token = activeTagToken();
        if (!token || !(tagInput instanceof HTMLInputElement)) {
            return;
        }
        const replacement = `${token.leadingSpace}${tagName}${token.trailingSpace}`;
        tagInput.value =
            tagInput.value.slice(0, token.start) + replacement + tagInput.value.slice(token.end);
        const caret = token.start + token.leadingSpace.length + tagName.length;
        tagInput.setSelectionRange(caret, caret);
        tagInput.dispatchEvent(new Event("change", { bubbles: true }));
        hideTagSuggestions();
        tagInput.focus();
    };

    const renderTagSuggestions = () => {
        const token = activeTagToken();
        if (!token || !tagSuggestionList || !(tagInput instanceof HTMLInputElement)) {
            return;
        }
        const query = normalizedTag(token.query);
        if (!query) {
            hideTagSuggestions();
            return;
        }
        const selectedTags = new Set(
            `${tagInput.value.slice(0, token.start)},${tagInput.value.slice(token.end)}`
                .split(",")
                .map(normalizedTag)
                .filter(Boolean),
        );
        const matches = tagSuggestions
            .filter((tagName) => {
                const candidate = normalizedTag(tagName);
                return candidate.includes(query) && !selectedTags.has(candidate);
            })
            .slice(0, 8);

        tagSuggestionList.replaceChildren();
        matches.forEach((tagName, index) => {
            const option = document.createElement("button");
            option.type = "button";
            option.id = `bookmark-tag-suggestion-${index}`;
            option.className = "tag-suggestion";
            option.dataset.tagSuggestion = tagName;
            option.setAttribute("role", "option");
            option.setAttribute("aria-selected", "false");
            option.textContent = tagName;
            tagSuggestionList.append(option);
        });
        if (!matches.length) {
            hideTagSuggestions();
            return;
        }
        tagSuggestionList.hidden = false;
        activeTagSuggestion = -1;
        tagInput.setAttribute("aria-expanded", "true");
        tagInput.removeAttribute("aria-activedescendant");
    };

    tagInput?.addEventListener("input", renderTagSuggestions);
    tagInput?.addEventListener("focus", renderTagSuggestions);
    tagInput?.addEventListener("keydown", (event) => {
        const buttons = tagSuggestionButtons();
        if (event.key === "Escape") {
            hideTagSuggestions();
            return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            if (!buttons.length) {
                renderTagSuggestions();
            }
            const available = tagSuggestionButtons();
            if (available.length) {
                event.preventDefault();
                const direction = event.key === "ArrowDown" ? 1 : -1;
                const nextIndex =
                    activeTagSuggestion < 0 && direction < 0
                        ? available.length - 1
                        : activeTagSuggestion + direction;
                activateTagSuggestion(nextIndex);
            }
            return;
        }
        if (event.key === "Enter" && activeTagSuggestion >= 0) {
            const selected = buttons[activeTagSuggestion];
            if (selected?.dataset.tagSuggestion) {
                event.preventDefault();
                chooseTagSuggestion(selected.dataset.tagSuggestion);
            }
        }
    });
    tagSuggestionList?.addEventListener("pointerdown", (event) => event.preventDefault());
    tagSuggestionList?.addEventListener("click", (event) => {
        const option = event.target.closest("[data-tag-suggestion]");
        if (option?.dataset.tagSuggestion) {
            chooseTagSuggestion(option.dataset.tagSuggestion);
        }
    });
    tagInput?.addEventListener("blur", () => window.setTimeout(hideTagSuggestions, 100));

    const refreshBookmarkPresentation = (payload) => {
        if (!payload.bookmark_id) {
            return;
        }
        document
            .querySelectorAll(`[data-bookmark-id="${CSS.escape(String(payload.bookmark_id))}"]`)
            .forEach((element) => {
                if (payload.favorite !== undefined && element.matches("[data-favorite-state]")) {
                    element.dataset.favoriteState = String(payload.favorite);
                    element.setAttribute("aria-pressed", String(payload.favorite));
                    element.textContent = element.classList.contains("tree-star")
                        ? payload.favorite
                            ? "★"
                            : "☆"
                        : payload.favorite
                          ? "★ Favorite"
                          : "☆ Favorite";
                }
                if (payload.pinned !== undefined && element.matches("[data-pin-state]")) {
                    element.dataset.pinState = String(payload.pinned);
                    element.setAttribute("aria-pressed", String(payload.pinned));
                    element.textContent = payload.pinned ? "Pinned" : "Pin";
                }
            });
    };

    const submitProductivityForm = async (form) => {
        const button = form.querySelector("button[type='submit']");
        button?.setAttribute("aria-busy", "true");
        if (button) {
            button.disabled = true;
        }
        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || "The change could not be saved.");
            }
            announce(payload.detail || payload.label || "Bookmark updated");
            refreshBookmarkPresentation(payload);

            const currentQuery = new URLSearchParams(window.location.search);
            const removedFromCurrentShortcut =
                (currentQuery.get("favorite") === "on" && payload.favorite === false) ||
                (currentQuery.get("pinned") === "on" && payload.pinned === false);
            if (removedFromCurrentShortcut) {
                window.location.reload();
                return;
            }

            if (payload.notes !== undefined) {
                document
                    .querySelectorAll(
                        `[data-notes-display][data-bookmark-id="${CSS.escape(String(payload.bookmark_id))}"]`,
                    )
                    .forEach((element) => {
                        element.textContent = payload.notes || "No personal note yet.";
                    });
            }
            if (Array.isArray(payload.tags)) {
                const text = payload.tags.length ? payload.tags.join(", ") : "No tags";
                document
                    .querySelectorAll(
                        `[data-tags-display][data-bookmark-id="${CSS.escape(String(payload.bookmark_id))}"]`,
                    )
                    .forEach((element) => {
                        element.textContent = text;
                    });
            }
            if (payload.redirect) {
                window.location.assign(payload.redirect);
            }
        } catch (error) {
            announce(error instanceof Error ? error.message : "The change could not be saved.");
        } finally {
            button?.removeAttribute("aria-busy");
            if (button) {
                button.disabled = false;
            }
        }
    };

    document.querySelectorAll("[data-productivity-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            await submitProductivityForm(form);
        });
    });

    document.querySelectorAll("[data-confirm-message]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (!window.confirm(form.dataset.confirmMessage)) {
                event.preventDefault();
            }
        });
    });

    document.querySelectorAll("[data-local-redirect-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            if (event.defaultPrevented) {
                return;
            }
            event.preventDefault();
            const button = form.querySelector("button[type='submit']");
            await submitLocalForm(form, statusText, button);
        });
    });

    const selectedOpenRequests = (checkedBookmarks = selectedBookmarkChecks()) =>
        checkedBookmarks
            .filter((checkbox) => checkbox.dataset.openUrl)
            .map((checkbox) => ({
                action: checkbox.dataset.openUrl,
                title: checkbox.dataset.bookmarkTitle || "Saved bookmark",
            }));

    const openBookmarkRequests = async ({
        requests,
        button,
        selectedCount = requests.length,
        openSelected = false,
    }) => {
        if (!requests.length) {
            announce("None of the selected bookmarks can be opened.");
            return;
        }
        const openedWindows = requests.map(() => {
            const externalWindow = window.open("about:blank", "_blank");
            if (externalWindow) {
                externalWindow.opener = null;
            }
            return externalWindow;
        });
        button?.setAttribute("aria-busy", "true");
        if (button) {
            button.disabled = true;
        }
        try {
            const results = await Promise.all(
                requests.map(async (request, index) => {
                    const requestData = new FormData();
                    requestData.set("csrfmiddlewaretoken", csrfToken());
                    try {
                        const response = await fetch(request.action, {
                            method: "POST",
                            body: requestData,
                            credentials: "same-origin",
                            headers: {
                                Accept: "application/json",
                                "X-Requested-With": "XMLHttpRequest",
                            },
                        });
                        const payload = await response.json();
                        if (!response.ok || !payload.url) {
                            throw new Error(
                                payload.detail || "The page could not be opened safely.",
                            );
                        }
                        if (openedWindows[index]) {
                            openedWindows[index].location.replace(payload.url);
                        } else {
                            window.open(payload.url, "_blank", "noopener,noreferrer");
                        }
                        return { opened: true, title: request.title, detail: payload.detail };
                    } catch (error) {
                        openedWindows[index]?.close();
                        return {
                            opened: false,
                            title: request.title,
                            detail:
                                error instanceof Error
                                    ? error.message
                                    : "The page could not be opened safely.",
                        };
                    }
                }),
            );
            const openedCount = results.filter((result) => result.opened).length;
            const failedResults = results.filter((result) => !result.opened);
            const unavailableCount = openSelected ? selectedCount - requests.length : 0;
            if (failedResults.length || unavailableCount) {
                const failedCount = failedResults.length + unavailableCount;
                announce(
                    `${openedCount} bookmark${
                        openedCount === 1 ? "" : "s"
                    } opened; ${failedCount} could not be opened.`,
                );
            } else {
                announce(
                    openSelected
                        ? openedCount === 1
                            ? "1 selected bookmark opened in a new tab."
                            : `${openedCount} selected bookmarks opened in separate tabs.`
                        : results[0]?.detail || "Opened saved bookmark",
                );
            }
        } finally {
            button?.removeAttribute("aria-busy");
            if (button) {
                button.disabled = false;
            }
            refreshSelectionControls();
        }
    };

    openSelectedButton?.addEventListener("click", async () => {
        const checkedBookmarks = selectedBookmarkChecks();
        await openBookmarkRequests({
            requests: selectedOpenRequests(checkedBookmarks),
            button: openSelectedButton,
            selectedCount: checkedBookmarks.length,
            openSelected: true,
        });
    });

    document.querySelectorAll("[data-external-open-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector("button[type='submit']");
            const checkedBookmarks = selectedBookmarkChecks();
            const openSelected =
                form.matches("[data-details-open-form]") && checkedBookmarks.length > 1;
            await openBookmarkRequests({
                requests: openSelected
                    ? selectedOpenRequests(checkedBookmarks)
                    : [{ action: form.action, title: "Saved bookmark" }],
                button,
                selectedCount: checkedBookmarks.length,
                openSelected,
            });
        });
    });

    const copyText = async (value) => {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            return;
        }
        const fallback = document.createElement("textarea");
        fallback.value = value;
        fallback.setAttribute("readonly", "");
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        const copied = document.execCommand("copy");
        fallback.remove();
        if (!copied) {
            throw new Error("Copy unavailable");
        }
    };

    const copyFeedbackTimers = new WeakMap();
    const resetCopyFeedback = (button) => {
        const defaultLabel = button.dataset.copyDefaultLabel;
        button.classList.remove("is-copied");
        if (defaultLabel) {
            button.setAttribute("aria-label", defaultLabel);
            button.title = defaultLabel;
        }
        copyFeedbackTimers.delete(button);
    };

    const showCopyFeedback = (button) => {
        if (!button.dataset.copyDefaultLabel) {
            return;
        }
        const previousTimer = copyFeedbackTimers.get(button);
        if (previousTimer) {
            window.clearTimeout(previousTimer);
        }
        const successLabel = button.dataset.copySuccess || "Copied";
        button.classList.add("is-copied");
        button.setAttribute("aria-label", successLabel);
        button.title = successLabel;
        copyFeedbackTimers.set(
            button,
            window.setTimeout(() => resetCopyFeedback(button), 1000),
        );
    };

    document.querySelectorAll("[data-copy-value]").forEach((button) => {
        button.addEventListener("click", async () => {
            try {
                await copyText(button.dataset.copyValue || "");
                const successLabel = button.dataset.copySuccess || "Copied";
                showCopyFeedback(button);
                announce(successLabel);
            } catch (_error) {
                announce("Copy was not available. Select the value and copy it manually.");
            }
        });
    });

    let searchTimer = null;
    const isCompleteHttpUrl = (value) => /^https?:\/\/\S+$/i.test(value.trim());
    const clearUrlSearchScope = () => {
        const category = bookmarkUnifiedForm?.querySelector("[name='category']");
        if (category instanceof HTMLInputElement) {
            category.value = "";
        }
    };

    const refocusUnmatchedUrlSearch = () => {
        if (
            !bookmarkSearch ||
            !bookmarkUnifiedForm ||
            bookmarkUnifiedForm.dataset.urlSearchComplete !== "true"
        ) {
            return;
        }
        const matchCount = Number.parseInt(
            bookmarkUnifiedForm.dataset.urlMatchCount || "0",
            10,
        );
        if (matchCount !== 0 || !isCompleteHttpUrl(bookmarkSearch.value)) {
            return;
        }
        bookmarkSearch.focus({ preventScroll: true });
        const caretPosition = bookmarkSearch.value.length;
        bookmarkSearch.setSelectionRange(caretPosition, caretPosition);
    };

    refocusUnmatchedUrlSearch();

    bookmarkSearch?.addEventListener("keydown", (event) => {
        if (
            event.key !== "Enter" ||
            event.isComposing ||
            event.ctrlKey ||
            event.metaKey ||
            event.altKey ||
            event.shiftKey ||
            !bookmarkUnifiedForm
        ) {
            return;
        }

        event.preventDefault();
        window.clearTimeout(searchTimer);
        const value = bookmarkSearch.value.trim();
        if (!isCompleteHttpUrl(value)) {
            bookmarkUnifiedForm.requestSubmit();
            return;
        }

        clearUrlSearchScope();
        const completedValue = (bookmarkUnifiedForm.dataset.completedSearch || "").trim();
        const searchComplete =
            bookmarkUnifiedForm.dataset.urlSearchComplete === "true" && completedValue === value;
        if (!searchComplete) {
            bookmarkUnifiedForm.requestSubmit();
            return;
        }

        const matchCount = Number.parseInt(bookmarkUnifiedForm.dataset.urlMatchCount || "0", 10);
        const matchHref = bookmarkUnifiedForm.dataset.urlMatchHref || "";
        if (matchCount > 0) {
            if (matchHref) {
                window.location.assign(matchHref);
            }
            return;
        }
        if (bookmarkSaveButton instanceof HTMLElement) {
            if (bookmarkSaveInFlight || bookmarkSaveButton.disabled) {
                return;
            }
            bookmarkUnifiedForm.requestSubmit(bookmarkSaveButton);
        }
    });

    bookmarkSearch?.addEventListener("input", (event) => {
        window.clearTimeout(searchTimer);
        const value = event.target.value.trim();
        if (isCompleteHttpUrl(value)) {
            clearUrlSearchScope();
        }
        const delay = /^\d+$/.test(value) || /^https?:\/\//i.test(value) ? 0 : 350;
        searchTimer = window.setTimeout(() => event.target.form?.requestSubmit(), delay);
    });

    const visibleTreeRows = () =>
        Array.from(document.querySelectorAll("[data-tree-row]")).filter(
            (row) => row.getClientRects().length > 0,
        );

    document.querySelector("[data-bookmark-tree]")?.addEventListener("keydown", (event) => {
        const keyTarget = event.target;
        if (
            keyTarget instanceof HTMLInputElement ||
            keyTarget instanceof HTMLButtonElement ||
            keyTarget instanceof HTMLAnchorElement ||
            keyTarget instanceof HTMLTextAreaElement ||
            keyTarget instanceof HTMLSelectElement ||
            keyTarget?.isContentEditable
        ) {
            return;
        }
        const row = event.target.closest("[data-tree-row]");
        if (!row) {
            return;
        }
        const rows = visibleTreeRows();
        const index = rows.indexOf(row);
        const toggle = row.querySelector("[data-tree-toggle]");
        const detailsUrl = row.dataset.detailsUrl;
        const shortcut = event.key.toLowerCase();

        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const offset = event.key === "ArrowDown" ? 1 : -1;
            rows[Math.max(0, Math.min(rows.length - 1, index + offset))]?.focus();
        } else if (event.key === "ArrowRight" && toggle) {
            event.preventDefault();
            if (toggle.getAttribute("aria-expanded") !== "true") {
                setTreeExpanded(toggle, true);
            } else {
                const currentLevel = Number.parseInt(row.getAttribute("aria-level") || "1", 10);
                const nextRow = rows[index + 1];
                const nextLevel = Number.parseInt(nextRow?.getAttribute("aria-level") || "0", 10);
                if (nextRow && nextLevel > currentLevel) {
                    nextRow.focus();
                }
            }
        } else if (event.key === "ArrowLeft") {
            event.preventDefault();
            if (toggle?.getAttribute("aria-expanded") === "true") {
                setTreeExpanded(toggle, false);
            } else {
                const currentLevel = Number.parseInt(row.getAttribute("aria-level") || "1", 10);
                for (let candidateIndex = index - 1; candidateIndex >= 0; candidateIndex -= 1) {
                    const candidate = rows[candidateIndex];
                    const candidateLevel = Number.parseInt(
                        candidate.getAttribute("aria-level") || "1",
                        10,
                    );
                    if (candidateLevel < currentLevel) {
                        candidate.focus();
                        break;
                    }
                }
            }
        } else if (event.key === "Enter" && detailsUrl) {
            event.preventDefault();
            showTreeRowDetails(row);
        } else if (event.key === "Enter" && toggle) {
            event.preventDefault();
            toggle.click();
        } else if (shortcut === "e" && toggle) {
            event.preventDefault();
            toggle.click();
        } else if (shortcut === "f") {
            const favoriteForm = row.querySelector("[data-favorite-form]");
            if (favoriteForm) {
                event.preventDefault();
                favoriteForm.requestSubmit();
            }
        } else if (shortcut === "p") {
            const pinForm = row.querySelector("[data-pin-form]");
            if (pinForm) {
                event.preventDefault();
                pinForm.requestSubmit();
            }
        }
    });
})();
