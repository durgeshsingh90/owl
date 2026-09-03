const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
    path.join(__dirname, "../../static/bitbucket_search/pdf_pipeline_dashboard.js"),
    "utf8",
);

const timestamp = (seconds) => `2026-09-03T12:00:${String(seconds).padStart(2, "0")}Z`;

function metrics(overrides = {}) {
    const base = {
        schemaVersion: 1,
        generatedAt: timestamp(10),
        seriesId: "series-a",
        seriesStartedAt: timestamp(0),
        snapshotStale: false,
        snapshotAgeSeconds: 1,
        state: { code: "warming_up", label: "Measuring PDF pipeline", confidence: "low" },
        activity: { code: "extracting", label: "Extracting", secondary: [] },
        topBarActivityIndicator: { state: "running" },
        run: {
            id: "run-a",
            acceptedAt: timestamp(1),
            repositories: { accepted: 1, queued: 0, active: 1 },
            pdfs: {
                inventoryRepositoriesKnown: 1,
                inventoryRepositoriesAccepted: 1,
                inventoryFinal: true,
            },
            totalEta: {
                state: "available",
                display: "ETA ~00:01:23",
                confidence: "medium",
                lowerSeconds: 70,
                upperSeconds: 100,
                asOf: timestamp(10),
            },
            repositoryProgress: [],
        },
        controller: { effectiveAdmissionTarget: 4, configuredPdfHardMax: 8 },
        workers: {
            active: 3,
            idleNoDemand: 1,
            waitingForEligibleInput: 0,
            backpressured: 0,
            pausedByController: 0,
            pausedByRecovery: 0,
            unavailable: 0,
            live: 4,
            expectedResident: 8,
        },
        publisher: { state: "busy", sqliteBusyErrors: null },
        counts: {
            pdfsDiscovered: 100,
            pdfsPendingExtraction: 40,
            pdfsCurrentlyExtracting: 3,
            pdfsSuccessfullyExtracted: 57,
            extractionFailures: 2,
            pdfsSuccessfullyWritten: 55,
        },
        jsonlStaging: {
            currentSizeBytes: 1024,
            sealedChunksWaiting: 2,
            queuedChunks: 3,
            queuedBytes: 4096,
            writerState: "WRITING",
            currentChunk: "chunk_000001.jsonl",
            retainedChunks: 4,
            retainedBytes: 8192,
            failedChunks: 0,
            nextCleanupEligibleAt: timestamp(30),
        },
        flowBalance: {
            state: "SQLITE_BACKLOG",
            label: "SQLite backlog",
            reason: "SQLite ingestion has sealed JSONL chunks waiting on disk.",
        },
        recovery: {
            state: "healthy",
            generation: 4,
            pauseGeneration: 1,
            lifetimeAttempts: 2,
            consecutiveFailedAttempts: 0,
            pauseAfterAttempts: 25,
            resumable: false,
            resumeSafety: "not_applicable",
            resumeAction: null,
        },
        queues: {
            backpressureDepthJobs: 2,
            backpressureThresholdJobs: 4,
            stagedWaitingJobs: 1,
            publicationInFlightJobs: 1,
            stagedBytes: 2048,
            oldestStagedWaitSeconds: 4,
            oldestEligibleWaitSeconds: 9,
        },
        throughput: {
            rateWindowSeconds: 60,
            extractedRate: { state: "available", perMinute: 12 },
            writtenRate: { state: "warming", perMinute: null },
            cacheReuseCompletionsPerSecond: 0,
            documentsCompletedPerSecond: 0.2,
            pagesExtractedPerSecond: 3,
            pagesPersistedPerSecond: 2.5,
            failedPerSecond: null,
        },
        resources: {
            owlProcessTreeCpuPct: null,
            hostMemoryAvailableBytes: 4 * 1024 ** 3,
            diskAvailableBytes: 20 * 1024 ** 3,
        },
        samples: [],
        tuningEvents: [],
    };
    return { ...base, ...overrides };
}

