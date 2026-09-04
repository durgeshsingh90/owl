import {FormEvent, useCallback, useEffect, useMemo, useState} from "react";

import {ApiError, requestJson} from "../api";
import {useTheme} from "../hooks/useTheme";
import {appHref, navigateTo} from "../routing";
import {BookmarkTree} from "./BookmarkTree";
import type {BookmarkItem, BookmarkTreeItem, BookmarkWorkspace} from "./types";

function messageFor(error: unknown): string {
    if (error instanceof ApiError) return error.data.detail || error.data.message || error.message;
    return error instanceof Error ? error.message : "The request could not be completed.";
}

function formatDate(value: string | null): string {
    if (!value) return "Not supplied";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "Not supplied" : new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function formData(values: Record<string, string | number | Array<string | number>>): FormData {
    const body = new FormData();
    Object.entries(values).forEach(([key, value]) => {
        (Array.isArray(value) ? value : [value]).forEach((item) => body.append(key, String(item)));
    });
    return body;
}

function Sidebar({workspace}: {workspace: BookmarkWorkspace}) {
    const shortcuts = [
        ["All bookmarks", "", workspace.counts.all_bookmarks],
        ["Favorites", "?favorite=on&sort=added_newest", workspace.counts.favorites],
        ["Pinned", "?pinned=on&sort=added_newest", workspace.counts.pinned],
        ["Recently viewed", "?min_open=1&sort=recently_opened", workspace.counts.viewed],
        ["Frequently viewed", "?min_open=1&sort=most_opened", workspace.counts.viewed],
        ["Never viewed", "?max_open=0&sort=added_newest", workspace.counts.never_viewed],
        ["Deleted pages", "?availability=not_found&sort=added_newest", workspace.counts.deleted_pages],
    ] as const;
    return <aside className="bookmark-sidebar">
        <a className="bookmark-brand" href={appHref(workspace.urls.home)}><span className="owl-mark">OWL</span><span><strong>Bookmark Manager</strong><small>Personal knowledge desk</small></span></a>
        <nav aria-label="Bookmark Manager functions">
            <p>Library</p>{shortcuts.map(([label, query, count]) => <a key={label} href={appHref(`${workspace.urls.index}${query}`)}>{label}<b>{count ?? 0}</b></a>)}
            <p>Domains</p>{workspace.categories.map((category) => <a href={appHref(`${workspace.urls.index}?category=${category.id}`)} key={category.id}>{category.name}<b>{category.bookmarkCount}</b></a>)}
        </nav>
        <a className="sidebar-settings" href={appHref(workspace.urls.settings)}>⚙ Integration settings</a>
    </aside>;
}

async function openBookmark(bookmark: BookmarkItem, csrfToken: string): Promise<void> {
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

function DetailPanel({bookmark, workspace, mutate, notify}: {
    bookmark: BookmarkItem | null;
    workspace: BookmarkWorkspace;
    mutate: () => Promise<void>;
    notify: (message: string) => void;
}) {
    const [notes, setNotes] = useState(bookmark?.notes || "");
    const [tags, setTags] = useState(bookmark?.tags.join(", ") || "");
    useEffect(() => { setNotes(bookmark?.notes || ""); setTags(bookmark?.tags.join(", ") || ""); }, [bookmark?.id]);
    if (!bookmark) return <aside className="bookmark-detail"><div className="panel-heading"><h2>Page details</h2><span>Local & source metadata</span></div><div className="panel-empty"><b>Select a saved page</b><p>Notes, tags, dates, and safe actions will appear here.</p></div></aside>;

    const action = async (url: string, values: Record<string, string> = {}) => {
        const response = await requestJson<{detail?: string}>(url, workspace.csrfToken, {method: "POST", body: formData(values)});
        notify(response.detail || "Bookmark updated");
        await mutate();
    };
    return <aside className="bookmark-detail">
        <div className="panel-heading"><h2>Page details</h2><span>Local & source metadata</span></div>
        <div className="detail-title"><span>{bookmark.outlineNumber}</span><div><h3>{bookmark.title}</h3><small>OWL #{bookmark.id} · {bookmark.sourceLabel}</small></div><b data-state={bookmark.availability}>{bookmark.availability === "active" ? bookmark.recencyLabel : bookmark.availabilityLabel}</b></div>
        {bookmark.changedSinceViewed && <p className="detail-alert">Changed since you last opened it.</p>}
        <label className="field">Quick notes<textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={4} /></label>
        <label className="field">Tags<input value={tags} list="bookmark-tags" onChange={(event) => setTags(event.target.value)} /><datalist id="bookmark-tags">{workspace.tagSuggestions.map((tag) => <option value={tag} key={tag} />)}</datalist></label>
        <button className="primary-button" onClick={() => void action(bookmark.organiseUrl, {notes, tags}).catch((error) => notify(messageFor(error)))}>Save personal details</button>
        <label className="field">Personal folder<select value={bookmark.manualFolder?.id || ""} onChange={(event) => void action(workspace.urls.folderMove, {bookmark_ids: String(bookmark.id), folder_id: event.target.value, return_to: window.location.pathname + window.location.search}).catch((error) => notify(messageFor(error)))}><option value="">Confluence hierarchy</option>{workspace.folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></label>
        <div className="detail-actions"><button onClick={() => void openBookmark(bookmark, workspace.csrfToken).then(mutate).catch((error) => notify(messageFor(error)))}>↗ Open page</button><button onClick={() => void action(bookmark.favoriteUrl)}>{bookmark.favorite ? "★ Favorite" : "☆ Favorite"}</button><button onClick={() => void action(bookmark.pinUrl)}>{bookmark.pinned ? "● Pinned" : "○ Pin"}</button></div>
        <div className="detail-url"><code>{bookmark.url}</code><button onClick={() => void navigator.clipboard.writeText(bookmark.url).then(() => notify("URL copied"))}>Copy</button></div>
        <dl className="detail-metadata">
            <div><dt>Category</dt><dd>{bookmark.category?.name || "Uncategorised"}</dd></div><div><dt>Space</dt><dd>{bookmark.spaceName || bookmark.spaceKey || "Not supplied"}</dd></div><div><dt>Author</dt><dd>{bookmark.author || bookmark.createdBy || "Not supplied"}</dd></div><div><dt>Last modified by</dt><dd>{bookmark.modifiedBy || "Not supplied"}</dd></div><div><dt>Created</dt><dd>{formatDate(bookmark.createdAt)}</dd></div><div><dt>Updated</dt><dd>{formatDate(bookmark.updatedAt)}</dd></div><div><dt>Added to OWL</dt><dd>{formatDate(bookmark.savedAt)}</dd></div><div><dt>Last refreshed</dt><dd>{formatDate(bookmark.lastRefreshedAt)}</dd></div><div><dt>Opened</dt><dd>{bookmark.openCount} times</dd></div><div><dt>Version</dt><dd>{bookmark.version}</dd></div>
        </dl>
        <button className="danger-button" onClick={() => { if (window.confirm("Delete this bookmark from OWL? The source page will not be changed.")) void action(bookmark.deleteUrl, {confirm: "delete"}); }}>Delete from OWL</button>
    </aside>;
}

export function BookmarkManagerApp() {
    const root = document.getElementById("bookmarks-root");
    const workspaceUrl = root?.dataset.workspaceUrl || "/bookmarks/workspace/";
    const [workspace, setWorkspace] = useState<BookmarkWorkspace | null>(null);
    const [error, setError] = useState("");
    const [toast, setToast] = useState("");
    const [search, setSearch] = useState("");
    const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
    const [selectedPeople, setSelectedPeople] = useState<Set<string>>(new Set());
    const [peopleSearch, setPeopleSearch] = useState("");
    const [folderName, setFolderName] = useState("");
    const [theme, toggleTheme] = useTheme();

    const load = useCallback(async () => {
        const url = new URL(workspaceUrl, window.location.href);
        url.search = window.location.search;
        const data = await requestJson<BookmarkWorkspace>(url.toString(), "");
        setWorkspace(data);
        setSearch(data.search.term);
        setSelectedPeople(new Set(data.people.filter((person) => person.selected).map((person) => person.name)));
        setError("");
    }, [workspaceUrl]);
    useEffect(() => { void load().catch((reason) => setError(messageFor(reason))); }, [load]);
    useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(""), 5000); return () => window.clearTimeout(timer); }, [toast]);
    useEffect(() => {
        if (!workspace?.refresh.active) return;
        const timer = window.setInterval(() => void requestJson<{active: boolean}>(workspace.urls.refreshStatus, workspace.csrfToken).then((result) => { if (!result.active) void load(); }).catch(() => undefined), 2000);
        return () => window.clearInterval(timer);
    }, [workspace?.refresh.active, workspace?.urls.refreshStatus, load]);

    const selectedBookmarks = useMemo(() => workspace?.flatItems.filter((item) => selectedIds.has(item.id)) || [], [workspace?.flatItems, selectedIds]);
    const navigateSearch = (event: FormEvent) => { event.preventDefault(); const params = new URLSearchParams(window.location.search); search ? params.set("q", search) : params.delete("q"); params.delete("selected"); navigateTo(`${workspace!.urls.index}?${params}`); };
    const saveBookmark = async () => {
        if (!workspace) return;
        try {
            const result = await requestJson<{detail: string; redirect: string}>(workspace.urls.save, workspace.csrfToken, {method: "POST", body: formData({q: search})});
            setToast(result.detail); navigateTo(result.redirect);
        } catch (reason) { setToast(messageFor(reason)); }
    };
    const chooseMany = (ids: number[], value: boolean) => setSelectedIds((current) => { const next = new Set(current); ids.forEach((id) => value ? next.add(id) : next.delete(id)); return next; });
    const openTreeItem = (item: BookmarkTreeItem) => { if (item.bookmark) void openBookmark(item.bookmark, workspace!.csrfToken).then(load).catch((reason) => setToast(messageFor(reason))); };
    const batchOpen = () => selectedBookmarks.forEach((bookmark) => void openBookmark(bookmark, workspace!.csrfToken).catch((reason) => setToast(messageFor(reason))));
    const deleteSelected = async () => {
        if (!workspace || !selectedBookmarks.length || !window.confirm(`Delete ${selectedBookmarks.length} selected bookmarks from OWL?`)) return;
        try { const result = await requestJson<{detail: string}>(workspace.urls.deleteSelected, workspace.csrfToken, {method: "POST", body: formData({confirm: "delete-selected", bookmark_ids: selectedBookmarks.map((item) => item.id)})}); setToast(result.detail); setSelectedIds(new Set()); await load(); } catch (reason) { setToast(messageFor(reason)); }
    };
    const moveSelected = async (folderId: string) => {
        if (!workspace || !selectedBookmarks.length) return;
        try { const result = await requestJson<{detail: string}>(workspace.urls.folderMove, workspace.csrfToken, {method: "POST", body: formData({bookmark_ids: selectedBookmarks.map((item) => item.id), folder_id: folderId, return_to: window.location.pathname + window.location.search})}); setToast(result.detail); setSelectedIds(new Set()); await load(); } catch (reason) { setToast(messageFor(reason)); }
    };

    if (!workspace) return <main className="app-loading"><div className="owl-mark">OWL</div><h1>{error ? "Bookmark Manager could not load" : "Opening Bookmark Manager…"}</h1>{error && <><p role="alert">{error}</p><button onClick={() => void load()}>Retry</button></>}</main>;
    const visiblePeople = workspace.people.filter((person) => person.name.toLocaleLowerCase().includes(peopleSearch.toLocaleLowerCase()));

    return <div className="bookmark-app">
        <Sidebar workspace={workspace} />
        <div className="bookmark-stage">
            <header className="bookmark-topbar"><a href={appHref(workspace.urls.index)}><strong>Bookmark Manager</strong><small>Browse bookmarks by domain and source hierarchy</small></a><div><button className="refresh-button" disabled={workspace.refresh.active} onClick={() => void requestJson(workspace.urls.refreshStart, workspace.csrfToken, {method: "POST"}).then(load).catch((reason) => setToast(messageFor(reason)))}>{workspace.refresh.active ? `Refreshing ${workspace.refresh.processed}/${workspace.refresh.total}` : "↻ Refresh Confluence"}</button><a href={appHref(workspace.urls.home)}>Apps</a><a href={appHref(workspace.urls.settings)}>Settings</a><button onClick={toggleTheme}>{theme === "dark" ? "☼" : "☾"}<span className="visually-hidden">Toggle theme</span></button></div></header>
            <main className="bookmark-main" id="main-content">
                {(workspace.inlineError || workspace.statusMessage) && <p className={`bookmark-status${workspace.inlineError ? " is-error" : ""}`} role="status">{workspace.inlineError || workspace.statusMessage}</p>}
                <form className="bookmark-search" role="search" onSubmit={navigateSearch}><input aria-label="Search bookmarks or add a URL" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search title, text, people, tags, or paste a URL…" /><button type="submit">Search</button><button className="primary-button" type="button" onClick={() => void saveBookmark()}>Add bookmark</button></form>
                {workspace.search.semanticFallback && <p className="search-hint">Showing related stored-text matches because exact multi-word search found none.</p>}
                {workspace.search.urlSearch && <p className="search-hint">{workspace.search.urlMatchCount ? `${workspace.search.urlMatchCount} saved bookmark matches this URL.` : "No saved bookmark matches this URL. Add it when ready."}</p>}
                <div className="filter-row"><span>{workspace.resultCount} of {workspace.totalBookmarks}</span>{workspace.activeFilters.map((filter) => <span key={`${filter.label}-${filter.value}`}><b>{filter.label}</b> {filter.value}</span>)}{workspace.activeFilters.length > 0 && <a href={appHref(workspace.urls.index)}>Clear all</a>}</div>
                <section className="bookmark-grid">
                    <div className="bookmark-library">
                        <div className="panel-heading"><h2>Bookmark tree</h2><span>{workspace.resultCount} matching</span></div>
                        <div className="bookmark-sort">{workspace.sortControls.map((control) => <a className={control.active ? "is-active" : ""} href={appHref(control.href)} title={control.ariaLabel} key={control.key}>{control.label} {control.active ? (control.direction.includes("highest") || control.direction.includes("newest") ? "↓" : "↑") : ""}</a>)}</div>
                        <div className="selection-bar"><label><input type="checkbox" checked={workspace.flatItems.length > 0 && selectedIds.size === workspace.flatItems.length} onChange={(event) => setSelectedIds(event.target.checked ? new Set(workspace.flatItems.map((item) => item.id)) : new Set())} /> Select all results</label><span>{selectedIds.size} selected</span><button disabled={!selectedIds.size} onClick={batchOpen}>Open selected</button><select aria-label="Move selected bookmarks" disabled={!selectedIds.size} defaultValue="" onChange={(event) => { void moveSelected(event.target.value); event.target.value = ""; }}><option value="" disabled>Move to…</option><option value="">Source hierarchy</option>{workspace.folders.map((folder) => <option value={folder.id} key={folder.id}>{folder.name}</option>)}</select><button disabled={!selectedIds.size} className="danger-text" onClick={() => void deleteSelected()}>Delete</button></div>
                        <div className="folder-create"><input value={folderName} onChange={(event) => setFolderName(event.target.value)} placeholder="New personal folder" /><button disabled={!folderName.trim()} onClick={() => void requestJson<{detail: string}>(workspace.urls.folderCreate, workspace.csrfToken, {method: "POST", body: formData({name: folderName})}).then((result) => {setToast(result.detail); setFolderName(""); return load();}).catch((reason) => setToast(messageFor(reason)))}>+ Add folder</button></div>
                        {workspace.manualFolders.map((folder) => <section className="manual-folder" key={folder.id}><h3>▰ {folder.name}<span>{folder.bookmarkCount} · {folder.openCount} opens</span></h3><BookmarkTree items={folder.items} selectedIds={selectedIds} onSelectMany={chooseMany} onOpen={openTreeItem} /></section>)}
                        <BookmarkTree items={workspace.tree} selectedIds={selectedIds} onSelectMany={chooseMany} onOpen={openTreeItem} />
                        {!workspace.tree.length && !workspace.manualFolders.length && <div className="panel-empty"><b>No bookmarks match</b><p>Try a broader search, clear filters, or add a URL.</p></div>}
                    </div>
                    <DetailPanel bookmark={workspace.selectedBookmark} workspace={workspace} mutate={load} notify={setToast} />
                    <aside className="people-panel"><div className="panel-heading"><h2>People</h2><span>{workspace.people.length} contributors</span></div><input aria-label="Search people" value={peopleSearch} onChange={(event) => setPeopleSearch(event.target.value)} placeholder="Search people…" /><form onSubmit={(event) => { event.preventDefault(); const params = new URLSearchParams({people_filter: "1", sort: "updated_newest"}); selectedPeople.forEach((name) => params.append("person", name)); navigateTo(`${workspace.urls.index}?${params}`); }}><ol>{visiblePeople.map((person) => <li key={person.name}><label><input type="checkbox" checked={selectedPeople.has(person.name)} name="person" value={person.name} onChange={(event) => setSelectedPeople((current) => { const next = new Set(current); event.target.checked ? next.add(person.name) : next.delete(person.name); return next; })} /><span className="person-avatar">{person.name.slice(0, 1).toUpperCase()}</span><span><strong>{person.name}</strong><small>{person.writtenCount} written · {person.updatedCount} updated</small></span><b>{person.pageCount}</b></label></li>)}</ol><button type="submit">Show selected people</button></form><div className="bookmark-timeline"><h2>Timeline</h2>{workspace.timeline.map((group) => <section key={group.key}><h3>{group.label}</h3>{group.bookmarks.map((bookmark) => <a href={appHref(bookmark.selectUrl)} key={bookmark.id}>{bookmark.title}<small>{formatDate(bookmark.savedAt)}</small></a>)}</section>)}{workspace.timelinePagination.total > 1 && <nav className="timeline-pagination" aria-label="Timeline pages"><a aria-disabled={!workspace.timelinePagination.previousUrl} href={workspace.timelinePagination.previousUrl ? appHref(workspace.timelinePagination.previousUrl) : undefined}>Previous</a><span>{workspace.timelinePagination.firstItem}–{workspace.timelinePagination.lastItem} of {workspace.timelinePagination.totalCount}</span><a aria-disabled={!workspace.timelinePagination.nextUrl} href={workspace.timelinePagination.nextUrl ? appHref(workspace.timelinePagination.nextUrl) : undefined}>Next</a></nav>}</div></aside>
                </section>
            </main>
        </div>
        <div className="app-toast" role="status" aria-live="polite" hidden={!toast}>{toast}</div>
    </div>;
}
