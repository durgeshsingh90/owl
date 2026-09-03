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

function boot({
    repositories = [{ id: 1 }, { id: 2 }], initiallyExtracting = false,
    duplicateCopies = false, connectionResponse = null, pdfCount = 0,
} = {}) {
    const makeControls = () => ({
        selectAll: element("data-selected-select-all", { type: "button", hidden: "", disabled: "" }, "button"),
        refresh: element("data-selected-refresh", { type: "submit", name: "operation", value: "refresh", disabled: "" }, "button"),
        exclude: element("data-selected-exclude", { type: "submit", name: "operation", value: "exclude", disabled: "" }, "button"),
        stop: element("data-selected-stop-indexing", { type: "submit", name: "operation", value: "stop_indexing", disabled: "" }, "button"),
        remove: element("data-selected-remove", { type: "submit", name: "operation", value: "remove", disabled: "", "data-delete-locked": "true" }, "button"),
        deleteIcon: element("data-selected-delete-icon", {}, "span"),
        excluded: element("data-selected-excluded-value", { type: "hidden", name: "excluded", value: "yes" }, "input"),
        count: element("data-repository-selection-count"),
        spinner: element("data-selected-refresh-spinner", { hidden: "" }),
        icon: element("data-selected-refresh-icon", {}, "svg"),
    });
    const controls = makeControls();
    const controlCopies = duplicateCopies ? [controls, makeControls()] : [controls];
    const form = element("data-repository-selection-form", {
        id: "bb-repository-selection-form", method: "post", action: "/pdfs/repositories/selected/",
    }, "form");
    controlCopies.forEach((copy) => {
        copy.refresh.appendChild(copy.icon);
        copy.refresh.appendChild(copy.spinner);
        copy.remove.appendChild(copy.deleteIcon);
        [copy.selectAll, copy.refresh, copy.exclude, copy.stop, copy.remove, copy.excluded, copy.count].forEach((node) => form.appendChild(node));
    });
    const global = {
        button: element("data-refresh-all-button", {}, "button"),
        icon: element("data-refresh-all-icon", {}, "svg"),
        spinner: element("data-refresh-all-spinner", { hidden: "" }),
        label: element("data-refresh-all-label"), detail: element("data-refresh-all-detail"),
        progress: element("data-overall-progress", { hidden: "" }),
        progressBar: element("data-overall-progress-bar", { max: "100" }, "progress"),
        progressLabel: element("data-overall-progress-label", {}, "small"),
        counts: element("data-overall-counts", {}, "small"),
        pdfCounts: element("data-overall-pdf-counts", {}, "small"),
        gitCounts: element("data-overall-git-counts", {}, "small"),
        timing: element("data-overall-timing", { hidden: "" }, "small"),
    };
    global.progress.appendChild(global.progressBar);
    global.progress.appendChild(global.progressLabel);
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
        ...(connectionResponse ? { "data-repository-connection-test-url": "/pdfs/repositories/connection/test/" } : {}),
        "data-daily-refresh-enabled": "true",
        "data-extraction-active": String(initiallyExtracting),
        "data-catalog-publication-signature": "catalog-initial",
        "data-extraction-publication-signature": "extraction-initial",
    });
    workspace.appendChild(form);
    workspace.appendChild(globalForm);
    const deleteStatus = element("data-repository-delete-status");
    workspace.appendChild(deleteStatus);
    const filter = element("data-repository-filter", { type: "search" }, "input");
    workspace.appendChild(filter);
    const operationOverlay = element("data-repository-operation-overlay", { hidden: "", tabindex: "-1" });
    const operationTitle = element("data-repository-operation-title", {}, "strong");
    const operationDetail = element("data-repository-operation-detail", {}, "span");
    operationOverlay.appendChild(operationTitle);
    operationOverlay.appendChild(operationDetail);
    workspace.appendChild(operationOverlay);
    const pdfSelectionCount = element("data-pdf-selection-count", { hidden: "" }, "span");
    const selectAllPdfs = element("data-select-all-pdfs", { type: "checkbox" }, "input");
    workspace.appendChild(pdfSelectionCount);
    workspace.appendChild(selectAllPdfs);
    const pdfs = Array.from({ length: pdfCount }, (_, index) => {
        const row = element("data-pdf-row", { "data-document-id": String(index + 1) }, "tr");
        const checkbox = element("data-pdf-select", { type: "checkbox", value: String(index + 1) }, "input");
        row.appendChild(checkbox);
        workspace.appendChild(row);
        return { row, checkbox };
    });
    let connection = null;
    if (connectionResponse) {
        connection = element("data-repository-connection-result", { "data-state": "idle" }, "button");
        connection.appendChild(element("data-repository-connection-message"));
        workspace.appendChild(connection);
    }
    const renderedRepositories = duplicateCopies ? repositories.flatMap((repo) => [repo, repo]) : repositories;
    const cards = renderedRepositories.map((repo) => {
        const card = element("data-repository-id", {
            "data-repository-id": String(repo.id),
            "data-repository-state": repo.active ? "fetching" : "ready",
            "data-repository-active-sync": String(Boolean(repo.active)),
            "data-repository-active-work": String(Boolean(repo.active || repo.work)),
            "data-repository-pdf-indexing-active": String(Boolean(repo.pdf)),
            "data-repository-refresh-excluded": String(Boolean(repo.excluded)),
            "data-repository-removal-pending": String(Boolean(repo.removal)),
            "data-repository-search-value": `Repository ${repo.id}`,
        }, "li");
        const checkbox = element("data-repository-select", {
            type: "checkbox", name: "repository_ids", value: repo.id, form: form.id,
            ...(repo.removal ? { disabled: "" } : {}),
        }, "input");
        const timer = element("data-repository-run-timer", { hidden: "" });
        const stateIcon = element("data-repository-state-icon");
        const workLabel = element("data-repository-work-label", { hidden: "" });
        const health = element("data-repository-health", { hidden: "" }, "small");
        const progressContainer = element("data-repository-progress", { hidden: "" });
        const progressBar = element("data-repository-progress-bar", { max: "100" }, "progress");
        const progressLabel = element("data-repository-progress-label", {}, "small");
        progressContainer.appendChild(progressBar);
        progressContainer.appendChild(progressLabel);
        card.appendChild(checkbox);
        card.appendChild(timer);
        card.appendChild(stateIcon);
        card.appendChild(workLabel);
        card.appendChild(health);
        card.appendChild(progressContainer);
        card.appendChild(element("data-repository-documents"));
        const remaining = element("data-repository-remaining", { hidden: "" }, "small");
        card.appendChild(remaining);
        card.appendChild(element("data-repository-exclusion"));
        workspace.appendChild(card);
        return {
            card, checkbox, timer, stateIcon, workLabel, health,
            progressContainer, progressBar, progressLabel, remaining,
        };
    });
    const document = new Element("document", {}, [workspace]);
    document.documentElement = new Element("html");
    document.getElementById = (id) => document.querySelector(`#${id}`);
    document.createElement = (tag) => new Element(tag);
    const timers = new Map();
    const timerUpdates = [];
    const staleTimers = [];
    const windows = new Map();
    const responses = [];
    if (connectionResponse) responses.push(connectionResponse);
    let timerId = 0;
    let reloads = 0;
    let fetchCount = 0;
    let now = 100000;
    const window = {
        location: { reload() { reloads += 1; } },
        addEventListener(name, callback) { windows.set(name, callback); },
        setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay, dueAt: now + delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
        OWLRepositoryTimers: {
            update(timer, timing) { timerUpdates.push({ timer, timing }); timer.textContent = timing?.label || ""; },
            stale(timer) { staleTimers.push(timer); },
        },
    };
    vm.runInNewContext(source, {
        document, window, Date: class extends Date { static now() { return now; } },
        fetch: async () => {
            fetchCount += 1;
            return { ok: true, json: async () => {
                const response = responses.shift();
                if (response instanceof Error) throw response;
                return response;
            } };
        },
    });
    return {
        controls, controlCopies, cards, form, global, globalForm, workspace, timerUpdates, staleTimers, deleteStatus,
        connection, pdfs, pdfSelectionCount, selectAllPdfs,
        operationOverlay, operationTitle, operationDetail, document,
        settle() { return new Promise((resolve) => setImmediate(resolve)); },
        queueResponse(response) { responses.push(response); },
        reloads: () => reloads,
        fetchCount: () => fetchCount,
        select(index, checked = true) { cards[index].checkbox.checked = checked; cards[index].checkbox.dispatch("change"); },
        unlock(copy = 0) { return controlCopies[copy].remove.dispatch("click"); },
        selectAll(copy = 0) { return controlCopies[copy].selectAll.dispatch("click"); },
        clickRemove(copy = 0) { return controlCopies[copy].remove.dispatch("click"); },
        async pageshow() {
            responses.push(new Error("Awaiting a fresh status after restoring the page"));
            windows.get("pageshow")?.({ persisted: true });
            await new Promise((resolve) => setImmediate(resolve));
        },
        deleteTimers() { return [...timers.values()].filter((timer) => timer.delay === 10000); },
        advanceTime(milliseconds, { runTimers = true } = {}) {
            now += milliseconds;
            if (!runTimers) return;
            for (const [id, timer] of [...timers]) {
                if (timer.delay === 10000 && timer.dueAt <= now) {
                    timers.delete(id);
                    timer.callback();
                }
            }
        },
        filter(value) { filter.value = value; filter.dispatch("input"); },
        submit(control) { return form.dispatch("submit", { submitter: controls[control] }); },
        async poll({ overrides = {}, extraction = {}, work, workerLimits, fail = false } = {}) {
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
                ...(workerLimits ? { workerLimits } : {}),
                ...(work ? { work } : {}),
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

test("PDF rows support independent multi-selection and select-all on the visible page", () => {
    const page = boot({ repositories: [{ id: 1 }], pdfCount: 3 });
    page.pdfs[0].checkbox.checked = true;
    page.pdfs[0].checkbox.dispatch("change");
    page.pdfs[2].checkbox.checked = true;
    page.pdfs[2].checkbox.dispatch("change");
    assert.equal(page.pdfSelectionCount.hidden, false);
    assert.equal(page.pdfSelectionCount.textContent, "2 PDFs selected");
    assert.equal(page.selectAllPdfs.indeterminate, true);

    page.selectAllPdfs.checked = true;
    page.selectAllPdfs.dispatch("change");
    assert.equal(page.pdfs.every(({ checkbox }) => checkbox.checked), true);
    assert.equal(page.pdfSelectionCount.textContent, "3 PDFs selected");
    assert.equal(page.selectAllPdfs.indeterminate, false);
    assert.equal(page.selectAllPdfs.checked, true);
});

test("stopping repository work blocks the page with a focused loading screen", () => {
    const page = boot({ repositories: [{ id: 1, pdf: true }] });
    page.select(0);
    const submission = page.submit("stop");
    assert.equal(submission.defaultPrevented, false);
    assert.equal(page.operationOverlay.hidden, false);
    assert.equal(page.operationOverlay.focused, true);
    assert.equal(page.operationTitle.textContent, "Stopping repository work…");
    assert.match(page.operationDetail.textContent, /Stopping Git and PDF workers/);
    assert.equal(page.document.documentElement.classList.contains("bb-operation-blocked"), true);
    assert.equal(page.controls.stop.disabled, true);
    assert.equal(page.controls.remove.disabled, true);
});

test("connection test stays idle on load and runs only when its top-bar icon is clicked", async () => {
    const page = boot({
        repositories: [{ id: 1 }],
        connectionResponse: { state: "connected", label: "Git connection passed", detail: "Verified" },
    });
    assert.equal(page.connection.dataset.state, "idle");
    assert.equal(page.connection.disabled, false);
    await page.settle();
    assert.equal(page.fetchCount(), 0, "page initialization performs no live Git request");

    page.connection.dispatch("click");
    page.connection.dispatch("click");
    assert.equal(page.connection.dataset.state, "checking", "click immediately shows checking state");
    assert.equal(page.connection.disabled, true, "parallel clicks are blocked while testing");
    await page.settle();
    assert.equal(page.fetchCount(), 1, "one explicit click starts one connection test");
    assert.equal(page.connection.dataset.state, "connected");
    assert.equal(page.connection.disabled, false);

    page.queueResponse({
        state: "failed",
        label: "Git connection failed",
        detail: "1 of 1 repository connection failed.",
        repositories: [{ id: 1, connected: false, detail: "Check the network or VPN." }],
    });
    page.connection.dispatch("click");
    assert.equal(page.connection.dataset.state, "checking", "click immediately restores connecting state");
    assert.equal(page.connection.disabled, true, "parallel clicks are blocked while testing");
    await page.settle();
    assert.equal(page.fetchCount(), 2, "clicking the indicator performs a fresh connection test");
    assert.equal(page.connection.dataset.state, "failed");
    assert.equal(page.connection.disabled, false);
    assert.equal(page.cards[0].stateIcon.title, "Check the network or VPN.");
    assert.equal(page.cards[0].stateIcon.getAttribute("aria-label"), "Check the network or VPN.");
});

test("selection enables a single locked delete control that arms on its first click", () => {
    const page = boot();
    for (const action of ["refresh", "exclude", "stop", "remove"]) assert.equal(page.controls[action].disabled, true);
    page.select(0);
    assert.equal(page.controls.refresh.disabled, false);
    assert.equal(page.controls.exclude.disabled, false);
    assert.equal(page.controls.remove.disabled, false, "An idle selection can click the locked button to arm it");
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.controls.deleteIcon.textContent, "🔒");
    assert.equal(page.controls.remove.title, "Unlock deletion for 1 selected repository");
    assert.equal(page.controls.remove.getAttribute("aria-label"), page.controls.remove.title);
    const click = page.unlock();
    assert.equal(click.defaultPrevented, true, "The first click never submits removal");
    assert.equal(page.form.querySelector('input[name="operation"]'), null, "Arming adds no removal intent");
    assert.equal(page.form.querySelector('input[name="confirmed"]'), null, "The first click never confirms deletion");
    assert.equal(page.fetchCount(), 0, "The first click does not send a backend request");
    assert.equal(page.global.button.disabled, false, "Arming does not start a repository request");
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.controls.remove.dataset.deleteLocked, "false");
    assert.equal(page.controls.deleteIcon.textContent, "🗑️");
    assert.equal(page.controls.remove.title, "Click again to delete 1 selected repository from this computer");
    assert.equal(page.controls.remove.getAttribute("aria-label"), page.controls.remove.title);
    page.select(1);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true", "Changing the selected target set relocks deletion");
    assert.equal(page.controls.deleteIcon.textContent, "🔒");
    assert.equal(page.controls.remove.title, "Unlock deletion for 2 selected repositories");
    assert.match(page.controls.count.textContent, /2/);
    page.select(0, false);
    page.select(1, false);
    for (const action of ["refresh", "exclude", "stop", "remove"]) {
        assert.equal(page.controls[action].disabled, true, `${action} requires a selection`);
    }
});