function boot({ hidden = false, responses = [] } = {}) {
    const documentListeners = new Map();
    const windowListeners = new Map();
    const timers = new Map();
    const clearedTimers = [];
    const requests = [];
    let timerSequence = 0;
    const consumers = [];
    const placeholderCookie = "csrftoken=placeholder";
    const document = {
        readyState: "loading",
        hidden,
        ["cookie"]: placeholderCookie,
        addEventListener(name, listener) { documentListeners.set(name, listener); },
        removeEventListener(name, listener) {
            if (documentListeners.get(name) === listener) documentListeners.delete(name);
        },
        dispatchEvent() {},
        querySelectorAll(selector) {
            return selector === "[data-pipeline-consumer]" ? consumers : [];
        },
    };
    const window = {
        document,
        location: { href: "http://127.0.0.1/pdfs/status/", origin: "http://127.0.0.1" },
        URL,
        URLSearchParams,
        AbortController,
        CustomEvent: class CustomEvent {
            constructor(name, options) { this.type = name; this.detail = options?.detail; }
        },
        addEventListener(name, listener) { windowListeners.set(name, listener); },
        removeEventListener(name, listener) {
            if (windowListeners.get(name) === listener) windowListeners.delete(name);
        },
        setTimeout(callback, delay) {
            timerSequence += 1;
            timers.set(timerSequence, { callback, delay });
            return timerSequence;
        },
        clearTimeout(identifier) {
            clearedTimers.push(identifier);
            timers.delete(identifier);
        },
        async fetch(url, options) {
            requests.push({ url, options });
            const response = responses.shift();
            if (response instanceof Error) throw response;
            if (!response) throw new Error("No synthetic response was provided.");
            return {
                ok: response.ok !== false,
                async json() { return response.payload || response; },
            };
        },
    };
    window.window = window;
    vm.runInNewContext(source, { window, globalThis: window, console });
    return {
        api: window.OWLPDFPipelineDashboard,
        window,
        document,
        documentListeners,
        windowListeners,
        timers,
        clearedTimers,
        requests,
        consumers,
    };
}

function markerRoot(markers = {}) {
    const listeners = new Map();
    const nodes = new Map(
        Object.entries(markers).map(([selector, value]) => [selector, [{
            textContent: value,
            hidden: false,
            disabled: false,
            dataset: {},
        }]]),
    );
    return {
        dataset: {},
        listeners,
        querySelectorAll(selector) { return nodes.get(selector) || []; },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        addEventListener(name, listener) { listeners.set(name, listener); },
        removeEventListener(name, listener) {
            if (listeners.get(name) === listener) listeners.delete(name);
        },
        dispatchEvent() {},
        contains(node) { return [...nodes.values()].some((values) => values.includes(node)); },
        node(selector) { return this.querySelector(selector); },
    };
}

function uiNode({ dataset = {}, attributes = {}, text = "" } = {}) {
    const attrs = new Map(Object.entries(attributes).map(([key, value]) => [key, String(value)]));
    const classes = new Set();
    return {
        dataset: { ...dataset },
        textContent: text,
        hidden: false,
        disabled: false,
        title: "",
        className: "",
        classList: {
            toggle(name, enabled) {
                if (enabled) classes.add(name); else classes.delete(name);
            },
            contains(name) { return classes.has(name); },
        },
        setAttribute(name, value) { attrs.set(name, String(value)); },
        getAttribute(name) { return attrs.get(name) ?? null; },
        removeAttribute(name) { attrs.delete(name); },
    };
}

function hookedRoot(hooks, { dataset = {}, attributes = {} } = {}) {
    const root = uiNode({ dataset, attributes });
    root.querySelectorAll = (selector) => hooks.get(selector) || [];
    root.querySelector = (selector) => root.querySelectorAll(selector)[0] || null;
    return root;
}

const flush = async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
};

test("public renderer contract is stable and exposes generic consumer selectors", () => {
    const { api } = boot();
    assert.equal(api.version, 1);
    for (const name of [
        "mount", "mountAll", "render", "renderActivityControl", "renderRepositoryCards",
        "activityControlPresentation", "repositoryPresentation", "exactRepositoryCompletion",
        "createFreshnessGate", "shouldAcceptSample", "pollInterval",
    ]) {
        assert.equal(typeof api[name], "function");
    }
    assert.equal(api.selectors.dashboard, "[data-pipeline-dashboard]");
    assert.equal(api.selectors.consumer, "[data-pipeline-consumer]");
    assert.deepEqual(
        { ...api.events },
        { metrics: "owl:pipeline-metrics", rendered: "owl:pipeline-rendered" },
    );
    assert.equal(Object.isFrozen(api), true);
    assert.equal(Object.isFrozen(api.events), true);
    assert.equal(Object.isFrozen(api.selectors), true);
});

