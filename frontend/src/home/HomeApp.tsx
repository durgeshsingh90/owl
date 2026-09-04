import {useCallback, useEffect, useState} from "react";

import {requestJson} from "../api";
import {useTheme} from "../hooks/useTheme";
import {appHref} from "../routing";
import type {HomeBookmark, HomeWorkspace, LinkChoice} from "./types";

function Choices({items, label}: {items: LinkChoice[]; label: string}) {
    return <nav className="home-choice-row" aria-label={label}>
        {items.map((item) => <a
            className={item.selected ? "is-active" : ""}
            href={appHref(item.href)}
            key={`${label}-${item.label}`}
            aria-current={item.selected ? "page" : undefined}
        >{item.label}</a>)}
    </nav>;
}

async function openBookmark(bookmark: HomeBookmark, csrfToken: string): Promise<void> {
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    try {
        const result = await requestJson<{url: string}>(bookmark.openUrl, csrfToken, {method: "POST"});
        if (popup) popup.location.replace(result.url);
        else window.open(result.url, "_blank", "noopener,noreferrer");
    } catch (error) {
        popup?.close();
        throw error;
    }
}

function Ranking({title, rows, unit}: {
    title: string;
    rows: Array<{name: string; value: number; detail?: string}>;
    unit: string;
}) {
    return <section className="home-ranking">
        <h3>{title}</h3>
        {rows.length ? <ol>{rows.slice(0, 10).map((row) => <li key={`${title}-${row.name}`}>
            <span>{row.name}<small>{row.detail}</small></span><strong>{row.value} {unit}</strong>
        </li>)}</ol> : <p className="home-empty">No activity in this period.</p>}
    </section>;
}

