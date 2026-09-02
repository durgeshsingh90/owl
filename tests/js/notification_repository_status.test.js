const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../static/owl/owl.js"), "utf8");

class Element {
    constructor(tagName = "div") {
        this.tagName = tagName;
        this.children = [];
        this.dataset = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this.hidden = false;
        this.open = false;
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.clientHeight = 0;
        this.style = { setProperty() {}, removeProperty() {} };
        this.classList = { toggle() {}, add() {}, remove() {} };
        this._text = "";
    }
    set textContent(value) {
        this._text = String(value);
        this.replaceChildren();
    }
    get textContent() {
        return this._text + this.children.map((child) => child.textContent).join("");
    }
    append(...children) {
        children.forEach((child) => this.insertBefore(child, null));
    }
    insertBefore(child, reference) {
        child.remove();
        const index = reference ? this.children.indexOf(reference) : this.children.length;
        this.children.splice(index, 0, child);
        child.parent = this;
    }
    remove() {
        if (this.parent) {
            this.parent.children.splice(this.parent.children.indexOf(this), 1);
            this.parent = null;
        }
    }
    replaceChildren(...children) {
        this.children.forEach((child) => { child.parent = null; });
        this.children = [];
        this.append(...children);
    }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); delete this[name]; }
    getAttribute(name) { return this.attributes.get(name); }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    closest() { return null; }
    contains(element) {
        return element === this || this.children.some((child) => child.contains(element));
    }
    focus() { this.focused = true; this.focusCount = (this.focusCount || 0) + 1; }
}

const payload = (items, overrides = {}) => ({
    notifications: [{ id: 1, title: "Earlier failed sync", state: "error", read: true }],
    unread_count: 0,
    refresh: {},
    schedule: { enabled: false },
    repositoryStatuses: {
        items,
        total: items.length,
        activeCount: items.filter((item) => item.statusTone === "progress").length,
        failedCount: items.filter((item) => item.status === "failed").length,
    },
    ...overrides,
});

const workerTiming = (overrides = {}) => ({
    startedAt: "2026-08-30T11:57:55Z",
    observedAt: "2026-08-30T12:00:00Z",
    label: "Downloading",
    kind: "sync",
    ...overrides,
});

const timerFor = (page, index = 0) => page.hooks.get("repository-list")
    .children[index].children[0].children[0].children[4];

test("running workers tick locally and keep elapsed time across polling and phases", async () => {
    const page = await boot(
        payload([repository(1, { workerTiming: workerTiming() })]),
        payload([repository(1, { workerTiming: workerTiming({ label: "Updating catalogue", observedAt: "2026-08-30T12:00:03Z" }) })]),
    );
    const timer = timerFor(page);
    assert.equal(timer.hidden, false);
    assert.equal(timer.textContent, "Downloading · 02:05");
    assert.equal(timer.getAttribute("aria-live"), "off");
    assert.equal(page.hooks.get("repository-list").children[0].children[0].children[0].getAttribute("aria-describedby"), timer.id);
    assert.equal(page.activeClocks(), 1);
    const requests = page.requests.length;
    page.advance(3000);
    assert.equal(timer.textContent, "Downloading · 02:08");
    assert.equal(page.requests.length, requests, "Clock ticks never fetch or start jobs");
    await page.poll();
    assert.equal(timerFor(page), timer);
    assert.equal(timer.textContent, "Updating catalogue · 02:08");
    assert.equal(page.activeClocks(), 1);
});

test("SSR sidebar and status share one clock and render durable time after reload", async () => {
    const sidebar = new Element("small");
    sidebar.hidden = true;
    const timing = workerTiming();
    sidebar.dataset = {
        workerStartedAt: timing.startedAt, workerObservedAt: timing.observedAt,
        workerLabel: timing.label, workerKind: timing.kind,
    };
    const page = await bootWithTimers([sidebar], payload([
        repository(1, { workerTiming: timing }),
        repository(2, { workerTiming: workerTiming({ kind: "indexing", label: "Indexing PDFs" }) }),
    ]));
    assert.equal(sidebar.textContent, "Downloading · 02:05");
    assert.equal(sidebar.hidden, false);
    assert.equal(page.activeClocks(), 1);
    page.advance(5000);
    assert.equal(sidebar.textContent, "Downloading · 02:10");
    assert.equal(timerFor(page).textContent, sidebar.textContent);
    assert.equal(timerFor(page, 1).textContent, "Indexing PDFs · 02:10");
    assert.match(timerFor(page, 1).title, /longest-running current PDF indexing worker/);
});

test("queued, missing, malformed and future worker starts never create timers", async () => {
    const page = await boot(payload([
        repository(1, { status: "queued", statusTone: "progress", workerTiming: null }),
        repository(2),
        repository(3, { workerTiming: workerTiming({ startedAt: "invalid" }) }),
        repository(4, { workerTiming: workerTiming({ startedAt: "2026-08-30T12:01:00Z" }) }),
        repository(5, { workerTiming: workerTiming({ observedAt: null }) }),
        repository(6, { workerTiming: workerTiming({ kind: "unknown" }) }),
    ]));
    for (let index = 0; index < 6; index += 1) {
        assert.equal(timerFor(page, index).hidden, true);
        assert.equal(timerFor(page, index).textContent, "");
    }
    assert.equal(page.activeClocks(), 0);
});

test("completion, failure, removal and a new attempt clear or restart the current-worker timer", async () => {
    const page = await boot(
        payload([repository(1, { workerTiming: workerTiming() }), repository(2, { workerTiming: workerTiming() })]),
        payload([repository(1, { workerTiming: null })]),
        payload([repository(1, { workerTiming: workerTiming({ startedAt: "2026-08-30T11:59:59Z" }) })]),
        payload([repository(1, { status: "failed", workerTiming: null })]),
    );
    const removed = timerFor(page, 1);
    await page.poll();
    assert.equal(timerFor(page).hidden, true);
    assert.equal(page.hooks.get("repository-list").children[0].children[0].children[0].getAttribute("aria-describedby"), undefined);
    assert.equal(removed.hidden, true);
    assert.equal(page.activeClocks(), 0);
    await page.poll();
    assert.equal(timerFor(page).textContent, "Downloading · 00:01");
    page.advance(2000);
    assert.equal(timerFor(page).textContent, "Downloading · 00:03");
    await page.poll();
    assert.equal(timerFor(page).hidden, true);
    assert.equal(page.activeClocks(), 0);
});

