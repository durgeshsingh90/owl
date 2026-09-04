import {FormEvent, useCallback, useEffect, useState} from "react";

import {ApiError, requestJson} from "../api";
import {useTheme} from "../hooks/useTheme";
import {appHref} from "../routing";

interface SettingsWorkspace {
    ok: true;
    csrfToken: string;
    statusMessage: string;
    notice: string;
    selectedSection: string;
    selectedTask: string;
    sections: Array<{key: string; label: string; href: string; active: boolean}>;
    urls: Record<string, string>;
    configuration: {source: string; complete: boolean; state: string; label: string; detail: string; last_verified_at: string | null; has_stored_credential: boolean; managed_externally: boolean; baseUrl: string; authMode: string};
    bookmarkCounts: Record<string, number>;
    lastImport: null | {id: number; status: string; outcome: string; imported: number; skipped: number; failed: number; completedAt: string | null};
}

function errorText(error: unknown): string {
    if (error instanceof ApiError) return error.data.detail || error.message;
    return error instanceof Error ? error.message : "The request could not be completed.";
}

function Csrf({token}: {token: string}) { return <input type="hidden" name="csrfmiddlewaretoken" value={token} />; }

function Overview({workspace}: {workspace: SettingsWorkspace}) {
    return <div className="settings-content"><header><p>Workspace configuration</p><h1>Settings overview</h1><span>Choose a focused section. Credentials stay blank after every reload.</span></header><div className="settings-overview-grid"><a href={appHref(`${workspace.urls.settings}?section=confluence`)}><b>Confluence</b><span>{workspace.configuration.label}</span><small>{workspace.configuration.detail}</small></a><a href={appHref(`${workspace.urls.settings}?section=bookmark-data`)}><b>Bookmark data</b><span>{workspace.bookmarkCounts.all_bookmarks || 0} bookmarks</span><small>Import or export credential-free local data.</small></a></div></div>;
}

function ConfluenceSettings({workspace, notify}: {workspace: SettingsWorkspace; notify: (text: string) => void}) {
    const [baseUrl, setBaseUrl] = useState(workspace.configuration.baseUrl);
    const [token, setToken] = useState("");
    const [showToken, setShowToken] = useState(false);
    const [receipt, setReceipt] = useState("");
    const [busy, setBusy] = useState(false);
    const post = async (url: string, extra: Record<string, string>) => {
        const body = new FormData(); Object.entries(extra).forEach(([key, value]) => body.set(key, value));
        return requestJson<{label: string; detail: string; verification_receipt?: string}>(url, workspace.csrfToken, {method: "POST", body});
    };
    const test = async () => { setBusy(true); try { const result = await post(workspace.urls.confluenceTest, {base_url: baseUrl, personal_access_token: token, auth_mode: workspace.configuration.authMode, verification_receipt: ""}); setReceipt(result.verification_receipt || ""); notify(`${result.label}: ${result.detail}`); } catch (error) { setReceipt(""); notify(errorText(error)); } finally { setBusy(false); } };
    const save = async (event: FormEvent) => { event.preventDefault(); setBusy(true); try { const result = await post(workspace.urls.confluenceSave, {base_url: baseUrl, personal_access_token: token, auth_mode: workspace.configuration.authMode, verification_receipt: receipt}); setToken(""); setReceipt(""); notify(`${result.label}: ${result.detail}`); } catch (error) { notify(errorText(error)); } finally { setBusy(false); } };
    return <div className="settings-content"><header><p>Knowledge source</p><h1>Confluence</h1><span>Test the exact origin and PAT before saving.</span></header><article className="settings-card"><div className="settings-state"><b data-state={workspace.configuration.state}>{workspace.configuration.label}</b><span>{workspace.configuration.detail}</span></div>{workspace.configuration.managed_externally ? <p>This profile is managed outside OWL and is read-only here.</p> : <form onSubmit={save}><label>Confluence base URL<input type="url" required value={baseUrl} onChange={(event) => {setBaseUrl(event.target.value); setReceipt("");}} placeholder="https://confluence.company.example/wiki" /></label><label>Personal access token<div className="secret-control"><input type={showToken ? "text" : "password"} required={!workspace.configuration.has_stored_credential} value={token} onChange={(event) => {setToken(event.target.value); setReceipt("");}} autoComplete="new-password" /><button type="button" onClick={() => setShowToken((value) => !value)}>{showToken ? "Hide" : "Show"}</button></div><small>Leave blank only when retaining the existing stored credential.</small></label><div className="settings-actions"><button className="primary-button" disabled={busy || !receipt} type="submit">Save settings</button><button disabled={busy} type="button" onClick={() => void test()}>Test connection</button></div></form>}</article>{workspace.configuration.has_stored_credential && !workspace.configuration.managed_externally && <form className="settings-danger" method="post" action={workspace.urls.confluenceRemove}><Csrf token={workspace.csrfToken} /><input type="hidden" name="return_to" value="settings" /><input type="hidden" name="confirm" value="remove" /><p><b>Remove Confluence connection</b><span>Saved bookmarks will remain in OWL.</span></p><button type="submit">Remove connection</button></form>}</div>;
}

