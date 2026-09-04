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

const today = "2026-09-01";
const yesterday = "2026-08-31";

function fixedDate(day) {
    return class FixedDate extends Date {
        constructor(...args) {
            super(...(args.length ? args : [2026, 8, day, 12, 0, 0]));
        }
    };
}

function boot(defaultTheme, saved = {}, {
    day = 1,
    systemDark = false,
    unavailable = false,
    matchMediaUnavailable = false,
    matchMediaThrows = false,
} = {}) {
    const labels = [{ textContent: "" }, { textContent: "" }];
    const attributes = [new Map(), new Map()];
    const toggles = labels.map((label, index) => ({
        setAttribute: (name, value) => attributes[index].set(name, value),
        querySelector: () => label,
    }));
    const listeners = new Map();
    const document = {
        body: { dataset: { theme: defaultTheme } },
        documentElement: { dataset: {} },
        querySelectorAll: () => toggles,
        addEventListener: (event, callback) => listeners.set(event, callback),
    };
    const initialStorage = typeof saved === "string" ? { "owl-theme": saved } : saved;
    const storage = new Map(
        Object.entries(initialStorage).filter(([, value]) => value !== null && value !== undefined),
    );
    const systemListeners = new Set();
    let currentSystemDark = systemDark;
    const mediaQuery = {
        get matches() { return currentSystemDark; },
        addEventListener(event, callback) {
            assert.equal(event, "change");
            systemListeners.add(callback);
        },
        addListener(callback) { systemListeners.add(callback); },
    };
    const window = {
        localStorage: {
            getItem(key) {
                if (unavailable) throw new Error("storage blocked");
                return storage.has(key) ? storage.get(key) : null;
            },
            setItem(key, value) {
                if (unavailable) throw new Error("storage blocked");
                storage.set(key, String(value));
            },
        },
    };
    if (!matchMediaUnavailable) {
        window.matchMedia = (query) => {
            assert.equal(query, "(prefers-color-scheme: dark)");
            if (matchMediaThrows) throw new Error("matchMedia blocked");
            return mediaQuery;
        };
    }
    const context = { document, window, Date: fixedDate(day) };
    vm.runInNewContext(bootstrap, context);
    return {
        document, labels, attributes,
        storage: () => Object.fromEntries(storage),
        loadDeferred() { vm.runInNewContext(`(() => {${themeSource}})()`, context); },
        toggle(index = 0) {
            listeners.get("click")({ target: { closest: () => toggles[index] } });
        },
        changeSystem(dark) {
            currentSystemDark = dark;
            systemListeners.forEach((callback) => callback({ matches: dark }));
        },
    };
}

