const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../static/bitbucket_search/people_sort.js"), "utf8");
const storageKey = "owl.bitbucket.peopleSort.v1";

// A small moving-node DOM exercises the shipped script without browser/network
// dependencies; cloning a row would lose its checkbox identity in these tests.
class Element {
    constructor(tag = "div", attributes = {}, children = []) {
        this.tagName = tag.toUpperCase();
        this.attributes = new Map(Object.entries(attributes));
        this.dataset = Object.fromEntries(Object.entries(attributes)
            .filter(([name]) => name.startsWith("data-"))
            .map(([name, value]) => [name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), String(value)]));
        this.children = [];
        this.listeners = new Map();
        this.hidden = false;
        this.checked = false;
        this.disabled = false;
        this.value = "";
        children.forEach((child) => this.appendChild(child));
    }
    matches(selector) {
        const match = selector.match(/^\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]$/);
        return Boolean(match && this.attributes.has(match[1])
            && (match[2] === undefined || String(this.attributes.get(match[1])) === match[2]));
    }
    querySelectorAll(selector) {
        return this.children.flatMap((child) => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
        ]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    appendChild(child) {
        if (child.parentElement) {
            child.parentElement.children = child.parentElement.children.filter((node) => node !== child);
        }
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(listener);
    }
    change(value) {
        this.value = value;
        for (const listener of this.listeners.get("change") || []) {
            listener({ target: this, currentTarget: this });
        }
    }
}

const initialPeople = [
    { name: "Zoe", lastCommit: "300", commits: 5, pdfs: 1 },
    { name: "Bob", lastCommit: "100", commits: 2, pdfs: 4 },
    { name: "Alice", lastCommit: "200", commits: 2, pdfs: 4 },
    { name: "Aaron", lastCommit: "", commits: 3, pdfs: 4 },
];

function makePanel(people, { withControl = true, withList = true } = {}) {
    const panel = new Element("div", { "data-people-panel": "" });
    const search = new Element("input", { "data-people-filter-search": "" });
    panel.appendChild(search);
    const select = new Element("select", { "data-people-sort": "" });
    select.value = "most_pdfs";
    select.disabled = !people.length;
    if (withControl) panel.appendChild(select);
    const form = new Element("form", { "data-people-filter-form": "" });
    let submits = 0;
    form.submit = form.requestSubmit = () => { submits += 1; };
    panel.appendChild(form);
    const groups = new Element("ul", { "data-people-groups-list": "" }, [
        new Element("li", { "data-people-entry-kind": "group", "data-people-name": "Team Z" }),
        new Element("li", { "data-people-entry-kind": "group", "data-people-name": "Team A" }),
    ]);
    form.appendChild(groups);
    const list = new Element("ul", { "data-git-people-list": "" });
    const rows = people.map((person) => {
        const checkbox = new Element("input", { "data-committer-select": "", name: "committer" });
        checkbox.value = person.name;
        checkbox.checked = Boolean(person.selected);
        const row = new Element("li", {
            "data-people-entry-kind": "committer",
            "data-people-name": person.name,
            "data-people-last-commit": person.lastCommit,
            "data-people-commit-count": person.commits,
            "data-people-pdf-count": person.pdfs,
        }, [checkbox]);
        row.hidden = Boolean(person.hidden);
        list.appendChild(row);
        return row;
    });
    if (withList) form.appendChild(list);
    return { panel, select, list, rows, groups, search, submits: () => submits };
}

function boot({ people = initialPeople, preference, storage, readFails = false, writeFails = false, accessFails = false, panelOptions = [{}, {}] } = {}) {
    const saved = storage || new Map(preference === undefined ? [] : [[storageKey, preference]]);
    const panels = panelOptions.map((options) => makePanel(people, options));
    const document = new Element("document", {}, panels.map(({ panel }) => panel));
    const writes = [];
    let navigations = 0;
    const window = {
        location: { reload() { navigations += 1; }, assign() { navigations += 1; } },
        get localStorage() {
            if (accessFails) throw new Error("Storage is unavailable");
            return {
                getItem(key) {
                    if (readFails) throw new Error("Storage read denied");
                    return saved.get(key) ?? null;
                },
                setItem(key, value) {
                    if (writeFails) throw new Error("Storage quota exceeded");
                    writes.push([key, value]);
                    saved.set(key, value);
                },
            };
        },
    };
    vm.runInNewContext(source, { document, window, Intl }, { filename: "people_sort.js" });
    return { panels, saved, writes, navigations: () => navigations };
}

const names = (panel) => panel.list.children.map((row) => row.dataset.peopleName);

test("default order retains PDF count, then commit count, then name ordering", () => {
    const { panels, writes } = boot();
    for (const panel of panels) {
        assert.deepEqual(names(panel), ["Aaron", "Alice", "Bob", "Zoe"]);
        assert.equal(panel.select.value, "most_pdfs");
    }
    assert.deepEqual(writes, []);
});

