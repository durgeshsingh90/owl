import {useEffect, useState} from "react";

import type {Theme} from "../types";

const STORAGE_KEY = "bitbucket-document-desk-theme";

function systemTheme(): Theme {
    try {
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch {
        return "light";
    }
}

function initialTheme(): Theme {
    try {
        const saved = window.localStorage.getItem(STORAGE_KEY);
        if (saved === "light" || saved === "dark") return saved;
    } catch {
        // A storage-disabled browser can still use its system colour scheme.
    }
    return systemTheme();
}

export function useTheme(): [Theme, () => void] {
    const [theme, setTheme] = useState<Theme>(initialTheme);

    useEffect(() => {
        document.documentElement.dataset.theme = theme;
        try {
            window.localStorage.setItem(STORAGE_KEY, theme);
        } catch {
            // Keep the theme for this page when browser storage is unavailable.
        }
    }, [theme]);

    return [theme, () => setTheme((current) => (current === "dark" ? "light" : "dark"))];
}