test("polling failures freeze at last confirmed elapsed time and recovery resumes it", async () => {
    const page = await boot(
        payload([repository(1, { workerTiming: workerTiming() })]),
        new Error("offline"),
        payload([repository(1, { workerTiming: workerTiming({ observedAt: "2026-08-30T12:00:12Z" }) })]),
    );
    page.advance(2000);
    await page.poll();
    const timer = timerFor(page);
    assert.equal(timer.textContent, "Downloading · 02:05 (last check)");
    assert.equal(page.activeClocks(), 0);
    page.advance(10000);
    assert.equal(timer.textContent, "Downloading · 02:05 (last check)");
    await page.poll();
    assert.equal(timer.textContent, "Downloading · 02:17");
    assert.equal(page.activeClocks(), 1);
});

test("missing snapshots and suspended polling mark worker timing as stale", async () => {
    const page = await boot(
        payload([repository(1, { workerTiming: workerTiming() })]),
        payload([], { repositoryStatuses: null }),
        payload([repository(1, { workerTiming: workerTiming() })]),
    );
    await page.poll();
    assert.match(timerFor(page).textContent, /last check/);
    await page.poll();
    page.advance(46000);
    assert.equal(timerFor(page).textContent, "Downloading · 02:05 (last check)");
    assert.equal(page.activeClocks(), 0);
});

test("long-running workers use hours without wrapping after 24 hours", async () => {
    const page = await boot(payload([
        repository(1, { workerTiming: workerTiming({ startedAt: "2026-08-30T10:58:59Z" }) }),
        repository(2, { workerTiming: workerTiming({ startedAt: "2026-08-29T10:00:00Z" }) }),
    ]));
    assert.equal(timerFor(page).textContent, "Downloading · 01:01:01");
    assert.equal(timerFor(page, 1).textContent, "Downloading · 26:00:00");
});

const repository = (id, overrides = {}) => ({
    id,
    name: `repository-${id}`,
    status: "ready",
    statusLabel: "Ready",
    statusTone: "success",
    updatedAt: "2026-08-30T11:00:00Z",
    lastSuccessAt: "2026-08-30T11:00:00Z",
    lastOutcome: "succeeded",
    lastOutcomeAt: "2026-08-30T11:00:00Z",
    detail: "Last sync completed successfully.",
    targetPath: `/pdfs/?repository=${id}`,
    statusTargetPath: "/pdfs/status/",
    cancelIndexingUrl: `/pdfs/repositories/${id}/indexing/cancel/`,
    ...overrides,
});

const payloadWithActivities = (items, activities) => payload(items, {
    repositoryStatuses: {
        items,
        total: items.length,
        activeCount: items.filter((item) => item.statusTone === "progress").length,
        failedCount: items.filter((item) => item.status === "failed").length,
        activities,
    },
});

const boot = (...responses) => bootWithTimers([], ...responses);

async function bootWithTimers(initialTimers, ...responses) {
    return bootPage(initialTimers, [], ...responses);
}

const bootWithLogs = (logResponses, ...responses) => bootPage([], logResponses, ...responses);

