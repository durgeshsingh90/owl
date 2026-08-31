(() => {
    "use strict";

    const panels = Array.from(document.querySelectorAll("[data-people-panel]"));
    const controls = panels.map((panel) => panel.querySelector("[data-people-sort]")).filter(Boolean);
    if (!controls.length) return;

    const storageKey = "owl.bitbucket.peopleSort.v1";
    const allowedOrders = new Set(["most_pdfs", "recent", "most_commits", "name"]);
    const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
    const validOrder = (value) => allowedOrders.has(value) ? value : "most_pdfs";
    const count = (entry, key) => Number(entry.dataset[key]) || 0;
    const lastCommit = (entry) => {
        const value = entry.dataset.peopleLastCommit;
        const timestamp = value ? Number(value) : NaN;
        return Number.isFinite(timestamp) ? timestamp : -Infinity;
    };
    const byName = (left, right) => {
        const leftName = left.dataset.peopleName || "";
        const rightName = right.dataset.peopleName || "";
        return collator.compare(leftName, rightName) || leftName.localeCompare(rightName);
    };
    const compare = (order, left, right) => {
        if (order === "recent") {
            const leftDate = lastCommit(left);
            const rightDate = lastCommit(right);
            if (leftDate !== rightDate) return leftDate > rightDate ? -1 : 1;
        } else if (order !== "name") {
            const primary = order === "most_commits" ? "peopleCommitCount" : "peoplePdfCount";
            const secondary = order === "most_commits" ? "peoplePdfCount" : "peopleCommitCount";
            const difference = count(right, primary) - count(left, primary)
                || count(right, secondary) - count(left, secondary);
            if (difference) return difference;
        }
        return byName(left, right);
    };
    const applyOrder = (value) => {
        const order = validOrder(value);
        controls.forEach((control) => { control.value = order; });
        panels.forEach((panel) => {
            const list = panel.querySelector("[data-git-people-list]");
            if (!list) return;
            const entries = Array.from(list.querySelectorAll('[data-people-entry-kind="committer"]'));
            entries.sort((left, right) => compare(order, left, right));
            // Move the existing rows, retaining checkbox selection and search visibility.
            entries.forEach((entry) => list.appendChild(entry));
        });
        return order;
    };

    let savedOrder = "most_pdfs";
    try {
        savedOrder = validOrder(window.localStorage.getItem(storageKey));
    } catch {
        // Sorting still works when this browser cannot store preferences.
    }
    applyOrder(savedOrder);

    controls.forEach((control) => {
        control.addEventListener("change", () => {
            const order = applyOrder(control.value);
            try {
                window.localStorage.setItem(storageKey, order);
            } catch {
                // Keep the chosen order for this page even if storage is unavailable.
            }
        });
    });
})();
