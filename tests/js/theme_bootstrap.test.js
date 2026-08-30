const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "../..");
const template = readFileSync(path.join(root, "templates/owl/base.html"), "utf8");
const bootstrap = template.match(/<script nonce="\{\{ csp_nonce \}\}" data-theme-bootstrap>([\s\S]*?)<\/script>/)?.[1];
const owl = readFileSync(path.join(root, "static/owl/owl.js"), "utf8");
const themeStart = owl.indexOf('    const themeStorageKey = "owl-theme";');
const themeEnd = owl.indexOf('    document.addEventListener("click", (event) => {\n        const toggle = event.target.closest("[data-app-sidebar-toggle]");', themeStart);
assert.ok(bootstrap, "A CSP-nonced bootstrap exists");
assert.ok(themeStart > 0 && themeEnd > themeStart, "Exercise the production deferred theme handler");
const themeSource = owl.slice(themeStart, themeEnd);

function boot(defaultTheme, saved, { unavailable = false } = {}) {
    const label = { textContent: "" };
    const attributes = new Map();
    const toggle = {
        setAttribute: (name, value) => attributes.set(name, value),
        querySelector: () => label,
    };
    const listeners = new Map();
    const document = {
        body: { dataset: { theme: defaultTheme } },
        documentElement: { dataset: {} },
        querySelectorAll: () => [toggle],
        addEventListener: (event, callback) => listeners.set(event, callback),
    };
    let stored = saved;
    const window = {
        localStorage: {
            getItem(key) {
                assert.equal(key, "owl-theme");
                if (unavailable) throw new Error("storage blocked");
                return stored;
            },
            setItem(key, value) {
                assert.equal(key, "owl-theme");
                if (unavailable) throw new Error("storage blocked");
                stored = value;
            },
        },
    };
    vm.runInNewContext(bootstrap, { document, window });
    return {
        document, label, attributes,
        stored: () => stored,
        loadDeferred() { vm.runInNewContext(`(() => {${themeSource}})()`, { document, window }); },
        toggle() { listeners.get("click")({ target: { closest: () => toggle } }); },
    };
}

test("theme bootstrap runs synchronously before any visible page content", () => {
    const body = template.indexOf("<body ");
    const start = template.indexOf("<script nonce=", body);
    const end = template.indexOf("</script>", start);
    const firstVisibleContent = template.indexOf('<a class="skip-link"', body);
    assert.ok(body < start && end < firstVisibleContent);
    assert.doesNotMatch(template.slice(start, template.indexOf(">", start)), /\b(?:defer|async)\b/);
});

test("saved light and dark themes apply before the deferred UI bundle and remain stable", () => {
    for (const [defaultTheme, saved] of [["dark", "light"], ["light", "dark"], ["dark", "dark"]]) {
        const page = boot(defaultTheme, saved);
        assert.equal(page.document.body.dataset.theme, saved);
        assert.equal(page.document.documentElement.dataset.theme, saved);
        page.loadDeferred();
        assert.equal(page.document.body.dataset.theme, saved);
        assert.equal(page.document.documentElement.dataset.theme, saved);
        assert.equal(page.attributes.get("aria-pressed"), String(saved === "dark"));
    }
});

test("missing invalid or unavailable storage preserves each page's own default", () => {
    for (const defaultTheme of ["light", "dark"]) {
        for (const saved of [null, "", "sepia", "DARK"]) {
            const page = boot(defaultTheme, saved);
            assert.equal(page.document.body.dataset.theme, defaultTheme);
            page.loadDeferred();
            assert.equal(page.document.body.dataset.theme, defaultTheme);
        }
        const page = boot(defaultTheme, null, { unavailable: true });
        assert.equal(page.document.body.dataset.theme, defaultTheme);
        page.loadDeferred();
        assert.equal(page.document.documentElement.dataset.theme, defaultTheme);
        page.toggle();
        assert.notEqual(page.document.body.dataset.theme, defaultTheme, "Toggling still works without storage");
    }
});

test("a theme toggle keeps the root canvas body controls and persisted preference consistent", () => {
    const page = boot("dark", "dark");
    page.loadDeferred();
    page.toggle();
    assert.equal(page.document.body.dataset.theme, "light");
    assert.equal(page.document.documentElement.dataset.theme, "light");
    assert.equal(page.stored(), "light");
    assert.equal(page.label.textContent, "Dark mode");
    assert.equal(page.attributes.get("aria-label"), "Switch to dark mode");
    page.toggle();
    assert.equal(page.document.body.dataset.theme, "dark");
    assert.equal(page.document.documentElement.dataset.theme, "dark");
    assert.equal(page.stored(), "dark");
    assert.equal(page.label.textContent, "Light mode");
});