test("selecting one repository reveals an icon that selects every available repository", () => {
    const page = boot({ repositories: [{ id: 1 }, { id: 2 }, { id: 3, removal: true }], duplicateCopies: true });
    assert.equal(page.controls.selectAll.hidden, true);
    page.select(0);
    for (const controls of page.controlCopies) {
        assert.equal(controls.selectAll.hidden, false);
        assert.equal(controls.selectAll.disabled, false);
        assert.equal(controls.selectAll.title, "Select all 2 repositories");
    }
    page.selectAll();
    assert.equal(page.cards.filter(({ checkbox }) => checkbox.value === "1").every(({ checkbox }) => checkbox.checked), true);
    assert.equal(page.cards.filter(({ checkbox }) => checkbox.value === "2").every(({ checkbox }) => checkbox.checked), true);
    assert.equal(page.cards.filter(({ checkbox }) => checkbox.value === "3").every(({ checkbox }) => !checkbox.checked), true);
    assert.equal(page.controls.count.textContent, "2 selected");
    assert.equal(page.controls.selectAll.disabled, true);
    assert.equal(page.controls.selectAll.getAttribute("aria-pressed"), "true");
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
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.match(page.controls.count.textContent, /^1 selected$/);
});

test("the same lock icon state is shared across desktop and mobile delete controls", () => {
    const page = boot({ duplicateCopies: true });
    page.select(0);
    const assertCopies = (locked) => {
        for (const copy of page.controlCopies) {
            assert.equal(copy.remove.disabled, false);
            assert.equal(copy.remove.dataset.deleteLocked, String(locked));
            assert.equal(copy.deleteIcon.textContent, locked ? "🔒" : "🗑️");
            assert.equal(copy.remove.title, locked
                ? "Unlock deletion for 1 selected repository"
                : "Click again to delete 1 selected repository from this computer");
        }
    };
    assertCopies(true);
    assert.equal(page.controlCopies[1].deleteIcon.dispatch("click").defaultPrevented, true,
        "Clicking the icon inside the mobile control arms the same shared button state");
    assertCopies(false);
    assert.match(page.deleteStatus.textContent, /unlocked.*10 seconds/i);
    assert.equal(page.clickRemove(0).defaultPrevented, false, "The desktop copy observes the mobile unlock");
    page.advanceTime(10000);
    assertCopies(true);
    assert.match(page.deleteStatus.textContent, /locked/i);
});