for (const [preference, expected] of [
    ["recent", ["Zoe", "Alice", "Bob", "Aaron"]],
    ["most_pdfs", ["Aaron", "Alice", "Bob", "Zoe"]],
    ["most_commits", ["Zoe", "Aaron", "Alice", "Bob"]],
    ["name", ["Aaron", "Alice", "Bob", "Zoe"]],
]) {
    test(`saved ${preference} preference is restored in both panels`, () => {
        const { panels } = boot({ preference });
        for (const panel of panels) {
            assert.equal(panel.select.value, preference);
            assert.deepEqual(names(panel), expected);
        }
    });
}

test("changing either panel synchronizes and saves the order without submitting or navigating", () => {
    const result = boot();
    result.panels[0].select.change("recent");
    assert.equal(result.saved.get(storageKey), "recent");
    result.panels.forEach((panel) => {
        assert.equal(panel.select.value, "recent");
        assert.deepEqual(names(panel), ["Zoe", "Alice", "Bob", "Aaron"]);
    });
    result.panels[1].select.change("most_commits");
    result.panels.forEach((panel) => {
        assert.equal(panel.select.value, "most_commits");
        assert.deepEqual(names(panel), ["Zoe", "Aaron", "Alice", "Bob"]);
        assert.equal(panel.submits(), 0);
    });
    assert.deepEqual(result.writes, [[storageKey, "recent"], [storageKey, "most_commits"]]);
    assert.equal(result.navigations(), 0);
    const reloaded = boot({ storage: result.saved });
    reloaded.panels.forEach((panel) => {
        assert.equal(panel.select.value, "most_commits");
        assert.deepEqual(names(panel), ["Zoe", "Aaron", "Alice", "Bob"]);
    });
});

test("recent ordering uses known commit times before missing or invalid dates, with alphabetical ties", () => {
    const { panels } = boot({
        preference: "recent",
        people: [
            { name: "Zoe", lastCommit: "200", commits: 1, pdfs: 1 },
            { name: "Alice", lastCommit: "200", commits: 1, pdfs: 1 },
            { name: "Missing", lastCommit: "", commits: 9, pdfs: 9 },
            { name: "Invalid", lastCommit: "invalid", commits: 9, pdfs: 9 },
            { name: "Epoch", lastCommit: "0", commits: 1, pdfs: 1 },
            { name: "Earlier", lastCommit: "-10", commits: 1, pdfs: 1 },
        ],
    });
    panels.forEach((panel) => assert.deepEqual(names(panel), ["Alice", "Zoe", "Epoch", "Earlier", "Invalid", "Missing"]));
});

test("sort moves original rows and preserves checked filters, hidden search results and groups", () => {
    const result = boot({ people: initialPeople.map((person) => ({ ...person, selected: person.name === "Bob", hidden: person.name === "Alice" })) });
    const originals = result.panels.map((panel) => ({
        rows: [...panel.rows], checkboxes: panel.rows.map((row) => row.children[0]), groups: [...panel.groups.children],
    }));
    result.panels.forEach((panel) => { panel.search.value = "bo"; });
    for (const order of ["recent", "name", "most_commits", "most_pdfs"]) result.panels[0].select.change(order);
    result.panels.forEach((panel, index) => {
        assert.equal(panel.list.children.length, originals[index].rows.length);
        for (const [rowIndex, row] of originals[index].rows.entries()) {
            assert.ok(panel.list.children.includes(row));
            assert.equal(row.children[0], originals[index].checkboxes[rowIndex]);
            assert.equal(row.children[0].checked, row.dataset.peopleName === "Bob");
            assert.equal(row.hidden, row.dataset.peopleName === "Alice");
        }
        assert.deepEqual(panel.groups.children, originals[index].groups);
        assert.equal(panel.search.value, "bo");
        assert.equal(panel.submits(), 0);
    });
});

for (const preference of ["unknown", "", "{\"sort\":\"recent\"}", "<script>recent</script>"]) {
    test(`invalid saved preference ${JSON.stringify(preference)} safely uses the default`, () => {
        const { panels } = boot({ preference });
        panels.forEach((panel) => {
            assert.equal(panel.select.value, "most_pdfs");
            assert.deepEqual(names(panel), ["Aaron", "Alice", "Bob", "Zoe"]);
        });
    });
}

for (const failure of ["readFails", "writeFails", "accessFails"]) {
    test(`${failure} does not break local sorting`, () => {
        const { panels } = boot({ [failure]: true });
        panels[1].select.change("recent");
        panels.forEach((panel) => {
            assert.equal(panel.select.value, "recent");
            assert.deepEqual(names(panel), ["Zoe", "Alice", "Bob", "Aaron"]);
        });
    });
}

test("empty or absent panels and lists do not throw or modify group order", () => {
    assert.doesNotThrow(() => boot({ panelOptions: [] }));
    assert.doesNotThrow(() => boot({ panelOptions: [{ withControl: false }] }));
    const { panels } = boot({ people: [], preference: "recent", panelOptions: [{ withList: false }, {}] });
    panels.forEach((panel) => {
        assert.equal(panel.select.disabled, true);
        assert.equal(panel.select.value, "recent");
        assert.deepEqual(panel.groups.children.map((group) => group.dataset.peopleName), ["Team Z", "Team A"]);
    });
});