async function bootPage(initialTimers, logResponses, ...responses) {
    const center = new Element();
    center.dataset.notificationsUrl = "/bookmarks/notifications/";
    const statusCenter = new Element();
    const hooks = new Map();
    const notificationHooks = [
        "toggle", "panel", "badge", "list", "empty", "live", "read-all", "unread-label",
    ];
    const statusHooks = [
        "background-state", "progress-card", "progress-label",
        "progress-detail", "progress", "schedule", "next-run", "last-attempt",
        "last-success", "retry-row", "retry", "repository-list", "repository-count",
        "repository-message",
    ];
    [...notificationHooks, ...statusHooks, "status-toggle", "status-panel", "status-indicator", "status-live",
        "status-idle-icon", "status-activities"]
        .forEach((name) => hooks.set(name, new Element()));
    const statusActivityNodes = new Map(["clone", "pull", "indexing"].map((operation) => {
        const element = new Element("span");
        const progress = new Element("span");
        const timer = new Element("small");
        element.dataset.repositoryStatusActivity = operation;
        element.hidden = true;
        timer.dataset.workerCompact = "true";
        timer.hidden = true;
        element.append(progress, timer);
        element.querySelector = (selector) => selector === "[data-repository-status-activity-progress]"
            ? progress : selector === "[data-repository-worker-timer]" ? timer : null;
        return [operation, { element, progress, timer }];
    }));
    hooks.get("status-activities").hidden = true;
    statusActivityNodes.forEach(({ element }) => hooks.get("status-activities").append(element));
    notificationHooks.forEach((name) => center.append(hooks.get(name)));
    [...statusHooks, "status-toggle", "status-panel", "status-indicator", "status-live",
        "status-idle-icon", "status-activities"]
        .forEach((name) => statusCenter.append(hooks.get(name)));
    hooks.get("panel").hidden = true;
    hooks.get("status-panel").hidden = true;
    center.querySelector = (selector) => {
        const name = selector.replace("[data-notification-", "").replace("]", "");
        return notificationHooks.includes(name) ? hooks.get(name) : null;
    };
    statusCenter.querySelector = (selector) => {
        const name = selector.startsWith("[data-repository-status-")
            ? selector.replace("[data-repository-status-", "status-").replace("]", "")
            : selector.replace("[data-notification-", "").replace("]", "");
        return notificationHooks.includes(name) ? null : hooks.get(name) || null;
    };
    statusCenter.querySelectorAll = (selector) => selector === "[data-repository-status-activity]"
        ? [...statusActivityNodes.values()].map(({ element }) => element) : [];
    const timers = [];
    const timeoutDelays = new Map();
    const activeTimeouts = new Set();
    const intervals = [];
    const intervalDelays = new Map();
    const activeIntervals = new Set();
    let clock = 0;
    const requests = [];
    const documentListeners = new Map();
    const windowListeners = new Map();
    const document = {
        body: new Element("body"),
        documentElement: new Element("html"),
        querySelector: (selector) => selector === "[data-repository-status-center]" ? statusCenter : null,
        querySelectorAll: (selector) => selector === "[data-notification-center]" ? [center]
            : selector === "[data-repository-worker-timer]" ? initialTimers : [],
        addEventListener(name, fn) {
            documentListeners.set(name, [...(documentListeners.get(name) || []), fn]);
        },
        createElement: (tag) => new Element(tag),
    };
    let lastResponse;
    let lastLogResponse;
    const confirmations = [];
    const confirmationAnswers = [];
    const window = {
        localStorage: { getItem() { return null; }, setItem() {} },
        matchMedia: () => ({ matches: false }),
        setTimeout(fn, delay) {
            timers.push(fn);
            timeoutDelays.set(timers.length, delay);
            activeTimeouts.add(timers.length);
            return timers.length;
        },
        clearTimeout(id) { activeTimeouts.delete(id); },
        setInterval(fn, delay) {
            intervals.push(fn);
            intervalDelays.set(intervals.length, delay);
            activeIntervals.add(intervals.length);
            return intervals.length;
        },
        clearInterval(id) { activeIntervals.delete(id); },
        performance: { now: () => clock },
        addEventListener(name, fn) {
            windowListeners.set(name, [...(windowListeners.get(name) || []), fn]);
        },
        dispatchEvent() {},
        confirm(message) {
            confirmations.push(message);
            return confirmationAnswers.length ? confirmationAnswers.shift() : false;
        },
        AbortController,
        async fetch(url, options) {
            requests.push({ url, options });
            if (options.method === "POST") {
                return { ok: true, async json() { return {}; } };
            }
            if (url.endsWith("/logs/")) {
                let response = logResponses.length ? logResponses.shift() : lastLogResponse;
                if (typeof response === "function") { response = await response(options); }
                if (response instanceof Error) { throw response; }
                lastLogResponse = response;
                return { ok: true, async json() { return response; } };
            }
            const response = responses.length ? responses.shift() : lastResponse;
            if (response instanceof Error) { throw response; }
            lastResponse = response;
            return { ok: true, async json() { return response; } };
        },
    };
    vm.runInNewContext(source, { window, document, URLSearchParams, CustomEvent: class {} });
    const flush = () => new Promise(setImmediate);
    await flush();
    return {
        hooks,
        center,
        statusCenter,
        statusActivityNodes,
        requests,
        intervals,
        confirmations,
        confirmNext(answer = true) { confirmationAnswers.push(answer); },
        flush,
        logRequests: () => requests.filter(({ url }) => url.endsWith("/logs/")),
        activeLogPolls: () => [...activeTimeouts].filter((id) => timers[id - 1].name === "synchronizeGitLogs").length,
        workerTimers: window.OWLRepositoryTimers,
        activeClocks: () => [...activeIntervals].filter((id) => intervalDelays.get(id) === 1000).length,
        advance(milliseconds) {
            clock += milliseconds;
            [...activeIntervals].forEach((id) => {
                if (intervalDelays.get(id) === 1000) { intervals[id - 1](); }
            });
        },
        async click(name) {
            const target = hooks.get(name);
            await target.listeners.get("click")?.({ target });
            documentListeners.get("click")?.forEach((fn) => fn({ target }));
            await flush();
        },
        escape() {
            documentListeners.get("keydown")?.forEach((fn) => fn({ key: "Escape", preventDefault() {} }));
        },
        outsideClick() {
            const target = new Element("button");
            documentListeners.get("click")?.forEach((fn) => fn({ target }));
        },
        async poll() {
            const load = timers.findLast((fn) => fn.name === "load");
            assert.ok(load, "A status poll is scheduled");
            await load();
            await flush();
        },
        async toggle(element, open) {
            element.open = open;
            element.listeners.get("toggle")?.();
            await flush();
        },
        async visibility(hidden) {
            document.hidden = hidden;
            documentListeners.get("visibilitychange")?.forEach((fn) => fn());
            await flush();
        },
        async windowEvent(name) {
            windowListeners.get(name)?.forEach((fn) => fn());
            await flush();
        },
        async pollLogs() {
            const id = [...activeTimeouts].find((value) => timers[value - 1].name === "synchronizeGitLogs");
            assert.ok(id, "An open live Git log has a poll scheduled");
            assert.ok(timeoutDelays.get(id) <= 2000, "Git logs poll about every two seconds");
            clock += timeoutDelays.get(id);
            activeTimeouts.delete(id);
            timers[id - 1]();
            await flush();
        },
        async expireLogRequest() {
            const id = [...activeTimeouts].find((value) => timeoutDelays.get(value) === 10000);
            assert.ok(id, "A pending Git request has a bounded timeout");
            activeTimeouts.delete(id);
            timers[id - 1]();
            await flush();
        },
    };
}

test("all repositories render compact disclosure rows without hiding past alerts", async () => {
    const items = Array.from({ length: 24 }, (_, index) => repository(index + 1));
    const { hooks } = await boot(payload(items));
    const list = hooks.get("repository-list");
    assert.equal(list.children.length, 24);
    assert.equal(list.hidden, false);
    assert.equal(list.getAttribute("aria-busy"), "false");
    assert.equal(hooks.get("repository-count").textContent, "24 repositories");
    assert.equal(hooks.get("repository-message").hidden, true);
    assert.match(hooks.get("list").textContent, /Earlier failed sync/);
    const row = list.children[23];
    assert.equal(row.dataset.tone, "success");
    assert.equal(row.children[0].tagName, "details");
    assert.equal(row.children[0].open, false);
    assert.match(row.children[0].children[0].getAttribute("aria-label"), /repository-24: Ready/);
    assert.match(row.textContent, /Last completed sync: Succeeded/);
});

test("polling updates rows while preserving expanded state, focus and history nodes", async () => {
    const failed = repository(1, { status: "failed", statusLabel: "Failed", statusTone: "error" });
    const page = await boot(payload([failed]), payload([repository(1)]));
    const row = page.hooks.get("repository-list").children[0];
    const details = row.children[0];
    details.open = true;
    const summary = details.children[0];
    summary.focus();
    const history = page.hooks.get("list").children[0];
    await page.poll();
    assert.equal(page.hooks.get("repository-list").children[0], row);
    assert.equal(details.open, true);
    assert.equal(details.children[0], summary);
    assert.equal(summary.focused, true);
    assert.equal(row.dataset.tone, "success");
    assert.equal(page.hooks.get("list").children[0], history);
});

test("fetch errors keep known rows but visibly mark them stale, then recover", async () => {
    const active = repository(1, { status: "cloning", statusLabel: "Cloning", statusTone: "progress" });
    const page = await boot(payload([active]), new Error("offline"), payload([repository(1)]));
    await page.poll();
    assert.equal(page.hooks.get("repository-list").children.length, 1);
    assert.equal(page.hooks.get("repository-list").dataset.stale, "true");
    assert.equal(page.hooks.get("repository-message").hidden, false);
    assert.match(page.hooks.get("repository-message").textContent, /showing the last check/);
    await page.poll();
    assert.equal(page.hooks.get("repository-list").dataset.stale, "false");
    assert.equal(page.hooks.get("repository-message").hidden, true);
});