test("top activity control has an exact non-animated state machine and detaches GIF resources", () => {
    const environment = boot();
    environment.window.matchMedia = () => ({ matches: false });
    const nodes = {
        button: uiNode(),
        label: uiNode(),
        detail: uiNode(),
        reload: uiNode(),
        waiting: uiNode(),
        spinner: uiNode(),
        attention: uiNode(),
        complete: uiNode(),
        animated: uiNode({ dataset: { activeSrc: "/static/work-in-progress.gif" } }),
        runningStatic: uiNode(),
    };
    const hooks = new Map([
        ["[data-refresh-all-button]", [nodes.button]],
        ["[data-refresh-all-label]", [nodes.label]],
        ["[data-refresh-all-detail]", [nodes.detail]],
        ["[data-refresh-all-icon]", [nodes.reload]],
        ["[data-refresh-all-waiting]", [nodes.waiting]],
        ["[data-refresh-all-spinner]", [nodes.spinner]],
        ["[data-refresh-all-attention]", [nodes.attention]],
        ["[data-refresh-all-complete]", [nodes.complete]],
        ["[data-refresh-all-running-visual]", [nodes.animated]],
        ["[data-refresh-all-running-static]", [nodes.runningStatic]],
    ]);
    const control = hookedRoot(hooks, {
        dataset: { repositoryCount: "2", enabledRepositoryCount: "2" },
        attributes: { action: "/pdfs/repositories/refresh/" },
    });

    const running = metrics({
        topBarActivityIndicator: {
            state: "running",
            hasFreshRunningWork: true,
            evidenceAt: timestamp(9),
            freshForSeconds: 15,
        },
    });
    let presentation = environment.api.renderActivityControl(control, running);
    assert.equal(presentation.state, "running");
    assert.equal(control.dataset.pipelineIndicatorState, "running");
    assert.equal(control.getAttribute("aria-busy"), "true");
    assert.equal(nodes.animated.hidden, false);
    assert.equal(nodes.animated.getAttribute("src"), "/static/work-in-progress.gif");
    assert.equal(nodes.runningStatic.hidden, true);
    assert.equal(nodes.button.disabled, true);

    presentation = environment.api.renderActivityControl(control, metrics({
        generatedAt: timestamp(11),
        activity: { code: "idle", label: "Idle", secondary: [] },
        topBarActivityIndicator: { state: "idle_actionable", hasFreshRunningWork: false },
    }));
    assert.equal(presentation.state, "idle_actionable");
    assert.equal(nodes.animated.getAttribute("src"), null, "leaving work detaches the GIF URL");
    assert.equal(nodes.animated.hidden, true);
    assert.equal(nodes.reload.hidden, false);
    assert.equal(nodes.button.disabled, false);
    assert.equal(control.getAttribute("aria-busy"), null);

    const cases = [
        ["queued", "waiting", "Added to queue"],
        ["retry_wait", "waiting", "Waiting to retry"],
        ["paused", "attention", "PDF pipeline paused"],
        ["idle_unavailable", "attention", "Refresh all unavailable"],
        ["unknown", "attention", "Pipeline status unavailable"],
        ["terminal", "complete", "Complete"],
        ["submitting", "submitting", "Adding to queue"],
        ["hidden", "reload", "No repositories"],
    ];
    for (const [state, visual, label] of cases) {
        const payload = metrics({
            generatedAt: timestamp(12),
            activity: { code: state === "terminal" ? "complete" : state, label, secondary: [] },
            topBarActivityIndicator: { state, hasFreshRunningWork: false },
        });
        presentation = environment.api.renderActivityControl(control, payload);
        assert.equal(presentation.state, state);
        assert.equal(presentation.visual, visual);
        assert.equal(nodes.animated.getAttribute("src"), null, `${state} must not attach a GIF`);
    }
    assert.equal(control.hidden, true);
    assert.equal(nodes.button.disabled, true);
});

