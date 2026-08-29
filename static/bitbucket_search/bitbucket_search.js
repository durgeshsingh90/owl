(() => {
    "use strict";

    const workspace = document.querySelector("[data-bitbucket-workspace]");
    if (!workspace) {
        return;
    }

    const cardsFor = (repositoryId) =>
        document.querySelectorAll(`[data-repository-id="${repositoryId}"]`);

    const formatTimestamp = (value) => {
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

    const updateRepository = (repository) => {
        cardsFor(repository.id).forEach((card) => {
            card.dataset.repositoryState = repository.state;
            const stateIcon = card.querySelector("[data-repository-state-icon]");
            if (stateIcon) {
                stateIcon.className = `bb-repository-state bb-repository-state--${repository.state}`;
            }
            const stateLabel = card.querySelector("[data-repository-state-label]");
            if (stateLabel) {
                stateLabel.textContent = repository.stateLabel;
            }
            const progressLabel = card.querySelector("[data-repository-progress-label]");
            if (progressLabel) {
                progressLabel.textContent = repository.active ? `${repository.progress}%` : "";
            }
            const progress = card.querySelector("[data-repository-progress]");
            if (progress) {
                progress.hidden = !repository.active;
                progress.setAttribute("aria-valuenow", String(repository.progress));
            }
            const progressBar = card.querySelector("[data-repository-progress-bar]");
            if (progressBar) {
                progressBar.style.width = `${repository.progress}%`;
            }
            const message = card.querySelector("[data-repository-message]");
            if (message) {
                message.textContent = repository.message || repository.stateLabel;
            }
            const automatic = repository.automatic || {};
            const automaticContainer = card.querySelector("[data-repository-automatic]");
            if (automaticContainer) {
                automaticContainer.className =
                    `bb-repository-card__automatic bb-repository-card__automatic--${automatic.state || "due"}`;
                automaticContainer.dataset.automaticState = automatic.state || "due";
            }
            const automaticLabel = card.querySelector(
                "[data-repository-automatic-label]",
            );
            if (automaticLabel) {
                automaticLabel.textContent = automatic.label || "Daily refresh";
            }
            const automaticDetail = card.querySelector(
                "[data-repository-automatic-detail]",
            );
            if (automaticDetail) {
                const automaticTime = formatTimestamp(automatic.nextActionAt);
                const detail =
                    automatic.detail || "Waiting for the daily refresh scheduler.";
                automaticDetail.textContent = automaticTime
                    ? `${detail} Next: ${automaticTime}.`
                    : detail;
            }
            const documents = card.querySelector("[data-repository-documents]");
            if (documents) {
                documents.textContent = `${repository.pdfCount} PDF · ${repository.vsdxCount} VSDX`;
            }
            const catalogStatus = card.querySelector("[data-repository-catalog-status]");
            if (catalogStatus) {
                const publishedAt = formatTimestamp(repository.catalogPublishedAt);
                catalogStatus.textContent = publishedAt
                    ? `${repository.catalogStale ? "Catalogue retained from" : "Catalogue updated"} ${publishedAt}`
                    : "No PDF catalogue published yet";
            }
            const refreshButton = card.querySelector("[data-repository-refresh-form] button");
            if (refreshButton) {
                refreshButton.disabled = repository.active;
            }
        });
    };

    const updateSummary = (summary) => {
        const container = document.querySelector("[data-sync-summary]");
        if (container) {
            container.classList.remove(
                "bb-sync-status--empty",
                "bb-sync-status--ready",
                "bb-sync-status--active",
                "bb-sync-status--scheduled",
                "bb-sync-status--attention",
            );
            container.classList.add(`bb-sync-status--${summary.state}`);
            container.setAttribute("aria-label", `Sync status: ${summary.label.toLowerCase()}`);
        }
        document.querySelectorAll("[data-sync-summary-label]").forEach((element) => {
            element.textContent = summary.label;
        });
        document.querySelectorAll("[data-sync-summary-detail]").forEach((element) => {
            element.textContent = summary.detail;
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
        const activeCount = repositories.filter((repository) => repository.active).length;
        document.querySelectorAll("[data-mobile-repository-count]").forEach((element) => {
            let activityLabel = activeCount ? ` · ${activeCount} syncing` : "";
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
                '[data-repository-state="queued"], [data-repository-state="cloning"], [data-repository-state="fetching"], [data-repository-state="updating"]',
            ),
            (card) => card.dataset.repositoryId,
        ).filter(Boolean),
    );
    let extractionActive = workspace.dataset.extractionActive === "true";
    let extractionPublicationSignature =
        workspace.dataset.extractionPublicationSignature || "";
    let catalogPublicationSignature = workspace.dataset.catalogPublicationSignature || "";
    let dailyRefreshEnabled = workspace.dataset.dailyRefreshEnabled === "true";

    const activePollDelay = 1500;
    const idlePollDelay = 30000;

    const shouldPoll = () =>
        activeRepositoryIds.size > 0 || extractionActive || dailyRefreshEnabled;

    const nextPollDelay = () =>
        activeRepositoryIds.size > 0 || extractionActive ? activePollDelay : idlePollDelay;

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
            updateSummary(payload.summary);
            updateTotals(payload.totals, payload.repositories, payload.automation);
            const repositoryCompleted = payload.repositories.some(
                (repository) =>
                    activeRepositoryIds.has(String(repository.id)) && !repository.active,
            );
            activeRepositoryIds = new Set(
                payload.repositories
                    .filter((repository) => repository.active)
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
            extractionActive = Boolean(extraction.active);
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
            if (
                repositoryCompleted ||
                extractionPublicationChanged ||
                catalogPublicationChanged
            ) {
                // The durable catalog is published in the same transaction as
                // the worker's terminal state. The extraction signature is
                // rendered back into the page, preventing a reload loop.
                window.location.reload();
                return;
            }
        } catch (_error) {
            // An active or scheduled refresh remains recoverable. Use the same
            // bounded cadence so a temporary status failure never creates a tight loop.
        }
        if (shouldPoll()) {
            window.setTimeout(poll, nextPollDelay());
        }
    };

    document.querySelectorAll("[data-repository-filter]").forEach((input) => {
        input.addEventListener("input", () => {
            const query = input.value.trim().toLocaleLowerCase();
            document.querySelectorAll("[data-repository-id]").forEach((card) => {
                const searchable = card.textContent.toLocaleLowerCase();
                card.hidden = Boolean(query) && !searchable.includes(query);
            });
        });
    });

    const peoplePanels = Array.from(
        workspace.querySelectorAll("[data-people-panel]"),
    );
    const normalizedPeopleQuery = (value) =>
        value.normalize("NFKC").trim().toLocaleLowerCase();

    const updatePeopleSelection = (panel) => {
        const selectedPeople = panel.querySelectorAll(
            "[data-committer-select]:checked",
        ).length;
        const selectedGroups = panel.querySelectorAll(
            "[data-people-group-select]:checked",
        ).length;
        const selectedTotal = selectedPeople + selectedGroups;
        const status = panel.querySelector("[data-people-selection-status]");
        const submit = panel.querySelector("[data-people-filter-submit]");

        if (status) {
            status.textContent =
                `${selectedPeople} committer${selectedPeople === 1 ? "" : "s"} · ` +
                `${selectedGroups} group${selectedGroups === 1 ? "" : "s"} selected`;
        }
        if (submit) {
            submit.textContent = selectedTotal
                ? `Apply people (${selectedTotal})`
                : "Apply people";
        }
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
            const query = normalizedPeopleQuery(search?.value || "");
            let visiblePeople = 0;
            let visibleGroups = 0;
            filterEntries.forEach((entry) => {
                const searchable = normalizedPeopleQuery(
                    entry.dataset.peopleSearchValue || "",
                );
                const isRealEntry = Boolean(searchable);
                const visible = !query || (isRealEntry && searchable.includes(query));
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
                const qualifier = query ? "shown" : "available";
                searchStatus.textContent =
                    `${visiblePeople} committer${visiblePeople === 1 ? "" : "s"} · ` +
                    `${visibleGroups} group${visibleGroups === 1 ? "" : "s"} ${qualifier}`;
            }
            if (noResults) {
                noResults.hidden = !query || visiblePeople + visibleGroups > 0;
            }
        };

        search?.addEventListener("input", applyPeopleSearch);
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
                (element) => element.textContent,
            ).filter((path) => path.length > 0);

        const announceBulkResult = (message) => {
            if (bulkResultStatus) {
                bulkResultStatus.textContent = "";
                window.requestAnimationFrame(() => {
                    bulkResultStatus.textContent = message;
                });
            }
        };

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
                    // Browsers can expose Clipboard API while denying it for
                    // the current context. The user gesture still permits the
                    // legacy textarea fallback in supported local browsers.
                }
            }
            copyWithTextarea(text);
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

    const timeline = workspace.querySelector("[data-pdf-timeline]");
    const loadOlderContainer = timeline?.querySelector("[data-load-older-container]");
    const loadOlderLink = timeline?.querySelector("[data-load-older]");

    if (timeline && loadOlderContainer && loadOlderLink) {
        const table = timeline.querySelector(".bb-results-table");
        const loadStatus = timeline.querySelector("[data-load-older-status]");
        const completeStatus = timeline.querySelector("[data-load-older-complete]");
        const visibleStart = document.querySelector("[data-pdf-visible-start]");
        const visibleEnd = document.querySelector("[data-pdf-visible-end]");
        const currentPage = document.querySelector("[data-pdf-current-page]");
        const footerNextPage = document.querySelector("[data-pdf-next-page]");
        const footerNextPageEnd = document.querySelector("[data-pdf-next-page-end]");
        const idleLabel = loadOlderLink.textContent.trim();
        let loadingOlder = false;
        let htmlFallbackRequired = false;
        let observer = null;

        const sameOriginUrl = (candidate) => {
            const url = new URL(candidate, window.location.href);
            if (url.origin !== window.location.origin) {
                throw new Error("Cross-origin PDF pagination URL rejected");
            }
            return url;
        };

        const announce = (message) => {
            if (loadStatus) {
                loadStatus.textContent = message;
            }
        };

        const setLoadingOlder = (loading) => {
            loadingOlder = loading;
            timeline.setAttribute("aria-busy", String(loading));
            loadOlderLink.setAttribute("aria-disabled", String(loading));
            loadOlderLink.textContent = loading ? "Loading older PDFs…" : idleLabel;
        };

        const setNextPage = (candidate, { manual = false } = {}) => {
            timeline.dataset.nextPageUrl = candidate;
            htmlFallbackRequired = false;
            if (candidate) {
                const nextUrl = sameOriginUrl(candidate).href;
                loadOlderLink.href = nextUrl;
                loadOlderLink.hidden = false;
                if (footerNextPage) {
                    footerNextPage.href = nextUrl;
                    footerNextPage.hidden = false;
                }
                if (footerNextPageEnd) {
                    footerNextPageEnd.hidden = true;
                }
                if (completeStatus) {
                    completeStatus.hidden = true;
                }
                return;
            }

            loadOlderLink.hidden = true;
            if (footerNextPage) {
                footerNextPage.hidden = true;
            }
            if (footerNextPageEnd) {
                footerNextPageEnd.hidden = false;
            }
            if (completeStatus) {
                completeStatus.hidden = false;
                if (manual) {
                    completeStatus.focus({ preventScroll: true });
                }
            } else {
                loadOlderContainer.hidden = true;
            }
            observer?.disconnect();
        };

        const appendTimelineGroups = (incomingTable) => {
            if (!table || !incomingTable) {
                throw new Error("PDF timeline was missing from the next page");
            }

            const existingIds = new Set(
                Array.from(table.querySelectorAll("[data-pdf-row]"), (row) => row.dataset.documentId),
            );
            const incomingRows = Array.from(incomingTable.querySelectorAll("[data-pdf-row]"));
            const acceptedRows = incomingRows.filter((row) => {
                const documentId = row.dataset.documentId;
                if (documentId && existingIds.has(documentId)) {
                    row.remove();
                    return false;
                }
                if (documentId) {
                    existingIds.add(documentId);
                }
                return true;
            });

            const incomingGroups = Array.from(incomingTable.tBodies).filter((group) =>
                group.matches("[data-pdf-group]"),
            );
            incomingGroups.forEach((group) => {
                if (!group.querySelector("[data-pdf-row]")) {
                    group.remove();
                }
            });

            const currentGroups = Array.from(table.tBodies).filter((group) =>
                group.matches("[data-pdf-group]"),
            );
            const firstIncomingGroup = incomingGroups.find((group) => group.isConnected);
            const lastCurrentGroup = currentGroups[currentGroups.length - 1];
            if (
                firstIncomingGroup &&
                lastCurrentGroup &&
                firstIncomingGroup.dataset.timelineGroupKey &&
                firstIncomingGroup.dataset.timelineGroupKey ===
                    lastCurrentGroup.dataset.timelineGroupKey
            ) {
                firstIncomingGroup.querySelectorAll("[data-pdf-row]").forEach((row) => {
                    lastCurrentGroup.append(row);
                });
                firstIncomingGroup.remove();
            }

            incomingGroups.forEach((group) => {
                if (group.isConnected) {
                    table.append(group);
                }
            });
            return acceptedRows.length;
        };

        const updateVisibleRange = (pageNumber) => {
            if (currentPage && Number.isInteger(pageNumber)) {
                currentPage.textContent = String(pageNumber);
            }
            const start = Number.parseInt(visibleStart?.textContent || "", 10);
            const rowCount = table?.querySelectorAll("[data-pdf-row]").length || 0;
            if (visibleEnd && Number.isInteger(start) && rowCount) {
                visibleEnd.textContent = String(start + rowCount - 1);
            }
        };

        const loadOlder = async ({ manual = false } = {}) => {
            const candidate = timeline.dataset.nextPageUrl;
            if (loadingOlder || htmlFallbackRequired || !candidate) {
                return;
            }

            let requestedUrl;
            try {
                requestedUrl = sameOriginUrl(candidate);
            } catch (_error) {
                announce("Older PDFs could not be loaded because the page URL was invalid.");
                return;
            }

            setLoadingOlder(true);
            announce("Loading older PDFs…");
            try {
                const response = await fetch(requestedUrl.href, {
                    credentials: "same-origin",
                    headers: {
                        Accept: "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });
                if (!response.ok) {
                    throw new Error("Older PDF request failed");
                }
                const payload = await response.json();
                if (!payload || typeof payload.html !== "string") {
                    throw new Error("Older PDF timeline response was invalid");
                }
                const responseDocument = new DOMParser().parseFromString(
                    `<table class="bb-results-table">${payload.html}</table>`,
                    "text/html",
                );
                const incomingTable = responseDocument.querySelector(".bb-results-table");

                const nextCandidate =
                    typeof payload.nextPageUrl === "string" ? payload.nextPageUrl : "";
                if (nextCandidate && sameOriginUrl(nextCandidate).href === requestedUrl.href) {
                    throw new Error("PDF pagination did not advance");
                }
                if (nextCandidate) {
                    sameOriginUrl(nextCandidate);
                }
                const addedCount = appendTimelineGroups(incomingTable);
                setNextPage(nextCandidate, { manual });
                updateVisibleRange(payload.page);
                announce(
                    addedCount
                        ? `${addedCount} older PDF${addedCount === 1 ? "" : "s"} loaded.`
                        : "No additional PDFs were returned.",
                );
            } catch (_error) {
                htmlFallbackRequired = true;
                observer?.disconnect();
                announce(
                    "Automatic loading failed. Activate Load older PDFs to open the HTML page.",
                );
            } finally {
                setLoadingOlder(false);
            }
        };

        loadOlderLink.addEventListener("click", (event) => {
            if (
                htmlFallbackRequired ||
                event.button !== 0 ||
                event.metaKey ||
                event.ctrlKey ||
                event.shiftKey ||
                event.altKey
            ) {
                return;
            }
            event.preventDefault();
            void loadOlder({ manual: true });
        });

        if ("IntersectionObserver" in window) {
            observer = new IntersectionObserver(
                (entries) => {
                    if (entries.some((entry) => entry.isIntersecting)) {
                        void loadOlder();
                    }
                },
                { rootMargin: "240px 0px" },
            );
            observer.observe(loadOlderLink);
        }
    }

    if (shouldPoll()) {
        window.setTimeout(poll, 500);
    }
})();
