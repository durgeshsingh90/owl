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

    settingsForm?.querySelectorAll("[data-settings-cancel]").forEach((button) => {
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
                    window.location.assign(target.href);
                    return;
                }
            }
            let payload = null;
            if ((response.headers.get("content-type") || "").includes("application/json")) {
                payload = await response.json();
            }
            if (payload?.redirect) {
                const target = new URL(payload.redirect, window.location.href);
                if (target.origin === window.location.origin) {
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
                    return;
                }
            }
            if (statusTarget) {
                statusTarget.hidden = false;
                statusTarget.textContent = payload
                    ? `${payload.label} — ${payload.detail}`
                    : "The action could not be completed. Review the form and try again.";
                statusTarget.dataset.state = payload?.state || "error";
            }
        } catch (_error) {
            if (statusTarget) {
                statusTarget.hidden = false;
                statusTarget.textContent = "The local action could not be completed.";
                statusTarget.dataset.state = "unreachable";
            }
        } finally {
            busyButton?.removeAttribute("aria-busy");
            if (busyButton) {
                busyButton.disabled = false;
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
    bookmarkUnifiedForm?.addEventListener("submit", async (event) => {
        const submitter = event.submitter;
        if (!(submitter instanceof HTMLElement) || !submitter.matches("[data-bookmark-save]")) {
            return;
        }
        event.preventDefault();
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
        await submitLocalForm(
            bookmarkUnifiedForm,
            bookmarkSaveResult,
            submitter,
            saveAction,
            formData,
            500,
        );
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

    const statusText = document.querySelector("[data-global-status-text]");
    const announce = (message) => {
        if (statusText && message) {
            statusText.textContent = message;
        }
    };

    const globalRefresh = document.querySelector("[data-global-refresh]");
    const globalRefreshButton = globalRefresh?.querySelector("[data-global-refresh-button]");
    const globalRefreshSpinner = globalRefresh?.querySelector("[data-global-refresh-spinner]");
    const globalRefreshLabel = globalRefresh?.querySelector("[data-global-refresh-label]");
    const globalRefreshTime = globalRefresh?.querySelector("[data-global-refresh-time]");
    const globalRefreshProgress = globalRefresh?.querySelector("[data-global-refresh-progress]");
    let globalRefreshPollTimer = null;
    let globalRefreshWasActive = globalRefresh?.dataset.active === "true";

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
            timeStyle: "short",
        }).format(parsed);
    };

    const renderGlobalRefresh = (refresh) => {
        if (!globalRefresh || !refresh) {
            return;
        }
        const active = Boolean(refresh.active);
        const total = Number(refresh.total) || 0;
        const processed = Number(refresh.processed) || 0;
        const progress = Math.max(0, Math.min(100, Number(refresh.progress) || 0));
        globalRefresh.dataset.active = String(active);
        globalRefresh.dataset.status = refresh.status || "idle";
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

        if (globalRefreshWasActive && !active) {
            announce(
                refresh.status === "succeeded"
                    ? `Confluence refresh completed: ${refresh.succeeded} bookmarks updated.`
                    : refresh.detail || "Confluence refresh completed with errors.",
            );
        }
        globalRefreshWasActive = active;
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
            renderGlobalRefresh(payload.refresh);
            announce(payload.detail || "Confluence refresh started in the background.");
            if (globalRefreshPollTimer) {
                window.clearTimeout(globalRefreshPollTimer);
            }
            globalRefreshPollTimer = window.setTimeout(pollGlobalRefresh, 500);
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
    const selectionCount = document.querySelector("[data-tree-selection-count]");
    const deleteSelectedButton = document.querySelector("[data-delete-selected]");
    let deleteRelockTimer = null;

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

    const selectedBookmarkChecks = () =>
        treeChecks.filter((checkbox) => checkbox.dataset.bookmarkId && checkbox.checked);

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
        const count = selectedBookmarkChecks().length;
        if (selectionCount) {
            selectionCount.textContent = `${count} selected`;
        }
        if (deleteSelectedButton) {
            deleteSelectedButton.disabled = count === 0;
            lockDeleteSelected();
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
                    safeStorage.set(selectionStorageKey, detailsCheckbox.dataset.bookmarkId);
                    window.location.assign(detailsCheckbox.dataset.detailsUrl);
                }
            }
        });
    });

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

    document.querySelectorAll("[data-external-open-form]").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector("button[type='submit']");
            const externalWindow = window.open("about:blank", "_blank");
            if (externalWindow) {
                externalWindow.opener = null;
            }
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
                if (!response.ok || !payload.url) {
                    throw new Error(payload.detail || "The page could not be opened safely.");
                }
                announce(payload.detail || "Opened in Confluence");
                if (externalWindow) {
                    externalWindow.location.replace(payload.url);
                } else {
                    window.open(payload.url, "_blank", "noopener,noreferrer");
                }
            } catch (error) {
                externalWindow?.close();
                announce(
                    error instanceof Error ? error.message : "The page could not be opened safely.",
                );
            } finally {
                button?.removeAttribute("aria-busy");
                if (button) {
                    button.disabled = false;
                }
            }
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

    document.querySelectorAll("[data-copy-value]").forEach((button) => {
        button.addEventListener("click", async () => {
            try {
                await copyText(button.dataset.copyValue || "");
                announce(button.dataset.copySuccess || "Copied");
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
        const selectLink = row.querySelector("[data-tree-select]");
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
        } else if (event.key === "Enter" && selectLink) {
            event.preventDefault();
            selectLink.click();
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