test("empty and unavailable snapshots are different states", async () => {
    const empty = await boot(payload([]));
    assert.equal(empty.hooks.get("repository-list").hidden, true);
    assert.equal(empty.hooks.get("repository-message").textContent, "No repositories added.");
    const unavailable = await boot(payload([], { repositoryStatuses: null }));
    assert.match(unavailable.hooks.get("repository-message").textContent, /temporarily unavailable/);
});

test("repository names stay text and unsafe navigation targets are disabled", async () => {
    const name = "<img src=x onerror=alert(1)>";
    const { hooks } = await boot(payload([repository(1, {
        name,
        targetPath: "/\\external.example/",
        statusTargetPath: "//external.example/",
    })]));
    const details = hooks.get("repository-list").children[0].children[0];
    assert.equal(details.children[0].children[1].textContent, name);
    const body = details.children[1];
    assert.equal(body.children.at(-2).hidden, true);
    assert.equal(body.children.at(-1).hidden, true);
});

test("repository logs open separately from notifications and switching panels never marks alerts read", async () => {
    const page = await boot(payload([repository(1)], { unread_count: 3 }));
    const { hooks } = page;
    assert.equal(page.center.contains(hooks.get("repository-list")), false);
    assert.equal(page.statusCenter.contains(hooks.get("repository-list")), true);
    assert.equal(page.statusCenter.contains(hooks.get("list")), false);
    await page.click("status-toggle");
    assert.equal(hooks.get("status-panel").hidden, false);
    assert.equal(hooks.get("panel").hidden, true);
    assert.match(hooks.get("status-toggle").getAttribute("aria-label"), /^Close repository logs/);
    assert.equal(hooks.get("badge").textContent, "3");
    await page.click("toggle");
    assert.equal(hooks.get("status-panel").hidden, true);
    assert.equal(hooks.get("panel").hidden, false);
    assert.equal(hooks.get("status-toggle").focusCount || 0, 0);
    await page.click("status-toggle");
    assert.equal(hooks.get("panel").hidden, true);
    assert.equal(hooks.get("status-panel").hidden, false);
    assert.equal(hooks.get("toggle").focusCount || 0, 0);
    assert.equal(page.requests.length, 4, "Initial shared poll plus one GET per opened panel");
    assert.ok(page.requests.every(({ options }) => options.method === "GET"));
    assert.equal(page.intervals.length, 1, "Only one existing scheduler timer is installed");
});

test("Escape restores the correct icon focus, outside clicks do not steal focus", async () => {
    const page = await boot(payload([repository(1)]));
    await page.click("status-toggle");
    page.escape();
    assert.equal(page.hooks.get("status-panel").hidden, true);
    assert.equal(page.hooks.get("status-toggle").focusCount, 1);
    await page.click("toggle");
    page.escape();
    assert.equal(page.hooks.get("panel").hidden, true);
    assert.equal(page.hooks.get("toggle").focusCount, 1);
    await page.click("status-toggle");
    page.outsideClick();
    assert.equal(page.hooks.get("status-panel").hidden, true);
    assert.equal(page.hooks.get("status-toggle").focusCount, 1);
});

test("background indicator tracks current status independently of unread alerts and old failures", async () => {
    const page = await boot(
        payload([repository(1, { status: "cloning", statusTone: "progress" })]),
        payload([repository(1, { status: "failed", statusTone: "error" })]),
        payload([repository(1)]),
        new Error("offline"),
        payload([]),
    );
    assert.equal(page.statusCenter.dataset.state, "active");
    assert.equal(page.hooks.get("badge").hidden, true);
    assert.equal(page.hooks.get("toggle").getAttribute("aria-label"), "Open notifications");
    assert.match(page.hooks.get("status-toggle").title, /1 repository active/);
    await page.poll();
    assert.equal(page.statusCenter.dataset.state, "error");
    await page.poll();
    assert.equal(page.statusCenter.dataset.state, "ready");
    assert.match(page.hooks.get("list").textContent, /Earlier failed sync/);
    await page.poll();
    assert.equal(page.statusCenter.dataset.state, "unknown");
    await page.poll();
    assert.equal(page.statusCenter.dataset.state, "neutral");
});

