import {appHref} from "../routing";
import type {Repository} from "../types";

interface RepositorySidebarProps {
    homeUrl: string;
    onOpenSettings: (repositoryUrl?: string) => void;
    repositories: Repository[];
    repositoryCount: number;
    scheduleHour: number;
    selectedRepository: Repository | null;
    totalPdfCount: number;
}

function formatUpdated(value: string | null): string | null {
    if (!value) return null;
    const timestamp = Date.parse(value);
    if (Number.isNaN(timestamp)) return null;
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
    if (minutes < 1) return "Updated just now";
    if (minutes < 60) return `Updated ${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `Updated ${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.floor(hours / 24);
    return `Updated ${days} day${days === 1 ? "" : "s"} ago`;
}

export function RepositorySidebar({
    homeUrl,
    onOpenSettings,
    repositories,
    repositoryCount,
    scheduleHour,
    selectedRepository,
    totalPdfCount,
}: RepositorySidebarProps) {
    return (
        <aside className="bb-repository-sidebar" aria-labelledby="repositories-heading">
            <a className="bb-wordmark" href={appHref(homeUrl)}>
                <span className="bb-wordmark__symbol" aria-hidden="true">B</span>
                <span><strong>Bitbucket</strong><small>Document desk</small></span>
            </a>

            <section className="bb-add-repository">
                <p className="bb-eyebrow">HTTPS repositories</p>
                <div className="bb-repository-heading">
                    <h1 id="repositories-heading">Your repositories</h1>
                    <button
                        type="button"
                        className="bb-settings-button"
                        aria-label="Bitbucket settings"
                        title="Bitbucket settings"
                        onClick={() => onOpenSettings()}
                    >
                        <span aria-hidden="true">⚙</span>
                    </button>
                </div>
                <p className="bb-form-help">Metadata is read directly from Bitbucket. No repository is cloned.</p>
            </section>

            <nav className="bb-repository-list" aria-label="Repositories">
                <a
                    className={`bb-repository-card bb-repository-card--all${selectedRepository ? "" : " is-selected"}`}
                    href={appHref("/bitbucket/")}
                >
                    <span className="bb-repository-card__icon" aria-hidden="true">⌘</span>
                    <span><strong>All repositories</strong><small>{repositoryCount} connected</small></span>
                    <span className="bb-repository-card__counts">{totalPdfCount} PDF</span>
                </a>

                {repositories.map((repository) => (
                    <article
                        className={`bb-repository-card${selectedRepository?.id === repository.id ? " is-selected" : ""}`}
                        data-repository-card={repository.id}
                        data-state={repository.state}
                        key={repository.id}
                    >
                        <a href={appHref(repository.selectUrl)}>
                            <span className="bb-repository-card__icon" aria-hidden="true">◫</span>
                            <span className="bb-repository-card__identity">
                                <strong>{repository.name}</strong>
                                <small>{repository.project} · {repository.repositoryHost}</small>
                            </span>
                        </a>
                        <div className="bb-repository-card__footer">
                            <span className="bb-state">{repository.stateLabel}</span>
                            <span>{repository.pdfCount} PDF · {repository.vsdxCount} VSDX</span>
                        </div>
                        <small className="bb-repository-card__index-counts">
                            {repository.indexedPdfCount} indexed · {repository.failedPdfCount} failed
                        </small>
                        <p>{repository.statusMessage}</p>
                        <button type="button" onClick={() => onOpenSettings(repository.url)}>
                            Update token or refresh
                        </button>
                        {formatUpdated(repository.lastSuccessfulSyncAt) && (
                            <time dateTime={repository.lastSuccessfulSyncAt ?? undefined}>
                                {formatUpdated(repository.lastSuccessfulSyncAt)}
                            </time>
                        )}
                    </article>
                ))}

                {repositories.length === 0 && (
                    <div className="bb-repository-empty">
                        <span aria-hidden="true">↳</span>
                        <strong>Add your first repository</strong>
                        <p>Open settings to enter its HTTPS URL and repository-read access token.</p>
                    </div>
                )}
            </nav>

            <footer>
                <span className="bb-live-dot" aria-hidden="true" />
                Daily API refresh at {String(scheduleHour).padStart(2, "0")}:00 local time · once per repository
            </footer>
        </aside>
    );
}
