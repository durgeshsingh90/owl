import type {ErrorResponse} from "./types";

export class ApiError extends Error {
    readonly data: ErrorResponse;

    constructor(message: string, data: ErrorResponse) {
        super(message);
        this.name = "ApiError";
        this.data = data;
    }
}

export async function requestJson<T>(
    requestUrl: string,
    csrfToken: string,
    options: RequestInit = {},
): Promise<T> {
    const headers = new Headers(options.headers ?? {});
    headers.set("X-Requested-With", "XMLHttpRequest");
    if (csrfToken) headers.set("X-CSRFToken", csrfToken);
    const response = await fetch(requestUrl, {
        credentials: "same-origin",
        ...options,
        headers,
    });
    const data = (await response.json().catch(() => ({}))) as T & ErrorResponse;
    if (!response.ok) {
        const fallback = response.status >= 500
            ? `The OWL server returned HTTP ${response.status}. Check the OWL terminal for the error.`
            : `The OWL server rejected the request with HTTP ${response.status}. Refresh and try again.`;
        throw new ApiError(data.message ?? data.detail ?? fallback, data);
    }
    return data;
}