test("clone, pull and indexing render together with honest progress, compact timers and operation-specific rows", async () => {
    const cloneTiming = workerTiming({ operation: "clone", label: "Cloning repository" });
    const indexingTiming = workerTiming({
        operation: "indexing", kind: "indexing", label: "Indexing PDFs",
        startedAt: "2026-08-30T11:59:00Z",
    });
    const items = [
        repository(1, {
            status: "cloning", statusLabel: "Downloading repository", statusTone: "progress",
            operation: "clone", progress: 37, phaseLabel: "Receiving Git objects",
            activity: { state: "running" }, workerTiming: cloneTiming,
        }),
        repository(2, {
            status: "queued", statusLabel: "Queued", statusTone: "progress",
            operation: "pull", progress: null, phaseLabel: "Git pull queued",
            activity: { state: "queued" }, workerTiming: null,
        }),
        repository(3, {
            status: "indexing", statusLabel: "Reading PDF text", statusTone: "progress",
            operation: "indexing", progress: null, phaseLabel: "Extracting PDF text",
            activity: { state: "running" }, workerTiming: indexingTiming,
        }),
    ];
    const activities = [
        {
            active: true, operation: "clone", state: "running", progress: 37,
            label: "Git clone", detail: "Receiving Git objects",
            startedAt: cloneTiming.startedAt, observedAt: cloneTiming.observedAt,
        },
        {
            active: true, operation: "pull", state: "queued", progress: null,
            label: "Git pull", detail: "Waiting for a Git worker",
            startedAt: null, observedAt: cloneTiming.observedAt,
        },
        {
            active: true, operation: "indexing", state: "running", progress: null,
            label: "PDF indexing", detail: "Two PDF workers active",
            startedAt: indexingTiming.startedAt, observedAt: indexingTiming.observedAt,
        },
    ];
    const page = await boot(
        payloadWithActivities(items, activities),
        new Error("offline"),
        payloadWithActivities(items.map((item) => repository(item.id)), []),
    );

    assert.equal(page.hooks.get("status-idle-icon").hidden, true);
    assert.equal(page.hooks.get("status-activities").hidden, false);
    assert.equal(page.hooks.get("status-activities").dataset.stale, "false");
    const clone = page.statusActivityNodes.get("clone");
    const pull = page.statusActivityNodes.get("pull");
    const indexing = page.statusActivityNodes.get("indexing");
    assert.deepEqual(
        [clone.element.hidden, pull.element.hidden, indexing.element.hidden],
        [false, false, false],
    );
    assert.deepEqual(
        [clone.progress.textContent, pull.progress.textContent, indexing.progress.textContent],
        ["37%", "Queued", "Running"],
    );
    assert.equal(clone.timer.textContent, "02:05");
    assert.equal(pull.timer.hidden, true, "Queued work never fabricates a start time");
    assert.equal(indexing.timer.textContent, "01:00");
    assert.match(clone.element.getAttribute("aria-label"), /Git clone, 37%/);
    assert.match(pull.element.getAttribute("aria-label"), /Git pull, Queued/);
    assert.match(indexing.element.getAttribute("aria-label"), /PDF indexing, Running/);

    const rows = page.hooks.get("repository-list").children;
    assert.deepEqual(rows.map((row) => row.dataset.operation), ["clone", "pull", "indexing"]);
    assert.deepEqual(
        rows.map((row) => row.children[0].children[0].children[0].textContent),
        ["↓", "↻", "⌕"],
    );
    assert.deepEqual(
        rows.map((row) => row.children[0].children[0].children[2].textContent),
        ["Clone · 37%", "Pull · Queued", "Index · Running"],
    );

    page.advance(3000);
    assert.equal(clone.timer.textContent, "02:08");
    assert.equal(indexing.timer.textContent, "01:03");
    await page.poll();
    assert.equal(page.hooks.get("status-activities").dataset.stale, "true");
    assert.equal(clone.timer.textContent, "02:05 · last");
    assert.equal(indexing.timer.textContent, "01:00 · last");
    assert.equal(page.activeClocks(), 0, "An outage freezes every compact activity timer");

    await page.poll();
    assert.equal(page.hooks.get("status-activities").hidden, true);
    assert.equal(page.hooks.get("status-idle-icon").hidden, false);
    assert.deepEqual(
        [clone.element.hidden, pull.element.hidden, indexing.element.hidden],
        [true, true, true],
    );
    assert.deepEqual([clone.timer.hidden, pull.timer.hidden, indexing.timer.hidden], [true, true, true]);
});

test("Confluence activity and retries belong to status, not the notification badge", async () => {
    const page = await boot(
        payload([], { refresh: { active: true, processed: 2, total: 4 } }),
        payload([], { schedule: { retrying: true }, refresh: { status: "failed" } }),
    );
    assert.equal(page.statusCenter.dataset.state, "active");
    assert.equal(page.hooks.get("progress-card").hidden, false);
    assert.match(page.hooks.get("progress-detail").textContent, /2 of 4 pages/);
    assert.equal(page.hooks.get("badge").hidden, true);
    await page.poll();
    assert.equal(page.statusCenter.dataset.state, "error");
    assert.equal(page.hooks.get("retry-row").hidden, false);
});

test("mark all read changes only notification state, not repository attention", async () => {
    const page = await boot(payload([repository(1, { status: "failed", statusTone: "error" })], { unread_count: 2 }));
    page.center.dataset.readAllUrl = "/bookmarks/notifications/read-all/";
    await page.click("read-all");
    assert.equal(page.hooks.get("badge").hidden, true);
    assert.equal(page.statusCenter.dataset.state, "error");
    assert.equal(page.requests.filter(({ options }) => options.method === "POST").length, 1);
});

const logRepository = (overrides = {}) => repository(1, {
    logsUrl: "/pdfs/repositories/1/logs/", ...overrides,
});

const previewFor = (page, index = 0) => page.hooks.get("repository-list")
    .children[index].children[0].children[0].children[5];

test("status shows the latest two Git lines even with repository details collapsed", async () => {
    const page = await boot(payload([logRepository({
        status: "refreshing", statusTone: "progress",
        logPreview: ["older line", "12:00:00 UTC INFO [fetch] Receiving objects: 25%", "12:00:01 UTC WARNING [fetch] Slow connection"],
    })]));
    await page.click("status-toggle");
    const row = page.hooks.get("repository-list").children[0];
    const preview = previewFor(page);
    assert.equal(row.children[0].open, false);
    assert.equal(preview.hidden, false);
    assert.equal(preview.children.length, 2);
    assert.equal(preview.children[0].textContent, "Receiving objects: 25%");
    assert.equal(preview.children[1].textContent, "WARNING: Slow connection");
    assert.equal(preview.children[0].title, "12:00:00 UTC INFO [fetch] Receiving objects: 25%");
    assert.equal(preview.getAttribute("aria-live"), "off");
    assert.match(preview.getAttribute("aria-label"), /Latest Git output for repository-1/);
    assert.equal(row.children[0].children[0].getAttribute("aria-describedby"), preview.id);
    assert.equal(page.logRequests().length, 0, "Preview uses the shared snapshot, not individual log requests");
});

test("live preview replaces old lines in place and preserves status focus", async () => {
    const page = await boot(
        payload([logRepository({ status: "cloning", logPreview: ["Receiving objects: 10%"] })]),
        payload([logRepository({ status: "cloning", logPreview: ["Receiving objects: 50%", "Resolving deltas: 20%"] })]),
        payload([logRepository({ logPreview: ["Git update complete."] })]),
    );
    const preview = previewFor(page);
    const line = preview.children[0];
    const summary = preview.parent;
    summary.focus();
    await page.poll();
    assert.equal(previewFor(page), preview);
    assert.equal(preview.children[0], line);
    assert.equal(line.textContent, "Receiving objects: 50%");
    assert.equal(preview.children[1].textContent, "Resolving deltas: 20%");
    assert.equal(summary.focusCount, 1);
    await page.poll();
    assert.equal(line.textContent, "Git update complete.");
    assert.equal(preview.children[1].hidden, true);
    assert.equal(preview.children[1].textContent, "");
    assert.equal(page.logRequests().length, 0);
});

