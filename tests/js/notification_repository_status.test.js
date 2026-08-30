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
    [...notificationHooks, ...statusHooks, "status-toggle", "status-panel", "status-indicator", "status-live"]
        .forEach((name) => hooks.set(name, new Element()));
    notificationHooks.forEach((name) => center.append(hooks.get(name)));
    [...statusHooks, "status-toggle", "status-panel", "status-indicator", "status-live"]
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
    const timers = [];
    const intervals = [];
    const requests = [];
    const documentListeners = new Map();
    const document = {
        body: new Element("body"),
        querySelector: (selector) => selector === "[data-repository-status-center]" ? statusCenter : null,
        querySelectorAll: (selector) => selector === "[data-notification-center]" ? [center] : [],
        addEventListener(name, fn) {
            documentListeners.set(name, [...(documentListeners.get(name) || []), fn]);
        },
        createElement: (tag) => new Element(tag),
    };
    let lastResponse;
    const window = {
        localStorage: { getItem() { return null; }, setItem() {} },
        matchMedia: () => ({ matches: false }),
        setTimeout(fn) { timers.push(fn); return timers.length; },
        clearTimeout() {},
        setInterval(fn) { intervals.push(fn); },
        addEventListener() {},
        dispatchEvent() {},
        async fetch(url, options) {
            requests.push({ url, options });
            if (options.method === "POST") {
                return { ok: true, async json() { return {}; } };
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
        requests,
        intervals,
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

test("status opens separately from notifications and switching panels never marks alerts read", async () => {
    const page = await boot(payload([repository(1)], { unread_count: 3 }));
    const { hooks } = page;
    assert.equal(page.center.contains(hooks.get("repository-list")), false);
    assert.equal(page.statusCenter.contains(hooks.get("repository-list")), true);
    assert.equal(page.statusCenter.contains(hooks.get("list")), false);
    await page.click("status-toggle");
    assert.equal(hooks.get("status-panel").hidden, false);
    assert.equal(hooks.get("panel").hidden, true);
    assert.match(hooks.get("status-toggle").getAttribute("aria-label"), /^Close background status/);
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
