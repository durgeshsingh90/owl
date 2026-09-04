(() => {
    "use strict";

    const themeStorageKey = "owl-theme";
    const themeDayStorageKey = "owl-theme-day";
    const themeSystemStorageKey = "owl-theme-system";
    const localThemeDay = () => {
        const now = new Date();
        const pad = (value) => String(value).padStart(2, "0");
        return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    };
    let systemThemeQuery = null;
    try {
        systemThemeQuery = typeof window.matchMedia === "function"
            ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    } catch {
        // A restricted matchMedia implementation leaves the bootstrap fallback in place.
    }
    const currentSystemTheme = () => systemThemeQuery
        ? (systemThemeQuery.matches ? "dark" : "light") : null;

    const applyTheme = (theme) => {
        document.body.dataset.theme = theme;
        document.documentElement.dataset.theme = theme;
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

    const persistTheme = (theme) => {
        try {
            window.localStorage.setItem(themeStorageKey, theme);
            window.localStorage.setItem(themeDayStorageKey, localThemeDay());
            const systemTheme = currentSystemTheme();
            if (systemTheme !== null) {
                window.localStorage.setItem(themeSystemStorageKey, systemTheme);
            }
        } catch {
            // Theme selection still applies for this visit when browser storage is unavailable.
        }
    };

    applyTheme(document.body.dataset.theme === "dark" ? "dark" : "light");

    if (systemThemeQuery) {
        const followSystemTheme = (event) => {
            const theme = event.matches ? "dark" : "light";
            applyTheme(theme);
            persistTheme(theme);
        };
        if (typeof systemThemeQuery.addEventListener === "function") {
            systemThemeQuery.addEventListener("change", followSystemTheme);
        } else if (typeof systemThemeQuery.addListener === "function") {
            systemThemeQuery.addListener(followSystemTheme);
        }
    }

    document.addEventListener("click", (event) => {
        const themeToggle = event.target.closest("[data-theme-toggle]");
        if (!themeToggle) {
            return;
        }

        const nextTheme = document.body.dataset.theme === "dark" ? "light" : "dark";
        applyTheme(nextTheme);
        persistTheme(nextTheme);
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