test("empty current jobs clear previous output and inactive empty previews stay hidden", async () => {
    const page = await boot(
        payload([logRepository({ logPreview: ["Previous run failed"] })]),
        payload([logRepository({ status: "queued", logPreview: [] })]),
        payload([logRepository({ status: "checking_connection", logPreview: [] })]),
        payload([logRepository({ logPreview: [] })]),
    );
    await page.poll();
    assert.equal(previewFor(page).children[0].textContent, "Waiting for Git worker…");
    assert.doesNotMatch(previewFor(page).textContent, /Previous run/);
    await page.poll();
    assert.equal(previewFor(page).children[0].textContent, "Waiting for Git output…");
    await page.poll();
    assert.equal(previewFor(page).hidden, true);
    assert.equal(previewFor(page).textContent, "");
});

test("preview remains bounded plain text for malformed or oversized snapshots", async () => {
    const page = await boot(payload([
        logRepository({ logPreview: [null, {}, "", "<img src=x onerror=alert(1)>", "x".repeat(2500) + "\nextra"] }),
        repository(2, { logPreview: "wrong type" }),
    ]));
    const preview = previewFor(page);
    assert.equal(preview.children[0].textContent, "<img src=x onerror=alert(1)>");
    assert.equal(preview.children[0].children.length, 0);
    assert.equal(preview.children[1].textContent.length, 1024);
    assert.equal(preview.children[1].title.length, 1024);
    assert.equal(previewFor(page, 1).hidden, true);
});

test("a failed status poll keeps the last preview with the existing stale warning", async () => {
    const page = await boot(payload([logRepository({ logPreview: ["Receiving objects: 10%"] })]), new Error("offline"));
    await page.poll();
    assert.equal(previewFor(page).children[0].textContent, "Receiving objects: 10%");
    assert.equal(page.hooks.get("repository-list").dataset.stale, "true");
    assert.match(page.hooks.get("repository-message").textContent, /showing the last check/);
});

const gitOutput = (overrides = {}) => ({
    repositoryId: 1, jobId: 10, status: "running", operation: "clone",
    phase: "cloning", log: "Receiving objects: 25%", truncated: false,
    updatedAt: "2026-08-30T12:00:00Z", ...overrides,
});

const indexingOutput = (overrides = {}) => gitOutput({
    status: "succeeded", operation: "refresh", log: "Git refresh complete.",
    indexing: {
        workerLimit: 4,
        active: true,
        counts: {
            total: 5006, queued: 4996, running: 4, succeeded: 0,
            failed: 2, interrupted: 1, cancelled: 3,
        },
        lines: [
            "12:00:00 UTC INFO [validating] Validating the PDF extraction target. · docs/a.pdf · 5% · worker 101",
            "12:00:01 UTC INFO [extracting] Extracting searchable PDF text. · docs/a.pdf · 60% · worker 101",
        ],
        truncated: false, updatedAt: "2026-08-30T12:00:01Z",
        ...overrides,
    },
});

const logFor = (page, index = 0) => {
    const details = page.hooks.get("repository-list").children[index].children[0];
    const log = details.children[1].children.find((child) => child.className === "notification-repository__git-log");
    return {
        details, log, summary: log.children[0], status: log.children[0].children[1],
        message: log.children[1], output: log.children[2], truncated: log.children[3],
    };
};

const indexLogFor = (page, index = 0) => {
    const details = page.hooks.get("repository-list").children[index].children[0];
    const log = details.children[1].children.find((child) => child.className.includes("notification-repository__index-log"));
    return {
        details, log, summary: log.children[0], status: log.children[0].children[1],
        message: log.children[1], output: log.children[2], truncated: log.children[3],
        stop: log.children[4],
    };
};

async function openLog(page, index = 0) {
    if (page.hooks.get("status-panel").hidden) { await page.click("status-toggle"); }
    const elements = logFor(page, index);
    await page.toggle(elements.details, true);
    await page.toggle(elements.log, true);
    return elements;
}

async function openIndexLog(page, index = 0) {
    if (page.hooks.get("status-panel").hidden) { await page.click("status-toggle"); }
    const elements = indexLogFor(page, index);
    await page.toggle(elements.details, true);
    await page.toggle(elements.log, true);
    return elements;
}

test("Git output loads only inside an open visible Background status disclosure", async () => {
    const page = await bootWithLogs([gitOutput()], payload([logRepository(), repository(2, {
        logsUrl: "/pdfs/repositories/2/logs/",
    })]));
    assert.equal(page.logRequests().length, 0);
    await page.click("toggle");
    assert.equal(page.logRequests().length, 0, "Opening alert history never loads Git output");
    await page.click("status-toggle");
    const elements = logFor(page);
    assert.equal(page.logRequests().length, 0);
    await page.toggle(elements.details, true);
    assert.equal(page.logRequests().length, 0, "The nested Git log remains opt-in");
    await page.toggle(elements.log, true);
    assert.equal(page.logRequests().length, 1);
    assert.equal(page.logRequests()[0].url, "/pdfs/repositories/1/logs/");
    assert.equal(page.logRequests()[0].options.cache, "no-store");
    assert.equal(page.logRequests()[0].options.redirect, "error");
    assert.ok(page.requests.every(({ options }) => options.method === "GET"));
    assert.equal(page.intervals.length, 1, "No additional scheduler is installed");
});

test("Git output is inert text in a keyboard-focusable console with the supplied connection label", async () => {
    const literal = '<img src=x onerror="throw 1">\n<script>throw 2</script>';
    const page = await bootWithLogs([gitOutput({ log: literal })], payload([logRepository({
        name: "<repository>", status: "checking_connection", statusLabel: "Checking connection",
        statusTone: undefined,
    })]));
    const elements = await openLog(page);
    assert.equal(elements.log.tagName, "details");
    assert.equal(elements.summary.tagName, "summary");
    assert.equal(elements.summary.getAttribute("aria-label"), "Git log for <repository>");
    assert.equal(elements.output.tagName, "pre");
    assert.equal(elements.output.tabIndex, 0);
    assert.equal(elements.output.getAttribute("aria-live"), "off");
    assert.equal(elements.output.textContent, literal);
    assert.equal(elements.output.children.length, 0);
    assert.equal(elements.status.textContent, "Clone · Live");
    assert.match(elements.details.children[0].textContent, /Checking connection/);
    assert.equal(page.hooks.get("repository-list").children[0].dataset.tone, "progress");
});

