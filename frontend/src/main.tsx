import {StrictMode} from "react";
import {createRoot} from "react-dom/client";

import App from "./App";
import {BookmarkManagerApp} from "./bookmarks/BookmarkManagerApp";
import {NotificationCenter} from "./components/NotificationCenter";
import {HomeApp} from "./home/HomeApp";
import {SettingsApp} from "./settings/SettingsApp";
import "./styles.css";
import "./home.css";
import "./bookmarks.css";
import "./settings.css";
import "./notifications.css";

const viteRoot = document.getElementById("home-root");
if (viteRoot && import.meta.env.DEV) {
    if (window.location.pathname.startsWith("/static/bookmarks/settings/")) {
        viteRoot.id = "settings-root";
        viteRoot.dataset.workspaceUrl = "/bookmarks/settings/workspace/";
    } else if (window.location.pathname.startsWith("/static/bookmarks/")) {
        viteRoot.id = "bookmarks-root";
        viteRoot.dataset.workspaceUrl = "/bookmarks/workspace/";
    } else if (window.location.pathname.startsWith("/static/bitbucket/")) {
        viteRoot.id = "bitbucket-root";
        viteRoot.dataset.workspaceUrl = "/bitbucket/workspace/";
    }
}

const roots = [
    {element: document.getElementById("home-root"), app: <HomeApp />},
    {element: document.getElementById("bookmarks-root"), app: <BookmarkManagerApp />},
    {element: document.getElementById("settings-root"), app: <SettingsApp />},
    {element: document.getElementById("bitbucket-root"), app: <App />},
];
const selected = roots.find((candidate) => candidate.element);
if (!selected?.element) throw new Error("OWL React mount point is missing.");

createRoot(selected.element).render(
    <StrictMode>
        <NotificationCenter />
        {selected.app}
    </StrictMode>,
);
