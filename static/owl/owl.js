(() => {
    "use strict";

    const themeStorageKey = "owl-theme";

    const applyTheme = (theme) => {
        document.body.dataset.theme = theme;
        document.querySelectorAll("[data-theme-toggle]").forEach((toggle) => {
            const isDark = theme === "dark";
            toggle.setAttribute("aria-pressed", String(isDark));
            toggle.setAttribute("aria-label", `Switch to ${isDark ? "light" : "dark"} mode`);
            const label = toggle.querySelector("[data-theme-toggle-label]");
            if (label) {
                label.textContent = isDark ? "Light mode" : "Dark mode";
            }
        });
    };

    try {
        const savedTheme = window.localStorage.getItem(themeStorageKey);
        if (savedTheme === "light" || savedTheme === "dark") {
            applyTheme(savedTheme);
        } else {
            applyTheme(document.body.dataset.theme || "light");
        }
    } catch {
        applyTheme(document.body.dataset.theme || "light");
    }

    document.addEventListener("click", (event) => {
        const themeToggle = event.target.closest("[data-theme-toggle]");
        if (!themeToggle) {
            return;
        }

        const nextTheme = document.body.dataset.theme === "dark" ? "light" : "dark";
        applyTheme(nextTheme);
        try {
            window.localStorage.setItem(themeStorageKey, nextTheme);
        } catch {
            // Theme selection still applies for this visit when browser storage is unavailable.
        }
    });

    document.addEventListener("click", (event) => {
        const toggle = event.target.closest("[data-app-sidebar-toggle]");
        if (!toggle) {
            return;
        }

        const shell = toggle.closest("[data-app-sidebar-shell]");
        if (!shell) {
            return;
        }

        const isOpen = shell.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", String(isOpen));
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }

        const shell = document.querySelector("[data-app-sidebar-shell].is-open");
        const toggle = shell?.querySelector("[data-app-sidebar-toggle]");
        if (!shell || !toggle || window.matchMedia("(min-width: 801px)").matches) {
            return;
        }

        shell.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
    });
})();