test("PDF indexing log shows bounded parallel-worker progress and keeps polling after Git completes", async () => {
    const page = await bootWithLogs([indexingOutput()], payload([logRepository({
        status: "indexing", statusLabel: "Reading PDF text", statusTone: "progress",
    })]));
    const elements = await openIndexLog(page);
    assert.equal(elements.summary.getAttribute("aria-label"), "PDF indexing log for repository-1");
    assert.equal(elements.output.tagName, "pre");
    assert.equal(elements.output.tabIndex, 0);
    assert.equal(elements.output.getAttribute("aria-live"), "off");
    assert.match(elements.output.textContent, /docs\/a\.pdf/);
    assert.match(elements.output.textContent, /Extracting searchable PDF text/);
    assert.equal(
        elements.status.textContent,
        "4996 queued · 4 running of 4 · 0 succeeded attempts · 2 failed · 1 interrupted · 3 cancelled",
    );
    assert.equal(elements.truncated.hidden, true);
    assert.equal(elements.stop.hidden, false);
    assert.equal(elements.stop.getAttribute("aria-label"), "Stop PDF indexing for repository-1");
    assert.equal(page.activeLogPolls(), 1, "Indexing activity keeps the log live after Git is complete");
});

test("PDF indexing can be stopped directly from the repository log popup", async () => {
    const stopped = indexingOutput({
        active: false,
        counts: {
            total: 5006, queued: 0, running: 0, succeeded: 0,
            failed: 2, interrupted: 1, cancelled: 5003,
        },
    });
    const page = await bootWithLogs(
        [indexingOutput(), stopped],
        payload([logRepository({
            status: "indexing", statusLabel: "Reading PDF text", statusTone: "progress",
        })]),
    );
    const elements = await openIndexLog(page);

    await elements.stop.listeners.get("click")({ target: elements.stop });
    await page.flush();
    assert.equal(
        page.requests.some(({ url, options }) =>
            url === "/pdfs/repositories/1/indexing/cancel/" && options.method === "POST"),
        false,
        "Declining confirmation must never cancel the repository queue",
    );

    page.confirmNext();
    await elements.stop.listeners.get("click")({ target: elements.stop });
    await page.flush();

    const request = page.requests.find(({ url, options }) =>
        url === "/pdfs/repositories/1/indexing/cancel/" && options.method === "POST");
    assert.ok(request, "The popup posts only to the selected repository cancellation endpoint");
    assert.equal(request.options.headers["X-Requested-With"], "XMLHttpRequest");
    assert.equal(String(request.options.body), "confirmed=yes");
    assert.match(page.confirmations.at(-1), /Stop all queued and currently running/);
    assert.equal(elements.stop.hidden, true);
    assert.match(elements.status.textContent, /^0 queued · 0 running/);
});

test("closing an in-flight repository log clears busy state on both consoles", async () => {
    const page = await bootWithLogs([
        (options) => new Promise((resolve) => {
            options.signal.addEventListener("abort", () => resolve(indexingOutput()));
        }),
    ], payload([logRepository()]));
    const elements = await openIndexLog(page);
    const gitElements = logFor(page);
    assert.equal(gitElements.output.getAttribute("aria-busy"), "true");
    assert.equal(elements.output.getAttribute("aria-busy"), "true");

    await page.toggle(elements.log, false);

    assert.equal(gitElements.output.getAttribute("aria-busy"), "false");
    assert.equal(elements.output.getAttribute("aria-busy"), "false");
});

test("empty Git logs distinguish not started, queued, running and completed runs", async () => {
    for (const [status, copy, terminal] of [
        ["not_started", "No Git run has started for this repository.", true],
        ["queued", "Waiting for the Git worker to start…", false],
        ["running", "Waiting for Git output…", false],
        ["succeeded", "No Git output was recorded for this run.", true],
        ["failed", "No Git output was recorded for this run.", true],
    ]) {
        const page = await bootWithLogs([gitOutput({ status, log: "", jobId: status === "not_started" ? null : 10 })], payload([logRepository()]));
        const elements = await openLog(page);
        assert.equal(elements.message.textContent, copy);
        assert.equal(elements.message.hidden, false);
        assert.equal(elements.output.hidden, true);
        assert.equal(page.activeLogPolls(), terminal ? 0 : 1);
    }
});

test("one two-second Git loop stops when the log, repository, panel or page is hidden", async () => {
    const page = await bootWithLogs([gitOutput()], payload([logRepository()]));
    const elements = await openLog(page);
    await page.pollLogs();
    assert.equal(page.logRequests().length, 2);
    await page.toggle(elements.log, false);
    assert.equal(page.activeLogPolls(), 0);
    await page.toggle(elements.log, true);
    assert.equal(page.logRequests().length, 3);
    await page.toggle(elements.details, false);
    assert.equal(page.activeLogPolls(), 0);
    await page.toggle(elements.details, true);
    assert.equal(page.logRequests().length, 4);
    await page.visibility(true);
    assert.equal(page.activeLogPolls(), 0);
    await page.visibility(false);
    assert.equal(page.logRequests().length, 5);
    await page.windowEvent("pagehide");
    assert.equal(page.activeLogPolls(), 0);
    await page.windowEvent("pageshow");
    assert.equal(page.logRequests().length, 6);
    await page.click("toggle");
    assert.equal(page.activeLogPolls(), 0, "Switching to notifications closes the log poll");
    assert.ok(page.requests.every(({ options }) => options.method === "GET"));
});

test("an offscreen Git disclosure waits for repository-list scrolling into view", async () => {
    const page = await bootWithLogs([gitOutput()], payload([logRepository()]));
    const elements = logFor(page);
    let top = 500;
    elements.log.getBoundingClientRect = () => ({ top, bottom: top + 100 });
    page.hooks.get("repository-list").getBoundingClientRect = () => ({ top: 50, bottom: 300 });
    await openLog(page);
    assert.equal(page.logRequests().length, 0);
    top = 80;
    page.hooks.get("repository-list").listeners.get("scroll")();
    await page.flush();
    assert.equal(page.logRequests().length, 1);
    top = 500;
    page.hooks.get("repository-list").listeners.get("scroll")();
    assert.equal(page.activeLogPolls(), 0);
});

test("a log clipped by the outer status panel does not poll until that panel scrolls", async () => {
    const page = await bootWithLogs([gitOutput()], payload([logRepository()]));
    const elements = logFor(page);
    let top = 250;
    elements.log.getBoundingClientRect = () => ({ top, bottom: top + 100 });
    page.hooks.get("repository-list").getBoundingClientRect = () => ({ top: 100, bottom: 350 });
    page.hooks.get("status-panel").getBoundingClientRect = () => ({ top: 50, bottom: 200 });
    await openLog(page);
    assert.equal(page.logRequests().length, 0);
    top = 150;
    page.hooks.get("status-panel").listeners.get("scroll")();
    await page.flush();
    assert.equal(page.logRequests().length, 1);
});

