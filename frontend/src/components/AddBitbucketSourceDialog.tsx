import {useEffect, useRef, useState, type FormEvent} from "react";

export type BitbucketSourceType = "project" | "repository";

interface AddBitbucketSourceDialogProps {
    configured: boolean;
    initialType: BitbucketSourceType;
    open: boolean;
    onAdd: (sourceType: BitbucketSourceType, sourceUrl: string) => Promise<void>;
    onClose: () => void;
    onOpenSettings: () => void;
}

export function AddBitbucketSourceDialog({
    configured,
    initialType,
    open,
    onAdd,
    onClose,
    onOpenSettings,
}: AddBitbucketSourceDialogProps) {
    const dialogRef = useRef<HTMLDialogElement>(null);
    const [sourceType, setSourceType] = useState<BitbucketSourceType>(initialType);
    const [sourceUrl, setSourceUrl] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        const dialog = dialogRef.current;
        if (!dialog) return;
        if (open && !dialog.open) {
            setSourceType(initialType);
            setSourceUrl("");
            setError("");
            dialog.showModal();
        } else if (!open && dialog.open) {
            dialog.close();
        }
    }, [initialType, open]);

    const submit = async (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
            await onAdd(sourceType, sourceUrl);
            onClose();
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : "The source could not be added.");
        } finally {
            setBusy(false);
        }
    };

    return (
        <dialog
            className="bb-settings-dialog"
            ref={dialogRef}
            aria-labelledby="bitbucket-source-heading"
            onCancel={(event) => {
                event.preventDefault();
                if (!busy) onClose();
            }}
        >
            <form onSubmit={submit}>
                <div className="bb-settings-dialog__header">
                    <div>
                        <p className="bb-eyebrow">Add source</p>
                        <h2 id="bitbucket-source-heading">
                            {sourceType === "project" ? "New project" : "New repository"}
                        </h2>
                    </div>
                    <button type="button" aria-label="Close add source" disabled={busy} onClick={onClose}>×</button>
                </div>
                <div className="bb-source-type" role="group" aria-label="Source type">
                    <button
                        type="button"
                        className={sourceType === "project" ? "is-selected" : ""}
                        aria-pressed={sourceType === "project"}
                        onClick={() => {setSourceType("project"); setError("");}}
                    >
                        Project
                    </button>
                    <button
                        type="button"
                        className={sourceType === "repository" ? "is-selected" : ""}
                        aria-pressed={sourceType === "repository"}
                        onClick={() => {setSourceType("repository"); setError("");}}
                    >
                        Repository
                    </button>
                </div>
                <label htmlFor="bitbucket-source-url">
                    {sourceType === "project" ? "Project HTTPS URL" : "Repository HTTPS clone URL"}
                </label>
                <input
                    id="bitbucket-source-url"
                    type="url"
                    required
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={sourceType === "project"
                        ? "https://server.example/projects/PROJECT"
                        : "https://server.example/scm/PROJECT/repository.git"}
                    value={sourceUrl}
                    onChange={(event) => setSourceUrl(event.target.value)}
                />
                <p className="bb-settings-dialog__help">
                    {sourceType === "project"
                        ? "OWL fetches every repository in this project, lists each one immediately, then crawls them through the one-worker queue."
                        : "OWL adds only this repository and fetches PDF metadata and its VSDX count."}
                </p>
                {!configured && (
                    <p className="bb-settings-callout">
                        Configure and test the Bitbucket server first.{" "}
                        <button type="button" onClick={() => {onClose(); onOpenSettings();}}>Open settings</button>
                    </p>
                )}
                <p className="bb-form-error" role="alert">{error}</p>
                <div className="bb-settings-dialog__actions">
                    <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
                    <button type="submit" className="is-primary" disabled={busy || !configured}>
                        {busy ? "Adding…" : sourceType === "project" ? "Add project" : "Add repository"}
                    </button>
                </div>
            </form>
        </dialog>
    );
}
