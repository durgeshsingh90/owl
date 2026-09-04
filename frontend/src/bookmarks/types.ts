export interface BookmarkItem {
    id: number;
    pageId: string;
    outlineNumber: string;
    title: string;
    url: string;
    sourceType: string;
    sourceLabel: string;
    spaceName: string;
    spaceKey: string;
    version: number;
    createdAt: string | null;
    updatedAt: string | null;
    savedAt: string;
    createdBy: string;
    modifiedBy: string;
    author: string;
    category: {id: number; name: string; domain: string} | null;
    manualFolder: {id: number; name: string} | null;
    pageTextSizeBytes: number;
    favorite: boolean;
    pinned: boolean;
    tags: string[];
    notes: string;
    openCount: number;
    firstOpenedAt: string | null;
    lastViewedAt: string | null;
    lastViewedVersion: number | null;
    changedSinceViewed: boolean;
    lastRefreshAttemptAt: string | null;
    lastRefreshedAt: string | null;
    availability: string;
    availabilityLabel: string;
    recency: string;
    recencyLabel: string;
    breadcrumb: Array<{title: string; url: string; isLeaf: boolean}>;
    selectUrl: string;
    openUrl: string;
    openParentUrl: string;
    organiseUrl: string;
    favoriteUrl: string;
    pinUrl: string;
    deleteUrl: string;
}

export interface BookmarkTreeItem {
    id: number;
    title: string;
    outlineNumber: string;
    depth: number;
    matches: boolean;
    selected: boolean;
    located: boolean;
    openCount: number;
    bookmark: BookmarkItem | null;
    children: BookmarkTreeItem[];
}

export interface BookmarkWorkspace {
    ok: true;
    csrfToken: string;
    statusMessage: string;
    inlineError: string;
    urls: Record<string, string>;
    search: {term: string; urlSearch: boolean; urlMatchCount: number; firstUrlMatch: string; semanticFallback: boolean};
    resultCount: number;
    totalBookmarks: number;
    counts: Record<string, number>;
    activeFilters: Array<{label: string; value: string}>;
    sortControls: Array<{key: string; label: string; href: string; active: boolean; direction: string; ariaLabel: string}>;
    refresh: {run_id: number | null; status: string; status_label: string; active: boolean; total: number; processed: number; progress: number; detail: string; last_completed_display: string};
    tree: BookmarkTreeItem[];
    manualFolders: Array<{id: number; name: string; bookmarkCount: number; openCount: number; items: BookmarkTreeItem[]}>;
    folders: Array<{id: number; name: string}>;
    flatItems: BookmarkItem[];
    selectedBookmark: BookmarkItem | null;
    similarBookmarks: BookmarkItem[];
    timeline: Array<{key: string; label: string; bookmarks: BookmarkItem[]}>;
    timelinePagination: {current: number; total: number; totalCount: number; firstItem: number; lastItem: number; previousUrl: string | null; nextUrl: string | null};
    people: Array<{name: string; pageCount: number; writtenCount: number; updatedCount: number; selected: boolean}>;
    categories: Array<{id: number; name: string; domain: string; description: string; bookmarkCount: number}>;
    tagSuggestions: string[];
}
