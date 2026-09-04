import {cleanup, render, screen} from "@testing-library/react";
import {afterEach, beforeEach, expect, it, vi} from "vitest";

import {BookmarkManagerApp} from "./BookmarkManagerApp";
import type {BookmarkItem, BookmarkWorkspace} from "./types";

const bookmark: BookmarkItem = {
    id: 1, pageId: "1001", outlineNumber: "1", title: "Availability sign-off", url: "https://docs.example.test/1001", sourceType: "confluence", sourceLabel: "Confluence", spaceName: "Architecture", spaceKey: "ADR", version: 3, createdAt: "2026-09-01T10:00:00Z", updatedAt: "2026-09-04T10:00:00Z", savedAt: "2026-09-04T11:00:00Z", createdBy: "Alex", modifiedBy: "Morgan", author: "Alex", category: {id: 1, name: "Engineering", domain: "docs.example.test"}, manualFolder: null, pageTextSizeBytes: 100, favorite: false, pinned: false, tags: ["architecture"], notes: "Review quarterly", openCount: 2, firstOpenedAt: null, lastViewedAt: null, lastViewedVersion: null, changedSinceViewed: false, lastRefreshAttemptAt: null, lastRefreshedAt: null, availability: "active", availabilityLabel: "Active", recency: "new", recencyLabel: "New", breadcrumb: [{title: "Availability sign-off", url: "https://docs.example.test/1001", isLeaf: true}], selectUrl: "/bookmarks/?selected=1", openUrl: "/bookmarks/1/open/", openParentUrl: "/bookmarks/1/open-parent/", organiseUrl: "/bookmarks/1/organise/", favoriteUrl: "/bookmarks/1/favorite/", pinUrl: "/bookmarks/1/pin/", deleteUrl: "/bookmarks/1/delete/",
};

const workspace: BookmarkWorkspace = {
    ok: true, csrfToken: "csrf", statusMessage: "Ready · 1 of 1 bookmarks", inlineError: "",
    urls: {home: "/", index: "/bookmarks/", workspace: "/bookmarks/workspace/", save: "/bookmarks/save/", settings: "/bookmarks/settings/", systemStatus: "/system-status/", bitbucket: "/bitbucket/", refreshStart: "/bookmarks/refresh/start/", refreshStatus: "/bookmarks/refresh/status/", connectionTest: "/bookmarks/connection/test/", deleteSelected: "/bookmarks/delete-selected/", folderCreate: "/bookmarks/folders/create/", folderMove: "/bookmarks/folders/move/"},
    search: {term: "", urlSearch: false, urlMatchCount: 0, firstUrlMatch: "", semanticFallback: false}, resultCount: 1, totalBookmarks: 1,
    counts: {all_bookmarks: 1, favorites: 0, pinned: 0, viewed: 1, never_viewed: 0, deleted_pages: 0}, activeFilters: [],
    sortControls: [{key: "saved", label: "OWL saved date", href: "?sort=added_oldest", active: true, direction: "newest first", ariaLabel: "Sort by saved date"}],
    refresh: {run_id: null, status: "idle", status_label: "Ready", active: false, total: 0, processed: 0, progress: 100, detail: "", last_completed_display: ""},
    tree: [{id: 1, title: bookmark.title, outlineNumber: "1", depth: 1, matches: true, selected: true, located: true, openCount: 2, bookmark, children: []}], manualFolders: [], folders: [], flatItems: [bookmark], selectedBookmark: bookmark, similarBookmarks: [], timeline: [{key: "month-2026-09", label: "September", bookmarks: [bookmark]}], timelinePagination: {current: 1, total: 1, totalCount: 1, firstItem: 1, lastItem: 1, previousUrl: null, nextUrl: null}, people: [{name: "Alex", pageCount: 1, writtenCount: 1, updatedCount: 0, selected: false}], categories: [{id: 1, name: "Engineering", domain: "docs.example.test", description: "", bookmarkCount: 1}], tagSuggestions: ["architecture"],
};

beforeEach(() => {
    window.history.replaceState({}, "", "/bookmarks/?selected=1");
    document.body.innerHTML = '<div id="bookmarks-root" data-workspace-url="/bookmarks/workspace/"></div>';
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).includes("connection/test") ? {state: "success", detail: "Connected"} : workspace), {status: 200, headers: {"Content-Type": "application/json"}})));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("renders searchable hierarchy, details, people, and timeline", async () => {
    render(<BookmarkManagerApp />, {container: document.getElementById("bookmarks-root")!});
    expect(await screen.findByRole("heading", {name: "Bookmark tree"})).toBeVisible();
    expect(screen.getAllByText("Availability sign-off").length).toBeGreaterThan(1);
    expect(screen.getByLabelText("Search bookmarks or add a URL")).toBeVisible();
    expect(screen.getAllByRole("complementary")).toHaveLength(3);
    expect(screen.getByDisplayValue("Review quarterly")).toBeVisible();
    expect(screen.getByRole("heading", {name: "People"})).toBeVisible();
    expect(screen.getByRole("heading", {name: "Timeline"})).toBeVisible();
});