test("delete automatically relocks after ten seconds without changing the selected targets", () => {
    const page = boot();
    page.select(0);
    page.unlock();
    assert.equal(page.deleteTimers().length, 1);
    page.advanceTime(9999);
    assert.equal(page.controls.remove.dataset.deleteLocked, "false");
    page.advanceTime(1);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.controls.deleteIcon.textContent, "🔒");
    assert.equal(page.deleteTimers().length, 0);
    assert.equal(page.controls.remove.disabled, false, "The user can start another explicit unlock cycle");
    assert.equal(page.cards[0].checkbox.checked, true);
    assert.equal(page.submit("remove").defaultPrevented, true);
    assert.equal(page.form.querySelector('input[name="operation"]'), null);
});

test("a delayed browser timer cannot leave expired deletion consent valid", () => {
    for (const action of ["click", "submit"]) {
        const page = boot();
        page.select(0);
        page.unlock();
        page.advanceTime(10000, { runTimers: false });
        assert.equal(page.deleteTimers().length, 1, "The suspended-tab timer has deliberately not fired");
        const event = action === "click" ? page.clickRemove() : page.submit("remove");
        assert.equal(event.defaultPrevented, true, `${action} checks the actual deadline before any submission`);
        assert.equal(page.form.querySelector('input[name="operation"]'), null);
        assert.equal(page.global.button.disabled, false);
        if (action === "click") {
            assert.equal(page.controls.remove.dataset.deleteLocked, "false", "An expired second click only starts a fresh unlock");
            assert.equal(page.deleteTimers().length, 1, "Only the fresh unlock timer remains");
            page.advanceTime(10000);
        }
        assert.equal(page.controls.remove.dataset.deleteLocked, "true");
        assert.equal(page.deleteTimers().length, 0);
    }
});

