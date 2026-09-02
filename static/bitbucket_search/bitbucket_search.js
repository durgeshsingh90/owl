(() => {
    "use strict";

    const workspace = document.querySelector("[data-bitbucket-workspace]");
    if (!workspace) {
        return;
    }

    const refreshAllForms = Array.from(
        workspace.querySelectorAll("[data-repositories-refresh-all]"),
    );
    const initialRefreshAllState = refreshAllForms[0]?.dataset;
    let repositoryCount = Number(initialRefreshAllState?.repositoryCount || 0);
    let enabledRepositoryCount = Number(
        initialRefreshAllState?.enabledRepositoryCount || 0,
    );
    let activeRepositoryCount = Number(
        initialRefreshAllState?.activeRepositoryCount || 0,
    );
    let repositorySubmissionPending = false;
    let repositoryStatusPending = false;
    const connectionResult = workspace.querySelector("[data-repository-connection-result]");
    const connectionTestUrl = workspace.dataset.repositoryConnectionTestUrl;
    let connectionTestPending = false;
    const setConnectionResult = (state, label, detail) => {
        if (!connectionResult) return;
        const message = detail ? `${label}: ${detail}` : label;
        connectionResult.dataset.state = state;
        connectionResult.setAttribute("aria-label", message);
        connectionResult.title = message;
        const hiddenMessage = connectionResult.querySelector("[data-repository-connection-message]");
        if (hiddenMessage) hiddenMessage.textContent = message;
    };
    const testRepositoryConnection = async () => {
        if (!connectionResult || !connectionTestUrl || connectionTestPending) return;
        connectionTestPending = true;
        connectionResult.disabled = true;
        setConnectionResult(
            "checking",
            "Testing stored credentials and Git connections",
            "Please wait.",
        );
        const csrf = workspace.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";
        try {
            const response = await fetch(connectionTestUrl, {
                method: "POST",
                headers: {"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"},
                credentials: "same-origin",
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Connection test failed");
            setConnectionResult(payload.state || "failed", payload.label, payload.detail);
            (payload.repositories || []).filter(item => !item.connected).forEach((item) => {
                document.querySelectorAll(`[data-repository-id="${item.id}"] [data-repository-state-icon]`).forEach((icon) => {
                    icon.className = "bb-repository-state bb-repository-state--git-failed";
                    icon.title = "Git connection test failed";
                });
            });
        } catch (_error) {
            setConnectionResult(
                "failed",
                "Git connection test failed",
                "Check network, VPN, SSH agent, or stored HTTPS credentials.",
            );
        } finally {
            connectionTestPending = false;
            connectionResult.disabled = false;
        }
    };
    if (connectionResult && connectionTestUrl) {
        connectionResult.addEventListener("click", testRepositoryConnection);
        testRepositoryConnection();
    }
    let extractionWorkActive = workspace.dataset.extractionActive === "true";
    let workSummary = {
        label: initialRefreshAllState?.workLabel || "",
        detail: initialRefreshAllState?.workDetail || "",
    };
    let overallWasActive = false;
    let overallDisplayedProgress = 0;
    let overallSnapshot = null;
    const repositoryNoun = (count) => (count === 1 ? "repository" : "repositories");
    const renderOverallStatus = (repositories = null, extraction = null, work = null, workerLimits = null) => {
        if (repositories) overallSnapshot = {
            repositories,
            extraction: extraction || {},
            work: work || {},
            workerLimits: workerLimits || {},
        };
        const snapshot = overallSnapshot;
        const active = snapshot
            ? snapshot.repositories.some((repository) => repository.active || repository.hasActiveWork)
                || Boolean(snapshot.extraction?.active)
            : activeRepositoryCount > 0 || extractionWorkActive;
        if (active && !overallWasActive) {
            overallDisplayedProgress = 0;
        } else if (!active) {
            overallDisplayedProgress = 0;
        }
        overallWasActive = active;

        let queuedGit = 0;
        let runningGit = 0;
        let completedGit = 0;
        let runningPdf = Number(snapshot?.extraction?.runningJobs || 0);
        let passed = 0;
        let failed = 0;
        let cancelled = 0;
        let gitProgressTotal = 0;
        let gitProgressWeight = 0;
        let indexProgressTotal = 0;
        let indexProgressWeight = 0;
        (snapshot?.repositories || []).forEach((repository) => {
            const repositoryQueuedGit = Number(repository.activity?.queuedSyncJobs || 0);
            const repositoryRunningGit = Number(repository.activity?.runningSyncJobs || 0);
            queuedGit += repositoryQueuedGit;
            runningGit += repositoryRunningGit;
            if (repository.gitSucceeded && !repositoryQueuedGit && !repositoryRunningGit) {
                completedGit += 1;
            }
            const counts = repository.activity?.pdfCounts || {};
            passed += Number(counts.passed || 0);
            failed += Number(counts.failed || 0) + Number(counts.interrupted || 0);
            cancelled += Number(counts.cancelled || 0);
            (repository.activity?.operations || []).forEach((operation) => {
                const weight = Math.max(1, Number(operation.count || 1));
                if (operation.operation === "indexing") {
                    indexProgressTotal += Number(operation.progress || 0) * weight;
                    indexProgressWeight += weight;
                } else if (operation.operation === "clone" || operation.operation === "pull") {
                    gitProgressTotal += Number(operation.progress || 0) * weight;
                    gitProgressWeight += weight;
                }
            });
        });
        const averageGitProgress = gitProgressWeight ? gitProgressTotal / gitProgressWeight : null;
        const averageIndexProgress = indexProgressWeight
            ? indexProgressTotal / indexProgressWeight : null;
        const measuredProgress = averageIndexProgress !== null
            ? Math.round(30 + averageIndexProgress * 0.7)
            : averageGitProgress !== null ? Math.round(averageGitProgress * 0.3) : null;
        if (active && measuredProgress !== null) {
            overallDisplayedProgress = Math.max(overallDisplayedProgress, measuredProgress);
        }
        const totalWorkerLimit = Number(snapshot?.workerLimits?.total || 0);
        const runningWorkers = runningGit + runningPdf;

        refreshAllForms.forEach((form) => {
            const progress = form.querySelector("[data-overall-progress]");
            const bar = form.querySelector("[data-overall-progress-bar]");
            const progressLabel = form.querySelector("[data-overall-progress-label]");
            const counts = form.querySelector("[data-overall-counts]");
            const pdfCounts = form.querySelector("[data-overall-pdf-counts]");
            const gitCounts = form.querySelector("[data-overall-git-counts]");
            const timing = form.querySelector("[data-overall-timing]");
            if (progress) progress.hidden = !active;
            if (bar) {
                if (active && measuredProgress !== null) bar.value = overallDisplayedProgress;
                else bar.removeAttribute("value");
            }
            if (progressLabel) progressLabel.textContent = measuredProgress === null
                ? "Working…" : `${overallDisplayedProgress}%`;
            if (counts) counts.textContent = `Workers ${active ? runningWorkers : 0} running / ${totalWorkerLimit || "–"} total`;
            if (pdfCounts) {
                const remainingPdf = Number(snapshot?.extraction?.queuedJobs || 0)
                    + Number(snapshot?.extraction?.runningJobs || 0);
                pdfCounts.textContent = `PDF · ${remainingPdf} remaining · ${passed} passed · ${active ? runningPdf : 0} running · ${failed} failed · ${cancelled} cancelled`;
            }
            if (gitCounts) gitCounts.textContent = `Git · ${active ? queuedGit : 0} queued · ${active ? runningGit : 0} working · ${completedGit} completed`;
            if (timing) timing.hidden = true;
        });
    };
    const setRefreshIconBusy = (icon, busy) => {
        if (!icon) return;
        // SVG elements do not reflect the HTML hidden property into attributes.
        // Toggle the attribute explicitly so idle/live transitions affect CSS.
        icon.hidden = busy;
        if (busy) icon.setAttribute("hidden", "");
        else icon.removeAttribute("hidden");
    };

    const updateRefreshAllButtons = (repositories, extraction, work, workerLimits) => {
        if (work) workSummary = work;
        else if (repositories) workSummary = { label: "", detail: "" };
        if (extraction) {
            extractionWorkActive = Boolean(extraction.active || extraction.queuedJobs > 0 || extraction.runningJobs > 0);
        }
        if (repositories) {
            repositoryCount = repositories.length;
            enabledRepositoryCount = repositories.filter(
                (repository) => repository.enabled && !repository.refreshExcluded && !repository.hasRemovalPending,
            ).length;
            activeRepositoryCount = repositories.filter(
                (repository) => repository.active || repository.hasActiveWork,
            ).length;
        }
        refreshAllForms.forEach((form) => {
            const button = form.querySelector("[data-refresh-all-button]");
            if (!button) {
                return;
            }
            const unavailable =
                !repositorySubmissionPending &&
                !repositoryStatusPending &&
                (!repositoryCount ||
                    !enabledRepositoryCount ||
                    !form.getAttribute("action"));
            const busy =
                repositorySubmissionPending ||
                repositoryStatusPending ||
                activeRepositoryCount > 0 || extractionWorkActive;
            let label = "Refresh all repositories";
            let detail = `Queue ${enabledRepositoryCount} included ${repositoryNoun(enabledRepositoryCount)} in the background`;
            let ariaLabel = "Refresh all repositories";
            let title = "Refresh all repositories";

            if (repositorySubmissionPending) {
                label = "Repository sync in progress";
                detail = "Starting repository work in the background";
                ariaLabel = "Refresh all repositories unavailable: repository request in progress";
                title = "Wait for the repository request to finish before refreshing all";
            } else if (repositoryStatusPending) {
                label = "Repository status unavailable";
                detail = "Cannot confirm current work; retrying the status check";
                ariaLabel = "Refresh all repositories unavailable: checking repository status";
                title = "Wait for the latest repository status before refreshing all";
            } else if (activeRepositoryCount > 0 || extractionWorkActive) {
                label = workSummary.label || "Repository work in progress";
                detail = workSummary.detail || (activeRepositoryCount > 0
                    ? `${activeRepositoryCount} ${repositoryNoun(activeRepositoryCount)} working in the background`
                    : "PDF indexing in progress");
                ariaLabel = `Refresh all repositories unavailable: ${detail}`;
                title = `${label}\n${detail}`;
            } else if (unavailable) {
                label = "Refresh all unavailable";
                if (!repositoryCount) {
                    detail = "Add a repository to enable workspace refresh";
                    ariaLabel = "Refresh all repositories unavailable: no repositories are connected";
                    title = "Add a repository before queuing a workspace refresh";
                } else if (!enabledRepositoryCount) {
                    detail = "No repositories included in refresh";
                    ariaLabel = "Refresh all repositories unavailable: no repositories are included";
                    title = "Include at least one repository in refresh before refreshing all";
                } else {
                    detail = "Workspace refresh endpoint unavailable";
                    ariaLabel = "Refresh all repositories unavailable";
                    title = "The workspace refresh endpoint is unavailable";
                }
            }

            form.dataset.repositoryCount = String(repositoryCount);
            form.dataset.enabledRepositoryCount = String(enabledRepositoryCount);
            form.dataset.activeRepositoryCount = String(activeRepositoryCount);
            const classPrefix = form.hasAttribute("data-repositories-refresh-all-mobile")
                ? "bb-mobile-refresh-all"
                : "bb-refresh-all";
            form.classList.toggle(`${classPrefix}--disabled`, unavailable && !busy);
            form.classList.toggle(`${classPrefix}--active`, busy);
            button.disabled = unavailable || busy;
            button.setAttribute("aria-label", ariaLabel);
            button.title = title;
            if (busy) {
                button.setAttribute("aria-busy", "true");
            } else {
                button.removeAttribute("aria-busy");
            }
            const labelElement = form.querySelector("[data-refresh-all-label]");
            const detailElement = form.querySelector("[data-refresh-all-detail]");
            const spinner = form.querySelector("[data-refresh-all-spinner]");
            const icon = form.querySelector("[data-refresh-all-icon]");
            if (labelElement) labelElement.textContent = label;
            if (detailElement) detailElement.textContent = detail;
            const hasOverallVisual = Boolean(form.querySelector("[data-refresh-all-visual]"));
            if (spinner) spinner.hidden = hasOverallVisual || !busy;
            setRefreshIconBusy(icon, hasOverallVisual ? false : busy);
        });
        renderOverallStatus(repositories, extraction, work, workerLimits);
    };

    updateRefreshAllButtons();
    if (typeof window.setInterval === "function") {
        window.setInterval(() => renderOverallStatus(), 1000);
    }

    const selectionForm = workspace.querySelector("[data-repository-selection-form]");
    const operationOverlay = workspace.querySelector("[data-repository-operation-overlay]");
    const showOperationOverlay = (operation, count) => {
        if (!operationOverlay || !["stop_indexing", "remove"].includes(operation)) return;
        const removing = operation === "remove";
        const title = operationOverlay.querySelector("[data-repository-operation-title]");
        const detail = operationOverlay.querySelector("[data-repository-operation-detail]");
        if (title) title.textContent = removing ? "Deleting repositories…" : "Stopping repository work…";
        if (detail) detail.textContent = removing
            ? `Removing ${count} selected ${repositoryNoun(count)}, downloaded files and indexed data. This can take a minute.`
            : `Stopping Git and PDF workers for ${count} selected ${repositoryNoun(count)}. This can take a minute.`;
        operationOverlay.hidden = false;
        document.documentElement.classList.add("bb-operation-blocked");
        operationOverlay.focus();
    };
    let deletionUnlocked = false;
    let deleteRelockTimer = null;
    let deleteUnlockExpiresAt = 0;
    const announceDeleteLock = (message) => {
        const status = workspace.querySelector("[data-repository-delete-status]");
        if (status) status.textContent = message;
    };
    const resetDeleteLock = () => {
        if (deletionUnlocked) announceDeleteLock("Repository deletion locked.");
        deletionUnlocked = false;
        deleteUnlockExpiresAt = 0;
        if (deleteRelockTimer !== null) {
            window.clearTimeout(deleteRelockTimer);
            deleteRelockTimer = null;
        }
    };
    const repositoryCheckboxes = () =>
        Array.from(workspace.querySelectorAll("[data-repository-select]"));
    const selectedRepositoryCards = () => {
        const selected = new Map();
        repositoryCheckboxes().forEach((checkbox) => {
            if (!checkbox.checked || checkbox.disabled) return;
            const card = checkbox.closest("[data-repository-id]");
            if (card) selected.set(checkbox.value, card);
        });
        return Array.from(selected.values());
    };
    const updateSelectedRepositoryActions = () => {
        if (!selectionForm) return;
        const selected = selectedRepositoryCards();
        const count = selected.length;
        const availableRepositoryIds = new Set(
            repositoryCheckboxes().filter((checkbox) => !checkbox.disabled).map((checkbox) => checkbox.value),
        );
        const allSelected = count > 0 && count === availableRepositoryIds.size;
        const busy = selected.some((card) =>
            card.dataset.repositoryActiveWork === "true" ||
            card.dataset.repositoryActiveSync === "true",
        );
        const pdfIndexing = selected.some((card) =>
            card.dataset.repositoryPdfIndexingActive === "true",
        );
        const gitActive = selected.some((card) =>
            card.dataset.repositoryActiveSync === "true",
        );
        const removalPending = selected.some((card) =>
            card.dataset.repositoryRemovalPending === "true",
        );
        const unavailable = !count || repositorySubmissionPending || repositoryStatusPending;
        const destructiveUnavailable = !count || repositorySubmissionPending || removalPending;
        const allExcluded = count > 0 && selected.every((card) =>
            card.dataset.repositoryRefreshExcluded === "true",
        );
        if (destructiveUnavailable ||
            (deletionUnlocked && Date.now() >= deleteUnlockExpiresAt)) resetDeleteLock();
        const disable = (selector, disabled, title) => {
            workspace.querySelectorAll(selector).forEach((button) => {
                button.disabled = disabled;
                button.title = title;
                button.setAttribute("aria-label", title);
            });
        };
        const suffix = `${count} selected ${repositoryNoun(count)}`;
        workspace.querySelectorAll("[data-selected-select-all]").forEach((button) => {
            button.hidden = count === 0;
            button.disabled = count === 0 || allSelected;
            button.setAttribute("aria-pressed", String(allSelected));
            button.title = allSelected
                ? `All ${availableRepositoryIds.size} repositories selected`
                : `Select all ${availableRepositoryIds.size} repositories`;
            button.setAttribute("aria-label", button.title);
        });
        disable("[data-selected-refresh]", unavailable || busy || removalPending,
            busy ? "Selected repository work in progress" : `Refresh ${suffix}`);
        disable("[data-selected-exclude]", unavailable || removalPending,
            `${allExcluded ? "Include" : "Exclude"} ${suffix} ${allExcluded ? "in" : "from"} refresh`);
        const stoppableWork = gitActive || pdfIndexing;
        const stopTitle = !count ? "Select a repository with active Git or PDF work to stop"
            : repositorySubmissionPending ? "Wait for the repository request to finish"
            : removalPending ? "Use Retry removal to finish incomplete repository removal"
            : stoppableWork ? `Stop active Git and PDF work for ${suffix}`
            : `No active Git or PDF work for ${suffix}`;
        disable("[data-selected-stop-indexing]", destructiveUnavailable || !stoppableWork, stopTitle);
        const deleteTitle = !count ? "Select repositories to delete"
            : repositorySubmissionPending ? "Wait for the repository request to finish"
            : removalPending ? "Use Retry removal to finish incomplete repository removal"
            : deletionUnlocked ? `Click again to delete ${suffix} from this computer`
            : `Unlock deletion for ${suffix}`;
        disable("[data-selected-remove]", destructiveUnavailable, deleteTitle);
        workspace.querySelectorAll("[data-selected-remove]").forEach((button) => {
            button.dataset.deleteLocked = String(!deletionUnlocked);
            const icon = button.querySelector("[data-selected-delete-icon]");
            const lock = button.querySelector("[data-selected-delete-lock]");
            const image = button.querySelector("[data-selected-delete-image]");
            if (lock && image) {
                lock.hidden = deletionUnlocked;
                image.hidden = !deletionUnlocked;
            } else if (icon) {
                icon.textContent = deletionUnlocked ? "🗑️" : "🔒";
            }
        });
        workspace.querySelectorAll("[data-selected-exclude]").forEach((button) => {
            button.setAttribute("aria-pressed", String(allExcluded));
        });
        workspace.querySelectorAll("[data-selected-excluded-value]").forEach((input) => {
            input.value = allExcluded ? "no" : "yes";
        });
        workspace.querySelectorAll("[data-repository-selection-count]").forEach((element) => {
            element.textContent = `${count} selected`;
        });
        workspace.querySelectorAll("[data-selected-refresh-spinner]").forEach((element) => {
            element.hidden = !busy;
        });
        workspace.querySelectorAll("[data-selected-refresh-icon]").forEach((element) => {
            setRefreshIconBusy(element, busy);
        });
        workspace.querySelectorAll("[data-selected-refresh]").forEach((button) => {
            button.setAttribute("aria-busy", String(busy));
        });
        repositoryCheckboxes().forEach((checkbox) => {
            checkbox.closest("[data-repository-id]")?.classList.toggle("is-selected", checkbox.checked);
        });
    };

    workspace.addEventListener("change", (event) => {
        if (!event.target.matches("[data-repository-select]")) return;
        const selected = event.target;
        repositoryCheckboxes().forEach((checkbox) => {
            if (checkbox.value === selected.value) checkbox.checked = selected.checked;
        });
        resetDeleteLock();
        updateSelectedRepositoryActions();
    });
    workspace.addEventListener("click", (event) => {
        const selectAll = event.target.closest?.("[data-selected-select-all]");
        if (selectAll) {
            event.preventDefault();
            if (selectAll.disabled) return;
            repositoryCheckboxes().forEach((checkbox) => {
                if (!checkbox.disabled) checkbox.checked = true;
            });
            resetDeleteLock();
            updateSelectedRepositoryActions();
            return;
        }
        const button = event.target.closest?.("[data-selected-remove]");
        if (!button) return;
        updateSelectedRepositoryActions();
        if (button.disabled) {
            event.preventDefault();
            return;
        }
        if (deletionUnlocked) return;
        // Match Bookmark Manager: first click only arms this same button.
        event.preventDefault();
        deletionUnlocked = true;
        deleteUnlockExpiresAt = Date.now() + 10000;
        deleteRelockTimer = window.setTimeout(() => {
            resetDeleteLock();
            updateSelectedRepositoryActions();
        }, 10000);
        updateSelectedRepositoryActions();
        announceDeleteLock("Delete unlocked. Click again to delete the selected repositories, downloaded files and indexed data from this computer. Remote repositories stay unchanged. Locks again in 10 seconds.");
    });
    updateSelectedRepositoryActions();

    workspace.addEventListener("submit", (event) => {
        const form = event.target;
        if (form === selectionForm) {
            const submitter = event.submitter;
            const operation = submitter?.value;
            if (event.defaultPrevented) return;
            // Timers can be delayed in background tabs; never accept expired consent.
            updateSelectedRepositoryActions();
            if (!submitter || submitter.disabled || repositorySubmissionPending ||
                repositoryStatusPending || !selectedRepositoryCards().length ||
                !["refresh", "exclude", "stop_indexing", "remove"].includes(operation) ||
                (operation === "remove" && !deletionUnlocked)) {
                event.preventDefault();
                return;
            }
            // Disabling the submitter removes its name/value from a native POST.
            // Preserve the validated intent before locking both toolbar copies.
            const intent = document.createElement("input");
            intent.type = "hidden";
            intent.name = "operation";
            intent.value = operation;
            form.appendChild(intent);
            if (operation === "remove") {
                // The valid second click is confirmation, just like Bookmark Manager.
                // Never include it in the initial page or merely unlocked state.
                const confirmation = document.createElement("input");
                confirmation.type = "hidden";
                confirmation.name = "confirmed";
                confirmation.value = "yes";
                form.appendChild(confirmation);
            }
            repositorySubmissionPending = true;
            resetDeleteLock();
            updateSelectedRepositoryActions();
            updateRefreshAllButtons();
            showOperationOverlay(operation, selectedRepositoryCards().length);
            return;
        }
        if (
            event.defaultPrevented ||
            !form.matches(
                "[data-repositories-refresh-all], [data-repository-add-form], [data-repository-refresh-form], [data-repository-exclusion-form]",
            )
        ) {
            return;
        }
        const refreshAll = form.matches("[data-repositories-refresh-all]");
        if (
            repositorySubmissionPending ||
            (refreshAll &&
                (activeRepositoryCount > 0 ||
                    form.querySelector("[data-refresh-all-button]")?.disabled))
        ) {
            event.preventDefault();
            return;
        }
        // Keep the native POST and CSRF handling, but lock both buttons before navigation.
        repositorySubmissionPending = true;
        resetDeleteLock();
        updateSelectedRepositoryActions();
        updateRefreshAllButtons();
    });

    const cardsFor = (repositoryId) =>
        document.querySelectorAll(`[data-repository-id="${repositoryId}"]`);

    const updateRepository = (repository) => {
        const working = Boolean(repository.activity?.active || repository.hasActiveWork || repository.active);
        const workLabel = repository.activity?.label || repository.workerTiming?.label || "Repository work in progress";
        const workDetail = repository.activity?.detail || workLabel;
        const operation = ["clone", "pull", "indexing"].includes(repository.activity?.operation)
            ? repository.activity.operation : "";
        const suppliedProgress = Number(repository.activity?.progress);
        const hasProgress = repository.activity?.progress !== null
            && repository.activity?.progress !== undefined && Number.isFinite(suppliedProgress);
        const progressValue = hasProgress
            ? Math.round(Math.min(100, Math.max(0, suppliedProgress))) : null;
        cardsFor(repository.id).forEach((card) => {
            card.dataset.repositoryState = repository.state;
            card.dataset.repositoryOperation = operation;
            card.dataset.repositoryActiveWork = String(working);
            card.dataset.repositoryActiveSync = String(Boolean(repository.active));
            card.dataset.repositoryPdfIndexingActive = String(Boolean(
                Number(repository.activity?.queuedPdfs || 0) +
                Number(repository.activity?.runningPdfs || 0),
            ));
            card.dataset.repositoryRefreshExcluded = String(Boolean(repository.refreshExcluded));
            card.dataset.repositoryRemovalPending = String(Boolean(repository.hasRemovalPending));
            const stateIcon = card.querySelector("[data-repository-state-icon]");
            if (stateIcon) {
                const visibleState = working ? "working"
                    : repository.gitSyncFailed ? "git-failed"
                    : Number(repository.pdfIndexFailedCount || 0) ? "indexing-failed"
                    : repository.state;
                const visibleLabel = working ? workLabel
                    : repository.gitSyncFailed ? "Git connection or pull failed"
                    : Number(repository.pdfIndexFailedCount || 0)
                        ? `${repository.pdfIndexFailedCount} PDF indexing failures`
                        : repository.stateLabel;
                stateIcon.className = `bb-repository-state bb-repository-state--${visibleState}`;
                stateIcon.dataset.repositoryOperation = operation;
                stateIcon.setAttribute(
                    "aria-label",
                    `${repository.name}: ${visibleLabel}`,
                );
                stateIcon.title = working ? workDetail : visibleLabel;
            }
            const workStatus = card.querySelector("[data-repository-work-label]");
            if (workStatus) {
                const visiblyRunning = Number(repository.activity?.runningSyncJobs || 0) > 0
                    || Number(repository.activity?.runningPdfs || 0) > 0;
                workStatus.textContent = visiblyRunning ? workDetail : "";
                workStatus.title = visiblyRunning ? workDetail : "";
                workStatus.hidden = !visiblyRunning;
            }
            const progressContainer = card.querySelector("[data-repository-progress]");
            const progressBar = card.querySelector("[data-repository-progress-bar]");
            const progressLabel = card.querySelector("[data-repository-progress-label]");
            const queuedOnly = String(repository.activity?.phase || "").includes("queued");
            const showProgress = working && Boolean(operation) && !queuedOnly;
            if (progressContainer) {
                progressContainer.hidden = !showProgress;
                progressContainer.title = showProgress ? workDetail : "";
            }
            if (progressBar) {
                if (showProgress && progressValue !== null) {
                    progressBar.value = progressValue;
                    progressBar.setAttribute("value", String(progressValue));
                } else {
                    progressBar.removeAttribute("value");
                }
            }
            if (progressLabel) {
                progressLabel.textContent = progressValue !== null
                    ? `${progressValue}%` : "Running";
            }
            const documents = card.querySelector("[data-repository-documents]");
            if (documents) {
                documents.textContent = `${repository.pdfCount} PDF · ${repository.vsdxCount} VSDX`;
            }
            const remaining = card.querySelector("[data-repository-remaining]");
            if (remaining) {
                const remainingCount = Number(repository.activity?.queuedPdfs || 0)
                    + Number(repository.activity?.runningPdfs || 0);
                remaining.textContent = `Remaining ${remainingCount} PDF${remainingCount === 1 ? "" : "s"}`;
                remaining.hidden = remainingCount === 0;
            }
            const ticks = card.querySelector("[data-repository-success-ticks]");
            if (ticks) {
                ticks.dataset.gitSucceeded = String(Boolean(repository.gitSucceeded));
                ticks.dataset.indexSucceeded = String(Boolean(repository.indexSucceeded));
                ticks.title = `Git ${repository.gitSucceeded ? "succeeded" : "not complete"}; PDF indexing ${repository.indexSucceeded ? "succeeded" : "not complete"}`;
            }
            const exclusionBadge = card.querySelector("[data-repository-exclusion]");
            if (exclusionBadge) {
                exclusionBadge.hidden = !repository.refreshExcluded;
            }
            const retryRemoval = card.querySelector("[data-repository-removal-retry]");
            if (retryRemoval) {
                retryRemoval.hidden = !repository.hasRemovalPending;
            }
        });
    };

    const updateTotals = (totals, repositories, automation) => {
        document.querySelectorAll("[data-total-repositories]").forEach((element) => {
            element.textContent = String(totals.repositories);
        });
        document.querySelectorAll("[data-total-pdfs]").forEach((element) => {
            element.textContent = String(totals.pdfs);
        });
        document.querySelectorAll("[data-total-vsdx]").forEach((element) => {
            element.textContent = String(totals.vsdx);
        });
        document.querySelectorAll("[data-total-bytes]").forEach((element) => {
            element.textContent = totals.bytesLabel;
        });
        const activeCount = repositories.filter((repository) => repository.active || repository.hasActiveWork).length;
        document.querySelectorAll("[data-mobile-repository-count]").forEach((element) => {
            let activityLabel = activeCount ? ` · ${activeCount} working` : "";
            if (!activeCount && automation?.state === "retry_wait") {
                activityLabel = " · retry scheduled";
            } else if (!activeCount && automation?.state === "exhausted") {
                activityLabel = " · retries exhausted";
            } else if (!activeCount && automation?.state === "due") {
                activityLabel = " · daily refresh due";
            }
            element.textContent = `${totals.repositories} connected${activityLabel}`;
        });
    };

    const statusUrl = workspace.dataset.repositoryStatusUrl;
    let activeRepositoryIds = new Set(
        Array.from(
            document.querySelectorAll(
                '[data-repository-state="queued"], [data-repository-state="cloning"], [data-repository-state="fetching"], [data-repository-state="updating"], [data-repository-active-work="true"]',
            ),
            (card) => card.dataset.repositoryId,
        ).filter(Boolean),
    );
    let extractionActive = workspace.dataset.extractionActive === "true";
    let extractionPublicationSignature =
        workspace.dataset.extractionPublicationSignature || "";
    let catalogPublicationSignature = workspace.dataset.catalogPublicationSignature || "";
    let dailyRefreshEnabled = workspace.dataset.dailyRefreshEnabled === "true";
    let catalogReloadPending = false;
    let settledPolls = 0;

    const backgroundPanelOpen = () =>
        Array.from(
            document.querySelectorAll(
                "[data-repository-status-panel], [data-notification-panel]",
            ),
        ).some((panel) => !panel.hidden);

    const activePollDelay = 1500;
    const idlePollDelay = 30000;
    let pollTimer;

    const shouldPoll = () =>
        repositoryCount > 0 ||
        activeRepositoryCount > 0 ||
        activeRepositoryIds.size > 0 ||
        repositoryStatusPending ||
        extractionActive ||
        dailyRefreshEnabled ||
        catalogReloadPending;

    const nextPollDelay = () =>
        activeRepositoryCount > 0 ||
        activeRepositoryIds.size > 0 ||
        repositoryStatusPending ||
        extractionActive ||
        (catalogReloadPending && settledPolls < 2)
            ? activePollDelay
            : idlePollDelay;

    const updateExtraction = (extraction) => {
        document.querySelectorAll("[data-extraction-summary]").forEach((element) => {
            const activeJobs = extraction.queuedJobs + extraction.runningJobs;
            element.textContent = extraction.active
                ? `PDF indexing in progress · ${activeJobs} active`
                : `${extraction.indexedDocuments} indexed · ${extraction.pendingDocuments} pending`;
        });
    };

    const poll = async () => {
        try {
            const response = await fetch(statusUrl, {
                credentials: "same-origin",
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                throw new Error("Repository status request failed");
            }
            const payload = await response.json();
            payload.repositories.forEach(updateRepository);
            repositoryStatusPending = false;
            updateRefreshAllButtons(
                payload.repositories,
                payload.extraction,
                payload.work,
                payload.workerLimits,
            );
            const knownRepositoryIds = new Set(payload.repositories.map((repository) => String(repository.id)));
            repositoryCheckboxes().forEach((checkbox) => {
                if (!knownRepositoryIds.has(checkbox.value)) {
                    checkbox.checked = false;
                    checkbox.disabled = true;
                    resetDeleteLock();
                }
            });
            updateSelectedRepositoryActions();
            updateTotals(payload.totals, payload.repositories, payload.automation);
            const repositoryCompleted = payload.repositories.some(
                (repository) =>
                    activeRepositoryIds.has(String(repository.id)) && !(repository.active || repository.hasActiveWork),
            );
            activeRepositoryIds = new Set(
                payload.repositories
                    .filter((repository) => repository.active || repository.hasActiveWork)
                    .map((repository) => String(repository.id)),
            );
            const extraction = payload.extraction || {
                active: false,
                queuedJobs: 0,
                runningJobs: 0,
                pendingDocuments: 0,
                indexedDocuments: 0,
                publicationSignature: extractionPublicationSignature,
            };
            updateExtraction(extraction);
            const wasExtracting = extractionActive;
            extractionActive = Boolean(
                extraction.active || extraction.queuedJobs > 0 || extraction.runningJobs > 0,
            );
            const extractionCompleted = wasExtracting && !extractionActive;
            const extractionPublicationChanged = Boolean(
                extractionPublicationSignature &&
                    extraction.publicationSignature &&
                    extraction.publicationSignature !== extractionPublicationSignature,
            );
            extractionPublicationSignature =
                extraction.publicationSignature || extractionPublicationSignature;
            dailyRefreshEnabled = Boolean(payload.automation?.enabled);
            const nextCatalogPublicationSignature =
                payload.catalog?.publicationSignature || catalogPublicationSignature;
            const catalogPublicationChanged = Boolean(
                catalogPublicationSignature &&
                    nextCatalogPublicationSignature &&
                    nextCatalogPublicationSignature !== catalogPublicationSignature,
            );
            catalogPublicationSignature = nextCatalogPublicationSignature;
            catalogReloadPending = catalogReloadPending ||
                repositoryCompleted ||
                extractionCompleted ||
                extractionPublicationChanged ||
                catalogPublicationChanged;
            const backgroundWorkActive = activeRepositoryCount > 0 || activeRepositoryIds.size > 0 ||
                extractionActive || repositorySubmissionPending || repositoryStatusPending;
            if (backgroundWorkActive || extractionPublicationChanged || catalogPublicationChanged) {
                settledPolls = 0;
            }
            if (catalogReloadPending && !backgroundWorkActive) {
                settledPolls = Math.min(2, settledPolls + 1);
            }
            if (catalogReloadPending && settledPolls >= 2 && !backgroundPanelOpen()) {
                // Intermediate catalogue/PDF publications update the small status
                // controls only. Wait for every repository and extraction worker,
                // then confirm an unchanged idle snapshot before reloading once.
                // Failed/interrupted jobs are terminal too; pending documents do
                // not hold the page indefinitely. Keep open logs readable.
                window.location.reload();
                return;
            }
        } catch (_error) {
            // A failed status request cannot confirm that all workers are idle.
            settledPolls = 0;
            repositoryStatusPending = true;
            resetDeleteLock();
            updateRefreshAllButtons();
            updateSelectedRepositoryActions();
            document.querySelectorAll("[data-repository-id]").forEach((card) => {
                const icon = card.querySelector("[data-repository-state-icon]");
                if (icon) {
                    icon.className = "bb-repository-state bb-repository-state--unknown";
                    icon.setAttribute("aria-label", "Repository status unavailable");
                    icon.title = "Cannot confirm current work; retrying the status check";
                }
                const workStatus = card.querySelector("[data-repository-work-label]");
                if (workStatus) {
                    workStatus.textContent = "Status unavailable";
                    workStatus.title = "Cannot confirm current work; retrying the status check";
                    workStatus.hidden = false;
                }
            });
            // An active or scheduled refresh remains recoverable. Use the same
            // bounded cadence so a temporary status failure never creates a tight loop.
        }
        if (shouldPoll()) {
            pollTimer = window.setTimeout(poll, nextPollDelay());
        }
    };

    window.addEventListener("pageshow", (event) => {
        if (!event.persisted) {
            return;
        }
        // A restored page may predate a submitted job. Recheck before unlocking it.
        repositorySubmissionPending = false;
        repositoryStatusPending = true;
        resetDeleteLock();
        selectionForm?.querySelectorAll('input[name="operation"], input[name="confirmed"]').forEach((input) => input.remove());
        updateSelectedRepositoryActions();
        updateRefreshAllButtons();
        window.clearTimeout(pollTimer);
        void poll();
    });

    document.querySelectorAll("[data-repository-filter]").forEach((input) => {
        input.addEventListener("input", () => {
            const query = input.value.trim().toLocaleLowerCase();
            document.querySelectorAll("[data-repository-id]").forEach((card) => {
                const searchable = (
                    card.dataset.repositorySearchValue || card.textContent
                ).toLocaleLowerCase();
                card.hidden = Boolean(query) && !searchable.includes(query);
                if (card.hidden) {
                    const checkbox = card.querySelector("[data-repository-select]");
                    if (checkbox) checkbox.checked = false;
                }
            });
            resetDeleteLock();
            updateSelectedRepositoryActions();
        });
    });

    const peoplePanels = Array.from(
        workspace.querySelectorAll("[data-people-panel]"),
    );
    const normalizedPeopleQuery = (value) =>
        String(value || "")
            .normalize("NFKD")
            .replace(/[\u0300-\u036f]/gu, "")
            .trim()
            .toLocaleLowerCase();

    const updatePeopleSelection = (panel) => {
        const selectedPeople = panel.querySelectorAll(
            "[data-committer-select]:checked",
        ).length;
        const selectedGroups = panel.querySelectorAll(
            "[data-people-group-select]:checked",
        ).length;
        const selectedTotal = selectedPeople + selectedGroups;
        panel.querySelectorAll(".bb-people-option").forEach((option) => {
            const checkbox = option.querySelector(
                "[data-committer-select], [data-people-group-select]",
            );
            option.classList.toggle("is-active", Boolean(checkbox?.checked));
        });

        return selectedTotal;
    };

    const updateMobilePeopleSummary = (selectedTotal) => {
        const summary = workspace.querySelector("[data-people-mobile-summary]");
        if (!summary) {
            return;
        }
        const peopleTotal = summary.dataset.peopleTotal || "0";
        summary.textContent = `${peopleTotal} available${
            selectedTotal ? ` · ${selectedTotal} selected` : ""
        }`;
    };

    peoplePanels.forEach((panel) => {
        const search = panel.querySelector("[data-people-filter-search]");
        const searchStatus = panel.querySelector(
            "[data-people-filter-search-status]",
        );
        const noResults = panel.querySelector("[data-people-filter-no-results]");
        const filterEntries = Array.from(
            panel.querySelectorAll("[data-people-filter-entry]"),
        );

        const applyPeopleSearch = () => {
            const terms = normalizedPeopleQuery(search?.value)
                .split(/\s+/u)
                .filter(Boolean);
            let visiblePeople = 0;
            let visibleGroups = 0;
            filterEntries.forEach((entry) => {
                const searchable = normalizedPeopleQuery(
                    entry.dataset.peopleSearchValue || "",
                );
                const isRealEntry = Boolean(searchable);
                const visible =
                    terms.length === 0 ||
                    (isRealEntry &&
                        terms.every((term) => searchable.includes(term)));
                entry.hidden = !visible;
                if (visible && isRealEntry) {
                    if (entry.dataset.peopleEntryKind === "group") {
                        visibleGroups += 1;
                    } else {
                        visiblePeople += 1;
                    }
                }
            });
            if (searchStatus) {
                if (terms.length && visiblePeople + visibleGroups === 0) {
                    searchStatus.textContent = "No people or groups found";
                } else {
                    const qualifier = terms.length ? "found" : "available";
                    searchStatus.textContent =
                        `${visiblePeople} committer${visiblePeople === 1 ? "" : "s"} · ` +
                        `${visibleGroups} group${visibleGroups === 1 ? "" : "s"} ${qualifier}`;
                }
            }
            if (noResults) {
                noResults.hidden =
                    terms.length === 0 || visiblePeople + visibleGroups > 0;
            }
        };

        search?.addEventListener("input", applyPeopleSearch);
        search?.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                search.value = "";
                applyPeopleSearch();
            }
        });
        applyPeopleSearch();

        const memberSearch = panel.querySelector("[data-group-member-search]");
        const memberStatus = panel.querySelector("[data-group-member-status]");
        const memberNoResults = panel.querySelector(
            "[data-group-member-no-results]",
        );
        const memberEntries = Array.from(
            panel.querySelectorAll("[data-group-member-entry]"),
        );

        const updateMemberPicker = () => {
            const query = normalizedPeopleQuery(memberSearch?.value || "");
            let visibleMembers = 0;
            let selectedMembers = 0;
            memberEntries.forEach((entry) => {
                const searchable = normalizedPeopleQuery(
                    entry.dataset.memberSearchValue || "",
                );
                const visible = !query || searchable.includes(query);
                entry.hidden = !visible;
                visibleMembers += visible ? 1 : 0;
                selectedMembers += entry.querySelector(
                    "[data-group-member-select]",
                )?.checked
                    ? 1
                    : 0;
            });
            if (memberStatus && memberEntries.length) {
                memberStatus.textContent = `${selectedMembers} of ${memberEntries.length} committers selected${
                    query ? ` · ${visibleMembers} shown` : ""
                }`;
            }
            if (memberNoResults) {
                memberNoResults.hidden = !query || visibleMembers > 0;
            }
        };

        memberSearch?.addEventListener("input", updateMemberPicker);
        panel
            .querySelectorAll("[data-group-member-select]")
            .forEach((checkbox) =>
                checkbox.addEventListener("change", updateMemberPicker),
            );
        updateMemberPicker();
        updatePeopleSelection(panel);
    });

    let peopleFilterSubmitTimer;
    workspace.addEventListener("change", (event) => {
        const changed = event.target.closest?.(
            "[data-committer-select], [data-people-group-select]",
        );
        if (!changed) {
            return;
        }
        peoplePanels.forEach((panel) => {
            panel
                .querySelectorAll(
                    "[data-committer-select], [data-people-group-select]",
                )
                .forEach((candidate) => {
                    if (
                        candidate.name === changed.name &&
                        candidate.value === changed.value
                    ) {
                        candidate.checked = changed.checked;
                    }
                });
        });
        const selectedTotal = peoplePanels.length
            ? updatePeopleSelection(peoplePanels[0])
            : 0;
        peoplePanels.slice(1).forEach(updatePeopleSelection);
        updateMobilePeopleSummary(selectedTotal);

        const form = changed.closest("[data-people-filter-form]");
        if (form) {
            window.clearTimeout(peopleFilterSubmitTimer);
            peopleFilterSubmitTimer = window.setTimeout(() => {
                if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                } else {
                    form.submit();
                }
            }, 350);
        }
    });

    const searchForm = workspace.querySelector("[data-pdf-search-form]");
    const searchInput = searchForm?.querySelector("[data-pdf-search-input]");
    const searchChips = searchForm?.querySelector("[data-pdf-search-chips]");

    if (searchForm && searchInput && searchChips) {
        const normalizedPhrase = (value) =>
            value.normalize("NFKC").trim().replace(/\s+/gu, " ");

        const chipInputs = () =>
            Array.from(searchChips.querySelectorAll('input[name="chip"]'));

        const addChip = (rawValue) => {
            const phrase = normalizedPhrase(rawValue);
            if (!phrase || phrase.length > 4096 || chipInputs().length >= 32) {
                return false;
            }
            const key = phrase.toLocaleLowerCase();
            if (
                chipInputs().some(
                    (input) => normalizedPhrase(input.value).toLocaleLowerCase() === key,
                )
            ) {
                searchInput.value = "";
                return true;
            }

            const chip = document.createElement("span");
            chip.className = "bb-search-chip";
            chip.dataset.pdfSearchChip = "";

            const label = document.createElement("span");
            label.textContent = phrase;
            chip.append(label);

            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "chip";
            hidden.value = phrase;
            chip.append(hidden);

            const remove = document.createElement("button");
            remove.type = "button";
            remove.dataset.removePdfSearchChip = "";
            remove.setAttribute("aria-label", `Remove phrase ${phrase}`);
            remove.textContent = "×";
            chip.append(remove);

            searchChips.append(chip);
            searchInput.value = "";
            return true;
        };

        searchInput.addEventListener("keydown", (event) => {
            if (
                event.key === "Backspace" &&
                !event.isComposing &&
                !event.metaKey &&
                !event.ctrlKey &&
                !event.shiftKey &&
                !event.altKey &&
                !searchInput.value
            ) {
                const lastChip = searchChips.querySelector(
                    "[data-pdf-search-chip]:last-of-type",
                );
                if (lastChip) {
                    event.preventDefault();
                    lastChip.remove();
                    searchForm.submit();
                }
                return;
            }
            if (
                event.key !== "Enter" ||
                event.isComposing ||
                event.metaKey ||
                event.ctrlKey ||
                event.shiftKey ||
                event.altKey ||
                !searchInput.value.trim()
            ) {
                return;
            }
            event.preventDefault();
            if (addChip(searchInput.value)) {
                searchForm.submit();
            }
        });

        searchForm.addEventListener("submit", (event) => {
            if (!searchInput.value.trim() || chipInputs().length === 0) {
                return;
            }
            event.preventDefault();
            if (addChip(searchInput.value)) {
                searchForm.submit();
            }
        });

        searchChips.addEventListener("click", (event) => {
            const remove = event.target.closest("[data-remove-pdf-search-chip]");
            if (!remove) {
                return;
            }
            remove.closest("[data-pdf-search-chip]")?.remove();
            searchInput.value = "";
            searchForm.submit();
        });
    }

    const copyWithTextarea = (text) => {
        const textarea = document.createElement("textarea");
        const previouslyFocused = document.activeElement;
        textarea.value = text;
        textarea.readOnly = true;
        textarea.className = "bb-clipboard-fallback";
        textarea.setAttribute("aria-hidden", "true");
        document.body.append(textarea);
        textarea.select();
        textarea.setSelectionRange(0, text.length);
        let copied = false;
        try {
            copied = document.execCommand("copy");
        } finally {
            textarea.remove();
            if (previouslyFocused instanceof HTMLElement) {
                previouslyFocused.focus();
            }
        }
        if (!copied) {
            throw new Error("Clipboard copy was rejected");
        }
    };

    const copyText = async (text) => {
        if (navigator.clipboard?.writeText) {
            try {
                await navigator.clipboard.writeText(text);
                return;
            } catch (_error) {
                // Some local browser contexts need the legacy clipboard fallback.
            }
        }
        copyWithTextarea(text);
    };

    document.querySelectorAll("[data-copy-repository-urls]").forEach((repositoryUrlCopyButton) => {
        repositoryUrlCopyButton.addEventListener("click", async () => {
        const status = repositoryUrlCopyButton.querySelector("[data-copy-repository-urls-status]");
        let urls = [];
        try {
            urls = JSON.parse(document.getElementById("bb-repository-copy-urls")?.textContent || "[]");
            if (!Array.isArray(urls) || urls.length === 0) throw new Error("No repository URLs");
            repositoryUrlCopyButton.disabled = true;
            await copyText(urls.join("\n"));
            repositoryUrlCopyButton.title = `Copied ${urls.length} repository URL${urls.length === 1 ? "" : "s"}`;
            if (status) status.textContent = repositoryUrlCopyButton.title;
        } catch (_error) {
            repositoryUrlCopyButton.title = "Could not copy repository URLs. Check clipboard permission.";
            if (status) status.textContent = "Copy failed";
        } finally {
            window.setTimeout(() => {
                repositoryUrlCopyButton.disabled = false;
                repositoryUrlCopyButton.title = "Copy all repository URLs";
                if (status) status.textContent = "";
            }, 2000);
        }
        });
    });

    const pdfSelectionCount = workspace.querySelector("[data-pdf-selection-count]");
    const selectAllPdfs = workspace.querySelector("[data-select-all-pdfs]");
    const visiblePdfCheckboxes = () => Array.from(
        workspace.querySelectorAll("[data-pdf-select]"),
    ).filter((checkbox) => !checkbox.disabled && !checkbox.closest("[data-pdf-row]")?.hidden);
    const updatePdfSelection = () => {
        const checkboxes = visiblePdfCheckboxes();
        const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
        if (pdfSelectionCount) {
            pdfSelectionCount.hidden = selected === 0;
            pdfSelectionCount.textContent = `${selected} PDF${selected === 1 ? "" : "s"} selected`;
        }
        if (selectAllPdfs) {
            selectAllPdfs.checked = checkboxes.length > 0 && selected === checkboxes.length;
            selectAllPdfs.indeterminate = selected > 0 && selected < checkboxes.length;
        }
    };
    workspace.addEventListener("change", (event) => {
        if (event.target.matches("[data-pdf-select]")) updatePdfSelection();
        if (event.target.matches("[data-select-all-pdfs]")) {
            visiblePdfCheckboxes().forEach((checkbox) => {
                checkbox.checked = event.target.checked;
            });
            updatePdfSelection();
        }
    });

    const pathCopyTimers = new WeakMap();
    const pendingPathCopies = new WeakSet();
    workspace.addEventListener("click", async (event) => {
        const button = event.target.closest?.("[data-copy-pdf-path]");
        if (!button || button.disabled || pendingPathCopies.has(button)) {
            return;
        }
        const path = button.dataset.pdfLocalPath;
        const status = button.querySelector("[data-pdf-path-copy-status]");
        const icon = button.querySelector("[data-pdf-path-copy-icon]");
        const initialTitle = path ? `Copy full path: ${path}` : "Local file path unavailable";
        window.clearTimeout(pathCopyTimers.get(button));
        pendingPathCopies.add(button);
        button.setAttribute("aria-busy", "true");
        button.classList.remove("is-copy-error");
        button.title = initialTitle;
        if (status) status.textContent = "";
        if (icon) icon.hidden = true;
        let copied = false;
        try {
            if (!path) {
                throw new Error("Local file path unavailable");
            }
            await copyText(path);
            copied = true;
            if (status) status.textContent = "Copied";
            if (icon) icon.hidden = false;
        } catch (_error) {
            if (status) status.textContent = "Copy failed";
            button.classList.add("is-copy-error");
            button.title = "Could not copy the path. Check this browser's clipboard permission.";
        } finally {
            pendingPathCopies.delete(button);
            button.removeAttribute("aria-busy");
            pathCopyTimers.set(
                button,
                window.setTimeout(() => {
                    if (status) status.textContent = "";
                    if (icon) icon.hidden = true;
                    button.classList.remove("is-copy-error");
                    button.title = initialTitle;
                    pathCopyTimers.delete(button);
                }, copied ? 2000 : 4000),
            );
        }
    });

    const bulkResultActions = workspace.querySelector(".bb-result-actions");
    const copyResultPathsButton = bulkResultActions?.querySelector(
        "[data-copy-search-result-paths]",
    );
    const bulkResultStatus = bulkResultActions?.querySelector(
        "[data-search-bulk-status]",
    );
    const openSearchResultsForm = bulkResultActions?.querySelector(
        "[data-open-search-results-form]",
    );
    const openAllConfirmation = bulkResultActions?.querySelector(
        "[data-open-all-confirmation]",
    );

    if (openSearchResultsForm) {
        const resultCount = Number(openSearchResultsForm.dataset.resultCount || "0");
        const confirmationThreshold = Number(
            openSearchResultsForm.dataset.confirmThreshold || "0",
        );
        const confirmedInput = openSearchResultsForm.querySelector(
            "[data-open-all-confirmed]",
        );
        const submitButton = openSearchResultsForm.querySelector(
            "[data-open-search-results-submit]",
        );

        if (resultCount > confirmationThreshold && confirmedInput) {
            openSearchResultsForm.addEventListener("submit", (event) => {
                if (confirmedInput.value === "1") {
                    return;
                }
                event.preventDefault();

                if (openAllConfirmation?.showModal) {
                    openAllConfirmation.showModal();
                    return;
                }

                if (
                    window.confirm(
                        `Open ${resultCount} PDFs from the current result page?`,
                    )
                ) {
                    confirmedInput.value = "1";
                    openSearchResultsForm.requestSubmit(submitButton || undefined);
                }
            });

            openAllConfirmation
                ?.querySelector("[data-confirm-open-search-results]")
                ?.addEventListener("click", () => {
                    confirmedInput.value = "1";
                    openAllConfirmation.close();
                    openSearchResultsForm.requestSubmit(submitButton || undefined);
                });
        }
    }

    if (bulkResultActions && copyResultPathsButton) {
        const currentSearchResultPaths = () =>
            Array.from(
                workspace.querySelectorAll(
                    "[data-search-result-row]:not([hidden]) [data-pdf-local-path]",
                ),
                (element) => element.dataset.pdfLocalPath || "",
            ).filter((path) => path.length > 0);

        const announceBulkResult = (message) => {
            if (bulkResultStatus) {
                bulkResultStatus.textContent = "";
                window.requestAnimationFrame(() => {
                    bulkResultStatus.textContent = message;
                });
            }
        };

        copyResultPathsButton.addEventListener("click", async () => {
            const paths = currentSearchResultPaths();
            if (!paths.length) {
                announceBulkResult("No PDF paths are available on this result page.");
                return;
            }

            copyResultPathsButton.disabled = true;
            try {
                await copyText(paths.join("\n"));
                announceBulkResult(
                    `${paths.length} PDF path${paths.length === 1 ? "" : "s"} copied.`,
                );
            } catch (_error) {
                announceBulkResult(
                    "PDF paths could not be copied. Check this browser's clipboard permission.",
                );
            } finally {
                copyResultPathsButton.disabled = false;
                copyResultPathsButton.focus();
            }
        });
    }

    if (shouldPoll()) {
        pollTimer = window.setTimeout(poll, 500);
    }
})();
