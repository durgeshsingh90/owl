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
    let extractionWorkActive = workspace.dataset.extractionActive === "true";
    let workSummary = {
        label: initialRefreshAllState?.workLabel || "",
        detail: initialRefreshAllState?.workDetail || "",
    };
    const repositoryNoun = (count) => (count === 1 ? "repository" : "repositories");
    const setRefreshIconBusy = (icon, busy) => {
        if (!icon) return;
        // SVG elements do not reflect the HTML hidden property into attributes.
        // Toggle the attribute explicitly so idle/live transitions affect CSS.
        icon.hidden = busy;
        if (busy) icon.setAttribute("hidden", "");
        else icon.removeAttribute("hidden");
    };

    const updateRefreshAllButtons = (repositories, extraction, work) => {
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
            if (spinner) spinner.hidden = !busy;
            setRefreshIconBusy(icon, busy);
        });
    };

    updateRefreshAllButtons();

    const selectionForm = workspace.querySelector("[data-repository-selection-form]");
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
        const busy = selected.some((card) =>
            card.dataset.repositoryActiveWork === "true" ||
            card.dataset.repositoryActiveSync === "true",
        );
        const removalPending = selected.some((card) =>
            card.dataset.repositoryRemovalPending === "true",
        );
        const unavailable = !count || repositorySubmissionPending || repositoryStatusPending;
        const allExcluded = count > 0 && selected.every((card) =>
            card.dataset.repositoryRefreshExcluded === "true",
        );
        if (unavailable || busy || removalPending ||
            (deletionUnlocked && Date.now() >= deleteUnlockExpiresAt)) resetDeleteLock();
        const disable = (selector, disabled, title) => {
            workspace.querySelectorAll(selector).forEach((button) => {
                button.disabled = disabled;
                button.title = title;
                button.setAttribute("aria-label", title);
            });
        };
        const suffix = `${count} selected ${repositoryNoun(count)}`;
        disable("[data-selected-refresh]", unavailable || busy || removalPending,
            busy ? "Selected repository work in progress" : `Refresh ${suffix}`);
        disable("[data-selected-exclude]", unavailable || removalPending,
            `${allExcluded ? "Include" : "Exclude"} ${suffix} ${allExcluded ? "in" : "from"} refresh`);
        const deleteTitle = !count ? "Select repositories to delete"
            : repositorySubmissionPending ? "Wait for the repository request to finish"
            : repositoryStatusPending ? "Wait for the latest repository status before deleting"
            : busy ? "Wait for selected repositories' Git and PDF workers to finish before deleting"
            : removalPending ? "Use Retry removal to finish incomplete repository removal"
            : deletionUnlocked ? `Click again to delete ${suffix} from this computer`
            : `Unlock deletion for ${suffix}`;
        disable("[data-selected-remove]", unavailable || busy || removalPending, deleteTitle);
        workspace.querySelectorAll("[data-selected-remove]").forEach((button) => {
            button.dataset.deleteLocked = String(!deletionUnlocked);
            const icon = button.querySelector("[data-selected-delete-lock-icon]");
            if (icon) icon.textContent = deletionUnlocked ? "🔓" : "🔒";
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
        announceDeleteLock("Delete unlocked. Click again to review removal of the selected repositories. Locks again in 10 seconds.");
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
                !["refresh", "exclude", "remove"].includes(operation) ||
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
            repositorySubmissionPending = true;
            resetDeleteLock();
            updateSelectedRepositoryActions();
            updateRefreshAllButtons();
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
        cardsFor(repository.id).forEach((card) => {
            window.OWLRepositoryTimers?.update(
                card.querySelector("[data-repository-worker-timer]"), repository.workerTiming,
            );
            card.dataset.repositoryState = repository.state;
            card.dataset.repositoryActiveWork = String(working);
            card.dataset.repositoryActiveSync = String(Boolean(repository.active));
            card.dataset.repositoryRefreshExcluded = String(Boolean(repository.refreshExcluded));
            card.dataset.repositoryRemovalPending = String(Boolean(repository.hasRemovalPending));
            const stateIcon = card.querySelector("[data-repository-state-icon]");
            if (stateIcon) {
                stateIcon.className = `bb-repository-state bb-repository-state--${working ? "working" : repository.state}`;
                stateIcon.setAttribute(
                    "aria-label",
                    `${repository.name}: ${working ? workLabel : repository.stateLabel}`,
                );
                stateIcon.title = working ? workDetail : repository.stateLabel;
            }
            const workStatus = card.querySelector("[data-repository-work-label]");
            if (workStatus) {
                workStatus.textContent = working ? workDetail : "";
                workStatus.title = working ? workDetail : "";
                workStatus.hidden = !working;
            }
            const documents = card.querySelector("[data-repository-documents]");
            if (documents) {
                documents.textContent = `${repository.pdfCount} PDF · ${repository.vsdxCount} VSDX`;
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
            updateRefreshAllButtons(payload.repositories, payload.extraction, payload.work);
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
            document.querySelectorAll("[data-repository-id] [data-repository-worker-timer]").forEach((timer) => {
                window.OWLRepositoryTimers?.stale(timer);
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
        selectionForm?.querySelectorAll('input[name="operation"]').forEach((input) => input.remove());
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
