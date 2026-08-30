const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../static/bitbucket_search/bitbucket_search.js"), "utf8");

// Deliberately small DOM: execute the shipped event handlers and polling code,
// without a browser, network, application database, or test-only product hooks.
class Element {
    constructor(tag = "div", attributes = {}, children = []) {
        this.tagName = tag.toUpperCase();
        this.attributes = new Map();
        this.dataset = {};
        this.children = [];
        this.listeners = new Map();
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.value = "";
        this.textContent = "";
        this.classes = new Set();
        this.classList = {
            add: (...names) => names.forEach((name) => this.classes.add(name)),
            remove: (...names) => names.forEach((name) => this.classes.delete(name)),
            contains: (name) => this.classes.has(name),
            toggle: (name, force) => {
                const enabled = force === undefined ? !this.classes.has(name) : force;
                if (enabled) this.classes.add(name); else this.classes.delete(name);
                return enabled;
            },
        };
        Object.entries(attributes).forEach(([name, value]) => this.setAttribute(name, value));
        children.forEach((child) => this.appendChild(child));
    }
    setAttribute(name, value) {
        const text = String(value);
        this.attributes.set(name, text);
        if (name.startsWith("data-")) {
            this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = text;
        }
        if (["id", "name", "type", "value", "title"].includes(name)) this[name] = text;
        if (name === "disabled") this.disabled = true;
        if (name === "hidden") this.hidden = true;
    }
    getAttribute(name) { return this.attributes.get(name) ?? (["name", "id", "type"].includes(name) ? this[name] ?? null : null); }
    hasAttribute(name) { return this.getAttribute(name) !== null; }
    removeAttribute(name) {
        this.attributes.delete(name);
        if (name === "disabled") this.disabled = false;
        if (name === "hidden") this.hidden = false;
    }
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
    remove() {
        if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
        this.parentElement = null;
    }
    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(listener);
    }
    matches(selector) {
        return selector.split(",").some((part) => {
            part = part.trim();
            if (part.includes(" ")) {
                const pieces = part.split(/\s+/);
                const final = pieces.pop();
                return this.matches(final) && Boolean(this.parentElement?.closest(pieces.join(" ")));
            }
            if (part.endsWith(":checked") && !this.checked) return false;
            part = part.replace(/:checked$/, "");
            const tag = part.match(/^[a-z]+/i)?.[0];
            if (tag && tag.toUpperCase() !== this.tagName) return false;
            const id = part.match(/^#([\w-]+)/)?.[1];
            if (id && id !== this.id) return false;
            const className = part.match(/^\.([\w-]+)/)?.[1];
            if (className && !this.classes.has(className)) return false;
            const attributes = [...part.matchAll(/\[([^\]=]+)(?:=["']?([^"'\]]+)["']?)?\]/g)];
            return attributes.every(([, name, value]) => {
                if (name.startsWith("data-")) {
                    const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
                    return Object.hasOwn(this.dataset, key) && (value === undefined || this.dataset[key] === value);
                }
                return this.hasAttribute(name) && (value === undefined || this.getAttribute(name) === value);
            });
        });
    }
    querySelectorAll(selector) {
        return this.children.flatMap((child) => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
    focus() { this.focused = true; }
    dispatch(name, properties = {}) {
        const event = {
            target: this, currentTarget: this, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.stopped = true; },
            ...properties,
        };
        for (let node = this; node && !event.stopped; node = node.parentElement) {
            event.currentTarget = node;
            for (const listener of node.listeners.get(name) || []) listener(event);
        }
        return event;
    }
}

const element = (hook, attributes = {}, tag = "div") =>
    new Element(tag, { [hook]: "", ...attributes });