test("new unlock cycles cancel earlier timers and keep their own full ten-second window", () => {
    const page = boot();
    page.select(0);
    page.unlock();
    page.advanceTime(5000);
    page.select(1);
    assert.equal(page.deleteTimers().length, 0);
    page.unlock();
    assert.equal(page.deleteTimers().length, 1);
    page.advanceTime(5000);
    assert.equal(page.controls.remove.dataset.deleteLocked, "false", "The cancelled first timer cannot relock a newer consent window");
    page.advanceTime(4999);
    assert.equal(page.controls.remove.dataset.deleteLocked, "false");
    page.advanceTime(1);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.deleteTimers().length, 0);
});

test("status polling and unrelated repository work neither relock nor extend current consent", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    const [timer] = page.deleteTimers();
    page.advanceTime(3000);
    await page.poll();
    assert.equal(page.controls.remove.dataset.deleteLocked, "false");
    page.advanceTime(3000);
    await page.poll({ overrides: { 2: { active: true, hasActiveWork: true, state: "fetching" } } });
    assert.equal(page.controls.remove.dataset.deleteLocked, "false", "Only work on selected targets revokes the unlock");
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.global.button.disabled, true, "Unrelated work still locks global refresh");
    assert.equal(page.deleteTimers()[0], timer, "Polling does not replace or postpone the expiry timer");
    page.advanceTime(4000);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
});

test("restoring a page revokes earlier consent without hiding a fresh delete unlock", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    await page.pageshow();
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.deleteTimers().length, 0);
    assert.equal(page.submit("remove").defaultPrevented, true);
    await page.poll();
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
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
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.deleteTimers().length, 0);
    assert.match(page.controls.count.textContent, /^1 selected$/);
});

test("editing repository search relocks deletion even when the selected repository remains visible", () => {
    const page = boot();
    page.select(0);
    page.unlock();
    page.filter("Repository");
    assert.equal(page.cards[0].checkbox.checked, true);
    assert.equal(page.cards[0].card.hidden, false);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.equal(page.deleteTimers().length, 0);
});

