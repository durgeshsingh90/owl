import {useEffect, useRef, useState, type FormEvent} from "react";

interface CredentialSummary {
    origin: string;
    configured: boolean;
    username: string;
    updatedAt: string;
}

interface BitbucketSettingsDialogProps {
    credentials: CredentialSummary[];
    initialRepositoryUrl: string;
    open: boolean;
    onClose: () => void;
    onSave: (repositoryUrl: string, username: string, accessToken: string) => Promise<void>;
}

export function BitbucketSettingsDialog({
    credentials,
    initialRepositoryUrl,
    open,
    onClose,
    onSave,
}: BitbucketSettingsDialogProps) {
    const dialogRef = useRef<HTMLDialogElement>(null);
    const [repositoryUrl, setRepositoryUrl] = useState("");
    const [username, setUsername] = useState("");
    const [accessToken, setAccessToken] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        const dialog = dialogRef.current;
        if (!dialog) return;
        if (open && !dialog.open) {
            setRepositoryUrl(initialRepositoryUrl);
            let origin = "";
            try {
                origin = new URL(initialRepositoryUrl).origin;
            } catch {
                // A new repository has no URL to match yet.
            }
            setUsername(credentials.find((item) => item.origin === origin)?.username ?? "");
            setAccessToken("");
            setError("");
            dialog.showModal();
        } else if (!open && dialog.open) {
            dialog.close();
        }
    }, [credentials, initialRepositoryUrl, open]);

    const submit = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
            await onSave(repositoryUrl, username, accessToken);
            setAccessToken("");
            onClose();
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "The settings could not be saved.");
        } finally {
            setBusy(false);
        }
    };

    return (
        <dialog
            className="bb-settings-dialog"
            ref={dialogRef}
            aria-labelledby="bitbucket-settings-heading"
            onCancel={(event) => {
                event.preventDefault();
                if (!busy) onClose();
            }}
        >
            <form onSubmit={submit}>
                <div className="bb-settings-dialog__header">
                    <div>
                        <p className="bb-eyebrow">Read-only API access</p>
                        <h2 id="bitbucket-settings-heading">Bitbucket settings</h2>
                    </div>
                    <button type="button" aria-label="Close settings" disabled={busy} onClick={onClose}>×</button>
                </div>
                <label htmlFor="bitbucket-repository-url">Repository HTTPS URL</label>
                <input
                    id="bitbucket-repository-url"
                    type="url"
                    required
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="https://server.example/scm/PROJECT/repository.git"
                    value={repositoryUrl}
                    onChange={(event) => setRepositoryUrl(event.target.value)}
                />
                <label htmlFor="bitbucket-username">Bitbucket username</label>
                <input
                    id="bitbucket-username"
                    type="text"
                    autoComplete="username"
                    spellCheck={false}
                    placeholder="Optional; enter for username + token authentication"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                />
                <label htmlFor="bitbucket-access-token">HTTP access token</label>
                <input
                    id="bitbucket-access-token"
                    type="password"
                    autoComplete="new-password"
                    spellCheck={false}
                    placeholder="Required for a new server; blank reuses its saved token"
                    value={accessToken}
                    onChange={(event) => setAccessToken(event.target.value)}
                />
                <p className="bb-settings-dialog__help">
                    Use a Bitbucket Data Center token with repository-read permission. With a username it uses HTTP
                    Basic authentication; without one it uses Bearer authentication. Tokens are encrypted locally,
                    sent only to the exact HTTPS server, and never shown again.
                </p>
                {credentials.length > 0 && (
                    <div className="bb-configured-origins" aria-label="Configured Bitbucket servers">
                        <strong>Configured servers</strong>
                        {credentials.map((credential) => (
                            <span key={credential.origin}>✓ {credential.origin}</span>
                        ))}
                    </div>
                )}
                <p className="bb-form-error" role="alert">{error}</p>
                <div className="bb-settings-dialog__actions">
                    <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
                    <button type="submit" className="is-primary" disabled={busy}>
                        {busy ? "Saving…" : "Save and fetch metadata"}
                    </button>
                </div>
            </form>
        </dialog>
    );
}