function boot({ repositories = [{ id: 1 }, { id: 2 }], initiallyExtracting = false, duplicateCopies = false } = {}) {
    const controls = {
        refresh: element("data-selected-refresh", { type: "submit", name: "operation", value: "refresh", disabled: "" }, "button"),
        exclude: element("data-selected-exclude", { type: "submit", name: "operation", value: "exclude", disabled: "" }, "button"),
        unlock: element("data-selected-unlock", { type: "button", disabled: "", "aria-pressed": "false" }, "button"),
        remove: element("data-selected-remove", { type: "submit", name: "operation", value: "remove", disabled: "" }, "button"),
        excluded: element("data-selected-excluded-value", { type: "hidden", name: "excluded", value: "yes" }, "input"),
        count: element("data-repository-selection-count"),
        spinner: element("data-selected-refresh-spinner", { hidden: "" }),
        icon: element("data-selected-refresh-icon", {}, "svg"),
    };
    controls.refresh.appendChild(controls.icon);
    controls.refresh.appendChild(controls.spinner);
    const form = element("data-repository-selection-form", {
        id: "bb-repository-selection-form", method: "post", action: "/pdfs/repositories/selected/",
    }, "form");
    [controls.refresh, controls.exclude, controls.unlock, controls.remove, controls.excluded, controls.count].forEach((node) => form.appendChild(node));
    const global = {
        button: element("data-refresh-all-button", {}, "button"),
        icon: element("data-refresh-all-icon", {}, "svg"),
        spinner: element("data-refresh-all-spinner", { hidden: "" }),
        label: element("data-refresh-all-label"), detail: element("data-refresh-all-detail"),
    };
    const globalForm = element("data-repositories-refresh-all", {
        action: "/pdfs/repositories/refresh/",
        "data-repository-count": repositories.length,
        "data-enabled-repository-count": repositories.filter((repo) => !repo.excluded).length,
        "data-active-repository-count": repositories.filter((repo) => repo.active).length,
        "data-active-work-repository-count": repositories.filter((repo) => repo.active || repo.work).length,
    }, "form");
    Object.values(global).forEach((node) => globalForm.appendChild(node));
    const workspace = element("data-bitbucket-workspace", {
        "data-repository-status-url": "/pdfs/repositories/status/",
        "data-daily-refresh-enabled": "true",
        "data-extraction-active": String(initiallyExtracting),
        "data-catalog-publication-signature": "catalog-initial",
        "data-extraction-publication-signature": "extraction-initial",
    });
    workspace.appendChild(form);
    workspace.appendChild(globalForm);
    const filter = element("data-repository-filter", { type: "search" }, "input");
    workspace.appendChild(filter);
    const renderedRepositories = duplicateCopies ? repositories.flatMap((repo) => [repo, repo]) : repositories;
    const cards = renderedRepositories.map((repo) => {
        const card = element("data-repository-id", {
            "data-repository-id": String(repo.id),
            "data-repository-state": repo.active ? "fetching" : "ready",
            "data-repository-active-sync": String(Boolean(repo.active)),
            "data-repository-active-work": String(Boolean(repo.active || repo.work)),
            "data-repository-refresh-excluded": String(Boolean(repo.excluded)),
            "data-repository-removal-pending": String(Boolean(repo.removal)),
            "data-repository-search-value": `Repository ${repo.id}`,
        }, "li");
        const checkbox = element("data-repository-select", {
            type: "checkbox", name: "repository_ids", value: repo.id, form: form.id,
            ...(repo.removal ? { disabled: "" } : {}),
        }, "input");
        const timer = element("data-repository-worker-timer", { hidden: "" });
        card.appendChild(checkbox);
        card.appendChild(timer);
        card.appendChild(element("data-repository-state-icon"));
        card.appendChild(element("data-repository-documents"));
        card.appendChild(element("data-repository-exclusion"));
        workspace.appendChild(card);
        return { card, checkbox, timer };
    });
    const document = new Element("document", {}, [workspace]);
    document.getElementById = (id) => document.querySelector(`#${id}`);
    document.createElement = (tag) => new Element(tag);
    const timers = new Map();
    const timerUpdates = [];
    const windows = new Map();
    const responses = [];
    let timerId = 0;
    let reloads = 0;
    const window = {
        location: { reload() { reloads += 1; } },
        addEventListener(name, callback) { windows.set(name, callback); },
        setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
        OWLRepositoryTimers: {
            update(timer, timing) { timerUpdates.push({ timer, timing }); timer.textContent = timing?.label || ""; },
            stale() {},
        },
    };
    vm.runInNewContext(source, {
        document, window,
        fetch: async () => ({ ok: true, json: async () => {
            const response = responses.shift();
            if (response instanceof Error) throw response;
            return response;
        } }),
    });
    return {
        controls, cards, form, global, globalForm, workspace, timerUpdates,
        reloads: () => reloads,
        select(index, checked = true) { cards[index].checkbox.checked = checked; cards[index].checkbox.dispatch("change"); },
        unlock() { return controls.unlock.dispatch("click"); },
        filter(value) { filter.value = value; filter.dispatch("input"); },
        submit(control) { return form.dispatch("submit", { submitter: controls[control] }); },
        async poll({ overrides = {}, extraction = {}, fail = false } = {}) {
            const payload = {
                repositories: repositories.map((repo) => ({
                    id: repo.id, name: `Repository ${repo.id}`, state: "ready", stateLabel: "Ready",
                    enabled: true, active: false, hasActiveWork: false,
                    refreshExcluded: Boolean(repo.excluded), hasRemovalPending: Boolean(repo.removal),
                    pdfCount: 1, vsdxCount: 0,
                    workerTiming: { active: false, label: "", kind: "", startedAt: "" },
                    ...(overrides[repo.id] || {}),
                })),
                totals: { repositories: repositories.length, pdfs: repositories.length, vsdx: 0, bytesLabel: "1 KB" },
                automation: { enabled: true },
                catalog: { publicationSignature: "catalog-initial" },
                extraction: {
                    active: false, queuedJobs: 0, runningJobs: 0, pendingDocuments: 0,
                    indexedDocuments: repositories.length, publicationSignature: "extraction-initial", ...extraction,
                },
            };
            responses.push(fail ? new Error("Synthetic status outage") : payload);
            const scheduled = [...timers].find(([, value]) => value.callback.name === "poll");
            assert.ok(scheduled, "Repository status poll must stay scheduled");
            timers.delete(scheduled[0]);
            await scheduled[1].callback();
            return payload;
        },
    };
}