test("shared exclusion value includes an entirely excluded selection and excludes mixed selections", () => {
    const page = boot({ repositories: [{ id: 1, excluded: true }, { id: 2 }] });
    page.select(0);
    assert.equal(page.controls.excluded.value, "no");
    page.select(1);
    assert.equal(page.controls.excluded.value, "yes");
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
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
    assert.equal(unlocked.clickRemove().defaultPrevented, false, "The second click can submit confirmed deletion directly");
    assert.equal(unlocked.submit("remove").defaultPrevented, false);
    assert.equal(unlocked.form.querySelector('input[name="operation"]').value, "remove", "Native POST keeps validated intent when submitter is disabled");
    assert.equal(unlocked.form.querySelector('input[name="confirmed"]').value, "yes", "The validated second click confirms deletion without a third confirmation page");
    assert.deepEqual(unlocked.cards.filter(({ checkbox }) => checkbox.checked).map(({ checkbox }) => checkbox.value), ["1", "2"]);
    assert.equal(unlocked.controls.remove.dataset.deleteLocked, "true", "A submitted request consumes the unlock");
    assert.equal(unlocked.controls.remove.disabled, true);
    assert.equal(unlocked.deleteTimers().length, 0, "Submission cancels the arming timer");
    assert.equal(unlocked.submit("remove").defaultPrevented, true, "Repeated submission cannot reuse consumed consent");
    assert.equal(unlocked.form.querySelectorAll('input[name="operation"]').length, 1);
    assert.equal(unlocked.form.querySelectorAll('input[name="confirmed"]').length, 1);
    for (const action of ["refresh", "exclude"]) {
        const page = boot();
        page.select(0);
        const event = page.submit(action);
        assert.equal(event.defaultPrevented, false, `${action} proceeds via the selected POST form`);
        assert.equal(page.form.querySelector('input[name="operation"]').value, action);
        assert.equal(page.form.querySelector('input[name="confirmed"]'), null, "Refresh and exclude never carry deletion confirmation");
        assert.equal(page.global.button.disabled, true, "Duplicate global submission is prevented");
    }
    const stopping = boot({ repositories: [{ id: 1, pdf: true }] });
    stopping.select(0);
    const stopEvent = stopping.submit("stop");
    assert.equal(stopEvent.defaultPrevented, false, "Stop indexing posts through the selected repository form");
    assert.equal(stopping.form.querySelector('input[name="operation"]').value, "stop_indexing");
    assert.equal(stopping.form.querySelector('input[name="confirmed"]'), null, "Stopping indexing never carries deletion confirmation");
    assert.deepEqual(stopping.cards.filter(({ checkbox }) => checkbox.checked).map(({ checkbox }) => checkbox.value), ["1"]);
});

test("restoring a submitted deletion clears its confirmation before another action", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    page.clickRemove();
    assert.equal(page.submit("remove").defaultPrevented, false);
    assert.equal(page.form.querySelector('input[name="confirmed"]').value, "yes");
    await page.pageshow();
    assert.equal(page.form.querySelector('input[name="confirmed"]'), null);
    assert.equal(page.form.querySelector('input[name="operation"]'), null);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    await page.poll();
    assert.equal(page.submit("remove").defaultPrevented, true, "Restored pages require a new two-click confirmation");
    assert.equal(page.form.querySelector('input[name="confirmed"]'), null);
    assert.equal(page.submit("refresh").defaultPrevented, false);
    assert.equal(page.form.querySelector('input[name="confirmed"]'), null);
});

test("expired unlock never creates a deletion confirmation", () => {
    const page = boot();
    page.select(0);
    page.unlock();
    page.advanceTime(10000, { runTimers: false });
    assert.equal(page.submit("remove").defaultPrevented, true);
    assert.equal(page.form.querySelector('input[name="confirmed"]'), null);
});

test("new Git or PDF work leaves accidental-delete consent intact and animates refresh", async () => {
    for (const [work, activity, stopEnabled] of [
        [{ active: true, hasActiveWork: true, state: "fetching" }, { queuedPdfs: 0, runningPdfs: 0 }, true],
        [{ active: false, hasActiveWork: true }, { queuedPdfs: 2, runningPdfs: 1 }, true],
    ]) {
        const page = boot();
        page.select(0);
        page.unlock();
        const timer = page.cards[0].timer;
        const timing = { active: true, label: work.active ? "Refreshing" : "Reading PDFs", startedAt: "2026-08-30T11:00:00Z" };
        await page.poll({ overrides: { 1: { ...work, activity, workerTiming: timing } } });
        assert.equal(page.controls.remove.disabled, false);
        assert.equal(page.controls.remove.dataset.deleteLocked, "false");
        assert.equal(page.controls.deleteIcon.textContent, "🗑️");
        assert.equal(page.deleteTimers().length, 1);
        assert.match(page.controls.remove.title, /click again to delete/i);
        assert.equal(page.controls.stop.disabled, !stopEnabled);
        assert.equal(page.global.button.disabled, true);
        assert.equal(page.global.spinner.hidden, false);
        assert.equal(page.global.icon.hidden, true);
        assert.equal(page.global.icon.hasAttribute("hidden"), true, "SVG visibility needs an explicit hidden attribute");
        assert.equal(page.controls.icon.hasAttribute("hidden"), true, "Selected refresh SVG must not appear beside its spinner");
        assert.equal(page.cards[0].timer, timer, "Worker timer element remains intact");
        assert.equal(page.cards[0].timer, timer);
        assert.equal(page.reloads(), 0);
        await page.poll();
        assert.equal(page.global.icon.hidden, false);
        assert.equal(page.global.icon.hasAttribute("hidden"), false, "Idle removes the SVG attribute, including server-rendered hidden state");
        assert.equal(page.controls.icon.hidden, false);
        assert.equal(page.controls.icon.hasAttribute("hidden"), false, "Selected refresh SVG becomes visible after workers finish");
        assert.equal(page.global.spinner.hidden, true);
        assert.equal(page.controls.spinner.hidden, true);
        assert.equal(page.controls.remove.dataset.deleteLocked, "false", "Worker completion does not rewrite the user's consent timer");
    }
});

