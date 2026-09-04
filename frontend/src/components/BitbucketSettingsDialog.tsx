import {useEffect, useRef, useState, type FormEvent} from "react";

interface CredentialSummary {
    origin: string;
    configured: boolean;
    apiBaseUrl: string;
    username: string;
    verifySsl: boolean;
    updatedAt: string;
}

export interface BitbucketSettingsValues {
    baseUrl: string;
    username: string;
    accessToken: string;
    verifySsl: boolean;
}

interface BitbucketSettingsDialogProps {
    credentials: CredentialSummary[];
    initialOrigin: string;
    open: boolean;
    onClose: () => void;
    onSave: (values: BitbucketSettingsValues) => Promise<void>;
    onTest: (values: BitbucketSettingsValues) => Promise<string>;
}

export function BitbucketSettingsDialog({
    credentials,
    initialOrigin,
    open,
    onClose,
    onSave,
    onTest,
}: BitbucketSettingsDialogProps) {
    const dialogRef = useRef<HTMLDialogElement>(null);
    const [baseUrl, setBaseUrl] = useState("");
    const [username, setUsername] = useState("");
    const [accessToken, setAccessToken] = useState("");
    const [verifySsl, setVerifySsl] = useState(true);
    const [showToken, setShowToken] = useState(false);
    const [busyAction, setBusyAction] = useState<"save" | "test" | "">("");
    const [error, setError] = useState("");
    const [testResult, setTestResult] = useState("");

    useEffect(() => {
        const dialog = dialogRef.current;
        if (!dialog) return;
        if (open && !dialog.open) {
            const selected = credentials.find((item) => item.origin === initialOrigin)
                ?? credentials[0];
            setBaseUrl(selected?.apiBaseUrl ?? "");
            setUsername(selected?.username ?? "");
            setVerifySsl(selected?.verifySsl ?? true);
            setAccessToken("");
            setShowToken(false);
            setError("");
            setTestResult("");
            dialog.showModal();
        } else if (!open && dialog.open) {
            dialog.close();
        }
    }, [credentials, initialOrigin, open]);

    const values = (): BitbucketSettingsValues => ({
        baseUrl,
        username,
        accessToken,
        verifySsl,
    });

    const submit = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        setBusyAction("save");
        setError("");
        try {
            await onSave(values());
            setAccessToken("");
            onClose();
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "The settings could not be saved.");
        } finally {
            setBusyAction("");
        }
    };

    const test = async () => {
        setBusyAction("test");
        setError("");
        setTestResult("");
        try {
            setTestResult(await onTest(values()));
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "The connection test failed.");
        } finally {
            setBusyAction("");
        }
    };

    const changed = () => {
        setError("");
        setTestResult("");
    };
    const busy = Boolean(busyAction);

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
                        <p className="bb-eyebrow">config.ini connection</p>
                        <h2 id="bitbucket-settings-heading">Bitbucket settings</h2>
                    </div>
                    <button type="button" aria-label="Close settings" disabled={busy} onClick={onClose}>×</button>
                </div>
                <label htmlFor="bitbucket-base-url">REST API base URL</label>
                <input
                    id="bitbucket-base-url"
                    type="url"
                    required
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="https://server.example/stash/rest/api/1.0"
                    value={baseUrl}
                    onChange={(event) => {setBaseUrl(event.target.value); changed();}}
                />
                <label htmlFor="bitbucket-username">Bitbucket username</label>
                <input
                    id="bitbucket-username"
                    type="text"
                    autoComplete="username"
                    spellCheck={false}
                    placeholder="Username used with the HTTP token"
                    value={username}
                    onChange={(event) => {setUsername(event.target.value); changed();}}
                />
                <label htmlFor="bitbucket-access-token">HTTP access token</label>
                <div className="bb-secret-field">
                    <input
                        id="bitbucket-access-token"
                        type={showToken ? "text" : "password"}
                        autoComplete="new-password"
                        spellCheck={false}
                        placeholder="Blank keeps the stored token for this server"
                        value={accessToken}
                        onChange={(event) => {setAccessToken(event.target.value); changed();}}
                    />
                    <button type="button" onClick={() => setShowToken((current) => !current)}>
                        {showToken ? "Hide" : "Show"}
                    </button>
                </div>
                <label className="bb-checkbox-field" htmlFor="bitbucket-verify-ssl">
                    <input
                        id="bitbucket-verify-ssl"
                        type="checkbox"
                        checked={verifySsl}
                        onChange={(event) => {setVerifySsl(event.target.checked); changed();}}
                    />
                    Verify SSL certificates
                </label>
                <p className="bb-settings-dialog__help">
                    These match the crawler’s base_url, username, token, and verify_ssl values. Turn SSL
                    verification off only when your internal Bitbucket uses a certificate your computer cannot
                    validate. The token is encrypted locally and is always blank after reload.
                </p>
                {credentials.length > 0 && (
                    <div className="bb-configured-origins" aria-label="Configured Bitbucket servers">
                        <strong>Configured servers</strong>
                        {credentials.map((credential) => (
                            <button
                                type="button"
                                key={credential.origin}
                                onClick={() => {
                                    setBaseUrl(credential.apiBaseUrl);
                                    setUsername(credential.username);
                                    setVerifySsl(credential.verifySsl);
                                    setAccessToken("");
                                    changed();
                                }}
                            >
                                ✓ {credential.origin}
                            </button>
                        ))}
                    </div>
                )}
                {testResult && <p className="bb-test-success" role="status">{testResult}</p>}
                <p className="bb-form-error" role="alert">{error}</p>
                <div className="bb-settings-dialog__actions">
                    <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
                    <button type="button" disabled={busy || !baseUrl} onClick={() => void test()}>
                        {busyAction === "test" ? "Testing…" : "Test connection"}
                    </button>
                    <button type="submit" className="is-primary" disabled={busy || !baseUrl}>
                        {busyAction === "save" ? "Saving…" : "Save settings"}
                    </button>
                </div>
            </form>
        </dialog>
    );
}