test("running and recovering use a static icon when reduced motion is requested", () => {
    const environment = boot();
    environment.window.matchMedia = () => ({ matches: true });
    const animated = uiNode({ dataset: { activeSrc: "/static/work-in-progress.gif" } });
    const runningStatic = uiNode();
    const control = hookedRoot(new Map([
        ["[data-refresh-all-running-visual]", [animated]],
        ["[data-refresh-all-running-static]", [runningStatic]],
    ]), {
        dataset: { repositoryCount: "1", enabledRepositoryCount: "1" },
        attributes: { action: "/refresh/" },
    });
    environment.api.renderActivityControl(control, metrics({
        topBarActivityIndicator: {
            state: "running", hasFreshRunningWork: true,
            evidenceAt: timestamp(9), freshForSeconds: 15,
        },
    }));
    assert.equal(animated.hidden, true);
    assert.equal(animated.getAttribute("src"), null);
    assert.equal(runningStatic.hidden, false);

    environment.api.renderActivityControl(control, metrics({
        generatedAt: timestamp(11),
        recovery: {
            ...metrics().recovery,
            state: "recovering",
            generation: 5,
            activeAttemptId: "00000000-0000-0000-0000-000000000005",
        },
        topBarActivityIndicator: { state: "recovering", hasFreshRunningWork: false },
    }));
    assert.equal(animated.hidden, true);
    assert.equal(animated.getAttribute("src"), null);
    assert.equal(runningStatic.hidden, false);
});

test("unconfirmed running or recovering state fails closed to unknown", () => {
    const { api } = boot();
    assert.equal(api.activityControlPresentation(metrics({
        topBarActivityIndicator: { state: "running", hasFreshRunningWork: false },
    })).state, "unknown");
    assert.equal(api.activityControlPresentation(metrics({
        recovery: { ...metrics().recovery, state: "retry_wait" },
        topBarActivityIndicator: { state: "recovering", hasFreshRunningWork: false },
    })).state, "unknown");
    assert.equal(api.activityControlPresentation(metrics({
        snapshotStale: true,
        topBarActivityIndicator: {
            state: "running", hasFreshRunningWork: true,
            evidenceAt: timestamp(10), freshForSeconds: 15,
        },
    })).state, "unknown");
});

function repositoryCard(repositoryId = 42) {
    const name = uiNode({ text: `Repository ${repositoryId}` });
    const icon = uiNode();
    const work = uiNode();
    const queueLabel = uiNode();
    const remaining = uiNode();
    const eta = uiNode();
    const progress = uiNode();
    const queued = uiNode();
    const queue = uiNode();
    const working = uiNode();
    const activeVisual = uiNode({ dataset: { activeSrc: "/static/indexing.gif" } });
    const activeStatic = uiNode();
    const complete = uiNode();
    const attention = uiNode();
    const unknown = uiNode();
    const card = hookedRoot(new Map([
        ["[data-repository-name]", [name]],
        ["[data-repository-state-icon]", [icon]],
        ["[data-repository-work-label]", [work]],
        ["[data-repository-queue-label]", [queueLabel]],
        ["[data-repository-remaining]", [remaining]],
        ["[data-repository-eta]", [eta]],
        ["[data-repository-progress]", [progress]],
        ["[data-repository-queued-icon]", [queued]],
        ["[data-repository-queue-icon]", [queue]],
        ["[data-repository-working-icon]", [working]],
        ["[data-repository-active-visual]", [activeVisual]],
        ["[data-repository-active-static]", [activeStatic]],
        ["[data-repository-complete-icon]", [complete]],
        ["[data-repository-attention-icon]", [attention]],
        ["[data-repository-unknown-icon]", [unknown]],
    ]));
    return {
        card, name, icon, work, queueLabel, remaining, eta, progress, queued, queue, working,
        activeVisual, activeStatic, complete, attention, unknown,
    };
}