test("queued and running PDF indexing can be stopped or deleted without a worker UI lock", async () => {
    for (const activity of [
        { active: true, queuedPdfs: 4, runningPdfs: 0 },
        { active: true, queuedPdfs: 2, runningPdfs: 2 },
    ]) {
        const page = boot();
        page.select(0);
        await page.poll({ overrides: { 1: { hasActiveWork: true, activity } } });
        assert.equal(page.controls.stop.disabled, false);
        assert.match(page.controls.stop.title, /stop active git and pdf work/i);
        assert.equal(page.controls.remove.disabled, false);
        page.unlock();
        await page.poll({ overrides: { 1: { hasActiveWork: true, activity } } });
        assert.equal(page.controls.remove.dataset.deleteLocked, "false", "PDF status polling cannot revoke accidental-delete consent");
        assert.equal(page.clickRemove().defaultPrevented, false);
        assert.equal(page.submit("remove").defaultPrevented, false);
        assert.equal(page.form.querySelector('input[name="confirmed"]').value, "yes");
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

test("a reopened page rebuilds overall progress and compact worker counts", async () => {
    const persistedWork = {
        active: true,
        label: "Extracting PDF text",
        detail: "Repository 1: Extracting PDF text",
        activeRepositories: 1,
        queuedPdfs: 6,
        runningPdfs: 4,
        activities: [{
            operation: "indexing", progress: 40, count: 10,
            startedAt: "1970-01-01T00:00:40.000Z",
        }],
    };
    const currentState = {
        overrides: { 1: {
            hasActiveWork: true,
            workerTiming: {
                active: true, kind: "indexing", label: "Extracting PDFs",
                startedAt: "1970-01-01T00:00:40.000Z",
            },
            activity: {
                active: true, queuedPdfs: 6, runningPdfs: 4,
                queuedSyncJobs: 0, runningSyncJobs: 0,
                pdfCounts: { passed: 12, failed: 1, interrupted: 0, cancelled: 0 },
                operations: [{ operation: "indexing", progress: 40, count: 10 }],
            },
        } },
        extraction: { active: true, queuedJobs: 6, runningJobs: 4 },
        workerLimits: { git: 4, indexing: 10, total: 14 },
        work: persistedWork,
    };

    const originalPage = boot({ repositories: [{ id: 1 }] });
    await originalPage.poll(currentState);
    const reopenedPage = boot({ repositories: [{ id: 1 }] });
    await reopenedPage.poll(currentState);

    for (const page of [originalPage, reopenedPage]) {
        assert.equal(page.global.progress.hidden, false);
        assert.equal(page.global.progressBar.value, 58);
        assert.equal(page.global.progressLabel.textContent, "58%");
        assert.match(page.global.counts.textContent, /Workers 4 running \/ 14 total/);
        assert.match(page.global.pdfCounts.textContent, /PDF · 10 remaining · 12 passed · 4 running · 1 failed · 0 cancelled/);
        assert.match(page.global.gitCounts.textContent, /Git · 0 queued · 0 working · 0 completed/);
        assert.equal(page.global.timing.hidden, true);
    }
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
    assert.equal(page.controls.remove.dataset.deleteLocked, "true", "Returning to idle never restores old unlock consent");
});

test("a status outage relocks consent but never turns the delete control into a worker lock", async () => {
    const page = boot();
    page.select(0);
    page.unlock();
    await page.poll({ fail: true });
    assert.equal(page.controls.remove.disabled, false);
    assert.equal(page.controls.remove.dataset.deleteLocked, "true");
    assert.match(page.controls.remove.title, /unlock deletion/i);
    assert.equal(page.controls.refresh.disabled, true);
    assert.equal(page.global.button.disabled, true);
    await page.poll();
    assert.equal(page.controls.remove.disabled, false, "Fresh idle status allows a new first click");
    assert.equal(page.controls.remove.dataset.deleteLocked, "true", "Recovery never restores earlier consent");
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

const pdfActivity = (queued, running) => ({
    active: queued + running > 0,
    kind: queued + running ? "indexing" : "idle",
    phase: running ? "extracting" : queued ? "pdf_queued" : "idle",
    label: running ? "Extracting PDF text" : queued ? "PDF extraction queued" : "Ready",
    detail: running ? `${running} PDFs extracting · ${queued} queued` : queued ? `${queued} PDFs queued` : "",
    queuedPdfs: queued, runningPdfs: running,
    queuedSyncJobs: 0, runningSyncJobs: 0, pendingCleanupJobs: 0,
});

test("repository polling selects clone, pull and indexing states with determinate or honest indeterminate progress", async () => {
    const page = boot({ repositories: [{ id: 1 }] });
    const card = page.cards[0];

    await page.poll({ overrides: { 1: {
        active: true, hasActiveWork: true, state: "cloning",
        activity: {
            active: true, operation: "clone", phase: "cloning", progress: 42,
            label: "Git clone", detail: "Receiving Git objects",
        },
    } } });
    assert.equal(card.card.dataset.repositoryOperation, "clone");
    assert.equal(card.stateIcon.dataset.repositoryOperation, "clone");
    assert.match(card.stateIcon.className, /bb-repository-state--working/);
    assert.equal(card.progressContainer.hidden, false);
    assert.equal(card.progressContainer.title, "Receiving Git objects");
    assert.equal(card.progressBar.getAttribute("value"), "42");
    assert.equal(card.progressBar.value, "42");
    assert.equal(card.progressLabel.textContent, "42%");

    await page.poll({ overrides: { 1: {
        active: true, hasActiveWork: true, state: "queued",
        activity: {
            active: true, operation: "pull", phase: "sync_queued", progress: null,
            label: "Git pull queued", detail: "Waiting for a Git worker",
        },
    } } });
    assert.equal(card.card.dataset.repositoryOperation, "pull");
    assert.equal(card.stateIcon.dataset.repositoryOperation, "pull");
    assert.equal(card.progressContainer.hidden, true);
    assert.equal(card.progressBar.hasAttribute("value"), false, "Queued pull stays indeterminate");
    assert.equal(card.progressLabel.textContent, "Running");

    await page.poll({ overrides: { 1: {
        active: false, hasActiveWork: true, state: "ready",
        activity: {
            active: true, operation: "indexing", phase: "extracting", progress: null,
            label: "PDF indexing", detail: "Four PDF workers active",
            queuedPdfs: 12, runningPdfs: 4,
        },
    } } });
    assert.equal(card.card.dataset.repositoryOperation, "indexing");
    assert.equal(card.stateIcon.dataset.repositoryOperation, "indexing");
    assert.match(card.stateIcon.className, /bb-repository-state--working/);
    assert.equal(card.progressContainer.hidden, false);
    assert.equal(card.progressBar.hasAttribute("value"), false, "Unknown indexing percent is not fabricated");
    assert.equal(card.progressLabel.textContent, "Running");

    await page.poll({ overrides: { 1: {
        active: false, hasActiveWork: false, state: "ready",
        activity: {
            active: false, operation: "idle", phase: "idle", progress: null,
            label: "Ready", detail: "",
        },
    } } });
    assert.equal(card.card.dataset.repositoryOperation, "");
    assert.equal(card.stateIcon.dataset.repositoryOperation, "");
    assert.match(card.stateIcon.className, /bb-repository-state--ready/);
    assert.equal(card.progressContainer.hidden, true);
    assert.equal(card.progressBar.hasAttribute("value"), false);
});

test("repository polling updates the separate worker heartbeat health signal", async () => {
    const page = boot({ repositories: [{ id: 1 }] });
    const card = page.cards[0];
    const activity = {
        active: true, operation: "indexing", phase: "extracting", progress: 45,
        label: "Extracting PDF text", detail: "2 PDFs extracting",
        queuedPdfs: 3, runningPdfs: 2,
    };

    await page.poll({ overrides: { 1: {
        hasActiveWork: true,
        activity: {
            ...activity, healthState: "healthy", healthLabel: "Worker responding",
            heartbeatAt: "2026-09-02T10:00:00+00:00",
        },
    } } });
    assert.equal(card.workLabel.textContent, "2 PDFs extracting");
    assert.equal(card.health.hidden, false);
    assert.equal(card.health.textContent, "Worker responding");
    assert.equal(card.health.dataset.healthState, "healthy");
    assert.equal(card.health.dataset.heartbeatAt, "2026-09-02T10:00:00+00:00");
    assert.equal(card.card.dataset.repositoryHealthState, "healthy");

    await page.poll({ overrides: { 1: {
        hasActiveWork: true,
        activity: {
            ...activity, healthState: "stalled",
            healthLabel: "Worker stalled · restarting automatically",
            heartbeatAt: "2026-09-02T09:49:00+00:00",
        },
    } } });
    assert.equal(card.workLabel.textContent, "2 PDFs extracting", "health never rewrites activity detail");
    assert.equal(card.health.textContent, "Worker stalled · restarting automatically");
    assert.equal(card.health.dataset.healthState, "stalled");

    await page.poll({ overrides: { 1: {
        hasActiveWork: false,
        activity: { active: false, healthState: null, healthLabel: "", heartbeatAt: null },
    } } });
    assert.equal(card.health.hidden, true);
    assert.equal(card.health.textContent, "");
    assert.equal(card.card.dataset.repositoryHealthState, "");
});

test("ready Git repositories display their queued and running PDF work instead of a green tick", async () => {
    const page = boot({ duplicateCopies: true });
    const timers = page.cards.map(({ timer }) => timer);
    for (const [queued, running] of [[7, 0], [5, 2]]) {
        const activity = pdfActivity(queued, running);
        const timing = running
            ? { active: true, label: "Extracting PDFs", kind: "indexing", startedAt: "2026-08-31T10:00:00Z" }
            : { active: false, label: "", kind: "", startedAt: "" };
        const work = {
            active: true, label: activity.label,
            detail: `Repository 1: ${activity.detail}`,
            activeRepositories: 1, queuedPdfs: queued, runningPdfs: running,
        };
        await page.poll({
            overrides: { 1: { active: false, state: "ready", hasActiveWork: true, activity, workerTiming: timing } },
            extraction: { active: true, queuedJobs: queued, runningJobs: running }, work,
        });
        for (const card of page.cards.slice(0, 2)) {
            assert.equal(card.card.dataset.repositoryState, "ready", "Git completion remains independent of PDF worker state");
            assert.match(card.stateIcon.className, /bb-repository-state--working/);
            assert.doesNotMatch(card.stateIcon.className, /bb-repository-state--ready/);
            assert.match(card.stateIcon.getAttribute("aria-label"), new RegExp(activity.label));
            assert.equal(card.workLabel.hidden, running === 0);
            assert.equal(card.workLabel.textContent, running ? activity.detail : "");
            assert.equal(card.remaining.hidden, false);
            assert.equal(card.remaining.textContent, `Remaining ${queued + running} PDFs`);
            assert.ok(card.timer);
        }
        for (const card of page.cards.slice(2)) {
            assert.match(card.stateIcon.className, /bb-repository-state--ready/);
            assert.equal(card.workLabel.hidden, true, "An unrelated idle repository is not shown as extracting");
        }
        assert.equal(page.global.spinner.hidden, false);
        assert.equal(page.global.icon.hasAttribute("hidden"), true);
        assert.equal(page.global.button.disabled, true);
        assert.equal(page.global.label.textContent, work.label);
        assert.equal(page.global.detail.textContent, work.detail);
        assert.match(page.global.button.title, /Repository 1/);
        assert.match(page.global.button.title, new RegExp(String(queued)));
        assert.equal(page.reloads(), 0);
    }
    assert.deepEqual(page.cards.map(({ timer }) => timer), timers, "Status updates never replace existing worker timer elements");
    await page.poll({
        overrides: { 1: { activity: pdfActivity(0, 0) } },
        extraction: { failedJobs: 2, interruptedJobs: 1, pendingDocuments: 3 },
        work: { active: false, label: "", detail: "", activeRepositories: 0, queuedPdfs: 0, runningPdfs: 0 },
    });
    assert.equal(page.global.spinner.hidden, true, "Terminal failed/interrupted jobs do not leave an indefinite spinner");
    assert.equal(page.global.icon.hasAttribute("hidden"), false);
    assert.equal(page.global.button.disabled, false);
    for (const card of page.cards) {
        assert.match(card.stateIcon.className, /bb-repository-state--ready/);
        assert.equal(card.workLabel.hidden, true);
    }
    assert.equal(page.reloads(), 0, "The first settled snapshot still waits for confirmation");
    await page.poll();
    assert.equal(page.reloads(), 1, "One final reload publishes the completed batch");
});

test("status failure explicitly shows unknown state and resumes real PDF status on recovery", async () => {
    const page = boot();
    const activity = pdfActivity(3, 1);
    const current = {
        overrides: { 1: { hasActiveWork: true, activity } },
        extraction: { active: true, queuedJobs: 3, runningJobs: 1 },
        work: { active: true, label: activity.label, detail: `Repository 1: ${activity.detail}`, activeRepositories: 1, queuedPdfs: 3, runningPdfs: 1 },
    };
    await page.poll(current);
    await page.poll({ fail: true });
    for (const card of page.cards) {
        assert.match(card.stateIcon.className, /bb-repository-state--unknown/);
        assert.doesNotMatch(card.stateIcon.className, /bb-repository-state--ready/);
        assert.equal(card.workLabel.hidden, false);
        assert.match(card.workLabel.textContent, /Status unavailable/i);
        assert.match(card.stateIcon.getAttribute("aria-label"), /Status unavailable/i);
        assert.equal(page.staleTimers.includes(card.timer), false);
    }
    assert.equal(page.global.button.disabled, true);
    assert.equal(page.reloads(), 0);
    await page.poll(current);
    assert.match(page.cards[0].stateIcon.className, /bb-repository-state--working/);
    assert.equal(page.cards[0].workLabel.textContent, activity.detail);
    assert.match(page.cards[1].stateIcon.className, /bb-repository-state--ready/);
    assert.equal(page.cards[1].workLabel.hidden, true);
    assert.equal(page.global.detail.textContent, current.work.detail);
    assert.equal(page.reloads(), 0);
});

test("the refresh tooltip attributes simultaneous background work to its repositories", async () => {
    const page = boot();
    const activity = pdfActivity(12, 3);
    const cloning = {
        active: true, kind: "sync", phase: "cloning", label: "Cloning repository",
        detail: "Downloading the repository", queuedPdfs: 0, runningPdfs: 0,
        queuedSyncJobs: 0, runningSyncJobs: 1, pendingCleanupJobs: 0,
    };
    const work = {
        active: true, label: "Cloning repository · Extracting PDF text",
        detail: "Repository 1: Cloning repository · Repository 2: 3 PDFs extracting · 12 queued",
        activeRepositories: 2, queuedPdfs: 12, runningPdfs: 3,
    };
    await page.poll({
        overrides: {
            1: { active: true, hasActiveWork: true, state: "cloning", activity: cloning },
            2: { active: false, hasActiveWork: true, state: "ready", activity },
        },
        extraction: { active: true, queuedJobs: 12, runningJobs: 3 }, work,
    });
    assert.equal(page.global.label.textContent, work.label);
    assert.equal(page.global.detail.textContent, work.detail);
    for (const expected of ["Repository 1", "Repository 2", "12", "3"]) {
        assert.ok(page.global.button.title.includes(expected), `Refresh tooltip identifies ${expected}`);
    }
    assert.equal(page.global.button.getAttribute("aria-busy"), "true");
    assert.equal(page.reloads(), 0);
});
