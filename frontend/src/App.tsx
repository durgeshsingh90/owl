import {useCallback, useEffect, useRef, useState} from "react";

import {ApiError, requestJson} from "./api";
import {
    AddBitbucketSourceDialog,
    type BitbucketSourceType,
} from "./components/AddBitbucketSourceDialog";
import {
    BitbucketSettingsDialog,
    type BitbucketSettingsValues,
} from "./components/BitbucketSettingsDialog";
import {DocumentLibrary} from "./components/DocumentLibrary";
import {RepositorySidebar} from "./components/RepositorySidebar";
import {useTheme} from "./hooks/useTheme";
import type {
    DocumentItem,
    JobsResponse,
    OpenDocumentResponse,
    Repository,
    SettingsResponse,
    SyncJob,
    WorkspacePayload,
} from "./types";

const ACTIVE_STATUSES = new Set(["queued", "running", "auth_required"]);

function errorMessage(error: unknown): string {
    if (error instanceof ApiError) {
        const firstFieldMessage = Object.values(error.data.errors ?? {})
            .flatMap((items) => items)
            .map((item) => item.message)
            .find(Boolean);
        return firstFieldMessage ?? error.message;
    }
    return error instanceof Error ? error.message : "The request could not be completed.";
}

async function copyText(value: string): Promise<void> {
    if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return;
    }
    const area = document.createElement("textarea");
    area.value = value;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("Clipboard access was denied.");
}

function mergeRepository(current: Repository, job: SyncJob): Repository {
    if (current.id !== job.repository.id) return current;
    return {
        ...current,
        state: job.repository.state,
        stateLabel: job.repository.state.replaceAll("_", " "),
        statusMessage: job.repository.statusMessage,
        pdfCount: job.repository.pdfCount,
        indexedPdfCount: job.repository.indexedPdfCount,
        failedPdfCount: job.repository.failedPdfCount,
        vsdxCount: job.repository.vsdxCount,
    };
}