test("repository cards follow durable queued, active, terminal and stale invariants", () => {
    const { api } = boot();
    const fixture = repositoryCard();
    const root = {
        querySelectorAll(selector) {
            return selector === '[data-repository-id="42"]' ? [fixture.card] : [];
        },
    };
    const baseRepository = {
        repositoryId: 42,
        runId: "run-a",
        lifecycleState: "queued",
        phase: "queued",
        inventoryFinal: false,
        totalPdfs: 0,
        successfulPdfs: 0,
        permanentFailedPdfs: 0,
        cancelledPdfs: 0,
        remainingPdfs: 0,
        stagedPdfs: 0,
        publishingPdfs: 0,
        unresolvedFailures: 0,
        eta: { state: "waiting_for_inventory", display: "Waiting for inventory" },
    };
    const renderRepository = (repository, overrides = {}) => api.renderRepositoryCards(root, metrics({
        ...overrides,
        run: { ...metrics().run, repositoryProgress: [repository] },
    }));

    renderRepository(baseRepository);
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "queued");
    assert.equal(fixture.queueLabel.textContent, "Added to queue");
    assert.equal(fixture.queueLabel.hidden, false);
    assert.equal(fixture.work.textContent, "Waiting in queue");
    assert.equal(fixture.queued.hidden, false);
    assert.equal(fixture.queue.hidden, false);
    assert.equal(fixture.activeVisual.getAttribute("src"), null);
    assert.equal(fixture.remaining.hidden, true);
    assert.equal(fixture.eta.hidden, true);
    assert.match(fixture.icon.getAttribute("aria-label"), /Added to queue$/);

    renderRepository({
        ...baseRepository,
        lifecycleState: "active",
        phase: "extracting_and_writing",
        inventoryFinal: true,
        totalPdfs: 5,
        successfulPdfs: 2,
        remainingPdfs: 3,
        eta: { state: "available", display: "ETA ~00:02:30" },
    });
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "active");
    assert.equal(fixture.queueLabel.hidden, true);
    assert.equal(fixture.work.textContent, "Extracting + writing");
    assert.equal(fixture.remaining.textContent, "Remaining 3 of 5 PDFs");
    assert.equal(fixture.remaining.hidden, false);
    assert.equal(fixture.eta.textContent, "ETA ~00:02:30");
    assert.equal(fixture.eta.hidden, false);
    assert.equal(fixture.complete.hidden, true);
    assert.equal(fixture.activeVisual.hidden, false);
    assert.equal(fixture.activeVisual.getAttribute("src"), "/static/indexing.gif");

    const complete = {
        ...baseRepository,
        lifecycleState: "complete",
        phase: "complete",
        inventoryFinal: true,
        totalPdfs: 5,
        successfulPdfs: 5,
        remainingPdfs: 0,
    };
    renderRepository(complete);
    assert.equal(api.exactRepositoryCompletion(complete), true);
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "complete");
    assert.equal(fixture.work.textContent, "PDF indexing complete");
    assert.equal(fixture.complete.hidden, false);
    assert.equal(fixture.activeVisual.getAttribute("src"), null);

    renderRepository({ ...complete, successfulPdfs: 4, permanentFailedPdfs: 1 });
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "unknown");
    assert.equal(fixture.complete.hidden, true, "partial failure cannot retain a green tick");

    assert.equal(api.exactRepositoryCompletion({ ...complete, stagedPdfs: 1 }), false);
    assert.equal(api.exactRepositoryCompletion({ ...complete, publishingPdfs: 1 }), false);
    const missingStaged = { ...complete };
    delete missingStaged.stagedPdfs;
    assert.equal(api.exactRepositoryCompletion(missingStaged), false);

    renderRepository({ ...complete, totalPdfs: 0, successfulPdfs: 0 });
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "complete",
        "a successful final zero-PDF inventory is complete");

    renderRepository({
        ...baseRepository,
        lifecycleState: "active",
        phase: "writing",
        inventoryFinal: true,
        totalPdfs: 2,
        remainingPdfs: 1,
        successfulPdfs: 1,
    }, { snapshotStale: true });
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "unknown");
    assert.equal(fixture.remaining.hidden, true);
    assert.equal(fixture.eta.hidden, true);
    assert.equal(fixture.complete.hidden, true);

    renderRepository(baseRepository);
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "queued",
        "a newer accepted run removes an old completion presentation");
    assert.equal(fixture.queueLabel.hidden, false);
    assert.equal(fixture.work.textContent, "Waiting in queue");
});

