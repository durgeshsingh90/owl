(function pipelineDashboardModule(global) {
    "use strict";

    const ACTIVE_INDICATOR_STATES = new Set([
        "submitting", "queued", "running", "retry_wait", "recovering", "paused",
    ]);
    const ACTIVITY_CONTROL_STATES = new Set([
        "hidden", "idle_actionable", "idle_unavailable", "submitting", "queued",
        "running", "retry_wait", "recovering", "paused", "terminal", "unknown",
    ]);
    const ACTIVE_REPOSITORY_PHASES = new Set([
        "queued", "checking_connection", "cloning", "pulling", "discovering", "cataloguing",
        "validating", "hashing", "extracting", "writing", "publishing",
        "extracting_and_writing", "reusing_cached", "backpressured", "source_blocked",
        "completing",
    ]);
    const SVG_NS = "http://www.w3.org/2000/svg";
    const MAX_CHART_SAMPLES = 90;
    const MAX_TABLE_SAMPLES = 60;
    const DEFAULT_ACTIVE_INTERVAL = 5000;
    const DEFAULT_IDLE_INTERVAL = 30000;
    const EVENTS = Object.freeze({
        metrics: "owl:pipeline-metrics",
        rendered: "owl:pipeline-rendered",
    });
    const SELECTORS = Object.freeze({
        dashboard: "[data-pipeline-dashboard]",
        consumer: "[data-pipeline-consumer]",
        totalEta: "[data-pipeline-total-eta]",
        activity: "[data-pipeline-activity]",
        extractedRate: "[data-pipeline-extracted-rate]",
        writtenRate: "[data-pipeline-written-rate]",
        overallState: "[data-pipeline-overall-state]",
        recoveryState: "[data-pipeline-recovery-state]",
        freshness: "[data-pipeline-freshness]",
    });
    const mounted = new WeakMap();

    const finite = (value) => typeof value === "number" && Number.isFinite(value);
    const number = (value, fallback = 0) => finite(value) ? value : fallback;
    const integer = (value, fallback = 0) => Math.max(0, Math.trunc(number(value, fallback)));
    const list = (value) => Array.isArray(value) ? value : [];
    const object = (value) => value && typeof value === "object" && !Array.isArray(value)
        ? value : {};

    function humanize(value) {
        if (typeof value !== "string" || !value) return "Unavailable";
        return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
    }

    function formatNumber(value, maximumFractionDigits = 1) {
        if (!finite(value)) return "—";
        return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value);
    }

    function formatBytes(value) {
        if (!finite(value) || value < 0) return "Unavailable";
        if (value === 0) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const exponent = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
        return `${formatNumber(value / (1024 ** exponent), exponent === 0 ? 0 : 1)} ${units[exponent]}`;
    }

    function formatAge(value) {
        if (!finite(value) || value < 0) return "Unavailable";
        const seconds = Math.ceil(value);
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        const remainder = seconds % 60;
        if (minutes < 60) return `${minutes}m ${remainder}s`;
        const hours = Math.floor(minutes / 60);
        return `${hours}h ${minutes % 60}m`;
    }

    function formatRate(rate) {
        const measurement = object(rate);
        if (measurement.state === "warming") return "Warming";
        if (measurement.state !== "available" || !finite(measurement.perMinute)) return "—";
        return `${formatNumber(measurement.perMinute, 1)}/min`;
    }

    function formatDateTime(value) {
        const timestamp = Date.parse(value);
        if (!Number.isFinite(timestamp)) return "Unavailable";
        return new Intl.DateTimeFormat(undefined, {
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        }).format(new Date(timestamp));
    }

    function formatRange(eta) {
        const value = object(eta);
        if (!finite(value.lowerSeconds) || !finite(value.upperSeconds)) return "Unavailable";
        return `${formatAge(value.lowerSeconds)} to ${formatAge(value.upperSeconds)}`;
    }

    function nonnegativeInteger(value) {
        return Number.isSafeInteger(value) && value >= 0 ? value : null;
    }

    function prefersReducedMotion() {
        try {
            return Boolean(global.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
        } catch {
            return false;
        }
    }

    function setAttribute(node, name, value) {
        if (!node) return;
        if (value === null || value === undefined || value === false) {
            node.removeAttribute?.(name);
        } else {
            node.setAttribute?.(name, String(value));
        }
    }

    function setNodeHidden(root, selector, hidden) {
        const node = root?.querySelector?.(selector);
        if (node) node.hidden = Boolean(hidden);
        return node;
    }

    function runningEvidenceIsFresh(payload, indicator) {
        if (payload?.snapshotStale || indicator.hasFreshRunningWork !== true) return false;
        const generatedAt = Date.parse(payload?.generatedAt);
        const evidenceAt = Date.parse(indicator.evidenceAt);
        const freshForSeconds = finite(indicator.freshForSeconds)
            ? Math.max(0, indicator.freshForSeconds) : 0;
        return Number.isFinite(generatedAt)
            && Number.isFinite(evidenceAt)
            && evidenceAt <= generatedAt + 10000
            && generatedAt - evidenceAt <= freshForSeconds * 1000;
    }

    function activityControlPresentation(payload, control = null) {
        const indicator = object(payload?.topBarActivityIndicator);
        const recovery = object(payload?.recovery);
        const activity = object(payload?.activity);
        const run = object(payload?.run);
        const repositories = object(run.repositories);
        let state = ACTIVITY_CONTROL_STATES.has(indicator.state) ? indicator.state : "unknown";
        if (payload?.snapshotStale) state = "unknown";
        const confirmedRunning = state === "running" && runningEvidenceIsFresh(payload, indicator);
        const confirmedRecovering = state === "recovering"
            && !payload?.snapshotStale
            && ["recovering", "recovering_half_open"].includes(recovery.state)
            && typeof recovery.activeAttemptId === "string"
            && recovery.activeAttemptId.length > 0;
        if (state === "running" && !confirmedRunning) state = "unknown";
        if (state === "recovering" && !confirmedRecovering) state = "unknown";
        const repositoryCount = integer(Number(control?.dataset?.repositoryCount));
        const enabledCount = integer(Number(control?.dataset?.enabledRepositoryCount));
        const actionable = enabledCount > 0 && Boolean(control?.getAttribute?.("action"));
        const queued = integer(repositories.queued);
        const active = integer(repositories.active);
        const labels = {
            hidden: ["No repositories", "Add a repository before starting a workspace refresh."],
            idle_actionable: ["Refresh all repositories", `Queue ${enabledCount} included ${enabledCount === 1 ? "repository" : "repositories"} in the background.`],
            idle_unavailable: ["Refresh all unavailable", repositoryCount
                ? "No repository is currently eligible for Refresh all."
                : "Add a repository before starting a workspace refresh."],
            submitting: ["Adding to queue", "Submitting one durable workspace refresh request."],
            queued: [activity.code === "queued" ? "Added to queue" : activity.label || "Waiting",
                activity.code === "backpressured"
                    ? "Extraction is waiting for durable publication backlog to drain."
                    : activity.code === "source_blocked"
                        ? "Queued work is waiting for an eligible repository source."
                        : activity.code === "completing"
                            ? "Final durable run state is being reconciled."
                            : queued
                                ? `${queued} ${queued === 1 ? "repository is" : "repositories are"} waiting in queue.`
                                : "The accepted repositories are waiting in queue."],
            running: [activity.label || "Repository work running", active || queued
                ? `${active} active · ${queued} queued.`
                : "Confirmed background work is making progress."],
            retry_wait: ["Waiting to retry", recovery.nextRetryAt
                ? `Next controlled retry at ${formatDateTime(recovery.nextRetryAt)}.`
                : "A controlled retry is pending."],
            recovering: ["Recovering PDF pipeline", "A supervised component recovery probe is active."],
            paused: ["PDF pipeline paused", recovery.reasonCode
                ? `${humanize(recovery.scope || "pipeline")} · ${humanize(recovery.reasonCode)}.`
                : "Review pipeline details before resuming."],
            terminal: [activity.label || humanize(run.state), run.id
                ? "The latest accepted pipeline run is terminal. Refresh all remains available."
                : "No current pipeline run is active."],
            unknown: ["Pipeline status unavailable", payload?.snapshotStale
                ? "The last supervisor snapshot is stale; running work cannot be confirmed."
                : "Waiting for a fresh authoritative pipeline snapshot."],
        };
        const [label, detail] = labels[state];
        return {
            state,
            label,
            detail,
            hidden: state === "hidden",
            actionable: (state === "idle_actionable" || state === "terminal") && actionable,
            busy: state === "submitting" || confirmedRunning || confirmedRecovering,
            showAnimation: confirmedRunning || confirmedRecovering,
            visual: state === "submitting" ? "submitting"
                : state === "queued" || state === "retry_wait" ? "waiting"
                    : confirmedRunning || confirmedRecovering ? "running"
                        : state === "terminal" ? "complete"
                            : ["idle_unavailable", "paused", "unknown"].includes(state)
                                ? "attention" : "reload",
        };
    }

    function renderActivityControl(control, payload) {
        if (!control) return null;
        const presentation = activityControlPresentation(payload, control);
        control.dataset.pipelineIndicatorState = presentation.state;
        control.hidden = presentation.hidden;
        ACTIVITY_CONTROL_STATES.forEach((state) => {
            control.classList?.toggle(`bb-refresh-all--${state}`, state === presentation.state);
        });
        control.classList?.toggle("bb-refresh-all--active", presentation.showAnimation);
        setAttribute(control, "aria-busy", presentation.busy ? "true" : null);
        const button = control.querySelector?.("[data-refresh-all-button]");
        if (button) {
            button.disabled = !presentation.actionable;
            const description = `${presentation.label}. ${presentation.detail}`;
            setAttribute(button, "aria-label", description);
            button.title = description;
        }
        setText(control, "[data-refresh-all-label]", presentation.label);
        setText(control, "[data-refresh-all-detail]", presentation.detail);
        setNodeHidden(control, "[data-refresh-all-icon]", presentation.visual !== "reload");
        setNodeHidden(control, "[data-refresh-all-waiting]", presentation.visual !== "waiting");
        setNodeHidden(control, "[data-refresh-all-spinner]", presentation.visual !== "submitting");
        setNodeHidden(control, "[data-refresh-all-attention]", presentation.visual !== "attention");
        setNodeHidden(control, "[data-refresh-all-complete]", presentation.visual !== "complete");
        const reducedMotion = prefersReducedMotion();
        const showAnimated = presentation.visual === "running" && !reducedMotion;
        const runningVisual = setNodeHidden(
            control, "[data-refresh-all-running-visual]", !showAnimated,
        );
        const runningStatic = setNodeHidden(
            control, "[data-refresh-all-running-static]",
            presentation.visual !== "running" || !reducedMotion,
        );
        if (runningVisual) {
            const activeSource = runningVisual.dataset.activeSrc;
            if (showAnimated && activeSource) {
                if (runningVisual.getAttribute?.("src") !== activeSource) {
                    runningVisual.setAttribute?.("src", activeSource);
                }
            } else {
                runningVisual.removeAttribute?.("src");
            }
        }
        if (runningStatic) runningStatic.hidden = presentation.visual !== "running" || !reducedMotion;
        return presentation;
    }

    function renderActivityControls(root, payload) {
        root?.querySelectorAll?.("[data-pipeline-activity-control]").forEach((control) => {
            renderActivityControl(control, payload);
        });
    }

    function exactRepositoryCompletion(repository) {
        const total = nonnegativeInteger(repository.totalPdfs);
        const successful = nonnegativeInteger(repository.successfulPdfs);
        const failed = nonnegativeInteger(repository.permanentFailedPdfs);
        const cancelled = nonnegativeInteger(repository.cancelledPdfs);
        const remaining = nonnegativeInteger(repository.remainingPdfs);
        const staged = nonnegativeInteger(repository.stagedPdfs);
        const publishing = nonnegativeInteger(repository.publishingPdfs);
        const unresolved = nonnegativeInteger(repository.unresolvedFailures);
        return repository.lifecycleState === "complete"
            && repository.inventoryFinal === true
            && total !== null
            && successful === total
            && failed === 0
            && cancelled === 0
            && remaining === 0
            && staged === 0
            && publishing === 0
            && unresolved === 0;
    }

    function repositoryPresentation(repository, { stale = false } = {}) {
        const lifecycle = typeof repository?.lifecycleState === "string"
            ? repository.lifecycleState : "unknown";
        const phase = typeof repository?.phase === "string" ? repository.phase : "unknown";
        const inventoryKnown = repository?.inventoryFinal === true
            && nonnegativeInteger(repository.totalPdfs) !== null
            && nonnegativeInteger(repository.remainingPdfs) !== null;
        if (stale) {
            return {
                state: "unknown", icon: "unknown", label: "Pipeline status unavailable",
                detail: "The latest pipeline snapshot is stale.", showRemaining: false,
                showEta: false, remaining: "", eta: "",
            };
        }
        if (lifecycle === "queued") {
            return {
                state: "queued", icon: "queued", label: "Added to queue",
                detail: "Waiting in queue", showRemaining: false, showEta: false,
                remaining: "", eta: "",
            };
        }
        if (exactRepositoryCompletion(repository)) {
            return {
                state: "complete", icon: "complete", label: "PDF indexing complete",
                detail: "Every current-run PDF reached durable searchable publication.",
                showRemaining: false, showEta: false, remaining: "", eta: "",
            };
        }
        if (lifecycle === "completed_with_errors") {
            return {
                state: "completed_with_errors", icon: "attention", label: "Completed with errors",
                detail: `${integer(repository.unresolvedFailures)} unresolved PDF ${integer(repository.unresolvedFailures) === 1 ? "failure" : "failures"}.`,
                showRemaining: false, showEta: false, remaining: "", eta: "",
            };
        }
        if (lifecycle === "cancelled") {
            return {
                state: "cancelled", icon: "attention", label: "PDF indexing cancelled",
                detail: "The current accepted repository run was cancelled.",
                showRemaining: false, showEta: false, remaining: "", eta: "",
            };
        }
        const paused = lifecycle === "paused" || phase === "paused";
        const retrying = phase === "retry_wait";
        const recovering = phase === "recovering";
        const activePhase = ACTIVE_REPOSITORY_PHASES.has(phase);
        if (paused || retrying || recovering || (lifecycle === "active" && activePhase)) {
            const state = paused ? "paused" : retrying ? "retry_wait" : recovering ? "recovering" : "active";
            const phaseLabels = {
                queued: "Waiting for extraction",
                publishing: "Writing",
                cataloguing: "Discovering PDFs",
                source_blocked: "Waiting for repository input",
                extracting_and_writing: "Extracting + writing",
                reusing_cached: "Reusing cached text",
            };
            const label = paused ? "Paused" : retrying ? "Waiting to retry"
                : recovering ? "Recovering" : phaseLabels[phase] || humanize(phase);
            const eta = object(repository.eta);
            return {
                state,
                icon: paused ? "attention" : retrying ? "waiting" : "working",
                label,
                detail: label,
                showRemaining: inventoryKnown,
                showEta: true,
                remaining: inventoryKnown
                    ? `Remaining ${repository.remainingPdfs} of ${repository.totalPdfs} PDFs` : "",
                eta: eta.display || (paused ? "ETA paused"
                    : inventoryKnown ? "Calculating ETA" : "Waiting for inventory"),
            };
        }
        return {
            state: "unknown", icon: "unknown", label: "Pipeline status unavailable",
            detail: "The current repository phase is unavailable.", showRemaining: false,
            showEta: false, remaining: "", eta: "",
        };
    }

    function renderRepositoryCards(root, payload) {
        const currentRun = object(payload?.run);
        const fallbackLabel = payload?.snapshotStale
            ? "Pipeline status unavailable"
            : currentRun.id ? "Not accepted into the current run" : "No current PDF pipeline run";
        const fallbackDetail = payload?.snapshotStale
            ? "The latest authoritative pipeline snapshot is stale."
            : currentRun.id
                ? "This repository is not a member of the current accepted run."
                : "No current accepted PDF pipeline run includes this repository.";
        root?.querySelectorAll?.("[data-repository-id]").forEach((card) => {
            const gitFailed = card.dataset.repositoryGitSyncFailed === "true";
            const historicalFailures = integer(Number(card.dataset.repositoryPdfIndexFailedCount));
            const fallbackState = gitFailed
                ? "git_failed" : historicalFailures ? "historical_indexing_failure" : "unknown";
            const cardLabel = gitFailed
                ? "Git connection or pull failed"
                : historicalFailures
                    ? `${historicalFailures} historical PDF indexing ${historicalFailures === 1 ? "failure" : "failures"}`
                    : fallbackLabel;
            const cardDetail = gitFailed
                ? "Repository access did not complete; see Repository logs."
                : historicalFailures
                    ? "Historical PDF failures remain available in Repository logs."
                    : fallbackDetail;
            card.dataset.pipelineRepositoryState = fallbackState;
            card.dataset.pipelineRun = "";
            const name = card.querySelector?.("[data-repository-name]")?.textContent?.trim()
                || "Repository";
            const stateIcon = card.querySelector?.("[data-repository-state-icon]");
            if (stateIcon) {
                stateIcon.className = `bb-repository-state bb-repository-state--${gitFailed
                    ? "git-failed" : historicalFailures ? "indexing-failed" : "pipeline-unknown"}`;
                stateIcon.dataset.pipelineRepositoryState = fallbackState;
                setAttribute(stateIcon, "aria-label", `${name}: ${cardLabel}`);
                stateIcon.title = cardDetail;
            }
            const queueLabel = card.querySelector?.("[data-repository-queue-label]");
            if (queueLabel) queueLabel.hidden = true;
            const workLabel = card.querySelector?.("[data-repository-work-label]");
            if (workLabel) {
                workLabel.textContent = cardLabel;
                workLabel.title = cardDetail;
                workLabel.hidden = false;
            }
            setNodeHidden(card, "[data-repository-remaining]", true);
            setNodeHidden(card, "[data-repository-eta]", true);
            setNodeHidden(card, "[data-repository-progress]", true);
            setNodeHidden(card, "[data-repository-queued-icon]", true);
            setNodeHidden(card, "[data-repository-queue-icon]", true);
            setNodeHidden(card, "[data-repository-working-icon]", true);
            setNodeHidden(card, "[data-repository-complete-icon]", true);
            setNodeHidden(card, "[data-repository-attention-icon]", true);
            setNodeHidden(card, "[data-repository-unknown-icon]", gitFailed || historicalFailures);
            const activeVisual = setNodeHidden(card, "[data-repository-active-visual]", true);
            if (activeVisual) activeVisual.removeAttribute?.("src");
            setNodeHidden(card, "[data-repository-active-static]", true);
        });
        const progress = list(object(payload?.run).repositoryProgress);
        progress.forEach((repository) => {
            const repositoryId = String(repository.repositoryId ?? "");
            if (!/^[1-9][0-9]{0,18}$/.test(repositoryId)) return;
            const presentation = repositoryPresentation(repository, {
                stale: Boolean(payload?.snapshotStale),
            });
            root?.querySelectorAll?.(`[data-repository-id="${repositoryId}"]`).forEach((card) => {
                card.dataset.pipelineRepositoryState = presentation.state;
                card.dataset.pipelineRun = String(repository.runId || object(payload.run).id || "");
                const name = card.querySelector?.("[data-repository-name]")?.textContent?.trim()
                    || `Repository ${repositoryId}`;
                const stateIcon = card.querySelector?.("[data-repository-state-icon]");
                if (stateIcon) {
                    stateIcon.className = `bb-repository-state bb-repository-state--pipeline-${presentation.icon}`;
                    stateIcon.dataset.pipelineRepositoryState = presentation.state;
                    setAttribute(stateIcon, "aria-label", `${name}: ${presentation.label}`);
                    stateIcon.title = presentation.detail;
                }
                const workLabel = card.querySelector?.("[data-repository-work-label]");
                const queueLabel = card.querySelector?.("[data-repository-queue-label]");
                if (queueLabel) {
                    queueLabel.textContent = "Added to queue";
                    queueLabel.hidden = presentation.state !== "queued";
                }
                if (workLabel) {
                    workLabel.textContent = presentation.state === "queued" && queueLabel
                        ? presentation.detail : presentation.label;
                    workLabel.title = presentation.detail;
                    workLabel.hidden = false;
                }
                const remaining = card.querySelector?.("[data-repository-remaining]");
                if (remaining) {
                    remaining.textContent = presentation.remaining;
                    remaining.hidden = !presentation.showRemaining;
                }
                const eta = card.querySelector?.("[data-repository-eta]");
                if (eta) {
                    eta.textContent = presentation.eta;
                    eta.hidden = !presentation.showEta;
                }
                setNodeHidden(card, "[data-repository-progress]", true);
                setNodeHidden(card, "[data-repository-queued-icon]", presentation.icon !== "queued");
                setNodeHidden(card, "[data-repository-queue-icon]", presentation.icon !== "queued");
                setNodeHidden(card, "[data-repository-working-icon]", presentation.icon !== "working");
                setNodeHidden(card, "[data-repository-complete-icon]", presentation.icon !== "complete");
                setNodeHidden(card, "[data-repository-attention-icon]", presentation.icon !== "attention");
                setNodeHidden(card, "[data-repository-unknown-icon]", presentation.icon !== "unknown");
                const reducedMotion = prefersReducedMotion();
                const showActiveAnimation = presentation.icon === "working" && !reducedMotion;
                const activeVisual = setNodeHidden(
                    card, "[data-repository-active-visual]", !showActiveAnimation,
                );
                setNodeHidden(
                    card, "[data-repository-active-static]",
                    presentation.icon !== "working" || !reducedMotion,
                );
                if (activeVisual) {
                    const activeSource = activeVisual.dataset.activeSrc;
                    if (showActiveAnimation && activeSource) {
                        if (activeVisual.getAttribute?.("src") !== activeSource) {
                            activeVisual.setAttribute?.("src", activeSource);
                        }
                    } else {
                        activeVisual.removeAttribute?.("src");
                    }
                }
            });
        });
    }

    function setText(root, selector, value) {
        root.querySelectorAll(selector).forEach((node) => {
            const next = String(value ?? "—");
            if (node.textContent !== next) node.textContent = next;
        });
    }

    function replaceChildren(node) {
        if (!node) return;
        if (typeof node.replaceChildren === "function") {
            node.replaceChildren();
            return;
        }
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function appendCell(document, row, value, heading = false) {
        const cell = document.createElement(heading ? "th" : "td");
        if (heading) cell.setAttribute("scope", "row");
        cell.textContent = String(value ?? "—");
        row.appendChild(cell);
        return cell;
    }

    function emptyTableRow(body, columns, message) {
        if (!body) return;
        replaceChildren(body);
        const row = body.ownerDocument.createElement("tr");
        const cell = body.ownerDocument.createElement("td");
        cell.colSpan = columns;
        cell.textContent = message;
        row.appendChild(cell);
        body.appendChild(row);
    }

    function downsample(samples, limit = MAX_CHART_SAMPLES) {
        if (samples.length <= limit) return samples.slice();
        const selected = [];
        const seen = new Set();
        const step = (samples.length - 1) / (limit - 1);
        for (let index = 0; index < limit; index += 1) {
            const sampleIndex = Math.round(index * step);
            if (!seen.has(sampleIndex)) {
                selected.push(samples[sampleIndex]);
                seen.add(sampleIndex);
            }
        }
        return selected;
    }

    function svgNode(document, name, attributes = {}) {
        const node = document.createElementNS(SVG_NS, name);
        node.dataset.pipelineDrawn = "true";
        Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
        return node;
    }

    function clearDrawing(svg) {
        if (!svg) return;
        svg.querySelectorAll("[data-pipeline-drawn]").forEach((node) => node.remove());
    }

    function chartDescription(svg, value) {
        const description = svg?.querySelector("desc");
        if (description) description.textContent = value;
    }

    function timelineRange(samples) {
        if (!samples.length) return "Waiting for history";
        const first = formatDateTime(samples[0].at);
        const last = formatDateTime(samples[samples.length - 1].at);
        return first === last ? `Sample at ${last}` : `${first}–${last}`;
    }

    function capacityValues(sample) {
        const workers = object(sample.workers);
        return {
            active: integer(workers.active),
            idle: integer(workers.idleNoDemand),
            waiting: integer(workers.waitingForEligibleInput),
            backpressured: integer(workers.backpressured),
            controller: integer(workers.pausedByController),
            recovery: integer(workers.pausedByRecovery),
            unavailable: integer(workers.unavailable),
        };
    }

    function renderCapacityChart(root, payload) {
        const svg = root.querySelector("[data-pipeline-capacity-chart]");
        const empty = root.querySelector("[data-pipeline-capacity-empty]");
        if (!svg) return;
        clearDrawing(svg);
        const samples = downsample(list(payload.samples));
        setText(root, "[data-pipeline-capacity-range]", timelineRange(samples));
        if (!samples.length) {
            if (empty) empty.hidden = false;
            chartDescription(svg, "No capacity samples are available yet.");
            return;
        }
        if (empty) empty.hidden = true;
        const document = svg.ownerDocument;
        const left = 34;
        const top = 16;
        const width = 590;
        const height = 178;
        const ribbonY = 214;
        const totals = samples.map((sample) => Object.values(capacityValues(sample))
            .reduce((total, value) => total + value, 0));
        const maximum = Math.max(1, ...totals);
        [0, 0.5, 1].forEach((ratio) => {
            const y = top + height - (height * ratio);
            svg.appendChild(svgNode(document, "line", {
                x1: left, x2: left + width, y1: y, y2: y, class: "bb-pipeline-chart__guide",
            }));
            const label = svgNode(document, "text", {
                x: left - 6, y: y + 3, "text-anchor": "end", class: "bb-pipeline-chart__label",
            });
            label.textContent = String(Math.round(maximum * ratio));
            svg.appendChild(label);
        });
        const barWidth = Math.max(1, width / samples.length);
        const series = ["active", "idle", "waiting", "backpressured", "controller", "recovery", "unavailable"];
        samples.forEach((sample, index) => {
            const values = capacityValues(sample);
            let bottom = top + height;
            series.forEach((name) => {
                const segmentHeight = values[name] * height / maximum;
                if (segmentHeight <= 0) return;
                bottom -= segmentHeight;
                const rect = svgNode(document, "rect", {
                    x: left + (index * barWidth), y: bottom,
                    width: Math.max(1, barWidth + 0.25), height: segmentHeight,
                    class: `bb-pipeline-chart__segment bb-pipeline-chart__segment--${name}`,
                });
                const title = document.createElementNS(SVG_NS, "title");
                title.textContent = `${formatDateTime(sample.at)}: ${values[name]} ${humanize(name)}`;
                rect.appendChild(title);
                svg.appendChild(rect);
            });
            const publisher = svgNode(document, "rect", {
                x: left + (index * barWidth), y: ribbonY,
                width: Math.max(1, barWidth + 0.25), height: 10,
                class: "bb-pipeline-chart__publisher",
                "data-state": sample.publisherState || "unavailable",
            });
            const publisherTitle = document.createElementNS(SVG_NS, "title");
            publisherTitle.textContent = `${formatDateTime(sample.at)}: publisher ${humanize(sample.publisherState)}`;
            publisher.appendChild(publisherTitle);
            svg.appendChild(publisher);
        });
        const ribbonLabel = svgNode(document, "text", {
            x: left, y: 209, class: "bb-pipeline-chart__label",
        });
        ribbonLabel.textContent = "Publisher state";
        svg.appendChild(ribbonLabel);
        chartDescription(
            svg,
            `${samples.length} retained samples from ${timelineRange(samples)}. `
            + `Latest capacity total ${totals[totals.length - 1]}; maximum shown ${maximum}.`,
        );
    }

    function lineSegments(values, xFor, yFor) {
        const segments = [];
        let current = [];
        values.forEach((value, index) => {
            if (!finite(value)) {
                if (current.length) segments.push(current);
                current = [];
                return;
            }
            current.push(`${xFor(index)},${yFor(value)}`);
        });
        if (current.length) segments.push(current);
        return segments;
    }

    function sampleRate(sample, field) {
        const value = sample[field];
        return finite(value) ? value * 60 : null;
    }

    function renderFlowChart(root, payload) {
        const svg = root.querySelector("[data-pipeline-flow-chart]");
        const empty = root.querySelector("[data-pipeline-flow-empty]");
        if (!svg) return;
        clearDrawing(svg);
        const samples = list(payload.samples).map((sample, index, source) => {
            if (index !== source.length - 1) return sample;
            const throughput = object(payload.throughput);
            const queues = object(payload.queues);
            return {
                ...sample,
                documentsCompletedPerSecond: finite(sample.documentsCompletedPerSecond)
                    ? sample.documentsCompletedPerSecond : throughput.documentsCompletedPerSecond,
                stagedBytes: finite(sample.stagedBytes) ? sample.stagedBytes : queues.stagedBytes,
            };
        });
        const chartSamples = downsample(samples);
        setText(root, "[data-pipeline-flow-range]", timelineRange(chartSamples));
        if (!chartSamples.length) {
            if (empty) empty.hidden = false;
            chartDescription(svg, "No extraction, publication, or backlog samples are available yet.");
            return;
        }
        if (empty) empty.hidden = true;
        const document = svg.ownerDocument;
        const left = 38;
        const width = 584;
        const top = 18;
        const rateHeight = 88;
        const backlogTop = 142;
        const backlogHeight = 76;
        const denominator = Math.max(1, chartSamples.length - 1);
        const xFor = (index) => left + (index * width / denominator);
        const extracted = chartSamples.map((sample) => sampleRate(sample, "extractorOutputsPerSecond"));
        const written = chartSamples.map((sample) => sampleRate(sample, "writerPublicationsPerSecond"));
        const completed = chartSamples.map((sample) => sampleRate(sample, "documentsCompletedPerSecond"));
        const backlog = chartSamples.map((sample) => finite(sample.backpressureDepthJobs)
            ? sample.backpressureDepthJobs : null);
        const threshold = chartSamples.map((sample) => finite(sample.backpressureThresholdJobs)
            ? sample.backpressureThresholdJobs : null);
        const rateMaximum = Math.max(1, ...extracted.filter(finite), ...written.filter(finite), ...completed.filter(finite));
        const backlogMaximum = Math.max(1, ...backlog.filter(finite), ...threshold.filter(finite));
        const rateY = (value) => top + rateHeight - (value * rateHeight / rateMaximum);
        const backlogY = (value) => backlogTop + backlogHeight - (value * backlogHeight / backlogMaximum);
        [top + rateHeight, backlogTop + backlogHeight].forEach((y) => {
            svg.appendChild(svgNode(document, "line", {
                x1: left, x2: left + width, y1: y, y2: y, class: "bb-pipeline-chart__axis",
            }));
        });
        const rateLabel = svgNode(document, "text", { x: left, y: 12, class: "bb-pipeline-chart__label" });
        rateLabel.textContent = `Events/min · max ${formatNumber(rateMaximum, 1)}`;
        svg.appendChild(rateLabel);
        const backlogLabel = svgNode(document, "text", { x: left, y: 135, class: "bb-pipeline-chart__label" });
        backlogLabel.textContent = `Durable backlog jobs · max ${formatNumber(backlogMaximum, 0)}`;
        svg.appendChild(backlogLabel);
        [
            [extracted, rateY, "extracted"],
            [written, rateY, "written"],
            [completed, rateY, "completed"],
            [backlog, backlogY, "backlog"],
            [threshold, backlogY, "threshold"],
        ].forEach(([values, yFor, name]) => {
            lineSegments(values, xFor, yFor).forEach((points) => {
                if (!points.length) return;
                svg.appendChild(svgNode(document, "polyline", {
                    points: points.join(" "),
                    class: `bb-pipeline-chart__line bb-pipeline-chart__line--${name}`,
                }));
            });
        });
        chartDescription(
            svg,
            `${chartSamples.length} retained samples from ${timelineRange(chartSamples)}. `
            + `The upper plot is events per minute; the lower plot is durable staged jobs.`,
        );
    }

    function renderSampleTable(root, payload) {
        const body = root.querySelector("[data-pipeline-sample-rows]");
        if (!body) return;
        const samples = list(payload.samples).slice(-MAX_TABLE_SAMPLES);
        if (!samples.length) {
            emptyTableRow(body, 6, "No timeline samples available.");
            return;
        }
        replaceChildren(body);
        samples.forEach((sample) => {
            const values = capacityValues(sample);
            const paused = values.controller + values.recovery;
            const row = body.ownerDocument.createElement("tr");
            appendCell(body.ownerDocument, row, formatDateTime(sample.at), true);
            appendCell(body.ownerDocument, row,
                `${values.active} / ${values.idle} / ${values.waiting} / ${values.backpressured} / ${paused} / ${values.unavailable}`);
            appendCell(body.ownerDocument, row, humanize(sample.publisherState));
            appendCell(body.ownerDocument, row,
                `${formatNumber(sampleRate(sample, "extractorOutputsPerSecond"))} / `
                + `${formatNumber(sampleRate(sample, "writerPublicationsPerSecond"))} / `
                + `${formatNumber(sampleRate(sample, "documentsCompletedPerSecond"))}`);
            appendCell(body.ownerDocument, row,
                `${formatNumber(sample.backpressureDepthJobs, 0)} / ${formatNumber(sample.backpressureThresholdJobs, 0)}`);
            appendCell(body.ownerDocument, row, formatBytes(sample.stagedBytes));
            body.appendChild(row);
        });
    }

    function renderRepositories(root, payload) {
        const body = root.querySelector("[data-pipeline-repository-rows]");
        if (!body) return;
        const run = object(payload.run);
        const repositories = list(run.repositoryProgress);
        setText(root, "[data-pipeline-run-id]", run.id ? `Run ${run.id}` : "No current run");
        if (!repositories.length) {
            emptyTableRow(body, 6, run.id ? "No repositories were accepted into this run." : "Waiting for a current run.");
            return;
        }
        replaceChildren(body);
        repositories.forEach((repository) => {
            const row = body.ownerDocument.createElement("tr");
            row.dataset.state = repository.lifecycleState || "unknown";
            appendCell(body.ownerDocument, row,
                repository.repositoryName || `Repository #${repository.repositoryId}`, true);
            appendCell(body.ownerDocument, row,
                `${humanize(repository.lifecycleState)} · ${humanize(repository.phase)}`);
            appendCell(body.ownerDocument, row,
                `${integer(repository.successfulPdfs)} of ${integer(repository.totalPdfs)}`);
            appendCell(body.ownerDocument, row, integer(repository.remainingPdfs));
            appendCell(body.ownerDocument, row, object(repository.eta).display || "Calculating ETA");
            appendCell(body.ownerDocument, row,
                repository.terminalOutcome ? humanize(repository.terminalOutcome)
                    : repository.unresolvedFailures
                        ? `${integer(repository.unresolvedFailures)} unresolved`
                        : "—");
            body.appendChild(row);
        });
    }

    function renderRecoveryHistory(root, payload) {
        const recovery = object(payload.recovery);
        const events = list(payload.recoveryEvents).length
            ? list(payload.recoveryEvents) : list(recovery.events);
        const body = root.querySelector("[data-pipeline-recovery-history-rows]");
        setText(root, "[data-pipeline-recovery-history-count]",
            `${events.length} event${events.length === 1 ? "" : "s"}`);
        if (!body) return;
        if (!events.length) {
            emptyTableRow(body, 5, "No recovery events recorded.");
            return;
        }
        replaceChildren(body);
        events.slice(0, 50).forEach((event) => {
            const row = body.ownerDocument.createElement("tr");
            appendCell(body.ownerDocument, row, formatDateTime(event.at || event.occurredAt), true);
            appendCell(body.ownerDocument, row, humanize(event.scope || recovery.scope));
            appendCell(body.ownerDocument, row, humanize(event.kind || event.event));
            appendCell(body.ownerDocument, row, event.reasonCode || "—");
            appendCell(body.ownerDocument, row, event.outcome || "—");
            body.appendChild(row);
        });
    }

    function renderTuningHistory(root, payload) {
        const events = list(payload.tuningEvents);
        const body = root.querySelector("[data-pipeline-tuning-history-rows]");
        setText(root, "[data-pipeline-tuning-history-count]",
            `${events.length} event${events.length === 1 ? "" : "s"}`);
        if (!body) return;
        if (!events.length) {
            emptyTableRow(body, 5, "No tuning recommendations or changes recorded.");
            return;
        }
        replaceChildren(body);
        events.slice(0, 50).forEach((event) => {
            const row = body.ownerDocument.createElement("tr");
            appendCell(body.ownerDocument, row,
                `${formatDateTime(event.at)} · ${humanize(event.mode)}`, true);
            appendCell(body.ownerDocument, row,
                `${integer(event.previousTarget)} → ${integer(event.proposedTarget)} · ${humanize(event.action)}`);
            appendCell(body.ownerDocument, row,
                `${event.reasonCode || "—"} · ${event.reason || "No explanation recorded"}`
                + `${event.expectedEffect ? ` · Expected: ${event.expectedEffect}` : ""}`);
            const evidence = Object.entries(object(event.evidence))
                .map(([key, value]) => `${humanize(key)} ${value}`).join(" · ");
            appendCell(body.ownerDocument, row,
                `${integer(event.observationWindowSeconds)}s${evidence ? ` · ${evidence}` : ""}`);
            const rollback = event.rollbackOutcome
                ? ` · rollback ${humanize(event.rollbackOutcome)}`
                : event.action === "rollback" ? " · rollback event" : "";
            appendCell(body.ownerDocument, row,
                `${event.outcome ? humanize(event.outcome) : "Pending"}`
                + rollback
                + `${event.cooldownUntil ? ` · cooldown to ${formatDateTime(event.cooldownUntil)}` : ""}`);
            body.appendChild(row);
        });
    }

    function renderWarnings(root, payload) {
        const body = root.querySelector("[data-pipeline-warnings]");
        if (!body) return;
        const warnings = [];
        const workers = object(payload.workers);
        const publisher = object(payload.publisher);
        const queues = object(payload.queues);
        const throughput = object(payload.throughput);
        if (payload.snapshotStale) warnings.push("The supervisor snapshot is stale; running state and ETA are unavailable.");
        if (integer(workers.unavailable) > 0) warnings.push(`${integer(workers.unavailable)} expected extraction controller${integer(workers.unavailable) === 1 ? " is" : "s are"} unavailable.`);
        if (publisher.state === "unavailable" && integer(queues.backpressureDepthJobs) > 0) warnings.push("Durable staged work is waiting while the PDF publisher is unavailable.");
        if (object(payload.recovery).state === "paused") warnings.push("A recovery circuit is paused; preserved work will not resume automatically.");
        if (number(throughput.failedPerSecond) > 0) warnings.push("PDF failures were observed in the current metrics window.");
        list(object(payload.state).constraints).forEach((constraint) => warnings.push(humanize(constraint)));
        replaceChildren(body);
        warnings.forEach((warning) => {
            const item = body.ownerDocument.createElement("li");
            item.textContent = warning;
            body.appendChild(item);
        });
    }

    function validResumeAction(action) {
        const candidate = object(action);
        if (candidate.method !== "POST" || typeof candidate.url !== "string" || !candidate.url) return false;
        if (!candidate.scope || !candidate.episodeId || !candidate.idempotencyKey) return false;
        if (!Number.isSafeInteger(candidate.expectedGeneration)
            || !Number.isSafeInteger(candidate.pauseGeneration)) return false;
        try {
            if (global.location) {
                if (typeof global.URL !== "function") return false;
                const target = new global.URL(candidate.url, global.location.href);
                if (target.origin !== global.location.origin) return false;
            }
        } catch {
            return false;
        }
        return true;
    }

    function renderRecovery(root, payload) {
        const recovery = object(payload.recovery);
        const state = recovery.state || "healthy";
        const card = root.querySelector("[data-pipeline-recovery-card]");
        if (card) card.dataset.state = state;
        setText(root, "[data-pipeline-recovery-state]", humanize(state));
        const detail = state === "healthy"
            ? "No component recovery episode is active."
            : recovery.pausedReason || recovery.reason || recovery.reasonCode
                || "A supervised PDF component is recovering from a sanitized failure.";
        setText(root, "[data-pipeline-recovery-detail]", detail);
        const attempts = `${integer(recovery.consecutiveFailedAttempts)} of ${integer(recovery.pauseAfterAttempts)} attempts`;
        const retry = recovery.nextRetryAt ? `next retry ${formatDateTime(recovery.nextRetryAt)}` : "no retry scheduled";
        const scope = recovery.scope ? humanize(recovery.scope) : "no affected scope";
        setText(root, "[data-pipeline-recovery-meta]", `${attempts} · ${scope} · ${retry}`);
        const button = root.querySelector("[data-pipeline-recovery-resume]");
        if (button) {
            const mayPreflight = recovery.resumable
                && ["safe", "requires_preflight"].includes(recovery.resumeSafety);
            const action = mayPreflight && validResumeAction(recovery.resumeAction)
                ? recovery.resumeAction : null;
            button.hidden = !action;
            button.disabled = false;
            button.textContent = action?.label || "Resume";
            button._pipelineResumeAction = action;
        }
    }

    function renderCurrentValues(root, payload) {
        const state = object(payload.state);
        const activity = object(payload.activity);
        const run = object(payload.run);
        const eta = object(run.totalEta);
        const throughput = object(payload.throughput);
        const workers = object(payload.workers);
        const queues = object(payload.queues);
        const resources = object(payload.resources);
        const controller = object(payload.controller);
        const publisher = object(payload.publisher);
        setText(root, "[data-pipeline-total-eta]", eta.display || "Waiting for a current run");
        setText(root, "[data-pipeline-eta-confidence]",
            eta.state === "available"
                ? `${humanize(eta.confidence)} confidence · ${eta.reasonCode || "measured critical path"}`
                : `${humanize(eta.state || "not applicable")} · ${eta.reasonCode || "no current run"}`);
        setText(root, "[data-pipeline-activity]", activity.label || "Idle");
        const secondary = list(activity.secondary).map((item) => {
            const count = integer(item.repositoryCount || item.count);
            return `${count ? `${count} ` : ""}${humanize(item.code)}`;
        });
        setText(root, "[data-pipeline-activity-secondary]",
            secondary.length ? secondary.join(" · ") : activity.reasonCode || "No secondary activity");
        setText(root, "[data-pipeline-extracted-rate]", formatRate(throughput.extractedRate));
        setText(root, "[data-pipeline-written-rate]", formatRate(throughput.writtenRate));
        const window = integer(throughput.rateWindowSeconds || payload.windowSeconds, 60);
        setText(root, "[data-pipeline-rate-window]", `${window}-second window`);
        setText(root, "[data-pipeline-written-window]", `${window}-second window`);
        setText(root, "[data-pipeline-capacity-value]",
            `${integer(workers.active)} active · ${integer(workers.idleNoDemand)} free`);
        setText(root, "[data-pipeline-capacity-detail]",
            `${integer(workers.live)} live of ${integer(workers.expectedResident)} expected · `
            + `${integer(workers.waitingForEligibleInput)} waiting · ${integer(workers.unavailable)} unavailable`);
        setText(root, "[data-pipeline-backlog-value]",
            `${integer(queues.backpressureDepthJobs)} / ${integer(queues.backpressureThresholdJobs)} jobs`);
        setText(root, "[data-pipeline-backlog-detail]",
            `${integer(queues.stagedWaitingJobs)} staged waiting · ${formatBytes(queues.stagedBytes)} · `
            + `oldest ${formatAge(queues.oldestStagedWaitSeconds)}`);
        setText(root, "[data-pipeline-target]", formatNumber(controller.effectiveAdmissionTarget, 0));
        setText(root, "[data-pipeline-hard-max]", formatNumber(controller.configuredPdfHardMax, 0));
        setText(root, "[data-pipeline-live-workers]", formatNumber(workers.live, 0));
        setText(root, "[data-pipeline-publisher]", humanize(publisher.state));
        setText(root, "[data-pipeline-owl-cpu]",
            finite(resources.owlProcessTreeCpuPct) ? `${formatNumber(resources.owlProcessTreeCpuPct)}%` : "Unavailable");
        setText(root, "[data-pipeline-memory]", formatBytes(resources.hostMemoryAvailableBytes));
        const resourceParts = [
            finite(resources.owlProcessTreeCpuPct)
                ? `OWL CPU ${formatNumber(resources.owlProcessTreeCpuPct)}%` : "OWL CPU unavailable",
            `memory ${formatBytes(resources.hostMemoryAvailableBytes)}`,
            `disk ${formatBytes(resources.diskAvailableBytes)}`,
        ];
        setText(root, "[data-pipeline-resource-summary]", resourceParts.join(" · "));
        const pagesPerSecond = throughput.pagesPersistedPerSecond;
        setText(root, "[data-pipeline-pages-rate]",
            finite(pagesPerSecond) ? `${formatNumber(pagesPerSecond * 60)}/min` : "Unavailable");
        setText(root, "[data-pipeline-staged-bytes]", formatBytes(queues.stagedBytes));
        setText(root, "[data-pipeline-oldest-staged]", formatAge(queues.oldestStagedWaitSeconds));
        setText(root, "[data-pipeline-disk]", formatBytes(resources.diskAvailableBytes));
        const cacheRate = throughput.cacheReuseCompletionsPerSecond;
        setText(root, "[data-pipeline-cache-reuse]",
            finite(cacheRate) ? `${formatNumber(cacheRate * 60)}/min` : "Unavailable");
        setText(root, "[data-pipeline-oldest-input]", formatAge(queues.oldestEligibleWaitSeconds));
        setText(root, "[data-pipeline-overall-state]", state.label || "Pipeline status unavailable");
        setText(root, "[data-pipeline-state-reason]", state.reason || "No evidence-based explanation is available.");
        setText(root, "[data-pipeline-state-meta]",
            `${humanize(state.confidence || "unknown")} confidence · ${state.reasonCode || "reason unavailable"}`);
        setText(root, "[data-pipeline-eta-detail]", eta.display || "Not applicable");
        const pdfs = object(run.pdfs);
        const known = integer(pdfs.inventoryRepositoriesKnown);
        const accepted = integer(pdfs.inventoryRepositoriesAccepted);
        setText(root, "[data-pipeline-inventory-coverage]",
            accepted ? `${known} of ${accepted} repositories · ${pdfs.inventoryFinal ? "final" : "provisional"}`
                : "No repositories accepted");
        setText(root, "[data-pipeline-eta-basis]", eta.reasonCode || "Insufficient evidence");
        setText(root, "[data-pipeline-eta-range]", formatRange(eta));
        setText(root, "[data-pipeline-eta-freshness]",
            eta.asOf ? `${formatDateTime(eta.asOf)} · ${humanize(eta.state)}` : "Unavailable");
        const forecast = object(run.etaAccuracy);
        setText(root, "[data-pipeline-forecast-error]",
            finite(forecast.medianAbsolutePercentageError)
                ? `${formatNumber(forecast.medianAbsolutePercentageError)}% median absolute error · `
                    + `${integer(forecast.checkpointCount)} checkpoints · ${humanize(forecast.workloadClass)}`
                : `${humanize(forecast.state || "not calibrated")} · ${humanize(forecast.workloadClass || "unknown workload")}`);
        setText(root, "[data-pipeline-forecast-bias]",
            finite(forecast.meanBiasPercentage)
                ? `${formatNumber(forecast.meanBiasPercentage)}% mean bias · `
                    + `${integer(forecast.overEstimateCount)} over / ${integer(forecast.underEstimateCount)} under`
                : "Not calibrated");
        const failures = throughput.failedPerSecond;
        setText(root, "[data-pipeline-failure-rate]",
            finite(failures) ? `${formatNumber(failures * 60)}/min` : "Unavailable");
        setText(root, "[data-pipeline-timeouts]",
            finite(throughput.timeoutPerSecond)
                ? `${formatNumber(throughput.timeoutPerSecond * 60)}/min` : "Unavailable");
        setText(root, "[data-pipeline-sqlite-busy-errors]",
            finite(publisher.sqliteBusyErrors)
                ? formatNumber(publisher.sqliteBusyErrors, 0) : "Unavailable");
        setText(root, "[data-pipeline-sqlite-lock-wait]",
            finite(publisher.sqliteLockWaitP50Ms) && finite(publisher.sqliteLockWaitP95Ms)
                ? `${formatNumber(publisher.sqliteLockWaitP50Ms)} / ${formatNumber(publisher.sqliteLockWaitP95Ms)} ms`
                : "Unavailable");
        setText(root, "[data-pipeline-sqlite-blocked-threshold]",
            finite(publisher.sqliteLockBlockedThresholdMs)
                ? `${formatNumber(publisher.sqliteLockBlockedThresholdMs, 0)} ms`
                : "Unavailable");
        const recovery = object(payload.recovery);
        setText(root, "[data-pipeline-recovery-probes]", integer(recovery.lifetimeAttempts));
        setText(root, "[data-pipeline-recovery-last-attempt]",
            recovery.lastAttemptAt ? formatDateTime(recovery.lastAttemptAt) : "None recorded");
        setText(root, "[data-pipeline-recovery-stability]",
            recovery.stabilityProgress || recovery.stabilityState
                ? humanize(recovery.stabilityProgress || recovery.stabilityState) : "Unavailable");
        setText(root, "[data-pipeline-recovery-blocked]",
            recovery.resumeBlockedReason || "No resume block reported");
        const repositories = object(run.repositories);
        setText(root, "[data-pipeline-run-repositories]", run.id
            ? `${integer(repositories.accepted)} accepted · ${integer(repositories.queued)} queued · `
                + `${integer(repositories.active)} active · ${integer(repositories.completed)} complete`
            : "No current run");
        setText(root, "[data-pipeline-run-pdfs]", run.id
            ? pdfs.inventoryFinal
                ? `${integer(pdfs.successful)} of ${integer(pdfs.total)} successful · `
                    + `${integer(pdfs.remaining)} remaining`
                : `${known} of ${accepted} inventories known · ${integer(pdfs.remaining)} remaining so far`
            : "No current run");
        setText(root, "[data-pipeline-fairness]",
            run.id
                ? `${integer(repositories.queued)} queued · ${integer(repositories.active)} active · `
                    + `oldest eligible input ${formatAge(queues.oldestEligibleWaitSeconds)}`
                : "No current run; fairness wait telemetry is not applicable.");
    }

    function renderFreshness(root, payload) {
        const generated = formatDateTime(payload.generatedAt);
        const stale = Boolean(payload.snapshotStale);
        const age = finite(payload.snapshotAgeSeconds) ? ` · ${formatAge(payload.snapshotAgeSeconds)} old` : "";
        root.dataset.pipelineFreshnessState = stale ? "stale" : "fresh";
        setText(root, "[data-pipeline-freshness]",
            `${stale ? "Stale snapshot" : "Updated"} ${generated}${age}`);
    }

    function stateSignature(payload) {
        return [
            object(payload.state).code,
            object(payload.activity).code,
            object(payload.recovery).state,
            object(payload.run).id,
        ].join(":");
    }

    function render(root, payload, options = {}) {
        if (!root || !payload || payload.schemaVersion !== 1) return false;
        const previousSignature = root.dataset.pipelineAnnouncement || "";
        const signature = stateSignature(payload);
        root.dataset.pipelineState = object(payload.state).code || "unknown";
        root.dataset.pipelineActivity = object(payload.activity).code || "idle";
        root.dataset.pipelineSeries = payload.seriesId || "unknown";
        root.dataset.pipelineRun = object(payload.run).id || "";
        renderFreshness(root, payload);
        renderCurrentValues(root, payload);
        renderActivityControls(root, payload);
        renderRepositoryCards(root, payload);
        renderWarnings(root, payload);
        renderRecovery(root, payload);
        renderRepositories(root, payload);
        renderRecoveryHistory(root, payload);
        renderTuningHistory(root, payload);
        renderCapacityChart(root, payload);
        renderFlowChart(root, payload);
        renderSampleTable(root, payload);
        if (options.announce !== false && signature !== previousSignature) {
            root.dataset.pipelineAnnouncement = signature;
            setText(root, "[data-pipeline-live]",
                `${object(payload.state).label || "Pipeline status unavailable"}. `
                + `${object(payload.activity).label || "No current activity"}.`);
        }
        return true;
    }

    function sampleMetadata(payload) {
        const generatedAt = Date.parse(payload?.generatedAt);
        const seriesStartedAt = Date.parse(payload?.seriesStartedAt);
        const run = object(payload?.run);
        const recovery = object(payload?.recovery);
        return {
            generatedAt,
            seriesStartedAt,
            seriesId: typeof payload?.seriesId === "string" ? payload.seriesId : "",
            runId: typeof run.id === "string" ? run.id : "",
            runAcceptedAt: Date.parse(run.acceptedAt),
            recoveryGeneration: Number.isSafeInteger(recovery.generation) && recovery.generation >= 0
                ? recovery.generation : null,
            pauseGeneration: Number.isSafeInteger(recovery.pauseGeneration) && recovery.pauseGeneration >= 0
                ? recovery.pauseGeneration : null,
            stale: Boolean(payload?.snapshotStale),
            retiredSeriesIds: [],
        };
    }

    function shouldAcceptSample(previous, payload, now = Date.now()) {
        if (!payload || payload.schemaVersion !== 1) return false;
        const candidate = sampleMetadata(payload);
        if (!Number.isFinite(candidate.generatedAt)
            || !Number.isFinite(candidate.seriesStartedAt)
            || candidate.seriesStartedAt > candidate.generatedAt + 10000
            || !candidate.seriesId) return false;
        if (candidate.runId && !Number.isFinite(candidate.runAcceptedAt)) return false;
        if (candidate.generatedAt > now + 10000) return false;
        if (!previous) return true;
        if (list(previous.retiredSeriesIds).includes(candidate.seriesId)) return false;
        if (candidate.generatedAt <= previous.generatedAt) return false;
        if (!previous.stale && candidate.stale) return false;
        const sameSeries = candidate.seriesId === previous.seriesId;
        if (!sameSeries && candidate.seriesStartedAt <= previous.seriesStartedAt) return false;
        if (sameSeries && previous.runId && !candidate.runId) return false;
        if (sameSeries && previous.runId && candidate.runId && previous.runId !== candidate.runId
            && Number.isFinite(previous.runAcceptedAt)
            && (!Number.isFinite(candidate.runAcceptedAt)
                || candidate.runAcceptedAt < previous.runAcceptedAt)) return false;
        if (Number.isSafeInteger(previous.recoveryGeneration)
            && (!Number.isSafeInteger(candidate.recoveryGeneration)
                || candidate.recoveryGeneration < previous.recoveryGeneration)) return false;
        if (Number.isSafeInteger(previous.pauseGeneration)
            && (!Number.isSafeInteger(candidate.pauseGeneration)
                || candidate.pauseGeneration < previous.pauseGeneration)) return false;
        return true;
    }

    function createFreshnessGate() {
        let current = null;
        return {
            accept(payload, now = Date.now()) {
                if (!shouldAcceptSample(current, payload, now)) return false;
                const next = sampleMetadata(payload);
                const retired = new Set(list(current?.retiredSeriesIds));
                if (current?.seriesId && current.seriesId !== next.seriesId) {
                    retired.add(current.seriesId);
                }
                next.retiredSeriesIds = [...retired].slice(-8);
                current = next;
                return true;
            },
            snapshot() { return current ? { ...current } : null; },
            reset() { current = null; },
        };
    }

    function pollInterval(payload, activeInterval = DEFAULT_ACTIVE_INTERVAL,
        idleInterval = DEFAULT_IDLE_INTERVAL) {
        const state = object(payload?.topBarActivityIndicator).state;
        return ACTIVE_INDICATOR_STATES.has(state) ? activeInterval : idleInterval;
    }

    function csrfToken() {
        const match = global.document?.cookie?.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    async function submitResume(controller, button) {
        const action = button?._pipelineResumeAction;
        if (!validResumeAction(action) || button.disabled) return;
        if (typeof global.URLSearchParams !== "function") return;
        const result = controller.root.querySelector("[data-pipeline-recovery-result]");
        button.disabled = true;
        if (result) result.textContent = "Checking whether recovery can resume safely…";
        const body = new global.URLSearchParams({
            scope: action.scope,
            episodeId: action.episodeId,
            expectedGeneration: String(action.expectedGeneration),
            pauseGeneration: String(action.pauseGeneration),
            idempotencyKey: action.idempotencyKey,
        });
        try {
            const response = await global.fetch(action.url, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "X-CSRFToken": csrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: body.toString(),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Resume was not accepted.");
            if (result) result.textContent = payload.duplicate
                ? "Recovery was already resumed from this pause generation."
                : "Recovery resume accepted. OWL is starting one controlled probe.";
            controller.refresh();
        } catch (error) {
            if (result) result.textContent = error instanceof Error
                ? error.message : "Recovery could not be resumed.";
            button.disabled = false;
        }
    }

    function dispatchPayload(root, payload) {
        global.document?.querySelectorAll?.(SELECTORS.consumer).forEach((consumer) => {
            if (consumer !== root) {
                render(consumer, payload, {
                    announce: consumer.dataset.pipelineAnnounce === "true",
                });
            }
        });
        if (typeof global.CustomEvent !== "function") return;
        const detail = { payload, root };
        root.dispatchEvent(new global.CustomEvent(EVENTS.rendered, { detail }));
        global.document?.dispatchEvent(new global.CustomEvent(EVENTS.metrics, { detail }));
    }

    function mount(root) {
        if (!root || mounted.has(root)) return mounted.get(root) || null;
        const url = root.dataset.pipelineMetricsUrl;
        if (!url || typeof global.fetch !== "function") return null;
        const activeInterval = integer(Number(root.dataset.pipelineActiveInterval), DEFAULT_ACTIVE_INTERVAL)
            || DEFAULT_ACTIVE_INTERVAL;
        const idleInterval = integer(Number(root.dataset.pipelineIdleInterval), DEFAULT_IDLE_INTERVAL)
            || DEFAULT_IDLE_INTERVAL;
        const gate = createFreshnessGate();
        let timer = null;
        let request = null;
        let requestSequence = 0;
        let latestPayload = null;
        let failures = 0;
        let destroyed = false;

        const controller = {
            root,
            gate,
            refresh() {
                if (destroyed || global.document?.hidden) return;
                if (timer !== null) global.clearTimeout(timer);
                timer = null;
                load();
            },
            destroy() {
                destroyed = true;
                if (timer !== null) global.clearTimeout(timer);
                request?.abort();
                root.removeEventListener?.("click", clickHandler);
                root.removeEventListener?.("submit", submissionHandler);
                global.document?.removeEventListener?.("visibilitychange", visibilityChanged);
                global.removeEventListener?.("pagehide", pageHidden);
                global.removeEventListener?.("pageshow", pageShown);
                mounted.delete(root);
            },
            latest() { return latestPayload; },
        };

        function schedule(delay) {
            if (destroyed || global.document?.hidden) return;
            if (timer !== null) global.clearTimeout(timer);
            timer = global.setTimeout(load, delay);
        }

        function markUnavailable() {
            root.dataset.pipelineFreshnessState = "unavailable";
            renderUnconfirmedInteractiveState();
            setText(root, "[data-pipeline-freshness]",
                latestPayload ? "Metrics request failed · retaining the last server snapshot"
                    : "Pipeline metrics unavailable");
            const signature = "metrics-request-failed";
            if (root.dataset.pipelineAnnouncement !== signature) {
                root.dataset.pipelineAnnouncement = signature;
                setText(root, "[data-pipeline-live]", "Pipeline metrics are temporarily unavailable.");
            }
        }

        function renderUnconfirmedInteractiveState() {
            const unavailablePayload = {
                ...(latestPayload || {}),
                schemaVersion: 1,
                snapshotStale: true,
                topBarActivityIndicator: {
                    state: "unknown",
                    hasFreshRunningWork: false,
                },
            };
            renderActivityControls(root, unavailablePayload);
            if (latestPayload) renderRepositoryCards(root, unavailablePayload);
            global.document?.querySelectorAll?.(SELECTORS.consumer).forEach((consumer) => {
                if (consumer === root) return;
                renderActivityControls(consumer, unavailablePayload);
                if (latestPayload) renderRepositoryCards(consumer, unavailablePayload);
                consumer.dataset.pipelineFreshnessState = "unavailable";
                setText(consumer, "[data-pipeline-freshness]", "Waiting for a fresh snapshot");
            });
        }

        async function load() {
            if (destroyed || global.document?.hidden) return;
            const sequence = ++requestSequence;
            request?.abort();
            request = typeof global.AbortController === "function" ? new global.AbortController() : null;
            try {
                const response = await global.fetch(url, {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
                    signal: request?.signal,
                });
                if (!response.ok) throw new Error("Pipeline metrics request failed.");
                const payload = await response.json();
                if (sequence !== requestSequence) return;
                if (!gate.accept(payload)) {
                    schedule(pollInterval(latestPayload || payload, activeInterval, idleInterval));
                    return;
                }
                latestPayload = payload;
                failures = 0;
                render(root, payload);
                dispatchPayload(root, payload);
                schedule(pollInterval(payload, activeInterval, idleInterval));
            } catch (error) {
                if (error?.name === "AbortError" || sequence !== requestSequence || destroyed) return;
                failures += 1;
                markUnavailable();
                const normal = latestPayload
                    ? pollInterval(latestPayload, activeInterval, idleInterval) : activeInterval;
                schedule(Math.min(idleInterval, normal * (2 ** Math.min(failures, 3))));
            }
        }

        function visibilityChanged() {
            if (global.document.hidden) {
                pausePolling();
            } else {
                controller.refresh();
            }
        }

        function pausePolling() {
            if (timer !== null) global.clearTimeout(timer);
            timer = null;
            request?.abort();
            renderUnconfirmedInteractiveState();
        }

        function pageHidden() {
            pausePolling();
        }

        function pageShown() {
            controller.refresh();
        }

        function clickHandler(event) {
            const button = event.target.closest?.("[data-pipeline-recovery-resume]");
            if (button && root.contains(button)) submitResume(controller, button);
        }

        function submissionHandler(event) {
            const control = event.target.closest?.("[data-pipeline-activity-control]");
            const button = control?.querySelector?.("[data-refresh-all-button]");
            if (!control || !root.contains(control) || event.defaultPrevented || button?.disabled) return;
            renderActivityControl(control, {
                ...(latestPayload || {}),
                snapshotStale: false,
                topBarActivityIndicator: { state: "submitting", hasFreshRunningWork: false },
            });
        }

        root.addEventListener("click", clickHandler);
        root.addEventListener("submit", submissionHandler);
        global.document?.addEventListener("visibilitychange", visibilityChanged);
        global.addEventListener?.("pagehide", pageHidden);
        global.addEventListener?.("pageshow", pageShown);
        mounted.set(root, controller);
        load();
        return controller;
    }

    function mountAll(scope = global.document) {
        if (!scope?.querySelectorAll) return [];
        return Array.from(scope.querySelectorAll(SELECTORS.dashboard))
            .map((root) => mount(root)).filter(Boolean);
    }

    const api = Object.freeze({
        version: 1,
        mount,
        mountAll,
        render,
        renderActivityControl,
        renderActivityControls,
        activityControlPresentation,
        renderRepositoryCards,
        repositoryPresentation,
        exactRepositoryCompletion,
        createFreshnessGate,
        shouldAcceptSample,
        pollInterval,
        formatters: Object.freeze({ formatAge, formatBytes, formatRate, humanize }),
        selectors: SELECTORS,
        events: EVENTS,
    });
    global.OWLPDFPipelineDashboard = api;

    if (global.document) {
        if (global.document.readyState === "loading") {
            global.document.addEventListener("DOMContentLoaded", () => mountAll());
        } else {
            mountAll();
        }
    }
}(typeof window === "undefined" ? globalThis : window));
