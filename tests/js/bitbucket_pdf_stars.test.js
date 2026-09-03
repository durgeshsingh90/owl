const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
    path.join(__dirname, "../../static/bitbucket_search/pdf_stars.js"),
    "utf8",
);

class StarButton {
    constructor(documentId, filename, starred = false) {
        this.dataset = {
            documentId: String(documentId),
            pdfStarName: filename,
            tooltip: starred ? "Remove star" : "Star PDF",
        };
        this.disabled = false;
        this.formAction =
            `http://127.0.0.1/pdfs/documents/${documentId}/star/` +
            `?starred=${String(!starred)}`;
        this.title = starred
            ? `Remove star from ${filename} in OWL`
            : `Star ${filename} in OWL`;
        this.attributes = new Map([
            ["aria-label", `Star PDF: ${filename}`],
            ["aria-pressed", String(starred)],
        ]);
        this.icon = { textContent: starred ? "★" : "☆" };
        this.row = { dataset: { pdfStarred: String(starred) } };
    }

    matches(selector) {
        return selector === "[data-pdf-star-toggle]";
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.get(name) ?? null;
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    querySelector(selector) {
        return selector === "[data-pdf-star-icon]" ? this.icon : null;
    }

    closest(selector) {
        return selector === "[data-pdf-row]" ? this.row : null;
    }
}

class StarForm {
    constructor() {
        this.values = new Map([
            ["csrfmiddlewaretoken", "csrf-token"],
            ["return_to", "/pdfs/?q=architecture"],
        ]);
        this.listeners = new Map();
    }

    addEventListener(name, listener) {
        this.listeners.set(name, listener);
    }

    querySelector(selector) {
        if (selector === "input[name='csrfmiddlewaretoken']") {
            return { value: this.values.get("csrfmiddlewaretoken") };
        }
        return null;
    }

    submitWith(submitter) {
        let prevented = false;
        const result = this.listeners.get("submit")?.({
            submitter,
            preventDefault() {
                prevented = true;
            },
        });
        return { prevented: () => prevented, result: Promise.resolve(result) };
    }
}

class TestFormData {
    constructor(form) {
        this.values = new Map(form.values);
    }

    set(name, value) {
        this.values.set(name, String(value));
    }

    get(name) {
        return this.values.get(name) ?? null;
    }
}

const response = (payload, { ok = true } = {}) => ({
    ok,
    async json() {
        return payload;
    },
});

function boot({
    fetchImplementation,
    pdfs = [{ id: 1, filename: "Plan.pdf" }],
    locationHref = "http://127.0.0.1/pdfs/?q=architecture",
} = {}) {
    const form = new StarForm();
    const buttons = pdfs.map(
        ({ id, filename, starred = false }) => new StarButton(id, filename, starred),
    );
    const status = { textContent: "" };
    const workspace = {
        querySelector(selector) {
            return selector === "[data-pdf-star-status]" ? status : null;
        },
        querySelectorAll(selector) {
            return selector === "[data-pdf-star-toggle]" ? buttons : [];
        },
    };
    const document = {
        querySelector(selector) {
            if (selector === "[data-bitbucket-workspace]") return workspace;
            if (selector === "[data-pdf-star-form]") return form;
            return null;
        },
    };
    const requests = [];
    const location = {
        href: locationHref,
        reloadCount: 0,
        reload() {
            this.reloadCount += 1;
        },
    };
    const window = {
        FormData: TestFormData,
        URL,
        location,
        fetch: async (...arguments_) => {
            requests.push(arguments_);
            return fetchImplementation(...arguments_);
        },
    };
    vm.runInNewContext(source, { document, window }, { filename: "pdf_stars.js" });
    return { buttons, form, location, requests, status };
}

test("successful PDF stars post desired state and render the server response", async () => {
    const payloads = [
        {
            state: "success",
            label: "PDF updated",
            detail: "Starred Plan.pdf",
            documentId: 1,
            filename: "Plan.pdf",
            starred: true,
        },
        {
            state: "success",
            label: "PDF updated",
            detail: "Removed star from Plan.pdf",
            documentId: 1,
            filename: "Plan.pdf",
            starred: false,
        },
    ];
    const page = boot({ fetchImplementation: async () => response(payloads.shift()) });

    const first = page.form.submitWith(page.buttons[0]);
    assert.equal(first.prevented(), true);
    await first.result;

    assert.equal(page.requests.length, 1);
    const [url, options] = page.requests[0];
    assert.equal(url, "http://127.0.0.1/pdfs/documents/1/star/?starred=true");
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers.Accept, "application/json");
    assert.equal(options.headers["X-CSRFToken"], "csrf-token");
    assert.equal(options.headers["X-Requested-With"], "XMLHttpRequest");
    assert.equal(options.body.get("starred"), "true");
    assert.equal(options.body.get("return_to"), "/pdfs/?q=architecture");
    assert.equal(page.buttons[0].getAttribute("aria-label"), "Star PDF: Plan.pdf");
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "true");
    assert.equal(page.buttons[0].getAttribute("aria-busy"), null);
    assert.equal(page.buttons[0].title, "Remove star from Plan.pdf in OWL");
    assert.equal(page.buttons[0].dataset.tooltip, "Remove star");
    assert.equal(
        page.buttons[0].formAction,
        "http://127.0.0.1/pdfs/documents/1/star/?starred=false",
    );
    assert.equal(page.buttons[0].icon.textContent, "★");
    assert.equal(page.buttons[0].row.dataset.pdfStarred, "true");
    assert.equal(page.status.textContent, "Starred Plan.pdf");