test("a repository absent from the newest run cannot retain an older green completion", () => {
    const { api } = boot();
    const fixture = repositoryCard();
    fixture.card.dataset.repositoryGitSyncFailed = "false";
    fixture.card.dataset.repositoryPdfIndexFailedCount = "0";
    const root = {
        querySelectorAll(selector) {
            return selector === "[data-repository-id]" || selector === '[data-repository-id="42"]'
                ? [fixture.card] : [];
        },
    };
    const complete = {
        repositoryId: 42,
        runId: "run-old",
        lifecycleState: "complete",
        phase: "complete",
        inventoryFinal: true,
        totalPdfs: 1,
        successfulPdfs: 1,
        permanentFailedPdfs: 0,
        cancelledPdfs: 0,
        remainingPdfs: 0,
        stagedPdfs: 0,
        publishingPdfs: 0,
        unresolvedFailures: 0,
        eta: { state: "complete", display: "Complete" },
    };

    api.renderRepositoryCards(root, metrics({
        run: { ...metrics().run, id: "run-old", repositoryProgress: [complete] },
    }));
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "complete");
    assert.equal(fixture.complete.hidden, false);

    api.renderRepositoryCards(root, metrics({
        run: { ...metrics().run, id: "run-new", repositoryProgress: [] },
    }));
    assert.equal(fixture.card.dataset.pipelineRepositoryState, "unknown");
    assert.equal(fixture.complete.hidden, true);
    assert.equal(fixture.unknown.hidden, false);
    assert.match(fixture.icon.getAttribute("aria-label"), /Not accepted into the current run$/);
});

test("freshness gate rejects malformed, out-of-order, stale, old-run and regressed recovery samples", () => {
    const { api } = boot();
    const now = Date.parse("2026-09-03T12:01:00Z");
    const gate = api.createFreshnessGate();
    assert.equal(gate.accept(metrics(), now), true);
    assert.equal(gate.accept(metrics(), now), false, "same snapshot is not a new sample");
    assert.equal(gate.accept(metrics({ generatedAt: timestamp(9) }), now), false);
    assert.equal(gate.accept(metrics({ generatedAt: timestamp(11), snapshotStale: true }), now), false);
    assert.equal(gate.accept(metrics({
        generatedAt: timestamp(12),
        run: { ...metrics().run, id: "older-run", acceptedAt: timestamp(0) },
    }), now), false);
    assert.equal(gate.accept(metrics({
        generatedAt: timestamp(13),
        recovery: { ...metrics().recovery, generation: 3 },
    }), now), false);
    assert.equal(gate.accept(metrics({
        generatedAt: timestamp(14),
        recovery: { ...metrics().recovery, pauseGeneration: 0 },
    }), now), false);
    assert.equal(gate.accept(metrics({ seriesStartedAt: undefined }), now), false);
    assert.equal(gate.accept(metrics({ generatedAt: "2099-01-01T00:00:00Z" }), now), false);
});

test("a newer telemetry series replaces the old series and the retired series cannot return", () => {
    const { api } = boot();
    const now = Date.parse("2026-09-03T12:01:00Z");
    const gate = api.createFreshnessGate();
    assert.equal(gate.accept(metrics(), now), true);
    assert.equal(gate.accept(metrics({
        generatedAt: timestamp(20),
        seriesId: "series-b",
        seriesStartedAt: timestamp(15),
        run: null,
    }), now), true);
    assert.deepEqual([...gate.snapshot().retiredSeriesIds], ["series-a"]);
    assert.equal(gate.accept(metrics({
        generatedAt: timestamp(30),
        seriesId: "series-a",
        seriesStartedAt: timestamp(0),
    }), now), false);
    gate.reset();
    assert.equal(gate.snapshot(), null);
    assert.equal(gate.accept(metrics({ snapshotStale: true }), now), true,
        "an initial stale snapshot is rendered truthfully when no fresher client state exists");
});

