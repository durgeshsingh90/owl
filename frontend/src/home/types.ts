export interface LinkChoice {
    label: string;
    href: string;
    selected: boolean;
}

export interface HomeBookmark {
    id: number;
    title: string;
    space: string;
    openCount: number;
    openUrl: string;
    selectUrl: string;
}

export interface HomeWorkspace {
    ok: true;
    csrfToken: string;
    statusMessage: string;
    urls: Record<string, string>;
    bookmarkMetrics: Array<{label: string; value: string; detail: string; kind: string}>;
    bookmarkActivity: {
        total: number;
        label: string;
        filters: LinkChoice[];
        years: LinkChoice[];
        monthLabels: Array<{label: string; column: number}>;
        weeks: Array<{days: Array<{
            date: string;
            ariaLabel: string;
            count: number;
            level: number;
            inYear: boolean;
        }>}>;
        trackingNote: string;
        addedCount: number;
        openedCount: number;
        refreshedCount: number;
        notesCount: number;
        mostActiveDay: string;
    };
    topViewed: Array<{
        rank: number;
        sizeLabel: string;
        lastViewedLabel: string;
        bookmark: HomeBookmark;
    }>;
    interesting: Array<{
        title: string;
        summary: string;
        emptyMessage: string;
        href: string;
        items: Array<{meta: string; bookmark: HomeBookmark}>;
    }>;
    bookmarkPeople: {
        label: string;
        written_pages: number;
        updated_pages: number;
        active_people: number;
        writers: Array<{name: string; page_count: number}>;
        updaters: Array<{name: string; page_count: number}>;
        filters: LinkChoice[];
    };
    database: {
        available: boolean;
        size_label: string;
        table_count: number;
        row_count: number;
        measured_at: string;
        detail: string;
    };
}
