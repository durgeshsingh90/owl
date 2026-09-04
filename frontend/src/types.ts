export type Theme = "light" | "dark";
export type JobStatus =
    | "queued"
    | "running"
    | "auth_required"
    | "succeeded"
    | "failed"
    | "cancelled";

export interface Repository {
    id: number;
    repositoryHost: string;
    url: string;
    project: string;
    name: string;
    state: string;
    stateLabel: string;
    statusMessage: string;
    pdfCount: number;
    indexedPdfCount: number;
    failedPdfCount: number;
    vsdxCount: number;
    lastSuccessfulSyncAt: string | null;
    selectUrl: string;
}

export interface DocumentItem {
    id: number;
    filename: string;
    relativePath: string;
    project: string;
    repository: string;
    addedAt: string | null;
    addedDate: string | null;
    addedBy: string | null;
    addedByEmail: string | null;
    additionCommitId: string | null;
    commitId: string | null;
    commitShort: string | null;
    commitMessage: string | null;
    commitAuthor: string | null;
    commitAt: string | null;
    fileSize: number;
    fileSizeLabel: string;
    pageCount: number;
    contentSha256: string | null;
    textTruncated: boolean;
    indexState: "pending" | "indexed" | "failed";
    indexStateLabel: string;
    indexError: string | null;
    lastScannedAt: string | null;
    textPreview: string | null;
    openCount: number;
    openUrl: string;
    browserUrl: string;
    folderUrl: string;
}

export interface TimelineGroup {
    label: string;
    documents: DocumentItem[];
}

export interface JobRepository {
    id: number;
    project: string;
    name: string;
    state: string;
    statusMessage: string;
    pdfCount: number;
    indexedPdfCount: number;
    failedPdfCount: number;
    vsdxCount: number;
    url: string;
}

export interface SyncJob {
    id: string;
    status: JobStatus;
    operation: "initial" | "refresh";
    errorCode: string;
    errorMessage: string;
    repository: JobRepository;
    authenticationUrl: string;
    retryUrl: string;
    cancelUrl: string;
}

export interface WorkspacePayload {
    ok: true;
    csrfToken: string;
    homeUrl: string;
    addRepositoryUrl: string;
    addSourceUrl: string;
    settingsSaveUrl: string;
    settingsTestUrl: string;
    statusUrl: string;
    scheduleUrl: string;
    repositories: Repository[];
    repositoryCount: number;
    totalPdfCount: number;
    totalIndexedPdfCount: number;
    totalFailedPdfCount: number;
    totalVsdxCount: number;
    selectedRepository: Repository | null;
    documentCount: number;
    pageSize: number;
    workerCount: number;
    search: {
        query: string;
        active: boolean;
        resultCount: number;
    };
    timeline: TimelineGroup[];
    pagination: {
        current: number;
        total: number;
        previousUrl: string | null;
        nextUrl: string | null;
    };
    credentials: Array<{
        origin: string;
        configured: boolean;
        baseUrl: string;
        apiBaseUrl: string;
        username: string;
        verifySsl: boolean;
        updatedAt: string;
    }>;
    jobs: SyncJob[];
    scheduleHour: number;
}

export interface OpenDocumentResponse {
    ok: true;
    openCount: number;
}

export interface JobResponse {
    ok: true;
    job: SyncJob;
}

export interface JobsResponse {
    ok: true;
    sourceType: "project" | "repository";
    repositoryCount: number;
    createdCount: number;
    jobs: SyncJob[];
}

export interface SettingsResponse {
    ok: true;
    message: string;
}

export interface ErrorResponse {
    message?: string;
    detail?: string;
    label?: string;
    errors?: Record<string, Array<{message: string}>>;
}