test("selection enables shared actions but removal stays locked until explicitly unlocked", () => {
    const page = boot();
    Object.values(page.controls).slice(0, 4).forEach((button) => assert.equal(button.disabled, true));
    page.select(0);
    assert.equal(page.controls.refresh.disabled, false);
    assert.equal(page.controls.exclude.disabled, false);
    assert.equal(page.controls.unlock.disabled, false);
    assert.equal(page.controls.remove.disabled, true);
    page.unlock();
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.controls.unlock.getAttribute("aria-pressed"), "true");
    page.select(1);
    assert.equal(page.controls.remove.disabled, true, "Changing the selected target set relocks deletion");
    assert.match(page.controls.count.textContent, /2/);
    page.select(0, false);
    page.select(1, false);
    for (const action of ["refresh", "exclude", "unlock", "remove"]) {
        assert.equal(page.controls[action].disabled, true, `${action} requires a selection`);
    }
});

test("desktop and mobile repository checkboxes stay synchronized and count unique repositories", () => {
    const page = boot({ duplicateCopies: true });
    page.select(0);
    assert.deepEqual(page.cards.map(({ checkbox }) => checkbox.checked), [true, true, false, false]);
    assert.match(page.controls.count.textContent, /^1 selected$/);
    page.select(3);
    assert.deepEqual(page.cards.map(({ checkbox }) => checkbox.checked), [true, true, true, true]);
    assert.match(page.controls.count.textContent, /^2 selected$/);
    page.unlock();
    page.select(1, false);
    assert.deepEqual(page.cards.map(({ checkbox }) => checkbox.checked), [false, false, true, true]);
    assert.equal(page.controls.remove.disabled, true);
    assert.match(page.controls.count.textContent, /^1 selected$/);
});

test("filtering away a selected repository clears it and relocks deletion", () => {
    const page = boot();
    page.select(0);
    page.select(1);
    page.unlock();
    page.filter("Repository 2");
    assert.equal(page.cards[0].checkbox.checked, false);
    assert.equal(page.cards[0].card.hidden, true);
    assert.equal(page.cards[1].checkbox.checked, true);
    assert.equal(page.controls.remove.disabled, true);
    assert.match(page.controls.count.textContent, /^1 selected$/);
});

test("shared exclusion value includes an entirely excluded selection and excludes mixed selections", () => {
    const page = boot({ repositories: [{ id: 1, excluded: true }, { id: 2 }] });
    page.select(0);
    assert.equal(page.controls.excluded.value, "no");
    page.select(1);
    assert.equal(page.controls.excluded.value, "yes");
    assert.equal(page.controls.remove.disabled, true);
});

test("native operation submits retain checked repository IDs and enforce the delete lock", () => {
    const locked = boot();
    assert.equal(locked.submit("refresh").defaultPrevented, true);
    locked.select(0);
    assert.equal(locked.submit("remove").defaultPrevented, true);
    const unlocked = boot();
    unlocked.select(0);
    unlocked.select(1);
    unlocked.unlock();
    assert.equal(unlocked.submit("remove").defaultPrevented, false);
    assert.equal(unlocked.form.querySelector('input[name="operation"]').value, "remove", "Native POST keeps validated intent when submitter is disabled");
    assert.deepEqual(unlocked.cards.filter(({ checkbox }) => checkbox.checked).map(({ checkbox }) => checkbox.value), ["1", "2"]);
    for (const action of ["refresh", "exclude"]) {
        const page = boot();
        page.select(0);
        const event = page.submit(action);
        assert.equal(event.defaultPrevented, false, `${action} proceeds via the selected POST form`);
        assert.equal(page.form.querySelector('input[name="operation"]').value, action);
        assert.equal(page.global.button.disabled, true, "Duplicate global submission is prevented");
    }
});

