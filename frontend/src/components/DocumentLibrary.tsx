import {useEffect, useState, type FormEvent} from "react";

import {appHref} from "../routing";
import type {DocumentItem, Theme, WorkspacePayload} from "../types";

interface DocumentLibraryProps {
    onCopyPath: (document: DocumentItem) => Promise<void>;
    onOpen: (document: DocumentItem) => Promise<void>;
    onSearch: (query: string) => Promise<void>;
    theme: Theme;
    toggleTheme: () => void;
    workspace: WorkspacePayload;
}

export function DocumentLibrary({
    onCopyPath,
    onOpen,
    onSearch,
    theme,
    toggleTheme,
    workspace,
}: DocumentLibraryProps) {
    const [query, setQuery] = useState(workspace.search.query);

    useEffect(() => setQuery(workspace.search.query), [workspace.search.query]);

    const submitSearch = (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        void onSearch(query);
    };

    return (
        <main className="bb-library" id="document-library" tabIndex={-1}>
            <header className="bb-library-header">
                <div>
                    <p className="bb-eyebrow">Bitbucket API catalogue</p>
                    <h2>
                        {workspace.selectedRepository
                            ? `${workspace.selectedRepository.project} / ${workspace.selectedRepository.name}`
                            : "All repository PDFs"}
                    </h2>
                    <p>
                        {workspace.search.active
                            ? `${workspace.search.resultCount} matching PDF${workspace.search.resultCount === 1 ? "" : "s"}`
                            : `${workspace.documentCount} PDF${workspace.documentCount === 1 ? "" : "s"}`}
                        {" · "}{workspace.totalIndexedPdfCount} indexed
                        {" · "}{workspace.totalFailedPdfCount} failed
                        {" · "}{workspace.totalVsdxCount} VSDX
                    </p>
                </div>
                <div className="bb-library-header__actions">
                    <button
                        type="button"
                        className="bb-theme-toggle"
                        aria-pressed={theme === "dark"}
                        onClick={toggleTheme}
                    >
                        <span aria-hidden="true">{theme === "dark" ? "☀" : "◐"}</span>
                        <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
                    </button>
                    <a href={appHref(workspace.homeUrl)}>OWL home <span aria-hidden="true">↗</span></a>
                </div>
            </header>

            <form className="bb-pdf-search" role="search" onSubmit={submitSearch}>
                <label htmlFor="bitbucket-pdf-search">Search saved PDF metadata and extracted text</label>
                <div>
                    <input
                        id="bitbucket-pdf-search"
                        type="search"
                        value={query}
                        placeholder="Filename, path, author, commit message, or PDF text"
                        onChange={(event) => setQuery(event.target.value)}
                    />
                    <button type="submit">Search database</button>
                    {workspace.search.active && (
                        <button type="button" onClick={() => {setQuery(""); void onSearch("");}}>
                            Clear
                        </button>
                    )}
                </div>
                <small>
                    Search reads the local Django database. PDF crawling uses {workspace.workerCount} worker
                    {workspace.workerCount === 1 ? "" : "s"}.
                </small>
            </form>

            <div className="bb-timeline">
                {workspace.timeline.map((group, groupIndex) => (
                    <section className="bb-timeline-group" aria-labelledby={`timeline-${groupIndex}`} key={group.label}>
                        <header>
                            <h3 id={`timeline-${groupIndex}`}>{group.label}</h3>
                            <span>{group.documents.length}</span>
                        </header>
                        <div className="bb-table-scroll" role="region" aria-label={`${group.label} PDFs`} tabIndex={0}>
                            <table>
                                <thead>
                                    <tr>
                                        <th>PDF name</th><th>Path</th><th>Project</th><th>Repository</th>
                                        <th>Date added to repo</th><th>Added by</th><th>Commit ID</th>
                                        <th>Pages</th><th>Size</th><th>Search status</th>
                                        <th>Open count</th><th>Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {group.documents.map((document) => (
                                        <tr key={document.id} data-document-row={document.id}>
                                            <th scope="row">
                                                <a
                                                    className="bb-file-link"
                                                    href={document.browserUrl}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    onClick={() => void onOpen(document)}
                                                >
                                                    {document.filename}
                                                </a>
                                                {document.textPreview && (
                                                    <small className="bb-text-preview">{document.textPreview}</small>
                                                )}
                                            </th>
                                            <td>
                                                <button
                                                    type="button"
                                                    className="bb-path-copy"
                                                    title={`Copy ${document.relativePath}`}
                                                    onClick={() => void onCopyPath(document)}
                                                >
                                                    <code>{document.relativePath}</code>
                                                </button>
                                            </td>
                                            <td>{document.project}</td>
                                            <td>{document.repository}</td>
                                            <td>
                                                {document.addedDate
                                                    ? <time dateTime={document.addedAt ?? undefined}>{document.addedDate}</time>
                                                    : <span className="bb-muted">Unavailable</span>}
                                            </td>
                                            <td>
                                                {document.addedBy
                                                    ? <span title={document.addedByEmail ?? undefined}>{document.addedBy}</span>
                                                    : <span className="bb-muted">Unavailable</span>}
                                            </td>
                                            <td>
                                                {document.commitShort
                                                    ? <span className="bb-commit-detail">
                                                        <code title={document.commitId ?? undefined}>{document.commitShort}</code>
                                                        {document.commitMessage && <small title={document.commitMessage}>{document.commitMessage}</small>}
                                                    </span>
                                                    : <span className="bb-muted">—</span>}
                                            </td>
                                            <td>{document.pageCount || <span className="bb-muted">—</span>}</td>
                                            <td>{document.fileSize ? document.fileSizeLabel : <span className="bb-muted">—</span>}</td>
                                            <td>
                                                <span className={`bb-index-state is-${document.indexState}`}>
                                                    {document.indexStateLabel}
                                                </span>
                                                {document.indexError && <small title={document.indexError}>{document.indexError}</small>}
                                                {document.textTruncated && <small>Text capped by settings</small>}
                                            </td>
                                            <td>{document.openCount}</td>
                                            <td>
                                                <a
                                                    className="bb-reveal-button"
                                                    href={document.folderUrl}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                >
                                                    Show in folder
                                                </a>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>
                ))}

                {workspace.timeline.length === 0 && (
                    <section className="bb-library-empty">
                        <span aria-hidden="true">PDF</span>
                        <h3>No PDFs to show yet</h3>
                        <p>Open Bitbucket settings and fetch repository metadata through the read-only API.</p>
                    </section>
                )}
            </div>

            {workspace.pagination.total > 1 && (
                <nav className="bb-pagination" aria-label="PDF pages">
                    {workspace.pagination.previousUrl
                        ? <a href={appHref(workspace.pagination.previousUrl)}>Previous 500</a>
                        : <span aria-disabled="true">Previous 500</span>}
                    <strong>Page {workspace.pagination.current} of {workspace.pagination.total}</strong>
                    {workspace.pagination.nextUrl
                        ? <a href={appHref(workspace.pagination.nextUrl)}>Next 500</a>
                        : <span aria-disabled="true">Next 500</span>}
                </nav>
            )}
        </main>
    );
}