export default function App() {
    const root = document.getElementById("bitbucket-root");
    const workspaceUrl = root?.dataset.workspaceUrl || "/bitbucket/workspace/";
    const [theme, toggleTheme] = useTheme();
    const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null);
    const [loadingError, setLoadingError] = useState("");
    const [toast, setToast] = useState("");
    const [settingsOpen, setSettingsOpen] = useState(false);
    const [settingsOrigin, setSettingsOrigin] = useState("");
    const [sourceOpen, setSourceOpen] = useState(false);
    const [sourceType, setSourceType] = useState<BitbucketSourceType>("project");
    const csrfTokenRef = useRef("");
    const activeJobIdsRef = useRef<Set<string>>(new Set());
    const completedJobIdsRef = useRef<Set<string>>(new Set());

    const showToast = useCallback((message: string) => setToast(message), []);

    useEffect(() => {
        if (!toast) return;
        const timer = window.setTimeout(() => setToast(""), 5000);
        return () => window.clearTimeout(timer);
    }, [toast]);

    const loadWorkspace = useCallback(async () => {
        const url = new URL(workspaceUrl, window.location.href);
        url.search = window.location.search;
        const data = await requestJson<WorkspacePayload>(url.toString(), "");
        csrfTokenRef.current = data.csrfToken;
        activeJobIdsRef.current = new Set(
            data.jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).map((job) => job.id),
        );
        const authenticationJob = data.jobs.find((job) => job.status === "auth_required");
        if (authenticationJob) {
            try {
                setSettingsOrigin(new URL(authenticationJob.repository.url).origin);
            } catch {
                setSettingsOrigin("");
            }
            setSettingsOpen(true);
        }
        setWorkspace(data);
        setLoadingError("");
        return data;
    }, [workspaceUrl]);

    useEffect(() => {
        void loadWorkspace().catch((error) => setLoadingError(errorMessage(error)));
    }, [loadWorkspace]);

    const handleJob = useCallback((job: SyncJob) => {
        setWorkspace((current) => current ? {
            ...current,
            repositories: current.repositories.map((repository) => mergeRepository(repository, job)),
            selectedRepository: current.selectedRepository
                ? mergeRepository(current.selectedRepository, job)
                : null,
        } : current);

        if (ACTIVE_STATUSES.has(job.status)) activeJobIdsRef.current.add(job.id);
        else activeJobIdsRef.current.delete(job.id);

        if (job.status === "auth_required") {
            try {
                setSettingsOrigin(new URL(job.repository.url).origin);
            } catch {
                setSettingsOrigin("");
            }
            setSettingsOpen(true);
            return;
        }

        if (completedJobIdsRef.current.has(job.id)) return;
        if (job.status === "succeeded") {
            completedJobIdsRef.current.add(job.id);
            showToast(`${job.repository.name} is ready. Refreshing the PDF list…`);
            void loadWorkspace().catch((error) => showToast(errorMessage(error)));
        } else if (job.status === "failed") {
            completedJobIdsRef.current.add(job.id);
            showToast(job.errorMessage || `${job.repository.name} could not be updated.`);
        }
    }, [loadWorkspace, showToast]);

    useEffect(() => {
        if (!workspace) return;
        let stopped = false;
        let timer = 0;
        const poll = async () => {
            try {
                const url = new URL(workspace.statusUrl, window.location.href);
                for (const jobId of activeJobIdsRef.current) url.searchParams.append("job", jobId);
                const data = await requestJson<{ok: true; jobs: SyncJob[]}>(
                    url.toString(),
                    csrfTokenRef.current,
                );
                for (const job of data.jobs) handleJob(job);
            } catch {
                // A later bounded poll retries without interrupting the user.
            }
            if (!stopped) {
                timer = window.setTimeout(poll, activeJobIdsRef.current.size ? 1500 : 5000);
            }
        };
        timer = window.setTimeout(poll, 0);
        return () => {
            stopped = true;
            window.clearTimeout(timer);
        };
    }, [handleJob, workspace?.statusUrl]);

    useEffect(() => {
        if (!workspace) return;
        const schedule = () => requestJson<{ok: true; queued: number}>(
            workspace.scheduleUrl,
            csrfTokenRef.current,
            {method: "POST"},
        ).catch(() => undefined);
        void schedule();
        const timer = window.setInterval(() => void schedule(), 60_000);
        return () => window.clearInterval(timer);
    }, [workspace?.scheduleUrl]);

    const settingsBody = (values: BitbucketSettingsValues) => {
        const body = new FormData();
        body.set("base_url", values.baseUrl);
        body.set("username", values.username);
        body.set("access_token", values.accessToken);
        body.set("verify_ssl", values.verifySsl ? "true" : "false");
        return body;
    };

    const saveSettings = async (values: BitbucketSettingsValues) => {
        if (!workspace) return;
        try {
            const data = await requestJson<SettingsResponse>(
                workspace.settingsSaveUrl,
                csrfTokenRef.current,
                {method: "POST", body: settingsBody(values)},
            );
            await loadWorkspace();
            showToast(data.message);
        } catch (error) {
            throw new Error(errorMessage(error));
        }
    };

    const testSettings = async (values: BitbucketSettingsValues): Promise<string> => {
        if (!workspace) throw new Error("The Bitbucket workspace is not ready.");
        try {
            const data = await requestJson<SettingsResponse>(
                workspace.settingsTestUrl,
                csrfTokenRef.current,
                {method: "POST", body: settingsBody(values)},
            );
            return data.message;
        } catch (error) {
            throw new Error(errorMessage(error));
        }
    };

    const addSource = async (kind: BitbucketSourceType, sourceUrl: string) => {
        if (!workspace) return;
        const body = new FormData();
        body.set("source_type", kind);
        body.set("source_url", sourceUrl);
        try {
            const data = await requestJson<JobsResponse>(
                workspace.addSourceUrl,
                csrfTokenRef.current,
                {method: "POST", body},
            );
            for (const job of data.jobs) {
                activeJobIdsRef.current.add(job.id);
                handleJob(job);
            }
            await loadWorkspace();
            const label = data.repositoryCount === 1 ? "repository" : "repositories";
            showToast(data.repositoryCount + " " + label + " added to the metadata queue.");
        } catch (error) {
            throw new Error(errorMessage(error));
        }
    };

    const searchDocuments = async (query: string) => {
        const current = new URL(window.location.href);
        if (query.trim()) current.searchParams.set("q", query.trim());
        else current.searchParams.delete("q");
        current.searchParams.delete("page");
        window.history.pushState({}, "", current);
        try {
            await loadWorkspace();
        } catch (error) {
            showToast(errorMessage(error));
        }
    };

    const openDocument = async (document: DocumentItem) => {
        try {
            const data = await requestJson<OpenDocumentResponse>(
                document.openUrl,
                csrfTokenRef.current,
                {method: "POST"},
            );
            setWorkspace((current) => current ? {
                ...current,
                timeline: current.timeline.map((group) => ({
                    ...group,
                    documents: group.documents.map((item) => item.id === document.id
                        ? {...item, openCount: data.openCount}
                        : item),
                })),
            } : current);
        } catch (error) {
            showToast(errorMessage(error));
        }
    };

    const copyPath = async (document: DocumentItem) => {
        try {
            await copyText(document.relativePath);
            showToast("Repository path copied.");
        } catch (error) {
            showToast(errorMessage(error));
        }
    };

    if (!workspace) {
        return (
            <main className="bb-bootstrap-state">
                <span className="bb-wordmark__symbol" aria-hidden="true">B</span>
                <h1>{loadingError ? "Bitbucket could not load" : "Opening Bitbucket…"}</h1>
                {loadingError && <p role="alert">{loadingError}</p>}
                {loadingError && <button type="button" onClick={() => void loadWorkspace()}>Retry</button>}
            </main>
        );
    }

    return (
        <>
            <a className="skip-link" href="#document-library">Skip to PDF library</a>
            <div className="bb-workspace">
                <RepositorySidebar
                    homeUrl={workspace.homeUrl}
                    onAddSource={(kind) => {
                        setSourceType(kind);
                        setSourceOpen(true);
                    }}
                    onOpenSettings={(url = "") => {
                        try {
                            setSettingsOrigin(url ? new URL(url).origin : "");
                        } catch {
                            setSettingsOrigin("");
                        }
                        setSettingsOpen(true);
                    }}
                    onRefresh={(repository) => addSource("repository", repository.url)}
                    repositories={workspace.repositories}
                    repositoryCount={workspace.repositoryCount}
                    scheduleHour={workspace.scheduleHour}
                    selectedRepository={workspace.selectedRepository}
                    totalPdfCount={workspace.totalPdfCount}
                />
                <DocumentLibrary
                    onCopyPath={copyPath}
                    onOpen={openDocument}
                    onSearch={searchDocuments}
                    theme={theme}
                    toggleTheme={toggleTheme}
                    workspace={workspace}
                />
                <BitbucketSettingsDialog
                    credentials={workspace.credentials}
                    initialOrigin={settingsOrigin}
                    open={settingsOpen}
                    onClose={() => setSettingsOpen(false)}
                    onSave={saveSettings}
                    onTest={testSettings}
                />
                <AddBitbucketSourceDialog
                    configured={workspace.credentials.some((credential) => credential.configured)}
                    initialType={sourceType}
                    open={sourceOpen}
                    onAdd={addSource}
                    onClose={() => setSourceOpen(false)}
                    onOpenSettings={() => {
                        setSettingsOrigin("");
                        setSettingsOpen(true);
                    }}
                />
                <div className="bb-toast" role="status" aria-live="polite" hidden={!toast}>{toast}</div>
            </div>
        </>
    );
}