export function HomeApp() {
    const root = document.getElementById("home-root");
    const workspaceUrl = root?.dataset.workspaceUrl || "/home/workspace/";
    const [workspace, setWorkspace] = useState<HomeWorkspace | null>(null);
    const [error, setError] = useState("");
    const [toast, setToast] = useState("");
    const [theme, toggleTheme] = useTheme();

    const load = useCallback(async () => {
        const url = new URL(workspaceUrl, window.location.href);
        url.search = window.location.search;
        const data = await requestJson<HomeWorkspace>(url.toString(), "");
        setWorkspace(data);
        setError("");
    }, [workspaceUrl]);

    useEffect(() => { void load().catch((reason) => setError(String(reason))); }, [load]);
    if (!workspace) return <main className="app-loading"><div className="owl-mark">OWL</div><h1>{error ? "Home could not load" : "Opening OWL…"}</h1>{error && <><p role="alert">{error}</p><button onClick={() => void load()}>Retry</button></>}</main>;

    const {bookmarkActivity: activity, bookmarkPeople: people} = workspace;
    return <div className="home-shell">
        <aside className="home-rail">
            <a className="home-brand" href={appHref(workspace.urls.home)}><span className="owl-mark">OWL</span><span><strong>OWL</strong><small>Knowledge, connected.</small></span></a>
            <nav aria-label="Workspace navigation">
                <a className="is-active" href={appHref(workspace.urls.home)}>⌂ Home</a>
                <a href="#apps">⊞ Apps</a>
                <a href={appHref(workspace.urls.bookmarks)}>⌑ Bookmark Manager</a>
                <a href={appHref(workspace.urls.bitbucket)}>◫ Bitbucket</a>
                <a href={appHref(workspace.urls.bookmarkSettings)}>⚙ Settings</a>
                <a href={workspace.urls.systemStatus}>◌ System status</a>
            </nav>
            <button className="theme-button" onClick={toggleTheme}>{theme === "dark" ? "☼ Light mode" : "☾ Dark mode"}</button>
        </aside>
        <main className="home-content" id="main-content">
            <header className="home-topbar"><span><i /> Local workspace</span><span>{workspace.statusMessage}</span></header>
            <section id="apps" className="home-section">
                <div className="home-heading"><div><p>Workspace</p><h1>Your apps</h1></div><span>2 available</span></div>
                <div className="home-app-grid">
                    <a href={appHref(workspace.urls.bookmarks)}><b>⌑</b><strong>Bookmark Manager</strong><small>Save, organise, search, and revisit important pages.</small><em>Open →</em></a>
                    <a href={appHref(workspace.urls.bitbucket)}><b>◫</b><strong>Bitbucket</strong><small>Bitbucket API metadata for repository PDFs and VSDX counts.</small><em>Open →</em></a>
                </div>
            </section>

            <section className="home-section">
                <div className="home-heading"><div><p>Library pulse</p><h2>Your bookmark statistics</h2></div><a href={appHref(workspace.urls.bookmarks)}>Browse bookmarks →</a></div>
                <dl className="home-metrics">{workspace.bookmarkMetrics.map((metric) => <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value}</dd><small>{metric.detail}</small></div>)}</dl>
            </section>

            <section className="home-section" id="bookmark-people-activity">
                <div className="home-heading"><div><p>Confluence attribution</p><h2>Bookmark Manager activity</h2></div><Choices items={people.filters} label="Bookmark people period" /></div>
                <dl className="home-metrics home-metrics--compact"><div><dt>Pages written</dt><dd>{people.written_pages}</dd></div><div><dt>Pages updated</dt><dd>{people.updated_pages}</dd></div><div><dt>Active people</dt><dd>{people.active_people}</dd></div></dl>
                <div className="home-two-column"><Ranking title="Writers" unit="pages" rows={people.writers.map((item) => ({name: item.name, value: item.page_count}))} /><Ranking title="Latest editors" unit="pages" rows={people.updaters.map((item) => ({name: item.name, value: item.page_count}))} /></div>
            </section>

            <section className="home-section">
                <div className="home-heading"><div><p>Local storage</p><h2>Your database</h2></div><span>Cached snapshot</span></div>
                <dl className="home-metrics home-metrics--compact"><div><dt>Approx. database size</dt><dd>{workspace.database.available ? workspace.database.size_label : "—"}</dd></div><div><dt>Database tables</dt><dd>{workspace.database.available ? workspace.database.table_count : "—"}</dd></div><div><dt>Stored table entries</dt><dd>{workspace.database.available ? workspace.database.row_count : "—"}</dd></div></dl>
            </section>

            <section className="home-section" id="activity">
                <div className="home-heading"><div><p>Timeline</p><h2>{activity.label}</h2></div><Choices items={activity.filters} label="Activity type" /></div>
                <div className="heatmap-wrap"><div className="heatmap" role="region" aria-label="Bookmark activity calendar" tabIndex={0}>{activity.weeks.map((week, index) => <div className="heatmap-week" key={index}>{week.days.map((day) => <span key={day.date} data-level={day.level} className={day.inYear ? "" : "outside"} title={day.ariaLabel} />)}</div>)}</div></div>
                <div className="activity-summary"><span>Added <b>{activity.addedCount}</b></span><span>Opened <b>{activity.openedCount}</b></span><span>Refreshed <b>{activity.refreshedCount}</b></span><span>Notes <b>{activity.notesCount}</b></span><span>Most active <b>{activity.mostActiveDay}</b></span></div>
                <p className="home-note">{activity.trackingNote}</p><Choices items={activity.years} label="Activity year" />
            </section>

            <section className="home-section home-two-column">
                <div className="home-ranking"><h2>Most viewed</h2>{workspace.topViewed.length ? <ol>{workspace.topViewed.map((item) => <li key={item.bookmark.id}><button className="rank-open" onClick={() => void openBookmark(item.bookmark, workspace.csrfToken).catch((reason) => setToast(String(reason)))}><b>#{item.rank}</b><span>{item.bookmark.title}<small>{item.bookmark.space} · {item.lastViewedLabel}</small></span></button><strong>{item.bookmark.openCount}</strong></li>)}</ol> : <p className="home-empty">No viewed pages yet.</p>}</div>
                <div className="home-ranking"><h2>Interesting pages</h2>{workspace.interesting.map((group) => <section className="discovery" key={group.title}><h3>{group.title}</h3><p>{group.summary}</p>{group.items.length ? group.items.map((item) => <a key={item.bookmark.id} href={appHref(item.bookmark.selectUrl)}>{item.bookmark.title}<small>{item.meta}</small></a>) : <small>{group.emptyMessage}</small>}</section>)}</div>
            </section>
        </main>
        <div className="app-toast" hidden={!toast} role="status" aria-live="polite">{toast}</div>
    </div>;
}
