const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../static/bookmark_manager/bookmarks.js"), "utf8");

class Element {
    constructor() {
        this.dataset = {};
        this.style = {};
        this.listeners = new Map();
        this.attributes = new Map();
    }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    toggleAttribute(name, enabled) {
        if (enabled) { this.attributes.set(name, ""); } else { this.attributes.delete(name); }
    }
}

function boot() {
    const statusPanel = { hidden: true };
    const notificationPanel = { hidden: true };
    const log = { open: true, scrollTop: 45, textContent: "Git run completed.", focusCount: 1 };
    const refreshForm = new Element();
    refreshForm.dataset = { active: "false", runId: "1" };
    const controls = new Map(["button", "spinner", "label", "progress"].map((name) => [name, new Element()]));
    refreshForm.querySelector = (selector) => controls.get(selector.replace("[data-global-refresh-", "").replace("]", "")) || null;
    const statusText = new Element();
    const document = {
        activeElement: log,
        addEventListener() {},
        querySelector(selector) {
            if (selector === "[data-global-refresh]") { return refreshForm; }
            if (selector === "[data-global-status-text]") { return statusText; }
            return null;
        },
        querySelectorAll(selector) {
            return selector === "[data-repository-status-panel], [data-notification-panel]"
                ? [statusPanel, notificationPanel] : [];
        },
    };
    const listeners = new Map();
    const timers = new Map();
    let sequence = 0;
    let reloads = 0;
    let fetches = 0;
    const window = {
        location: { pathname: "/bookmarks/", search: "", reload() { reloads += 1; } },
        localStorage: { getItem() { return null; }, setItem() {} },
        addEventListener(name, listener) { listeners.set(name, listener); },
        setTimeout(callback, delay) {
            const id = ++sequence;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
    };
    vm.runInNewContext(source, {
        document, window,
        fetch: async () => { fetches += 1; throw new Error("No network request expected"); },
    });
    return {
        statusPanel, notificationPanel, log, document, controls, statusText,
        reloadCount: () => reloads,
        fetchCount: () => fetches,
        pendingTimers: () => timers.size,
        update(refresh) { listeners.get("owl:refresh-status")({ detail: refresh }); },
        tick() {
            const timer = [...timers][0];
            assert.ok(timer, "A completed refresh has a pending reload check");
            timers.delete(timer[0]);
            timer[1].callback();
            return timer[1].delay;
        },
    };
}

const completed = (overrides = {}) => ({
    active: false, status: "succeeded", run_id: 2, succeeded: 3, total: 3,
    processed: 3, progress: 100, ...overrides,
});

test("ordinary terminal refresh outcomes retain their normal delayed reload", () => {
    for (const status of ["succeeded", "succeeded_with_errors", "failed", "interrupted"]) {
        const page = boot();
        page.update(completed({ status }));
        assert.equal(page.reloadCount(), 0);
        assert.equal(page.tick(), 250);
        assert.equal(page.reloadCount(), 1);
        assert.equal(page.fetchCount(), 0);
    }
});

test("an open status log keeps completion readable until the panel closes", () => {
    const page = boot();
    page.statusPanel.hidden = false;
    page.update(completed());
    assert.equal(page.tick(), 250);
    assert.equal(page.reloadCount(), 0);
    assert.equal(page.tick(), 1000);
    assert.equal(page.reloadCount(), 0);
    assert.equal(page.document.activeElement, page.log);
    assert.equal(page.log.open, true);
    assert.equal(page.log.scrollTop, 45);
    assert.equal(page.log.textContent, "Git run completed.");
    assert.equal(page.log.focusCount, 1);
    assert.equal(page.controls.get("label").textContent, "3 refreshed");
    assert.equal(page.controls.get("button").disabled, false);
    page.statusPanel.hidden = true;
    page.tick();
    assert.equal(page.reloadCount(), 1);
    assert.equal(page.pendingTimers(), 0);
    assert.equal(page.fetchCount(), 0, "Waiting only checks local panel state");
});

test("notification history and switching panels retain one pending reload", () => {
    const page = boot();
    page.notificationPanel.hidden = false;
    page.update(completed());
    page.tick();
    page.update(completed());
    page.update(completed());
    assert.equal(page.pendingTimers(), 1);
    page.notificationPanel.hidden = true;
    page.statusPanel.hidden = false;
    page.tick();
    assert.equal(page.reloadCount(), 0);
    page.statusPanel.hidden = true;
    page.tick();
    assert.equal(page.reloadCount(), 1);
});

test("a panel opened during the original completion delay still prevents navigation", () => {
    const page = boot();
    page.update(completed());
    page.statusPanel.hidden = false;
    page.tick();
    assert.equal(page.reloadCount(), 0);
    page.statusPanel.hidden = true;
    page.tick();
    assert.equal(page.reloadCount(), 1);
});

test("a previously completed run does not create a reload just from panel activity", () => {
    const page = boot();
    page.statusPanel.hidden = false;
    page.update(completed({ run_id: 1 }));
    page.statusPanel.hidden = true;
    page.update(completed({ run_id: 1 }));
    assert.equal(page.pendingTimers(), 0);
    assert.equal(page.reloadCount(), 0);
});
