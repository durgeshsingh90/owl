import {cleanup, fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";

import App from "./App";
import type {WorkspacePayload} from "./types";

const workspace: WorkspacePayload = {
    ok: true,
    csrfToken: "test-csrf",
    homeUrl: "/",
    addRepositoryUrl: "/bitbucket/repositories/add/",
    addSourceUrl: "/bitbucket/sources/add/",
    settingsSaveUrl: "/bitbucket/settings/save/",
    settingsTestUrl: "/bitbucket/settings/test/",
    statusUrl: "/bitbucket/sync/status/",
    scheduleUrl: "/bitbucket/schedule/",
    repositories: [{
        id: 1,
        repositoryHost: "scm.example.test",
        project: "adr",
        name: "engineering-sign-off.git",
        state: "ready",
        stateLabel: "Ready",
        statusMessage: "Repository is current.",
        pdfCount: 1,
        indexedPdfCount: 1,
        failedPdfCount: 0,
        vsdxCount: 2,
        url: "https://scm.example.test/stash/scm/adr/engineering-sign-off.git",
        lastSuccessfulSyncAt: "2026-09-04T09:00:00Z",
        selectUrl: "?repository=1",
    }],
    repositoryCount: 1,
    totalPdfCount: 1,
    totalIndexedPdfCount: 1,
    totalFailedPdfCount: 0,
    totalVsdxCount: 2,
    selectedRepository: null,
    documentCount: 1,
    pageSize: 500,
    workerCount: 1,
    search: {query: "", active: false, resultCount: 1},
    timeline: [{
        label: "Today",
        documents: [{
            id: 11,
            filename: "availability.pdf",
            relativePath: "architecture/availability.pdf",
            project: "adr",
            repository: "engineering-sign-off.git",
            addedAt: "2026-09-04T08:00:00Z",
            addedDate: "04 Sep 2026",
            addedBy: "Alex Engineer",
            addedByEmail: "alex@example.test",
            additionCommitId: "added123",
            commitId: "abc12345deadbeef",
            commitShort: "abc12345",
            commitMessage: "Approve the availability design",
            commitAuthor: "Alex Engineer",
            commitAt: "2026-09-04T08:00:00Z",
            fileSize: 2048,
            fileSizeLabel: "2.0 KB",
            pageCount: 3,
            contentSha256: "f".repeat(64),
            textTruncated: false,
            indexState: "indexed",
            indexStateLabel: "Indexed",
            indexError: null,
            lastScannedAt: "2026-09-04T09:00:00Z",
            textPreview: "Service availability and recovery design",
            openCount: 2,
            openUrl: "/bitbucket/documents/11/open/",
            browserUrl: "https://scm.example.test/stash/projects/adr/repos/engineering-sign-off/browse/architecture/availability.pdf",
            folderUrl: "https://scm.example.test/stash/projects/adr/repos/engineering-sign-off/browse/architecture",
        }],
    }],
    pagination: {current: 1, total: 1, previousUrl: null, nextUrl: null},
    credentials: [{
        origin: "https://scm.example.test",
        configured: true,
        baseUrl: "https://scm.example.test/stash",
        apiBaseUrl: "https://scm.example.test/stash/rest/api/1.0",
        username: "api-reader",
        verifySsl: false,
        updatedAt: "2026-09-04T09:00:00Z",
    }],
    jobs: [],
    scheduleHour: 9,
};

function json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data), {
        status,
        headers: {"Content-Type": "application/json"},
    });
}