test("poll cadence is fast for work and recovery but quiet for idle or terminal state", () => {
    const { api } = boot();
    for (const state of ["submitting", "queued", "running", "retry_wait", "recovering", "paused"]) {
        assert.equal(api.pollInterval({ topBarActivityIndicator: { state } }), 5000, state);
    }
    for (const state of ["hidden", "idle_actionable", "idle_unavailable", "terminal", "unknown"]) {
        assert.equal(api.pollInterval({ topBarActivityIndicator: { state } }), 30000, state);
    }
    assert.equal(api.pollInterval(
        { topBarActivityIndicator: { state: "running" } }, 1500, 9000,
    ), 1500);
});

test("generic markers render truthful units, warming and unavailable values", () => {
    const { api } = boot();
    const root = markerRoot({
        "[data-pipeline-total-eta]": "",
        "[data-pipeline-activity]": "",
        "[data-pipeline-extracted-rate]": "",
        "[data-pipeline-written-rate]": "",
        "[data-pipeline-pages-rate]": "",
        "[data-pipeline-extracted-pages-rate]": "",
        "[data-pipeline-current-jsonl]": "",
        "[data-pipeline-jsonl-queue]": "",
        "[data-pipeline-jsonl-writer]": "",
        "[data-pipeline-flow-diagnosis]": "",
        "[data-pipeline-pdfs-written]": "",
        "[data-pipeline-jsonl-retained]": "",
        "[data-pipeline-owl-cpu]": "",
        "[data-pipeline-failure-rate]": "",
        "[data-pipeline-freshness]": "",
        "[data-pipeline-live]": "",
    });
    assert.equal(api.render(root, metrics()), true);
    assert.equal(root.node("[data-pipeline-total-eta]").textContent, "ETA ~00:01:23");
    assert.equal(root.node("[data-pipeline-activity]").textContent, "Extracting");
    assert.equal(root.node("[data-pipeline-extracted-rate]").textContent, "12/min");
    assert.equal(root.node("[data-pipeline-written-rate]").textContent, "Warming");
    assert.equal(root.node("[data-pipeline-pages-rate]").textContent, "150/min");
    assert.equal(root.node("[data-pipeline-extracted-pages-rate]").textContent, "180/min");
    assert.equal(root.node("[data-pipeline-current-jsonl]").textContent, "1 KB");
    assert.equal(root.node("[data-pipeline-jsonl-queue]").textContent, "2 sealed · 4 KB");
    assert.equal(root.node("[data-pipeline-jsonl-writer]").textContent,
        "WRITING · chunk_000001.jsonl");
    assert.match(root.node("[data-pipeline-flow-diagnosis]").textContent, /^SQLite backlog/);
    assert.equal(root.node("[data-pipeline-pdfs-written]").textContent, "55");
    assert.equal(root.node("[data-pipeline-jsonl-retained]").textContent, "4 chunks · 8 KB");
    assert.equal(root.node("[data-pipeline-owl-cpu]").textContent, "Unavailable");
    assert.equal(root.node("[data-pipeline-failure-rate]").textContent, "Unavailable");
    assert.match(root.node("[data-pipeline-freshness]").textContent, /^Updated /);
    assert.equal(root.dataset.pipelineFreshnessState, "fresh");
    assert.equal(root.node("[data-pipeline-live]").textContent,
        "Measuring PDF pipeline. Extracting.");

    const priorAnnouncement = root.node("[data-pipeline-live]").textContent;
    api.render(root, metrics({ generatedAt: timestamp(11) }));
    assert.equal(root.node("[data-pipeline-live]").textContent, priorAnnouncement,
        "ordinary samples do not repeat the state announcement");
    api.render(root, metrics({
        generatedAt: timestamp(12),
        snapshotStale: true,
        state: { code: "degraded", label: "Pipeline status unavailable", confidence: "low" },
        activity: { code: "idle", label: "Idle", secondary: [] },
    }));
    assert.equal(root.dataset.pipelineFreshnessState, "stale");
    assert.equal(root.node("[data-pipeline-live]").textContent,
        "Pipeline status unavailable. Idle.");
});