    const second = page.form.submitWith(page.buttons[0]);
    await second.result;
    assert.equal(page.requests[1][1].body.get("starred"), "false");
    assert.equal(page.buttons[0].getAttribute("aria-label"), "Star PDF: Plan.pdf");
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "false");
    assert.equal(page.buttons[0].title, "Star Plan.pdf in OWL");
    assert.equal(page.buttons[0].dataset.tooltip, "Star PDF");
    assert.equal(page.buttons[0].icon.textContent, "☆");
    assert.equal(page.buttons[0].row.dataset.pdfStarred, "false");
    assert.equal(page.status.textContent, "Removed star from Plan.pdf");
});

test("one pending PDF star locks every document and prevents a competing write", async () => {
    let finishRequest;
    const page = boot({
        fetchImplementation: () =>
            new Promise((resolve) => {
                finishRequest = resolve;
            }),
        pdfs: [
            { id: 1, filename: "Plan.pdf" },
            { id: 2, filename: "Controls.pdf" },
        ],
    });

    const first = page.form.submitWith(page.buttons[0]);
    await Promise.resolve();
    assert.ok(page.buttons.every((button) => button.disabled));
    assert.ok(page.buttons.every((button) => button.getAttribute("aria-busy") === "true"));

    const competing = page.form.submitWith(page.buttons[1]);
    assert.equal(competing.prevented(), true);
    await competing.result;
    assert.equal(page.requests.length, 1);

    finishRequest(
        response({
            state: "success",
            label: "PDF updated",
            detail: "Starred Plan.pdf",
            documentId: 1,
            filename: "Plan.pdf",
            starred: true,
        }),
    );
    await first.result;
    assert.ok(page.buttons.every((button) => !button.disabled));
    assert.equal(page.buttons[1].getAttribute("aria-pressed"), "false");
});

test("a malformed response can be retried with the same desired PDF state", async () => {
    const responses = [
        response({ state: "success", documentId: 1, starred: true }),
        response({
            state: "success",
            detail: "Starred Plan.pdf",
            documentId: 1,
            filename: "Plan.pdf",
            starred: true,
        }),
        response({
            state: "success",
            detail: "Removed star from Plan.pdf",
            documentId: 1,
            filename: "Plan.pdf",
            starred: false,
        }),
    ];
    const page = boot({ fetchImplementation: async () => responses.shift() });

    let submission = page.form.submitWith(page.buttons[0]);
    await submission.result;
    assert.equal(page.status.textContent, "OWL returned an incomplete PDF-star response.");
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "false");
    assert.equal(page.buttons[0].formAction.endsWith("?starred=true"), true);

    submission = page.form.submitWith(page.buttons[0]);
    await submission.result;
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "true");

    submission = page.form.submitWith(page.buttons[0]);
    await submission.result;
    assert.deepEqual(
        page.requests.map(([, options]) => options.body.get("starred")),
        ["true", "true", "false"],
    );
    assert.deepEqual(
        page.requests.map(([url]) => new URL(url).searchParams.get("starred")),
        ["true", "true", "false"],
    );
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "false");
});

test("an HTTP error keeps the rendered PDF state and announces backend detail", async () => {
    const page = boot({
        fetchImplementation: async () =>
            response({ detail: "That PDF is no longer available." }, { ok: false }),
        pdfs: [{ id: 7, filename: "Archive.pdf", starred: true }],
    });

    const submission = page.form.submitWith(page.buttons[0]);
    await submission.result;

    assert.equal(page.requests[0][1].body.get("starred"), "false");
    assert.equal(page.status.textContent, "That PDF is no longer available.");
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "true");
    assert.equal(page.buttons[0].icon.textContent, "★");
    assert.equal(page.buttons[0].disabled, false);
});

test("starred-first results use native submission so redirect feedback and ordering are preserved", async () => {
    const page = boot({
        fetchImplementation: async () => {
            throw new Error("The native submission must not fetch.");
        },
        locationHref: "http://127.0.0.1/pdfs/?sort=starred_first",
    });

    const submission = page.form.submitWith(page.buttons[0]);
    await submission.result;

    assert.equal(submission.prevented(), false);
    assert.equal(page.requests.length, 0);
    assert.equal(page.buttons[0].getAttribute("aria-pressed"), "false");
    assert.equal(page.status.textContent, "");
    assert.equal(page.location.reloadCount, 0);
});

test("missing enhancement prerequisites preserve the native PDF form fallback", () => {
    const form = new StarForm();
    const document = {
        querySelector(selector) {
            if (selector === "[data-bitbucket-workspace]") return { querySelector() {} };
            if (selector === "[data-pdf-star-form]") return form;
            return null;
        },
    };

    assert.doesNotThrow(() =>
        vm.runInNewContext(source, { document, window: {} }, { filename: "pdf_stars.js" }),
    );
    assert.equal(form.listeners.size, 0);
});
