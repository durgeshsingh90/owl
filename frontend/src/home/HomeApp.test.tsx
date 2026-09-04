import {cleanup, render, screen} from "@testing-library/react";
import {afterEach, beforeEach, expect, it, vi} from "vitest";

import {HomeApp} from "./HomeApp";
import type {HomeWorkspace} from "./types";

const workspace: HomeWorkspace = {
    ok: true,
    csrfToken: "csrf",
    statusMessage: "Ready · Home",
    urls: {home: "/", bookmarks: "/bookmarks/", bookmarkSettings: "/bookmarks/settings/", bitbucket: "/bitbucket/", systemStatus: "/system-status/"},
    bookmarkMetrics: [{label: "Bookmarks saved", value: "2", detail: "Across every domain", kind: "bookmarks"}],
    bookmarkActivity: {total: 1, label: "1 activity in 2026", filters: [{label: "All", href: "/?activity=all", selected: true}], years: [{label: "2026", href: "/?year=2026", selected: true}], monthLabels: [], weeks: [{days: [{date: "2026-09-04", ariaLabel: "1 activity", count: 1, level: 4, inYear: true}]}], trackingNote: "Tracked locally.", addedCount: 1, openedCount: 0, refreshedCount: 0, notesCount: 0, mostActiveDay: "4 Sep"},
    topViewed: [], interesting: [],
    bookmarkPeople: {label: "Last 7 days", written_pages: 1, updated_pages: 1, active_people: 1, writers: [{name: "Alex", page_count: 1}], updaters: [{name: "Alex", page_count: 1}], filters: []},
    database: {available: true, size_label: "2 MB", table_count: 10, row_count: 20, measured_at: "2026-09-04T10:00:00Z", detail: ""},
};

beforeEach(() => {
    document.body.innerHTML = '<div id="home-root" data-workspace-url="/home/workspace/"></div>';
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(workspace), {status: 200, headers: {"Content-Type": "application/json"}})));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("renders Home app cards and dashboard data from Django JSON", async () => {
    render(<HomeApp />, {container: document.getElementById("home-root")!});
    expect(await screen.findByRole("heading", {name: "Your apps"})).toBeVisible();
    expect(screen.getByText("Bookmarks saved")).toBeVisible();
    expect(screen.getByRole("region", {name: "Bookmark activity calendar"})).toBeVisible();
    expect(screen.getByText("2 MB")).toBeVisible();
    expect(screen.queryByText("Bitbucket Search")).not.toBeInTheDocument();
});