test("requires-preflight resume renders only a valid same-origin server action", () => {
    const { api } = boot();
    const root = markerRoot({ "[data-pipeline-recovery-resume]": "Resume" });
    const button = root.node("[data-pipeline-recovery-resume]");
    const action = {
        method: "POST",
        url: "/pdfs/pipeline/recovery/resume/",
        scope: "publisher",
        episodeId: "episode-a",
        expectedGeneration: 9,
        pauseGeneration: 2,
        idempotencyKey: "opaque-key",
        label: "Resume publisher",
    };
    api.render(root, metrics({
        recovery: {
            ...metrics().recovery,
            state: "paused",
            resumable: true,
            resumeSafety: "requires_preflight",
            resumeAction: action,
        },
    }));
    assert.equal(button.hidden, false);
    assert.equal(button.textContent, "Resume publisher");
    assert.deepEqual(button._pipelineResumeAction, action);

    api.render(root, metrics({
        generatedAt: timestamp(11),
        recovery: {
            ...metrics().recovery,
            state: "paused",
            resumable: true,
            resumeSafety: "requires_preflight",
            resumeAction: { ...action, url: "https://attacker.example/resume" },
        },
    }));
    assert.equal(button.hidden, true);
    assert.equal(button._pipelineResumeAction, null);
});

test("mount pauses while hidden, resumes on visibility, and reschedules rejected snapshots", async () => {
    const first = metrics();
    const environment = boot({ hidden: true, responses: [first, first] });
    const root = markerRoot();
    root.dataset.pipelineMetricsUrl = "/pdfs/pipeline/metrics/";
    root.dataset.pipelineActiveInterval = "5000";
    root.dataset.pipelineIdleInterval = "30000";
    const controller = environment.api.mount(root);
    assert.ok(controller);
    assert.equal(environment.requests.length, 0, "hidden documents do not poll");

    environment.document.hidden = false;
    environment.documentListeners.get("visibilitychange")();
    await flush();
    assert.equal(environment.requests.length, 1);
    assert.equal([...environment.timers.values()].at(-1).delay, 5000);

    const pending = [...environment.timers.values()].at(-1);
    pending.callback();
    await flush();
    assert.equal(environment.requests.length, 2);
    assert.equal(environment.timers.size, 1,
        "a duplicate/out-of-order response is rejected without stopping future polling");
    assert.equal([...environment.timers.values()].at(-1).delay, 5000);

    environment.windowListeners.get("pagehide")();
    assert.equal(environment.timers.size, 0);
    environment.document.hidden = true;
    environment.windowListeners.get("pageshow")();
    assert.equal(environment.requests.length, 2, "pageshow still respects hidden state");

    controller.destroy();
    assert.equal(root.listeners.has("click"), false);
    assert.equal(environment.documentListeners.has("visibilitychange"), false);
    assert.equal(environment.windowListeners.has("pagehide"), false);
    assert.equal(environment.windowListeners.has("pageshow"), false);
});

test("one accepted poll renders passive consumers and dispatches the shared payload event", async () => {
    const environment = boot({ responses: [metrics()] });
    const poller = markerRoot();
    poller.dataset.pipelineMetricsUrl = "/pdfs/pipeline/metrics/";
    const consumer = markerRoot({
        "[data-pipeline-total-eta]": "",
        "[data-pipeline-activity]": "",
        "[data-pipeline-live]": "unchanged",
    });
    consumer.dataset.pipelineAnnounce = "false";
    environment.consumers.push(consumer);
    let event = null;
    environment.document.dispatchEvent = (candidate) => { event = candidate; };
    environment.api.mount(poller);
    await flush();
    assert.equal(environment.requests.length, 1);
    assert.equal(consumer.node("[data-pipeline-total-eta]").textContent, "ETA ~00:01:23");
    assert.equal(consumer.node("[data-pipeline-activity]").textContent, "Extracting");
    assert.equal(consumer.node("[data-pipeline-live]").textContent, "unchanged");
    assert.equal(event.type, environment.api.events.metrics);
    assert.equal(event.detail.payload.seriesId, "series-a");
    assert.equal(event.detail.root, poller);
});