describe("Bitbucket React workspace", () => {
    beforeEach(() => {
        window.localStorage.clear();
        window.history.replaceState({}, "", "/bitbucket/");
        document.body.innerHTML = '<div id="bitbucket-root" data-workspace-url="/bitbucket/workspace/"></div>';
        vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
            const url = String(input);
            if (url.includes("/workspace/")) return json(workspace);
            if (url.includes("/sync/status/")) return json({ok: true, jobs: []});
            if (url.includes("/schedule/")) return json({ok: true, queued: 0});
            if (url.includes("/documents/11/open/")) return json({ok: true, openCount: 3});
            if (url.includes("/settings/test/")) return json({
                ok: true,
                message: "Connection successful. Bitbucket accepted the credentials.",
            });
            if (url.includes("/settings/save/")) return json({
                ok: true,
                message: "Bitbucket API settings saved.",
            });
            return json({ok: false, message: `Unexpected URL: ${url}`}, 404);
        }));
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {writeText: vi.fn(async () => undefined)},
        });
    });

    afterEach(() => {
        cleanup();
        vi.unstubAllGlobals();
    });

    it("renders PDF metadata and direct Bitbucket file and folder links", async () => {
        render(<App />, {container: document.getElementById("bitbucket-root")!});

        expect(await screen.findByRole("heading", {name: "All repository PDFs"})).toBeVisible();
        expect(screen.getAllByText("engineering-sign-off.git")).toHaveLength(2);
        const fileLink = screen.getByRole("link", {name: "availability.pdf"});
        const row = fileLink.closest("tr");
        expect(row).not.toBeNull();
        expect(within(row!).getByText("architecture/availability.pdf")).toBeVisible();
        expect(within(row!).getByText("Alex Engineer")).toBeVisible();
        expect(fileLink).toHaveAttribute("target", "_blank");
        expect(fileLink).toHaveAttribute("href", expect.stringContaining("/browse/architecture/availability.pdf"));
        expect(screen.getByRole("link", {name: "Show in folder"})).toHaveAttribute(
            "href",
            expect.stringContaining("/browse/architecture"),
        );

        fireEvent.click(fileLink);
        await waitFor(() => expect(within(row!).getByText("3")).toBeVisible());

        fireEvent.click(screen.getByTitle("Copy architecture/availability.pdf"));
        await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
            "architecture/availability.pdf",
        ));
    });

    it("opens token settings from the gear without repopulating the token", async () => {
        render(<App />, {container: document.getElementById("bitbucket-root")!});

        fireEvent.click(await screen.findByRole("button", {name: "Bitbucket settings"}));

        expect(screen.getByRole("heading", {name: "Bitbucket settings"})).toBeVisible();
        expect(screen.getByLabelText("Bitbucket base URL")).toHaveValue(
            "https://scm.example.test/stash",
        );
        expect(screen.getByLabelText("HTTP access token")).toHaveValue("");
        expect(screen.getByLabelText("Verify SSL certificates")).not.toBeChecked();
        expect(screen.queryByText("Configured servers")).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole("button", {name: "Test connection"}));
        expect(await screen.findByText(/Connection successful/)).toBeVisible();
    });

    it("offers separate new project and new repository flows", async () => {
        render(<App />, {container: document.getElementById("bitbucket-root")!});

        fireEvent.click(await screen.findByRole("button", {name: /New project/}));
        expect(screen.getByRole("heading", {name: "New project"})).toBeVisible();
        expect(screen.getByLabelText("Project HTTPS URL")).toBeVisible();
        fireEvent.click(screen.getByRole("button", {name: "Close add source"}));

        fireEvent.click(screen.getByRole("button", {name: /New repository/}));
        expect(screen.getByRole("heading", {name: "New repository"})).toBeVisible();
        expect(screen.getByLabelText("Repository HTTPS clone URL")).toBeVisible();
    });

    it("searches the saved database without reloading the application", async () => {
        render(<App />, {container: document.getElementById("bitbucket-root")!});

        const search = await screen.findByRole("searchbox", {
            name: "Search saved PDF metadata and extracted text",
        });
        fireEvent.change(search, {target: {value: "recovery plan"}});
        fireEvent.click(screen.getByRole("button", {name: "Search database"}));

        await waitFor(() => {
            const requestedUrls = vi.mocked(fetch).mock.calls.map(([url]) => String(url));
            expect(requestedUrls.some((url) => url.includes("q=recovery+plan"))).toBe(true);
        });
        expect(window.location.search).toBe("?q=recovery+plan");
    });

    it("persists an explicit dark-mode choice", async () => {
        render(<App />, {container: document.getElementById("bitbucket-root")!});

        const toggle = await screen.findByRole("button", {name: "Dark mode"});
        fireEvent.click(toggle);

        expect(document.documentElement).toHaveAttribute("data-theme", "dark");
        expect(window.localStorage.getItem("bitbucket-document-desk-theme")).toBe("dark");
        expect(screen.getByRole("button", {name: "Light mode"})).toHaveAttribute("aria-pressed", "true");
    });
});
