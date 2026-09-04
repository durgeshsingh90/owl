import {useCallback, useEffect, useState} from "react";

import {ApiError, requestJson} from "../api";
import {appHref} from "../routing";

const URLS = {
    status: "/bookmarks/notifications/",
    read: "/bookmarks/notifications/read/",
    readAll: "/bookmarks/notifications/read-all/",
} as const;

interface NotificationItem {
    id: number;
    kind: string;
    kindLabel: string;
    state: string;
    stateLabel: string;
    title: string;
    message: string;
    targetPath: string;
    occurredAt: string;
    read: boolean;
}

interface NotificationStatus {
    notifications: NotificationItem[];
    unread_count: number;
}

function csrfCookie(): string {
    const match = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith("csrftoken="));
    return match ? decodeURIComponent(match.slice("csrftoken=".length)) : "";
}

function errorText(error: unknown): string {
    if (error instanceof ApiError) return error.data.detail || error.message;
    return error instanceof Error ? error.message : "The notification action could not be completed.";
}

function formatDate(value: string): string {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "Time unavailable" : new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

export function NotificationCenter() {
    const [status, setStatus] = useState<NotificationStatus | null>(null);
    const [open, setOpen] = useState(false);
    const [message, setMessage] = useState("");

    const load = useCallback(async () => {
        const data = await requestJson<NotificationStatus>(URLS.status, "");
        setStatus({
            notifications: Array.isArray(data.notifications) ? data.notifications : [],
            unread_count: Number(data.unread_count) || 0,
        });
        setMessage("");
    }, []);

    useEffect(() => { void load().catch((error) => setMessage(errorText(error))); }, [load]);
    const markRead = async (id: number) => {
        const body = new FormData(); body.set("notification_id", String(id));
        await requestJson(URLS.read, csrfCookie(), {method: "POST", body});
        await load();
    };
    const markAllRead = async () => {
        await requestJson(URLS.readAll, csrfCookie(), {method: "POST", body: new FormData()});
        await load();
    };
    const notifications = status?.notifications ?? [];
    const unread = status?.unread_count ?? 0;
    return <>
        <div className="react-notifications">
            <button className="react-notifications__toggle" aria-label="Open notifications" aria-expanded={open} onClick={() => { setOpen((value) => !value); if (!open) void load().catch((error) => setMessage(errorText(error))); }}>♢{unread > 0 && <span>{unread > 99 ? "99+" : unread}</span>}</button>
            {open && <section className="react-notifications__panel" role="dialog" aria-modal="false" aria-labelledby="react-notifications-heading">
                <header><div><small>OWL activity</small><h2 id="react-notifications-heading">Notifications</h2></div>{unread > 0 && <button onClick={() => void markAllRead().catch((error) => setMessage(errorText(error)))}>Mark all read</button>}</header>
                {message && <p role="status" className="react-notifications__message">{message}</p>}
                <div className="react-notifications__list">{notifications.map((item) => <article className={item.read ? "" : "is-unread"} key={item.id}><span aria-hidden="true">{(item.kindLabel || "Update").slice(0, 1).toUpperCase()}</span><div><header>{item.targetPath ? <a href={appHref(item.targetPath)}>{item.title}</a> : <strong>{item.title}</strong>}<small>{item.stateLabel}</small></header>{item.message && <p>{item.message}</p>}<footer><time dateTime={item.occurredAt}>{formatDate(item.occurredAt)}</time>{!item.read && <button onClick={() => void markRead(item.id).catch((error) => setMessage(errorText(error)))}>Mark read</button>}</footer></div></article>)}</div>
                {!notifications.length && <div className="react-notifications__empty"><b>No notifications yet</b><p>Import, refresh, and repository updates will appear here.</p></div>}
            </section>}
        </div>
    </>;
}