test("completed Git logs stop polling and an existing status update discovers the next run", async () => {
    const initial = payload([logRepository()]);
    const next = payload([logRepository({
        status: "checking_connection", statusLabel: "Checking connection",
        workerTiming: workerTiming({ startedAt: "2026-08-30T12:00:00Z" }),
    })]);
    const page = await bootWithLogs([
        gitOutput({ status: "succeeded", log: "Completed first run." }),
        gitOutput({ jobId: 11, operation: "refresh", log: "Checking remote connection…" }),
    ], initial, initial, initial, next);
    const elements = await openLog(page);
    assert.equal(page.activeLogPolls(), 0);
    await page.poll();
    assert.equal(page.logRequests().length, 1, "Unchanged terminal status does not fetch again");
    await page.poll();
    assert.equal(page.logRequests().length, 2);
    assert.equal(logFor(page).log, elements.log);
    assert.equal(elements.log.dataset.jobId, "11");
    assert.equal(elements.output.textContent, "Checking remote connection…");
    assert.equal(elements.status.textContent, "Refresh · Live");
    assert.equal(page.activeLogPolls(), 1);
});

test("Git updates preserve console focus and manual scrolling while tail readers follow new lines", async () => {
    const first = "initial\n".repeat(40);
    const page = await bootWithLogs([
        gitOutput({ log: first }), gitOutput({ log: first + "more\n".repeat(10) }),
        gitOutput({ log: first + "more\n".repeat(30) }),
    ], payload([logRepository()]));
    const elements = logFor(page);
    Object.defineProperty(elements.output, "scrollHeight", {
        get() { return this.hidden ? 0 : this.textContent.split("\n").length * 10; },
    });
    elements.output.clientHeight = 160;
    await openLog(page);
    assert.ok(elements.output.scrollTop > 0, "Initial output is revealed before scrolling to the tail");
    elements.output.focus();
    elements.output.scrollTop = 25;
    await page.pollLogs();
    assert.equal(elements.output.scrollTop, 25);
    assert.equal(elements.output.focusCount, 1);
    assert.equal(logFor(page).output, elements.output);
    elements.output.scrollTop = elements.output.scrollHeight - elements.output.clientHeight;
    await page.pollLogs();
    assert.equal(elements.output.scrollTop, elements.output.scrollHeight);
    assert.equal(elements.log.open, true);
    assert.equal(elements.details.open, true);
});

test("Git console bounds multiline output and explains truncation", async () => {
    const large = "old output\n" + "line with bounded output\r\n".repeat(1000);
    const page = await bootWithLogs([gitOutput({ log: large })], payload([logRepository()]));
    const elements = await openLog(page);
    assert.ok(elements.output.textContent.length <= 65536);
    assert.ok(elements.output.textContent.split("\n").length <= 400);
    assert.equal(elements.output.textContent.includes("old output"), false);
    assert.equal(elements.output.textContent.includes("\r"), false);
    assert.equal(elements.truncated.hidden, false);
});

test("log failures retain the last output with safe retry copy and reject mismatched payloads", async () => {
    const page = await bootWithLogs([
        gitOutput(), new Error("private diagnostic content must not render"),
        gitOutput({ repositoryId: 99, log: "wrong repository" }),
        gitOutput({ log: "Recovered" }),
    ], payload([logRepository()]));
    const elements = await openLog(page);
    await page.pollLogs();
    assert.equal(elements.output.textContent, "Receiving objects: 25%");
    assert.equal(elements.message.textContent, "Could not load Git output. Showing the last check.");
    assert.doesNotMatch(elements.log.textContent, /private diagnostic/);
    await page.pollLogs();
    assert.doesNotMatch(elements.log.textContent, /wrong repository/);
    await page.pollLogs();
    assert.equal(elements.output.textContent, "Recovered");
    assert.equal(elements.message.hidden, true);
});

test("closing cancels a Git request and an older response cannot overwrite a reopened latest run", async () => {
    let finishOld;
    const delayed = new Promise((resolve) => { finishOld = resolve; });
    const page = await bootWithLogs([
        () => delayed, gitOutput({ jobId: 12, log: "Latest run" }),
    ], payload([logRepository()]));
    const elements = await openLog(page);
    assert.equal(elements.message.textContent, "Loading Git output…");
    assert.equal(elements.output.getAttribute("aria-busy"), "true");
    await page.toggle(elements.log, false);
    assert.equal(page.logRequests()[0].options.signal.aborted, true);
    await page.toggle(elements.log, true);
    assert.equal(elements.output.textContent, "Latest run");
    finishOld(gitOutput({ log: "Stale earlier response" }));
    await page.flush();
    assert.equal(elements.output.textContent, "Latest run");
    assert.equal(elements.log.dataset.jobId, "12");
    assert.equal(elements.output.getAttribute("aria-busy"), "false");
});

test("a stalled Git output request times out safely and retries only while open", async () => {
    const page = await bootWithLogs([
        ({ signal }) => new Promise((_resolve, reject) => {
            signal.addEventListener("abort", () => reject(new Error("private timeout detail")));
        }),
        gitOutput({ log: "Recovered after timeout" }),
    ], payload([logRepository()]));
    const elements = await openLog(page);
    await page.expireLogRequest();
    assert.equal(page.logRequests()[0].options.signal.aborted, true);
    assert.equal(elements.message.textContent, "Could not load Git output. Retrying while this log is open…");
    assert.doesNotMatch(elements.log.textContent, /private timeout/);
    await page.pollLogs();
    assert.equal(elements.output.textContent, "Recovered after timeout");
});

test("unsafe log targets never fetch and removed rows cancel outstanding output requests", async () => {
    for (const logsUrl of ["https://external.example/logs/", "//external.example/logs/", "/\\external.example/logs/"]) {
        const page = await bootWithLogs([], payload([logRepository({ logsUrl })]));
        const elements = await openLog(page);
        assert.equal(page.logRequests().length, 0);
        assert.equal(elements.message.textContent, "Git output is not available for this repository.");
    }
    let finish;
    const initial = payload([logRepository()]);
    const page = await bootWithLogs([() => new Promise((resolve) => { finish = resolve; })], initial, initial, payload([]));
    await openLog(page);
    await page.poll();
    assert.equal(page.logRequests()[0].options.signal.aborted, true);
    assert.equal(page.activeLogPolls(), 0);
    finish(gitOutput());
    await page.flush();
    assert.equal(page.hooks.get("repository-list").children.length, 0);
});