function assertTheme(page, theme) {
    assert.equal(page.document.body.dataset.theme, theme);
    assert.equal(page.document.documentElement.dataset.theme, theme);
    page.attributes.forEach((attributes) => {
        assert.equal(attributes.get("aria-pressed"), String(theme === "dark"));
        assert.equal(attributes.get("aria-label"), `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
    });
    page.labels.forEach((label) => {
        assert.equal(label.textContent, theme === "dark" ? "Light mode" : "Dark mode");
    });
}

test("theme bootstrap runs synchronously before any visible page content", () => {
    const body = template.indexOf("<body ");
    const start = template.indexOf("<script nonce=", body);
    const end = template.indexOf("</script>", start);
    const firstVisibleContent = template.indexOf('<a class="skip-link"', body);
    assert.ok(body < start && end < firstVisibleContent);
    assert.doesNotMatch(template.slice(start, template.indexOf(">", start)), /\b(?:defer|async)\b/);
});

test("the first load of each local day follows the current system theme before paint", () => {
    for (const [defaultTheme, systemDark] of [["dark", false], ["light", true]]) {
        const expected = systemDark ? "dark" : "light";
        const page = boot(defaultTheme, {
            "owl-theme": systemDark ? "light" : "dark",
            "owl-theme-day": yesterday,
            "owl-theme-system": systemDark ? "light" : "dark",
        }, { systemDark });
        assert.equal(page.document.body.dataset.theme, expected);
        assert.equal(page.document.documentElement.dataset.theme, expected);
        assert.deepEqual(page.storage(), {
            "owl-theme": expected,
            "owl-theme-day": today,
            "owl-theme-system": expected,
        });
        page.loadDeferred();
        assertTheme(page, expected);
    }
});

test("a same-day manual selection survives reload while the system setting is unchanged", () => {
    const page = boot("dark", {
        "owl-theme": "light",
        "owl-theme-day": today,
        "owl-theme-system": "dark",
    }, { systemDark: true });
    assert.equal(page.document.body.dataset.theme, "light");
    page.loadDeferred();
    assertTheme(page, "light");
});

test("stale invalid and undated themes are replaced by today's system setting", () => {
    for (const saved of [
        { "owl-theme": "light", "owl-theme-day": yesterday, "owl-theme-system": "dark" },
        { "owl-theme": "light" },
        { "owl-theme": "sepia", "owl-theme-day": today, "owl-theme-system": "dark" },
    ]) {
        const page = boot("light", saved, { systemDark: true });
        assert.equal(page.document.body.dataset.theme, "dark");
        assert.equal(page.storage()["owl-theme-day"], today);
    }
});

test("a system change while OWL was closed overrides a same-day manual selection", () => {
    const page = boot("light", {
        "owl-theme": "light",
        "owl-theme-day": today,
        "owl-theme-system": "light",
    }, { systemDark: true });
    assert.equal(page.document.body.dataset.theme, "dark");
    assert.equal(page.storage()["owl-theme-system"], "dark");
});

test("live system changes synchronize the page and every theme control", () => {
    const page = boot("dark", {}, { systemDark: false });
    page.loadDeferred();
    assertTheme(page, "light");
    page.changeSystem(true);
    assertTheme(page, "dark");
    assert.equal(page.storage()["owl-theme"], "dark");
    assert.equal(page.storage()["owl-theme-system"], "dark");
    page.changeSystem(false);
    assertTheme(page, "light");
});

test("manual toggles persist for today and the next day resets to the system", () => {
    const page = boot("light", {}, { systemDark: true });
    page.loadDeferred();
    assertTheme(page, "dark");
    page.toggle();
    assertTheme(page, "light");
    assert.deepEqual(page.storage(), {
        "owl-theme": "light",
        "owl-theme-day": today,
        "owl-theme-system": "dark",
    });

    const sameDay = boot("dark", page.storage(), { systemDark: true });
    sameDay.loadDeferred();
    assertTheme(sameDay, "light");

    const nextDay = boot("light", page.storage(), { day: 2, systemDark: true });
    nextDay.loadDeferred();
    assertTheme(nextDay, "dark");
    assert.equal(nextDay.storage()["owl-theme-day"], "2026-09-02");
});

test("a later system change takes precedence over a manual selection", () => {
    const page = boot("dark", {}, { systemDark: false });
    page.loadDeferred();
    page.toggle();
    assertTheme(page, "dark");
    page.changeSystem(true);
    assertTheme(page, "dark");
    page.changeSystem(false);
    assertTheme(page, "light");
});

test("blocked storage still follows system changes and permits an in-page manual toggle", () => {
    const page = boot("dark", {}, { systemDark: false, unavailable: true });
    assert.equal(page.document.body.dataset.theme, "light");
    page.loadDeferred();
    assertTheme(page, "light");
    page.toggle();
    assertTheme(page, "dark");
    page.changeSystem(true);
    assertTheme(page, "dark");
    page.changeSystem(false);
    assertTheme(page, "light");
});

test("without matchMedia the saved theme and page default remain safe fallbacks", () => {
    const saved = boot("light", "dark", { matchMediaUnavailable: true });
    saved.loadDeferred();
    assertTheme(saved, "dark");

    const fallback = boot("dark", {}, { matchMediaThrows: true });
    fallback.loadDeferred();
    assertTheme(fallback, "dark");
    fallback.toggle();
    assertTheme(fallback, "light");
});