test("new Git or PDF work relocks selection and replaces the global refresh icon with a spinner", async () => {
    for (const work of [
        { active: true, hasActiveWork: true, state: "fetching" },
        { active: false, hasActiveWork: true },
    ]) {
        const page = boot();
        page.select(0);
        page.unlock();
        const timer = page.cards[0].timer;
        const timing = { active: true, label: work.active ? "Refreshing" : "Reading PDFs", startedAt: "2026-08-30T11:00:00Z" };
        await page.poll({ overrides: { 1: { ...work, workerTiming: timing } } });
        assert.equal(page.controls.remove.disabled, true);
        assert.equal(page.controls.unlock.getAttribute("aria-pressed"), "false");
        assert.equal(page.global.button.disabled, true);
        assert.equal(page.global.spinner.hidden, false);
        assert.equal(page.global.icon.hidden, true);
        assert.equal(page.global.icon.hasAttribute("hidden"), true, "SVG visibility needs an explicit hidden attribute");
        assert.equal(page.controls.icon.hasAttribute("hidden"), true, "Selected refresh SVG must not appear beside its spinner");
        assert.equal(page.cards[0].timer, timer, "Worker timer element remains intact");
        assert.equal(page.timerUpdates.some((update) => update.timer === timer && update.timing === timing), true);
        assert.equal(page.reloads(), 0);
        await page.poll();
        assert.equal(page.global.icon.hidden, false);
        assert.equal(page.global.icon.hasAttribute("hidden"), false, "Idle removes the SVG attribute, including server-rendered hidden state");
        assert.equal(page.controls.icon.hidden, false);
        assert.equal(page.controls.icon.hasAttribute("hidden"), false, "Selected refresh SVG becomes visible after workers finish");
        assert.equal(page.global.spinner.hidden, true);
        assert.equal(page.controls.spinner.hidden, true);
    }
});

test("extraction work without per-repository sync keeps the global spinner active until idle", async () => {
    const page = boot({ initiallyExtracting: true });
    assert.equal(page.global.spinner.hidden, false);
    assert.equal(page.global.icon.hidden, true);
    assert.equal(page.global.icon.hasAttribute("hidden"), true);
    assert.equal(page.global.button.disabled, true);
    await page.poll({ extraction: { active: false, runningJobs: 1 } });
    assert.equal(page.global.spinner.hidden, false);
    assert.equal(page.global.icon.hidden, true);
    await page.poll();
    assert.equal(page.global.spinner.hidden, true);
    assert.equal(page.global.icon.hidden, false);
    assert.equal(page.global.icon.hasAttribute("hidden"), false);
    assert.equal(page.global.button.disabled, false);
    assert.equal(page.reloads(), 0, "Settled completion still uses the existing reload confirmation");
});

test("pending removal and completed work cannot silently unlock destructive actions", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    await page.poll({ overrides: { 1: { hasRemovalPending: true } } });
    assert.equal(page.controls.remove.disabled, true);
    assert.equal(page.controls.refresh.disabled, true);
    assert.equal(page.controls.exclude.disabled, true);
    await page.poll();
    assert.equal(page.controls.remove.disabled, true, "Returning to idle never restores old unlock consent");
});

test("a status outage relocks destructive actions until fresh state arrives", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    await page.poll({ fail: true });
    assert.equal(page.controls.remove.disabled, true);
    assert.equal(page.controls.refresh.disabled, true);
    assert.equal(page.global.button.disabled, true);
    await page.poll();
    assert.equal(page.controls.remove.disabled, true);
    assert.equal(page.controls.refresh.disabled, false);
    assert.equal(page.global.button.disabled, false);
});

test("workers on excluded repositories still animate the global status icon", async () => {
    const page = boot({ repositories: [{ id: 1, excluded: true }] });
    await page.poll({ overrides: { 1: { hasActiveWork: true } } });
    assert.equal(page.global.spinner.hidden, false);
    assert.equal(page.global.icon.hidden, true);
    assert.equal(page.global.button.disabled, true);
});
