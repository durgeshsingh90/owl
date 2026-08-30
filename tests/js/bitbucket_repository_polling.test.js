const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../static/bitbucket_search/bitbucket_search.js"), "utf8");

const repository = (active = false, overrides = {}) => ({
    id: 1, name: "Synthetic repository", state: active ? "fetching" : "ready",
    stateLabel: active ? "Refreshing" : "Ready", enabled: true, active,
    pdfCount: 1, vsdxCount: 0,
    ...overrides,
});

const snapshot = (overrides = {}) => ({
    repositories: [repository()],
    totals: { repositories: 1, pdfs: 1, vsdx: 0, bytesLabel: "10 KB" },
    automation: { enabled: true },
    catalog: { publicationSignature: "catalog-initial" },
    extraction: {
        active: false, queuedJobs: 0, runningJobs: 0, pendingDocuments: 0,
        indexedDocuments: 1, publicationSignature: "extraction-initial",
    },
    ...overrides,
});

function boot(responses, { initiallyActive = false, initiallyExtracting = false } = {}) {
    const statusPanel = { hidden: true };
    const notificationPanel = { hidden: true };
    const log = { open: true, scrollTop: 25, textContent: "Git output completed.", focusCount: 1 };
    const requests = [];
    const timers = new Map();
    let timerId = 0;
    let reloadCount = 0;
    let lastResponse;
    const workspace = {
        dataset: {
            repositoryStatusUrl: "/pdfs/repositories/status/",
            catalogPublicationSignature: "catalog-initial",
            extractionPublicationSignature: "extraction-initial",
            extractionActive: String(initiallyExtracting),
            dailyRefreshEnabled: "true",
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
    };
    const document = {
        activeElement: log,
        querySelector: (selector) => selector === "[data-bitbucket-workspace]" ? workspace : null,
        querySelectorAll(selector) {
            if (selector === "[data-repository-status-panel], [data-notification-panel]") {
                return [statusPanel, notificationPanel];
            }
            if (initiallyActive && selector.startsWith('[data-repository-state="queued"]')) {
                return [{ dataset: { repositoryId: "1" } }];
            }
            return [];
        },
    };
    const window = {
        location: { reload() { reloadCount += 1; } },
        addEventListener() {},
        setTimeout(callback, delay) {
            const id = ++timerId;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
    };
    const fetch = async (url, options) => {
        requests.push({ url, options });
        const response = responses.length ? responses.shift() : lastResponse;
        if (response instanceof Error) { throw response; }
        lastResponse = response;
        return { ok: true, async json() { return response; } };
    };
    vm.runInNewContext(source, { document, window, fetch });
    return {
        statusPanel, notificationPanel, log, document, requests,
        reloadCount: () => reloadCount,
        pendingPolls: () => timers.size,
        async poll() {
            const scheduled = [...timers].find(([, timer]) => timer.callback.name === "poll");
            assert.ok(scheduled, "The existing repository poll remains scheduled");
            timers.delete(scheduled[0]);
            await scheduled[1].callback();
            return scheduled[1].delay;
        },
    };
}

test("terminal transitions and publication changes reload once after two settled snapshots", async () => {
    for (const [response, initiallyActive] of [
        [snapshot(), true],
        [snapshot({ catalog: { publicationSignature: "catalog-next" } }), false],
        [snapshot({ extraction: { active: false, publicationSignature: "extraction-next" } }), false],
    ]) {
        const page = boot([response], { initiallyActive });
        await page.poll();
        assert.equal(page.reloadCount(), 0, "The first idle snapshot waits for confirmation");
        assert.equal(await page.poll(), 1500, "Confirmation uses the active cadence");
        assert.equal(page.reloadCount(), 1);
        assert.equal(page.pendingPolls(), 0);
    }
});

test("a terminal worker leaves an open status log readable and reloads after closing", async () => {
    const page = boot([snapshot(), snapshot()], { initiallyActive: true });
    page.statusPanel.hidden = false;
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    assert.equal(page.document.activeElement, page.log);
    assert.equal(page.log.open, true);
    assert.equal(page.log.scrollTop, 25);
    assert.equal(page.log.textContent, "Git output completed.");
    assert.equal(page.log.focusCount, 1);
    page.statusPanel.hidden = true;
    assert.equal(await page.poll(), 1500, "An idle confirmation remains scheduled");
    assert.equal(page.reloadCount(), 1, "Completion stays pending after active IDs have been cleared");
});

test("notification history also defers catalogue and extraction updates until all panels close", async () => {
    const response = snapshot({
        catalog: { publicationSignature: "catalog-next" },
        extraction: { active: false, publicationSignature: "extraction-next" },
    });
    const page = boot([response, response, response]);
    page.notificationPanel.hidden = false;
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    page.notificationPanel.hidden = true;
    page.statusPanel.hidden = false;
    await page.poll();
    assert.equal(page.reloadCount(), 0, "Switching panels does not discard the pending update");
    page.statusPanel.hidden = true;
    await page.poll();
    assert.equal(page.reloadCount(), 1, "Advanced publication signatures do not lose the deferred reload");
    assert.ok(page.requests.every(({ options }) => !options.method || options.method === "GET"));
});

test("a pending reload survives a temporary status failure and is applied after recovery", async () => {
    const response = snapshot({ catalog: { publicationSignature: "catalog-next" } });
    const page = boot([response, new Error("offline"), response, response]);
    page.statusPanel.hidden = false;
    await page.poll();
    page.statusPanel.hidden = true;
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    await page.poll();
    assert.equal(page.reloadCount(), 0, "Recovery must confirm idle again");
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("deferred catalogue updates keep a poll even if repositories and automation disappear", async () => {
    const response = snapshot({
        repositories: [], automation: { enabled: false },
        catalog: { publicationSignature: "catalog-empty" },
    });
    const page = boot([response, response]);
    page.statusPanel.hidden = false;
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    assert.equal(page.pendingPolls(), 1);
    page.statusPanel.hidden = true;
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("unchanged publications do not reload simply because a panel opens or closes", async () => {
    const page = boot([snapshot(), snapshot()]);
    page.statusPanel.hidden = false;
    await page.poll();
    page.statusPanel.hidden = true;
    await page.poll();
    assert.equal(page.reloadCount(), 0);
});

test("multiple repositories and intermediate PDF publications never reload while workers remain", async () => {
    const extraction = (active, version) => ({
        active, runningJobs: active ? 2 : 0, queuedJobs: active ? 8 : 0,
        pendingDocuments: active ? 10 : 0, indexedDocuments: active ? 5 : 15,
        publicationSignature: `extraction-${version}`,
    });
    const responses = [
        snapshot({ repositories: [repository(true), repository(true, { id: 2 })] }),
        snapshot({
            repositories: [repository(), repository(true, { id: 2 })],
            catalog: { publicationSignature: "catalog-one" }, extraction: extraction(true, 1),
        }),
        snapshot({
            repositories: [repository(), repository(true, { id: 2 })],
            catalog: { publicationSignature: "catalog-one" }, extraction: extraction(true, 2),
        }),
        snapshot({
            repositories: [repository(), repository(false, { id: 2 })],
            catalog: { publicationSignature: "catalog-two" }, extraction: extraction(true, 3),
        }),
        snapshot({
            repositories: [repository(), repository(false, { id: 2 })],
            catalog: { publicationSignature: "catalog-two" }, extraction: extraction(false, 4),
        }),
    ];
    const page = boot(responses, { initiallyActive: true });
    for (let step = 0; step < 5; step += 1) {
        await page.poll();
        assert.equal(page.reloadCount(), 0, `No navigation during batch step ${step}`);
    }
    assert.equal(await page.poll(), 1500);
    assert.equal(page.reloadCount(), 1, "Only the full settled batch navigates");
    assert.equal(page.pendingPolls(), 0);
});

test("a new queue handoff resets idle confirmation before the single reload", async () => {
    const changed = snapshot({ catalog: { publicationSignature: "catalog-next" } });
    const page = boot([
        changed,
        { ...changed, repositories: [repository(true)] },
        { ...changed, extraction: { active: true, queuedJobs: 2, publicationSignature: "extract-queued" } },
        { ...changed, extraction: { active: false, publicationSignature: "extract-finished" } },
    ]);
    for (let step = 0; step < 4; step += 1) {
        await page.poll();
        assert.equal(page.reloadCount(), 0);
    }
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("queued/running extraction counts prevent an inconsistent false active flag from reloading", async () => {
    const page = boot([
        snapshot({ extraction: { active: false, queuedJobs: 3, publicationSignature: "queued" } }),
        snapshot({ extraction: { active: false, runningJobs: 2, publicationSignature: "running" } }),
        snapshot({ extraction: { active: false, publicationSignature: "complete" } }),
    ]);
    for (let step = 0; step < 3; step += 1) {
        await page.poll();
        assert.equal(page.reloadCount(), 0);
    }
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("failed and interrupted jobs settle even if their documents remain pending", async () => {
    const page = boot([snapshot({
        repositories: [repository(false, { state: "failed", stateLabel: "Needs attention" })],
        extraction: {
            active: false, queuedJobs: 0, runningJobs: 0, failedJobs: 4,
            interruptedJobs: 1, pendingDocuments: 5, publicationSignature: "failures-final",
        },
    })], { initiallyActive: true, initiallyExtracting: true });
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("extraction cancellation is a terminal transition even without a changed publication", async () => {
    const page = boot([snapshot()], { initiallyExtracting: true });
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});

test("settled work waits for an open status panel and a later new worker starts a new wait", async () => {
    const changed = snapshot({ catalog: { publicationSignature: "catalog-next" } });
    const page = boot([changed, changed, { ...changed, repositories: [repository(true)] }, changed, changed]);
    page.statusPanel.hidden = false;
    await page.poll();
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    page.statusPanel.hidden = true;
    await page.poll();
    assert.equal(page.reloadCount(), 0, "Closing the panel cannot bypass a newly active worker");
    await page.poll();
    assert.equal(page.reloadCount(), 0);
    await page.poll();
    assert.equal(page.reloadCount(), 1);
});
