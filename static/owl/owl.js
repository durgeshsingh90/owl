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
        return path.startsWith("/") && !path.startsWith("//") ? path : "";
    };

    document.querySelectorAll("[data-notification-center]").forEach((center) => {
        const toggle = center.querySelector("[data-notification-toggle]");
        const panel = center.querySelector("[data-notification-panel]");
        const badge = center.querySelector("[data-notification-badge]");
        const activity = center.querySelector("[data-notification-activity]");
        const list = center.querySelector("[data-notification-list]");
        const empty = center.querySelector("[data-notification-empty]");
        const live = center.querySelector("[data-notification-live]");
        const readAll = center.querySelector("[data-notification-read-all]");
        const unreadLabel = center.querySelector("[data-notification-unread-label]");
        const backgroundState = center.querySelector("[data-notification-background-state]");
        const progressCard = center.querySelector("[data-notification-progress-card]");
        const progressLabel = center.querySelector("[data-notification-progress-label]");
        const progressDetail = center.querySelector("[data-notification-progress-detail]");
        const progress = center.querySelector("[data-notification-progress]");
        const scheduleCard = center.querySelector("[data-notification-schedule]");
        const nextRun = center.querySelector("[data-notification-next-run]");
        const lastAttempt = center.querySelector("[data-notification-last-attempt]");
        const lastSuccess = center.querySelector("[data-notification-last-success]");
        const retryRow = center.querySelector("[data-notification-retry-row]");
        const retry = center.querySelector("[data-notification-retry]");
        const bitbucketScheduleTickForm = center.querySelector(
            "[data-bitbucket-schedule-tick-form]",
        );
        const csrfToken = center.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";

        if (!toggle || !panel || !list) {
            return;
        }

        let isOpen = false;
        let isActive = false;
        let hasLoaded = false;
        let unreadCount = 0;
        let pollTimer = null;
        let requestInFlight = false;

        const announce = (message) => {
            if (!live) {
                return;
            }
            live.textContent = "";
            window.setTimeout(() => {
                live.textContent = message;
            }, 20);
        };

        const closePanel = ({ restoreFocus = true } = {}) => {
            if (!isOpen) {
                return;
            }
            isOpen = false;
            panel.hidden = true;
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", "Open notifications");
            if (restoreFocus) {
                toggle.focus();
            }
        };

        const positionPanel = () => {
            if (!window.matchMedia("(max-width: 620px)").matches) {
                panel.style.removeProperty("--notification-mobile-top");
                return;
            }
            const toggleBounds = toggle.getBoundingClientRect();
            const availableHeight = window.innerHeight || document.documentElement.clientHeight;
            const top = Math.max(8, Math.min(toggleBounds.bottom + 8, availableHeight - 220));
            panel.style.setProperty("--notification-mobile-top", `${Math.round(top)}px`);
        };

        const openPanel = () => {
            isOpen = true;
            panel.hidden = false;
            positionPanel();
            toggle.setAttribute("aria-expanded", "true");
            toggle.setAttribute("aria-label", "Close notifications");
            panel.focus();
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

        const refreshBadge = (nextUnreadCount, active) => {
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
            if (activity) {
                activity.hidden = !active;
            }
            center.classList.toggle("has-background-activity", active);

            const parts = ["Open notifications"];
            if (nextCount) {
                parts.push(`${nextCount} unread`);
            }
            if (active) {
                parts.push("background refresh in progress");
            }
            if (!isOpen) {
                toggle.setAttribute("aria-label", parts.join(", "));
            }

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
                        refreshBadge(unreadCount - 1, isActive);
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
                backgroundState.textContent = isActive ? "In progress" : retrying ? "Retry scheduled" : scheduleEnabled ? "Weekly refresh on" : "Automatic refresh off";
            }
        };

        const render = (payload) => {
            const notifications = Array.isArray(payload.notifications) ? payload.notifications : [];
            list.replaceChildren(...notifications.map(renderNotification));
            list.setAttribute("aria-busy", "false");
            if (empty) {
                empty.hidden = notifications.length > 0;
            }

            renderRefresh(payload.refresh || {}, payload.schedule || {});
            refreshBadge(payload.unread_count ?? payload.unreadCount ?? 0, isActive);
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

        toggle.addEventListener("click", () => {
            if (isOpen) {
                closePanel();
            } else {
                openPanel();
            }
        });

        document.addEventListener("click", (event) => {
            if (isOpen && !center.contains(event.target)) {
                closePanel();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (isOpen && event.key === "Escape") {
                event.preventDefault();
                closePanel();
            }
        });

        window.addEventListener("resize", () => {
            if (isOpen) {
                positionPanel();
            }
        });

        readAll?.addEventListener("click", async () => {
            readAll.disabled = true;
            try {
                await post(center.dataset.readAllUrl);
                center.querySelectorAll(".notification-card.is-unread").forEach((card) => card.classList.remove("is-unread"));
                center.querySelectorAll(".notification-card__read").forEach((button) => button.remove());
                refreshBadge(0, isActive);
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
