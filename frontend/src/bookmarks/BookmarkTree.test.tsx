import {cleanup, fireEvent, render, screen} from "@testing-library/react";
import {afterEach, expect, it, vi} from "vitest";

import {BookmarkTree} from "./BookmarkTree";
import type {BookmarkItem, BookmarkTreeItem} from "./types";

function leaf(id: number, title: string): BookmarkTreeItem {
    return {
        id,
        title,
        outlineNumber: String(id),
        depth: 2,
        matches: true,
        selected: false,
        located: false,
        openCount: 0,
        bookmark: {id, title, selectUrl: `/bookmarks/?selected=${id}`} as BookmarkItem,
        children: [],
    };
}

afterEach(cleanup);

it("selects every bookmark below a hierarchy branch", () => {
    const onSelectMany = vi.fn();
    const tree: BookmarkTreeItem[] = [{
        id: 10,
        title: "Architecture",
        outlineNumber: "1",
        depth: 1,
        matches: true,
        selected: false,
        located: false,
        openCount: 0,
        bookmark: null,
        children: [leaf(1, "Decision one"), leaf(2, "Decision two")],
    }];

    render(<BookmarkTree items={tree} selectedIds={new Set()} onSelectMany={onSelectMany} onOpen={vi.fn()} />);
    fireEvent.click(screen.getByRole("checkbox", {name: "Select Architecture branch"}));

    expect(onSelectMany).toHaveBeenCalledWith([1, 2], true);
});
