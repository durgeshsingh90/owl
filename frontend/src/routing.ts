export function isVitePreview(): boolean {
    return import.meta.env.DEV && window.location.pathname.startsWith("/static/");
}

export function appHref(href: string): string {
    if (!isVitePreview()) return href;
    const url = new URL(href, window.location.origin);
    const mapped = url.pathname === "/"
        ? "/static/"
        : url.pathname.startsWith("/bookmarks/")
            ? `/static${url.pathname}`
            : url.pathname.startsWith("/bitbucket/")
                ? `/static${url.pathname}`
                : href;
    if (mapped === href) return href;
    return `${mapped}${url.search}${url.hash}`;
}

export function navigateTo(href: string): void {
    window.location.assign(appHref(href));
}
