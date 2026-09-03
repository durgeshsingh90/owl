const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
    path.join(__dirname, "../../static/bitbucket_search/people_stars.js"),
    "utf8",
);

class StarButton {
    constructor(identityKey, personName, starred = false) {
        this.dataset = {
            peopleIdentityKey: identityKey,
            peopleStarName: personName,
        };
        this.value = identityKey;
        this.disabled = false;
        this.title = starred
            ? `Remove star from ${personName} locally in OWL`
            : `Star ${personName} locally in OWL`;
        this.formAction = `/pdfs/people/star/?starred=${String(!starred)}`;
        this.attributes = new Map([
            ["aria-label", `Star ${personName}`],
            ["aria-pressed", String(starred)],
        ]);
        this.icon = { textContent: starred ? "★" : "☆" };
        this.row = { dataset: { peopleStarred: String(starred) } };
    }

    matches(selector) {
        return selector === "[data-people-star-toggle]";
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
        return selector === "[data-people-star-icon]" ? this.icon : null;
    }

    closest(selector) {
        return selector === "[data-people-filter-entry]" ? this.row : null;
    }
}

class StarForm {
    constructor() {
        this.action = "/pdfs/people/star/";
        this.values = new Map([
            ["csrfmiddlewaretoken", "csrf-token"],
            ["return_to", "/pdfs/?committer=Alice+Smith"],
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

function boot({ fetchImplementation, people = [{ identity: "alice smith", name: "Alice Smith" }] } = {}) {
    const form = new StarForm();
    const buttons = people.flatMap(({ identity, name, starred = false }) => [
        new StarButton(identity, name, starred),
        new StarButton(identity, name, starred),
    ]);
    const status = { textContent: "" };
    const workspace = {
        querySelector(selector) {
            return selector === "[data-people-star-status]" ? status : null;
        },
        querySelectorAll(selector) {
            return selector === "[data-people-star-toggle]" ? buttons : [];
        },
    };
    const document = {
        querySelector(selector) {
            if (selector === "[data-bitbucket-workspace]") return workspace;
            if (selector === "[data-people-star-form]") return form;
            return null;
        },
    };
    const requests = [];
    const window = {
        FormData: TestFormData,
        fetch: async (...arguments_) => {
            requests.push(arguments_);
            return fetchImplementation(...arguments_);
        },
    };
    vm.runInNewContext(source, { document, window }, { filename: "people_stars.js" });
    return { buttons, form, requests, status };
}

test("successful toggles mirror state, text and accessibility across both People panels", async () => {
    const payloads = [
        {
            state: "success",
            label: "Person updated",
            detail: "Starred Alice Smith",
            person: "Alice Smith",
            identity_key: "alice smith",
            starred: true,
        },
        {
            state: "success",
            label: "Person updated",
            detail: "Removed star from Alice Smith",
            person: "Alice Smith",
            identity_key: "alice smith",
            starred: false,
        },
    ];
    const page = boot({ fetchImplementation: async () => response(payloads.shift()) });

    const first = page.form.submitWith(page.buttons[0]);
    assert.equal(first.prevented(), true);
    await first.result;

    assert.equal(page.requests.length, 1);
    const [url, options] = page.requests[0];
    assert.equal(url, "/pdfs/people/star/");
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers.Accept, "application/json");
    assert.equal(options.headers["X-CSRFToken"], "csrf-token");
    assert.equal(options.headers["X-Requested-With"], "XMLHttpRequest");
    assert.equal(options.body.get("person"), "alice smith");
    assert.equal(options.body.get("starred"), "true");
    assert.equal(options.body.get("return_to"), "/pdfs/?committer=Alice+Smith");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-label"), "Star Alice Smith");
        assert.equal(button.getAttribute("aria-pressed"), "true");
        assert.equal(button.getAttribute("aria-busy"), null);
        assert.equal(button.title, "Remove star from Alice Smith locally in OWL");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=false");
        assert.equal(button.icon.textContent, "★");
        assert.equal(button.row.dataset.peopleStarred, "true");
        assert.equal(button.disabled, false);
    }
    assert.equal(page.status.textContent, "Starred Alice Smith");

    const second = page.form.submitWith(page.buttons[1]);
    await second.result;
    assert.equal(page.requests[1][1].body.get("starred"), "false");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-label"), "Star Alice Smith");
        assert.equal(button.getAttribute("aria-pressed"), "false");
        assert.equal(button.title, "Star Alice Smith locally in OWL");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=true");
        assert.equal(button.icon.textContent, "☆");
        assert.equal(button.row.dataset.peopleStarred, "false");
    }
    assert.equal(page.status.textContent, "Removed star from Alice Smith");
});

test("one pending request locks every person and prevents a competing write", async () => {
    let finishRequest;
    const page = boot({
        fetchImplementation: () =>
            new Promise((resolve) => {
                finishRequest = resolve;
            }),
        people: [
            { identity: "alice smith", name: "Alice Smith" },
            { identity: "bob jones", name: "Bob Jones" },
        ],
    });

    const first = page.form.submitWith(page.buttons[0]);
    await Promise.resolve();
    for (const button of page.buttons) {
        assert.equal(button.disabled, true);
        assert.equal(button.getAttribute("aria-busy"), "true");
    }

    const competing = page.form.submitWith(page.buttons[2]);
    assert.equal(competing.prevented(), true);
    await competing.result;
    assert.equal(page.requests.length, 1);
    assert.equal(page.requests[0][1].body.get("starred"), "true");
    assert.equal(page.requests[0][1].body.get("person"), "alice smith");

    finishRequest(
        response({
            state: "success",
            label: "Person updated",
            detail: "Starred Alice Smith",
            person: "Alice Smith",
            identity_key: "alice smith",
            starred: true,
        }),
    );
    await first.result;
    assert.ok(page.buttons.every((button) => !button.disabled));
    assert.equal(page.buttons[2].getAttribute("aria-pressed"), "false");
});

test("a retry after a malformed response keeps the desired state and then reconciles", async () => {
    const responses = [
        response({ state: "success", detail: "Incomplete" }),
        response({
            state: "success",
            label: "Person updated",
            detail: "Starred Alice Smith",
            person: "Alice Smith",
            identity_key: "alice smith",
            starred: true,
        }),
        response({
            state: "success",
            label: "Person updated",
            detail: "Removed star from Alice Smith",
            person: "Alice Smith",
            identity_key: "alice smith",
            starred: false,
        }),
    ];
    const page = boot({
        fetchImplementation: async () => responses.shift(),
    });

    let submission = page.form.submitWith(page.buttons[0]);
    await submission.result;
    assert.equal(page.requests[0][1].body.get("starred"), "true");
    assert.equal(page.status.textContent, "OWL returned an incomplete person-star response.");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-pressed"), "false");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=true");
        assert.equal(button.title, "Star Alice Smith locally in OWL");
    }

