(() => {
    "use strict";

    const themeStorageKey = "owl-theme";

    const applyTheme = (theme) => {
        document.body.dataset.theme = theme;
        document.querySelectorAll("[data-theme-toggle]").forEach((toggle) => {
            const isDark = theme === "dark";
            toggle.setAttribute("aria-pressed", String(isDark));
            toggle.setAttribute("aria-label", `Switch to ${isDark ? "light" : "dark"} mode`);
            const label = toggle.querySelector("[data-theme-toggle-label]");
            if (label) {
                label.textContent = isDark ? "Light mode" : "Dark mode";
            }
        });
    };

    try {
        const savedTheme = window.localStorage.getItem(themeStorageKey);
        if (savedTheme === "light" || savedTheme === "dark") {
            applyTheme(savedTheme);
        } else {
            applyTheme(document.body.dataset.theme || "light");
        }
    } catch {
        applyTheme(document.body.dataset.theme || "light");
    }

    document.addEventListener("click", (event) => {
        const themeToggle = event.target.closest("[data-theme-toggle]");
        if (!themeToggle) {
            return;
        }

        const nextTheme = document.body.dataset.theme === "dark" ? "light" : "dark";
        applyTheme(nextTheme);
        try {
            window.localStorage.setItem(themeStorageKey, nextTheme);
        } catch {
            // Theme selection still applies for this visit when browser storage is unavailable.
        }
    });

    document.addEventListener("click", (event) => {
        const toggle = event.target.closest("[data-app-sidebar-toggle]");
        if (!toggle) {
            return;
        }

        const shell = toggle.closest("[data-app-sidebar-shell]");
        if (!shell) {
            return;
        }

        const isOpen = shell.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", String(isOpen));
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }

        const shell = document.querySelector("[data-app-sidebar-shell].is-open");
        const toggle = shell?.querySelector("[data-app-sidebar-toggle]");
        if (!shell || !toggle || window.matchMedia("(min-width: 801px)").matches) {
            return;
        }

        shell.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
    });

    const exactDateFormatter = (() => {
        try {
            return new Intl.DateTimeFormat(undefined, {
                year: "numeric",
                month: "short",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                timeZoneName: "short",
            });
        } catch {
            return null;
        }
    })();

    const formatNotificationDate = (value, fallback = "Not yet") => {
        if (!value) {
            return fallback;
        }

        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return fallback;
        }

        return exactDateFormatter ? exactDateFormatter.format(date) : date.toLocaleString();
    };

    const compactRepositoryDate = (value) => {
        if (!value) {
            return "—";
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return "—";
        }
        const now = new Date();
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        }
        return date.toLocaleDateString(undefined, {
            day: "numeric",
            month: "short",
            ...(date.getFullYear() === now.getFullYear() ? {} : { year: "2-digit" }),
        });
    };

    const createNotificationElement = (tagName, className, text) => {
        const element = document.createElement(tagName);
        if (className) {
            element.className = className;
        }
        if (text !== undefined && text !== null) {
            element.textContent = String(text);
        }
        return element;
    };

    const notificationState = (value) => {
        const allowedStates = new Set(["info", "running", "success", "succeeded", "warning", "partial", "failed", "error"]);
        const state = String(value || "info").toLowerCase();
        return allowedStates.has(state) ? state : "info";
    };

    const localTargetPath = (value) => {
        const path = String(value || "");
        return path.startsWith("/") && !path.startsWith("//") && !/[\\\u0000-\u001f\u007f]/.test(path) ? path : "";
    };

    const headerPanels = [];
    const createHeaderPanel = (container, toggle, panel, label, onOpen) => {
        if (!container || !toggle || !panel) {
            return null;
        }
        let isOpen = false;
        let summary = "";
        const updateLabel = () => {
            toggle.setAttribute("aria-label", `${isOpen ? "Close" : "Open"} ${label}${summary ? `, ${summary}` : ""}`);
            toggle.setAttribute("aria-expanded", String(isOpen));
        };
        const close = ({ restoreFocus = true } = {}) => {
            if (!isOpen) {
                return;
            }
            isOpen = false;
            panel.hidden = true;
            updateLabel();
            if (restoreFocus) {
                toggle.focus();
            }
        };
        const position = () => {
            if (!window.matchMedia("(max-width: 620px)").matches) {
                panel.style.removeProperty("--notification-mobile-top");
                return;
            }
            const bounds = toggle.getBoundingClientRect();
            const availableHeight = window.innerHeight || document.documentElement.clientHeight;
            const top = Math.max(8, Math.min(bounds.bottom + 8, availableHeight - 220));
            panel.style.setProperty("--notification-mobile-top", `${Math.round(top)}px`);
        };
        const controller = {
            close,
            setSummary(value) {
                summary = value;
                updateLabel();
            },
        };
        headerPanels.push(controller);
        toggle.addEventListener("click", () => {
            if (isOpen) {
                close();
                return;
            }
            headerPanels.forEach((other) => other.close({ restoreFocus: false }));
            isOpen = true;
            panel.hidden = false;
            position();
            updateLabel();
            panel.focus();
            onOpen();
        });
        document.addEventListener("click", (event) => {
            if (isOpen && !container.contains(event.target)) {
                close({ restoreFocus: false });
            }
        });
        document.addEventListener("keydown", (event) => {
            if (isOpen && event.key === "Escape") {
                event.preventDefault();
                close();
            }
        });
        window.addEventListener("resize", () => {
            if (isOpen) {
                position();
            }
        });
        return controller;
    };

    document.querySelectorAll("[data-notification-center]").forEach((center) => {
        const toggle = center.querySelector("[data-notification-toggle]");
        const panel = center.querySelector("[data-notification-panel]");
        const badge = center.querySelector("[data-notification-badge]");
        const list = center.querySelector("[data-notification-list]");
        const empty = center.querySelector("[data-notification-empty]");
        const live = center.querySelector("[data-notification-live]");
        const readAll = center.querySelector("[data-notification-read-all]");
        const unreadLabel = center.querySelector("[data-notification-unread-label]");
        const statusCenter = document.querySelector("[data-repository-status-center]");
        const statusToggle = statusCenter?.querySelector("[data-repository-status-toggle]");
        const statusPanel = statusCenter?.querySelector("[data-repository-status-panel]");
        const statusIndicator = statusCenter?.querySelector("[data-repository-status-indicator]");
        const statusLive = statusCenter?.querySelector("[data-repository-status-live]");
        const backgroundState = statusCenter?.querySelector("[data-notification-background-state]");
        const progressCard = statusCenter?.querySelector("[data-notification-progress-card]");
        const progressLabel = statusCenter?.querySelector("[data-notification-progress-label]");
        const progressDetail = statusCenter?.querySelector("[data-notification-progress-detail]");
        const progress = statusCenter?.querySelector("[data-notification-progress]");
        const scheduleCard = statusCenter?.querySelector("[data-notification-schedule]");
        const nextRun = statusCenter?.querySelector("[data-notification-next-run]");
        const lastAttempt = statusCenter?.querySelector("[data-notification-last-attempt]");
        const lastSuccess = statusCenter?.querySelector("[data-notification-last-success]");
        const retryRow = statusCenter?.querySelector("[data-notification-retry-row]");
        const retry = statusCenter?.querySelector("[data-notification-retry]");
        const repositoryList = statusCenter?.querySelector("[data-notification-repository-list]");
        const repositoryCount = statusCenter?.querySelector("[data-notification-repository-count]");
        const repositoryMessage = statusCenter?.querySelector("[data-notification-repository-message]");
        const bitbucketScheduleTickForm = center.querySelector(
            "[data-bitbucket-schedule-tick-form]",
        );
        const csrfToken = center.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";

        if (!toggle || !panel || !list) {
            return;
        }

        let isActive = false;
        let hasLoaded = false;
        let unreadCount = 0;
        let pollTimer = null;
        let requestInFlight = false;
        let repositoriesActive = false;
        let notificationSignature = null;
        const repositoryRows = new Map();
        // One read-only snapshot feeds two independent panels; do not duplicate
        // polling requests or the hidden daily-scheduler submissions.
        const notificationPanel = createHeaderPanel(center, toggle, panel, "notifications", () => void load());
        const backgroundPanel = createHeaderPanel(statusCenter, statusToggle, statusPanel, "background status", () => void load());

        const announce = (message) => {
            if (!live) {
                return;
            }
            live.textContent = "";
            window.setTimeout(() => {
                live.textContent = message;
            }, 20);
        };

        const post = async (url, values = {}) => {
            if (!url) {
                throw new Error("This notification action is unavailable.");
            }
            const body = new URLSearchParams();
            Object.entries(values).forEach(([key, value]) => body.set(key, String(value)));
            const response = await window.fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body,
            });
            if (!response.ok) {
                throw new Error("The notification action could not be completed.");
            }
            return response;
        };

        const refreshBadge = (nextUnreadCount) => {
            const nextCount = Math.max(0, Number.parseInt(nextUnreadCount, 10) || 0);
            if (badge) {
                badge.textContent = nextCount > 99 ? "99+" : String(nextCount);
                badge.hidden = nextCount === 0;
            }
            if (readAll) {
                readAll.hidden = nextCount === 0;
                readAll.disabled = nextCount === 0;
            }
            if (unreadLabel) {
                unreadLabel.textContent = nextCount === 0 ? "No unread" : `${nextCount} unread`;
            }
            notificationPanel.setSummary(nextCount ? `${nextCount} unread` : "");

            if (hasLoaded && nextCount > unreadCount) {
                const added = nextCount - unreadCount;
                announce(`${added} new notification${added === 1 ? "" : "s"}.`);
            }
            unreadCount = nextCount;
        };

        const renderNotification = (item) => {
            const state = notificationState(item.state);
            const article = createNotificationElement(
                "article",
                `notification-card${item.read ? "" : " is-unread"}`,
            );
            article.dataset.state = state;

            const icon = createNotificationElement(
                "span",
                "notification-card__icon",
                String(item.kindLabel || item.kind || "Update").trim().charAt(0).toUpperCase() || "•",
            );
            icon.setAttribute("aria-hidden", "true");

            const body = createNotificationElement("div", "notification-card__body");
            const heading = createNotificationElement("div", "notification-card__heading");
            const targetPath = localTargetPath(item.targetPath || item.target_path);
            const title = targetPath
                ? createNotificationElement("a", "notification-card__title", item.title || "OWL update")
                : createNotificationElement("strong", "notification-card__title", item.title || "OWL update");
            if (targetPath) {
                title.href = targetPath;
            }
            heading.append(title);
            heading.append(createNotificationElement("span", "notification-card__state", item.stateLabel || item.state || "Update"));
            body.append(heading);

            if (item.message) {
                body.append(createNotificationElement("p", "notification-card__message", item.message));
            }

            const footer = createNotificationElement("footer", "notification-card__footer");
            const occurredAt = item.occurredAt || item.occurred_at;
            const time = createNotificationElement("time", "", formatNotificationDate(occurredAt, "Time unavailable"));
            if (occurredAt) {
                time.dateTime = occurredAt;
            }
            footer.append(time);
            footer.append(createNotificationElement("span", "", item.kindLabel || item.kind || "OWL"));

            if (!item.read) {
                const readButton = createNotificationElement("button", "notification-card__read", "Mark read");
                readButton.type = "button";
                readButton.addEventListener("click", async () => {
                    readButton.disabled = true;
                    try {
                        await post(center.dataset.readUrl, { notification_id: item.id });
                        article.classList.remove("is-unread");
                        readButton.remove();
                        refreshBadge(unreadCount - 1);
                        announce("Notification marked as read.");
                    } catch (error) {
                        readButton.disabled = false;
                        announce(error.message);
                    }
                });
                footer.append(readButton);
            }

            body.append(footer);
            article.append(icon, body);
            return article;
        };

        const createRepositoryRow = () => {
            const row = createNotificationElement("li", "notification-repository");
            const details = createNotificationElement("details", "notification-repository__details");
            const summary = createNotificationElement("summary", "notification-repository__summary");
            const icon = createNotificationElement("span", "notification-repository__icon");
            icon.setAttribute("aria-hidden", "true");
            const name = createNotificationElement("span", "notification-repository__name");
            const status = createNotificationElement("span", "notification-repository__status");
            const time = createNotificationElement("time", "notification-repository__time");
            summary.append(icon, name, status, time);
            const body = createNotificationElement("div", "notification-repository__body");
            const fullName = createNotificationElement("strong", "notification-repository__full-name");
            const detail = createNotificationElement("p", "notification-repository__detail");
            const updated = createNotificationElement("p", "notification-repository__date");
            const lastSuccess = createNotificationElement("p", "notification-repository__date");
            const lastOutcome = createNotificationElement("p", "notification-repository__date");
            const link = createNotificationElement("a", "notification-repository__link", "View repository");
            const statusLink = createNotificationElement("a", "notification-repository__link", "Full status");
            body.append(fullName, detail, updated, lastOutcome, lastSuccess, link, statusLink);
            details.append(summary, body);
            row.append(details);
            return { row, details, summary, icon, name, status, time, fullName, detail, updated, lastSuccess, lastOutcome, link, statusLink };
        };

        const renderRepositories = (snapshot) => {
            if (!repositoryList) {
                return;
            }
            repositoryList.setAttribute("aria-busy", "false");
            if (!snapshot || !Array.isArray(snapshot.items)) {
                repositoryList.dataset.stale = "true";
                if (repositoryMessage) {
                    repositoryMessage.hidden = false;
                    repositoryMessage.textContent = repositoryRows.size
                        ? "Status unavailable · showing the last check."
                        : "Repository status is temporarily unavailable.";
                }
                return;
            }

            const items = snapshot.items;
            repositoryList.dataset.stale = "false";
            repositoriesActive = Number(snapshot.activeCount) > 0;
            if (repositoryCount) {
                repositoryCount.textContent = `${items.length} ${items.length === 1 ? "repository" : "repositories"}`;
                repositoryCount.title = `${Math.max(0, Number(snapshot.activeCount) || 0)} active · ${Math.max(0, Number(snapshot.failedCount) || 0)} need attention`;
            }
            if (repositoryMessage) {
                repositoryMessage.hidden = items.length > 0;
                repositoryMessage.textContent = "No repositories added.";
            }
            repositoryList.hidden = items.length === 0;
            const currentIds = new Set();
            items.forEach((item, index) => {
                const id = String(item.id);
                currentIds.add(id);
                let elements = repositoryRows.get(id);
                if (!elements) {
                    elements = createRepositoryRow();
                    repositoryRows.set(id, elements);
                }
                const name = String(item.name || "Unnamed repository");
                const status = String(item.statusLabel || "Status unavailable");
                const compactStatuses = {
                    pending: "Not synced", queued: "Queued", cloning: "Downloading",
                    refreshing: "Refreshing", cataloging: "Cataloguing", indexing: "Indexing",
                    failed: "Needs attention", ready: "Ready", disabled: "Disabled", cancelled: "Cancelled",
                };
                const tone = ["success", "progress", "error", "neutral"].includes(item.statusTone)
                    ? item.statusTone
                    : item.status === "ready" ? "success" : item.status === "failed" ? "error"
                        : ["queued", "cloning", "refreshing", "cataloging", "indexing"].includes(item.status) ? "progress" : "neutral";
                elements.row.dataset.tone = tone;
                elements.icon.textContent = tone === "success" ? "✓" : tone === "error" ? "!" : tone === "progress" ? "" : "·";
                elements.name.textContent = name;
                elements.status.textContent = compactStatuses[item.status] || status;
                elements.time.textContent = compactRepositoryDate(item.updatedAt);
                elements.time.dateTime = item.updatedAt || "";
                elements.time.title = formatNotificationDate(item.updatedAt, "Time unavailable");
                elements.fullName.textContent = name;
                elements.detail.textContent = `${status}. ${item.detail || "No additional status details."}`;
                elements.updated.textContent = `Last update: ${formatNotificationDate(item.updatedAt, "Not yet")}`;
                elements.lastSuccess.textContent = `Last successful sync: ${formatNotificationDate(item.lastSuccessAt, "Not yet")}`;
                const outcomes = { succeeded: "Succeeded", success: "Succeeded", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted" };
                const outcome = outcomes[item.lastOutcome];
                elements.lastOutcome.hidden = !outcome;
                elements.lastOutcome.textContent = outcome
                    ? `Last completed sync: ${outcome} · ${formatNotificationDate(item.lastOutcomeAt, "Time unavailable")}`
                    : "";
                elements.summary.title = `${name} · ${status}\n${item.detail || ""}\n${formatNotificationDate(item.updatedAt, "Time unavailable")}`;
                elements.summary.setAttribute("aria-label", `${name}: ${status}. Updated ${formatNotificationDate(item.updatedAt, "time unavailable")}. Details`);
                const targetPath = localTargetPath(item.targetPath);
                elements.link.hidden = !targetPath;
                if (targetPath) {
                    elements.link.href = targetPath;
                } else {
                    elements.link.removeAttribute("href");
                }
                const statusPath = localTargetPath(item.statusTargetPath);
                elements.statusLink.hidden = !statusPath;
                if (statusPath) {
                    elements.statusLink.href = statusPath;
                } else {
                    elements.statusLink.removeAttribute("href");
                }

                // Keep existing nodes, disclosure state, keyboard focus and scroll position across polling.
                if (repositoryList.children[index] !== elements.row) {
                    repositoryList.insertBefore(elements.row, repositoryList.children[index] || null);
                }
            });
            repositoryRows.forEach((elements, id) => {
                if (!currentIds.has(id)) {
                    elements.row.remove();
                    repositoryRows.delete(id);
                }
            });
        };

        const renderRefresh = (refresh = {}, schedule = {}) => {
            isActive = Boolean(refresh.active);
            const processed = Math.max(0, Number.parseInt(refresh.processed, 10) || 0);
            const total = Math.max(0, Number.parseInt(refresh.total, 10) || 0);
            const suppliedProgress = Number.parseFloat(refresh.progress);
            const calculatedProgress = total > 0 ? (processed / total) * 100 : 0;
            const progressValue = Math.min(100, Math.max(0, Number.isFinite(suppliedProgress) ? suppliedProgress : calculatedProgress));

            if (progressCard) {
                progressCard.hidden = !isActive;
            }
            if (progress) {
                progress.style.width = `${progressValue}%`;
            }
            if (progressLabel) {
                progressLabel.textContent = "Refreshing Confluence pages";
            }
            if (progressDetail) {
                progressDetail.textContent = total > 0
                    ? `${processed} of ${total} pages checked · ${Math.round(progressValue)}%`
                    : "Preparing the background refresh…";
            }

            const scheduleEnabled = schedule.enabled !== false;
            if (scheduleCard) {
                scheduleCard.hidden = false;
            }
            if (nextRun) {
                nextRun.textContent = scheduleEnabled
                    ? formatNotificationDate(schedule.next_run_at, "Being scheduled")
                    : "Automatic refresh is off";
            }
            if (lastAttempt) {
                lastAttempt.textContent = formatNotificationDate(schedule.last_attempt_at);
            }
            if (lastSuccess) {
                lastSuccess.textContent = formatNotificationDate(schedule.last_success_at);
            }

            const retrying = Boolean(schedule.retrying);
            const failures = Math.max(0, Number.parseInt(schedule.consecutive_failures, 10) || 0);
            if (retryRow) {
                retryRow.hidden = !retrying;
            }
            if (retry) {
                retry.textContent = `${failures || 1} failed attempt${failures === 1 ? "" : "s"}; next try ${formatNotificationDate(schedule.next_run_at, "soon")}`;
            }
            if (backgroundState) {
                backgroundState.textContent = isActive ? "Refreshing" : retrying ? "Retry scheduled" : scheduleEnabled ? "Weekly · on" : "Off";
            }
        };

        const renderBackgroundIndicator = (snapshot, refresh = {}, schedule = {}) => {
            if (!statusCenter || !backgroundPanel) {
                return;
            }
            const known = snapshot && Array.isArray(snapshot.items);
            const failed = known ? Math.max(0, Number(snapshot.failedCount) || 0) : 0;
            const active = known ? Math.max(0, Number(snapshot.activeCount) || 0) : 0;
            const confluenceFailed = Boolean(schedule.retrying)
                || ["failed", "interrupted", "succeeded_with_errors"].includes(refresh.status);
            const parts = [];
            if (failed) {
                parts.push(`${failed} ${failed === 1 ? "repository needs" : "repositories need"} attention`);
            }
            if (active) {
                parts.push(`${active} ${active === 1 ? "repository" : "repositories"} active`);
            }
            if (refresh.active) {
                parts.push("Confluence refresh in progress");
            } else if (confluenceFailed) {
                parts.push("Confluence needs attention");
            }
            if (!known) {
                parts.push("Repository status unavailable");
            }
            const allReady = known && snapshot.items.length > 0
                && snapshot.items.every((item) => item.status === "ready");
            const state = failed || confluenceFailed ? "error"
                : active || refresh.active ? "active"
                    : !known ? "unknown" : allReady ? "ready" : "neutral";
            const summary = parts.join("; ") || (allReady ? "All repositories up to date" : "No background work running");
            statusCenter.dataset.state = state;
            statusToggle.title = `Background status: ${summary}`;
            if (statusIndicator) {
                statusIndicator.hidden = false;
            }
            backgroundPanel.setSummary(summary);
            if (statusLive && statusLive.textContent !== summary) {
                statusLive.textContent = summary;
            }
        };

        const render = (payload) => {
            const notifications = Array.isArray(payload.notifications) ? payload.notifications : [];
            const nextSignature = JSON.stringify(notifications);
            if (nextSignature !== notificationSignature) {
                list.replaceChildren(...notifications.map(renderNotification));
                notificationSignature = nextSignature;
            }
            list.setAttribute("aria-busy", "false");
            if (empty) {
                empty.hidden = notifications.length > 0;
            }

            renderRefresh(payload.refresh || {}, payload.schedule || {});
            renderRepositories(payload.repositoryStatuses);
            isActive = isActive || repositoriesActive;
            renderBackgroundIndicator(payload.repositoryStatuses, payload.refresh || {}, payload.schedule || {});
            refreshBadge(payload.unread_count ?? payload.unreadCount ?? 0);
            window.dispatchEvent(new CustomEvent("owl:refresh-status", {
                detail: payload.refresh || {},
            }));
            hasLoaded = true;
        };

        const schedulePoll = () => {
            window.clearTimeout(pollTimer);
            pollTimer = window.setTimeout(load, isActive ? 2000 : 30000);
        };

        const load = async () => {
            if (requestInFlight || !center.dataset.notificationsUrl) {
                schedulePoll();
                return;
            }
            requestInFlight = true;
            try {
                const response = await window.fetch(center.dataset.notificationsUrl, {
                    method: "GET",
                    credentials: "same-origin",
                    headers: { Accept: "application/json" },
                    cache: "no-store",
                });
                if (!response.ok) {
                    throw new Error("Notifications are temporarily unavailable.");
                }
                render(await response.json());
            } catch (error) {
                list.setAttribute("aria-busy", "false");
                renderBackgroundIndicator(null);
                if (repositoryList) {
                    repositoryList.setAttribute("aria-busy", "false");
                    repositoryList.dataset.stale = "true";
                }
                if (repositoryMessage) {
                    repositoryMessage.hidden = false;
                    repositoryMessage.textContent = repositoryRows.size
                        ? "Could not update · showing the last check."
                        : "Repository status is temporarily unavailable.";
                }
                if (!hasLoaded) {
                    list.replaceChildren(createNotificationElement("p", "notification-center__error", error.message));
                    if (empty) {
                        empty.hidden = true;
                    }
                }
            } finally {
                requestInFlight = false;
                schedulePoll();
            }
        };

        readAll?.addEventListener("click", async () => {
            readAll.disabled = true;
            try {
                await post(center.dataset.readAllUrl);
                center.querySelectorAll(".notification-card.is-unread").forEach((card) => card.classList.remove("is-unread"));
                center.querySelectorAll(".notification-card__read").forEach((button) => button.remove());
                refreshBadge(0);
                announce("All notifications marked as read.");
            } catch (error) {
                readAll.disabled = false;
                announce(error.message);
            }
        });

        const tickSchedule = async () => {
            try {
                const response = await post(center.dataset.scheduleTickUrl);
                const payload = await response.json();
                if (payload.refresh) {
                    window.dispatchEvent(new CustomEvent("owl:refresh-status", {
                        detail: payload.refresh,
                    }));
                }
                if (!requestInFlight) {
                    await load();
                }
            } catch {
                // The persistent scheduler may still be running; the next minute retries this lightweight tick.
            }
        };

        const tickBitbucketSchedule = () => {
            if (!bitbucketScheduleTickForm) {
                return;
            }
            if (typeof bitbucketScheduleTickForm.requestSubmit === "function") {
                bitbucketScheduleTickForm.requestSubmit();
            } else {
                bitbucketScheduleTickForm.submit();
            }
        };

        load();
        tickSchedule();
        tickBitbucketSchedule();
        window.setInterval(() => {
            tickSchedule();
            tickBitbucketSchedule();
        }, 60000);
    });
})();