function BookmarkData({workspace}: {workspace: SettingsWorkspace}) {
    return <div className="settings-content"><header><p>Local portability</p><h1>Bookmark data</h1><span>Credentials are never included in imports or exports.</span></header>{workspace.lastImport && <article className="settings-notice"><b>Import {workspace.lastImport.status.toLowerCase()}</b><span>{workspace.lastImport.outcome}</span></article>}<div className="settings-data-grid"><article className="settings-card"><h2>Export JSON</h2><p>Download bookmarks and personal organisation data in a credential-free backup.</p><form method="post" action={workspace.urls.bookmarkExport}><Csrf token={workspace.csrfToken} /><button className="primary-button" type="submit">Export JSON</button></form></article><article className="settings-card"><h2>Import bookmarks</h2><p>Merge an OWL JSON export or a UTF-8 text file of URLs.</p><form method="post" action={workspace.urls.bookmarkImport} encType="multipart/form-data"><Csrf token={workspace.csrfToken} /><input type="hidden" name="return_to" value="settings" /><label>Import file<input type="file" name="import_file" accept=".json,.txt,application/json,text/plain" required /></label><button className="primary-button" type="submit">Import bookmarks</button></form></article></div></div>;
}

export function SettingsApp() {
    const root = document.getElementById("settings-root");
    const workspaceUrl = root?.dataset.workspaceUrl || "/bookmarks/settings/workspace/";
    const [workspace, setWorkspace] = useState<SettingsWorkspace | null>(null);
    const [error, setError] = useState("");
    const [toast, setToast] = useState("");
    const [theme, toggleTheme] = useTheme();
    const load = useCallback(async () => { const url = new URL(workspaceUrl, window.location.href); url.search = window.location.search; const data = await requestJson<SettingsWorkspace>(url.toString(), ""); setWorkspace(data); setError(""); }, [workspaceUrl]);
    useEffect(() => { void load().catch((reason) => setError(errorText(reason))); }, [load]);
    useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(""), 6000); return () => window.clearTimeout(timer); }, [toast]);
    if (!workspace) return <main className="app-loading"><div className="owl-mark">OWL</div><h1>{error ? "Settings could not load" : "Opening Settings…"}</h1>{error && <p role="alert">{error}</p>}</main>;
    return <div className="settings-app"><aside className="settings-sidebar"><a className="bookmark-brand" href={appHref(workspace.urls.home)}><span className="owl-mark">OWL</span><span><strong>Settings</strong><small>Local workspace</small></span></a><nav aria-label="Settings sections">{workspace.sections.map((section) => <a className={section.active ? "is-active" : ""} href={appHref(section.href)} key={section.key}>{section.label}</a>)}</nav><a href={appHref(workspace.urls.bookmarks)}>← Bookmark Manager</a><button onClick={toggleTheme}>{theme === "dark" ? "☼ Light mode" : "☾ Dark mode"}</button></aside><main className="settings-main">{workspace.notice && <p className="settings-notice" role="status">{workspace.notice}</p>}{workspace.selectedSection === "overview" && <Overview workspace={workspace} />}{workspace.selectedSection === "confluence" && <ConfluenceSettings workspace={workspace} notify={setToast} />}{workspace.selectedSection === "bookmark-data" && <BookmarkData workspace={workspace} />}</main><div className="app-toast" hidden={!toast} role="status" aria-live="polite">{toast}</div></div>;
}