    submission = page.form.submitWith(page.buttons[1]);
    await submission.result;
    assert.equal(page.requests[1][1].body.get("starred"), "true");
    assert.equal(page.status.textContent, "Starred Alice Smith");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-pressed"), "true");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=false");
        assert.equal(button.title, "Remove star from Alice Smith locally in OWL");
    }

    submission = page.form.submitWith(page.buttons[0]);
    await submission.result;
    assert.deepEqual(
        page.requests.map(([, options]) => options.body.get("starred")),
        ["true", "true", "false"],
    );
    assert.equal(page.status.textContent, "Removed star from Alice Smith");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-label"), "Star Alice Smith");
        assert.equal(button.getAttribute("aria-pressed"), "false");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=true");
        assert.equal(button.title, "Star Alice Smith locally in OWL");
        assert.equal(button.icon.textContent, "☆");
        assert.equal(button.row.dataset.peopleStarred, "false");
        assert.equal(button.disabled, false);
    }
});

test("an HTTP failure retains state and announces the backend detail", async () => {
    const page = boot({
        fetchImplementation: async () =>
            response({ detail: "Unknown Git committer selection." }, { ok: false }),
        people: [{ identity: "alice smith", name: "Alice Smith", starred: true }],
    });

    const submission = page.form.submitWith(page.buttons[0]);
    await submission.result;

    assert.equal(page.requests[0][1].body.get("starred"), "false");
    assert.equal(page.status.textContent, "Unknown Git committer selection.");
    for (const button of page.buttons) {
        assert.equal(button.getAttribute("aria-pressed"), "true");
        assert.equal(button.formAction, "/pdfs/people/star/?starred=false");
        assert.equal(button.icon.textContent, "★");
        assert.equal(button.row.dataset.peopleStarred, "true");
    }
});

test("missing enhancement prerequisites leave the native external form untouched", () => {
    const form = new StarForm();
    const document = {
        querySelector(selector) {
            if (selector === "[data-bitbucket-workspace]") return { querySelector() {} };
            if (selector === "[data-people-star-form]") return form;
            return null;
        },
    };
    assert.doesNotThrow(() =>
        vm.runInNewContext(source, { document, window: {} }, { filename: "people_stars.js" }),
    );
    assert.equal(form.listeners.size, 0);
});
