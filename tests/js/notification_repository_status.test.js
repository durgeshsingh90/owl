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
    focus() { this.focused = true; }
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
    ...overrides,
});

async function boot(...responses) {
    const center = new Element();
    center.dataset.notificationsUrl = "/bookmarks/notifications/";
    const hooks = new Map();
    [
        "toggle", "panel", "badge", "activity", "list", "empty", "live", "read-all",
        "unread-label", "background-state", "progress-card", "progress-label",
        "progress-detail", "progress", "schedule", "next-run", "last-attempt",
        "last-success", "retry-row", "retry", "repository-list", "repository-count",
        "repository-message",
    ].forEach((name) => hooks.set(name, new Element()));
    center.querySelector = (selector) => hooks.get(selector.replace("[data-notification-", "").replace("]", "")) || null;
    const timers = [];
    const document = {
        body: new Element("body"),
        querySelectorAll: (selector) => selector === "[data-notification-center]" ? [center] : [],
        addEventListener() {},
        createElement: (tag) => new Element(tag),
    };
    const window = {
        localStorage: { getItem() { return null; }, setItem() {} },
        matchMedia: () => ({ matches: false }),
        setTimeout(fn) { timers.push(fn); return timers.length; },
        clearTimeout() {},
        setInterval() {},
        addEventListener() {},
        dispatchEvent() {},
        async fetch() {
            const response = responses.shift();
            if (response instanceof Error) { throw response; }
            return { ok: true, async json() { return response; } };
        },
    };
    vm.runInNewContext(source, { window, document, URLSearchParams, CustomEvent: class {} });
    const flush = () => new Promise(setImmediate);
    await flush();
    return {
        hooks,
        async poll() {
            const load = timers.findLast((fn) => fn.name === "load");
            assert.ok(load, "A status poll is scheduled");
            await load();
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
