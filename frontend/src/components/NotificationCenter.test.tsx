import {cleanup, fireEvent, render, screen} from "@testing-library/react";
import {afterEach, expect, it, vi} from "vitest";

import {NotificationCenter} from "./NotificationCenter";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("displays normal OWL notifications without legacy recovery controls", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
        notifications: [{
            id: 1,
            kind: "bookmark_import",
            kindLabel: "Bookmark import",
            state: "success",
            stateLabel: "Success",
            title: "Bookmark import complete",
            message: "One bookmark was imported.",
            targetPath: "/bookmarks/",
            occurredAt: "2026-09-04T10:00:00Z",
            read: false,
        }],
        unread_count: 1,
    }), {status: 200, headers: {"Content-Type": "application/json"}})));

    render(<NotificationCenter />);

    fireEvent.click(await screen.findByRole("button", {name: "Open notifications"}));
    expect(await screen.findByText("Bookmark import complete")).toBeVisible();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});
