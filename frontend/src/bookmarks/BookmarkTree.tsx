import {useState} from "react";

import {appHref} from "../routing";
import type {BookmarkTreeItem} from "./types";

interface TreeProps {
    items: BookmarkTreeItem[];
    selectedIds: Set<number>;
    onSelectMany: (ids: number[], selected: boolean) => void;
    onOpen: (item: BookmarkTreeItem) => void;
}

function descendantBookmarkIds(item: BookmarkTreeItem): number[] {
    return [
        ...(item.bookmark ? [item.bookmark.id] : []),
        ...item.children.flatMap(descendantBookmarkIds),
    ];
}

function TreeRow({item, selectedIds, onSelectMany, onOpen}: TreeProps & {item: BookmarkTreeItem}) {
    const [expanded, setExpanded] = useState(item.selected || item.located || item.depth < 2);
    const hasChildren = item.children.length > 0;
    const branchIds = descendantBookmarkIds(item);
    const wholeBranchSelected = branchIds.length > 0 && branchIds.every((id) => selectedIds.has(id));
    return <li>
        <div className={`bookmark-tree-row${item.selected ? " is-current" : ""}`} style={{paddingLeft: `${Math.max(0, item.depth - 1) * 15 + 8}px`}}>
            {hasChildren ? <button className="tree-toggle" aria-label={`${expanded ? "Collapse" : "Expand"} ${item.title}`} onClick={() => setExpanded((value) => !value)}>{expanded ? "⌄" : "›"}</button> : <span className="tree-spacer" />}
            {branchIds.length > 0 && <input aria-label={hasChildren ? `Select ${item.title} branch` : `Select ${item.title}`} type="checkbox" checked={wholeBranchSelected} onChange={(event) => onSelectMany(branchIds, event.target.checked)} />}
            {item.bookmark ? <a href={appHref(item.bookmark.selectUrl)} onDoubleClick={(event) => {event.preventDefault(); onOpen(item);}}><small>{item.outlineNumber}</small><span>{item.title}</span></a> : <span className="tree-folder"><small>{item.outlineNumber}</small>{item.title}</span>}
            <b>{item.openCount}</b>
            {item.bookmark && <button className="tree-open" onClick={() => onOpen(item)} aria-label={`Open ${item.title}`}>↗</button>}
        </div>
        {hasChildren && expanded && <ul>{item.children.map((child) => <TreeRow key={child.id} item={child} items={[]} selectedIds={selectedIds} onSelectMany={onSelectMany} onOpen={onOpen} />)}</ul>}
    </li>;
}

export function BookmarkTree({items, selectedIds, onSelectMany, onOpen}: TreeProps) {
    return <ul className="bookmark-tree">{items.map((item) => <TreeRow key={item.id} item={item} items={[]} selectedIds={selectedIds} onSelectMany={onSelectMany} onOpen={onOpen} />)}</ul>;
}
