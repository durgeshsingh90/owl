# OWL master product and implementation requirements

- Work-prompt order: 001
- Version: 1.2
- Status: Approved requirements baseline
- Repository: git@github.com:durgeshsingh90/owl.git
- Local project path: /Users/durgesh/Projects/owl
- Last consolidated: 26 August 2026

## 1. Product outcome

Build **OWL — Organised Workspace Locator**, a local, single-user web application for finding and reusing technical knowledge.

OWL has two Django applications:

1. **bookmark_manager** manages Confluence bookmarks, reconstructs the real Confluence page hierarchy, and preserves personal organization such as notes, tags, favorites, pins, and usage history.
2. **bitbucket_search** synchronizes many Git/Bitbucket repositories, indexes only their PDFs, and provides fast local discovery across repository names, paths, filenames, extracted page text, and personal notes.

The final OWL shell also provides a dashboard and one global search across both sources.

The primary user outcomes are:

- Find a known Confluence page by URL, Page ID, title, metadata, tag, note, or tree location.
- Understand exactly where a bookmarked page lives in Confluence.
- See newly saved, recently changed, changed-since-viewed, inaccessible, favorite, pinned, recent, and frequently used bookmarks.
- Search approximately 50 GB of PDFs without reopening or rescanning documents at query time.
- Narrow large PDF result sets rapidly with phrase-aware keyword chips and ALL/ANY matching.
- Understand why each PDF matched and jump to the most relevant page where possible.
- Keep all credentials, internal documents, notes, databases, repository clones, and search indexes local and outside this public Git repository.

## 2. Scope and non-goals

### 2.1 Required final scope

- Local Django website using server-rendered templates.
- Shared OWL navigation and visual language.
- Confluence bookmark manager with secure in-app base URL/PAT configuration, tree, metadata, refresh, search, filters, notes, tags, favorites, pins, usage tracking, and JSON import/export.
- Git/Bitbucket repository registration, one-time clone, later incremental synchronization, multi-worker processing, PDF extraction, full-text indexing, search, preview, open/copy actions, user metadata, and usage tracking.
- Dashboard views for recent, changed, favorite, pinned, frequently used, and failed items.
- Final unified search across Confluence bookmarks and PDFs.
- Automated tests, safe configuration, useful diagnostics, and local run documentation.

### 2.2 Explicit non-goals for the initial product

- Public SaaS, multi-tenant hosting, or team collaboration.
- Editing, deleting, commenting on, or writing any data back to Confluence.
- Editing PDF contents.
- VSDX indexing or extraction. The source material is available as PDF, so only PDFs are in scope.
- OCR or image interpretation. Image-only pages may remain text-unsearchable but their filename, repository, path, metadata, and notes remain searchable.
- AI embeddings or semantic search. Start with explainable keyword and full-text search.
- Indexing historical versions of every PDF from Git history. Index the current checked-out document and retain useful Git metadata.
- A React, Vue, Angular, or other SPA frontend.
- Elasticsearch, OpenSearch, or other externally operated search infrastructure for the first local release.
- Automatic synchronization while OWL and its worker are not running.
- Monitoring files opened outside OWL.

## 3. Confirmed defaults and product decisions

These defaults remove ambiguity and allow implementation to proceed. They remain configurable where stated.

| Topic | Approved default |
|---|---|
| Runtime | Local, single user, bound to 127.0.0.1 |
| Backend | Python and a currently supported Django release, with exact dependencies pinned |
| Frontend | Django templates, HTML5, CSS3, JavaScript, Bootstrap |
| Interactive Confluence secret storage | Operating-system credential store; macOS Keychain by default |
| Confluence configuration precedence | Complete environment profile, otherwise Bookmark Manager settings plus credential store |
| Primary database | SQLite |
| Full-text search | SQLite FTS5, verified at startup |
| Confluence identity | Page ID |
| OWL bookmark number | Immutable Django BigAutoField displayed as #number; gaps are allowed |
| Recency window | Rolling 30 elapsed days, configurable |
| Default bookmark sort | Added newest, without flattening the tree |
| PDF types | PDF only, matched case-insensitively |
| PDF OCR | Disabled and out of scope initially |
| Git history mode | Recent history, approximately three years, using a shallow strategy |
| Full Git history | Optional per repository and obtainable later without recloning |
| PDF identity | Repository plus normalized relative path |
| PDF change detection | Git change data plus content hash |
| Default PDF match mode | ALL keyword chips |
| Default PDF scopes | Repository, path, filename, and extracted PDF text |
| Repository sync workers | Five, configurable |
| Confluence refresh workers | Five, configurable and rate-limit aware |
| Open All warning | Confirmation when more than ten files would open |
| Removed PDFs | Soft-retained as REMOVED to preserve local metadata |
| Routine feedback | Permanent bottom status bar and inline state, not popups |
| Destructive feedback | Confirmation is required |
| Time storage | UTC-aware timestamps; display in configured local timezone |

### 3.1 Meaning of the three-year Git option

The three-year setting limits downloaded **Git history**, not the age of files in the current branch. A PDF created eight years ago but still present in the current branch must be available and indexed.

The UI must explain this distinction. The implementation may use shallow-since, partial clone, or another safe Git strategy, but correctness of the current working tree is more important than minimizing download size.

## 4. Quality and architecture principles

1. **Local database first.** Normal page loads and searches use local data. Confluence and Git are contacted only during explicit save, refresh, clone, synchronization, or recovery actions.
2. **Tree first.** The Confluence hierarchy is the primary Bookmark Manager navigation, not a decorative side panel.
3. **Index once, search many times.** PDF parsing happens during indexing; a search never scans 50 GB of PDF files.
4. **Separate source-owned and OWL-owned data.** Refreshing source metadata never destroys personal notes, tags, favorites, pins, usage, saved dates, or saved searches.
5. **Explain matches.** A PDF result says which term matched the repository, path, filename, page text, or notes.
6. **Incremental work.** Unchanged PDFs are not re-extracted. One failed repository, page, PDF, or import record does not fail the entire run.
7. **No silent data loss.** Missing Confluence pages and removed PDFs are preserved as historical local records until explicitly deleted.
8. **Safe public repository.** Only code, redacted examples, small synthetic fixtures, and documentation may be committed.
9. **Accessible desktop productivity UI.** Keyboard access, meaningful status text, visible focus, and screen-reader semantics are requirements.
10. **Thin HTTP layer.** Views coordinate requests and responses; service modules contain external integration and business logic.
11. **Use the platform secret store.** Do not invent application-level token encryption or persist a reversible PAT in OWL-owned storage.

## 5. Recommended project structure

The implementation may refine this structure while preserving clear responsibilities:

~~~text
owl/
├── manage.py
├── README.md
├── requirements.txt or pyproject.toml
├── .env.example
├── .gitignore
├── owl/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── templates/
│   └── owl/
├── static/
│   └── owl/
├── bookmark_manager/
│   ├── migrations/
│   ├── services/
│   │   ├── confluence.py
│   │   ├── configuration.py
│   │   ├── secret_store.py
│   │   ├── hierarchy.py
│   │   ├── import_export.py
│   │   └── refresh.py
│   ├── templates/bookmark_manager/
│   ├── static/bookmark_manager/
│   ├── models.py
│   ├── forms.py
│   ├── views.py
│   ├── urls.py
│   └── tests/
├── bitbucket_search/
│   ├── migrations/
│   ├── services/
│   │   ├── git.py
│   │   ├── discovery.py
│   │   ├── extraction.py
│   │   ├── indexing.py
│   │   ├── search.py
│   │   └── opening.py
│   ├── templates/bitbucket_search/
│   ├── static/bitbucket_search/
│   ├── management/commands/
│   ├── models.py
│   ├── forms.py
│   ├── views.py
│   ├── urls.py
│   └── tests/
├── media/
│   └── bitbucket/              local runtime data; ignored by Git
└── work_prompts/
~~~

Long-running work must not remain inside an HTTP request. Use durable database job/run records plus a local worker process or another similarly simple and reliable background mechanism. Avoid adding Redis or a message broker unless evidence shows it is necessary.

## 6. Configuration and secrets

Commit a redacted environment example, never a real environment file. OWL supports two secure Confluence configuration sources:

1. **Bookmark Manager settings:** the normal interactive path. Store the non-secret base URL, authentication mode, and sanitized connection metadata locally; store the PAT only in the operating-system credential store through an injectable `SecretStore` abstraction. On macOS, the default backend is Keychain.
2. **Environment-managed configuration:** an optional read-only override for development, CI, and headless use. When both the Confluence base URL and PAT are supplied by environment, they take precedence and the settings UI labels them **Managed externally**.

Never silently fall back to SQLite, a plaintext file, `.env`, a cookie, a session, browser storage, or another reversible application-managed value when secure credential storage is unavailable. In that case, explain how to enable the credential store or use an ignored environment variable.

Expected configuration includes:

~~~text
DJANGO_SECRET_KEY=
DJANGO_DEBUG=true
OWL_TIME_ZONE=Europe/Dublin
OWL_DATA_ROOT=

# Optional environment-managed override. Leave blank for UI/Keychain setup.
CONFLUENCE_BASE_URL=
CONFLUENCE_PAT=
CONFLUENCE_AUTH_MODE=bearer
CONFLUENCE_SECRET_BACKEND=keyring
CONFLUENCE_REQUEST_TIMEOUT_SECONDS=30
CONFLUENCE_MAX_WORKERS=5

BITBUCKET_DATA_ROOT=
BITBUCKET_ALLOWED_HOSTS=
BITBUCKET_HISTORY_YEARS=3
BITBUCKET_MAX_REPO_WORKERS=5
PDF_MAX_EXTRACTION_WORKERS=

NEW_DURATION_DAYS=30
UPDATED_DURATION_DAYS=30
OPEN_ALL_CONFIRM_THRESHOLD=10
~~~

Rules:

- Environment configuration is valid only when the required base URL and PAT form one complete profile. Do not combine an environment base URL with a UI-stored PAT, or the reverse.
- A PAT entered in the loopback-only settings form may travel once in a CSRF-protected POST to the server. It is never returned in a response, pre-populated, or placed in a URL.
- The PAT and Git credentials remain server-side after submission.
- Reopening settings shows **Stored securely** and an empty **Replace PAT** field; OWL never reveals the stored value or a token fragment.
- If the canonical Confluence origin changes, require PAT entry again. Never reuse or send a credential stored for the previous origin to the new origin.
- Store only non-secret connection state such as configuration source, canonical origin, authentication mode, configured/verified timestamps, and a sanitized result code in SQLite.
- Access the credential store without putting the PAT in process arguments. Tests use an isolated fake `SecretStore` and never read or modify the user's real Keychain.
- Never store credentials inside repository URLs.
- Use the operating system SSH agent, Git credential manager, or another external credential mechanism.
- Never expose secrets through response HTML, JavaScript, browser storage, logs, diagnostics, exports, backups, screenshots, traces, test fixtures, or Git history.
- Restrict Confluence requests and redirects to the configured Confluence origin.
- Restrict Git repository URLs to approved protocols and configured hosts.
- Use TLS verification and bounded timeouts.
- Accept a canonical HTTPS base URL with an optional application context path such as `/wiki`. Reject embedded credentials, query strings, fragments, unsafe schemes, malformed ports, and local/link-local/metadata targets before any external request. Explicit synthetic localhost testing may opt into HTTP in the test profile only.
- The default data root may be media/bitbucket, but it must be configurable and excluded from Git because the corpus is approximately 50 GB.

## 7. Shared OWL shell and interaction model

### 7.1 Navigation

The shared shell exposes three top-level navigation choices:

- **Home**
- **Bookmark Manager**
- **Bitbucket Search**

**Home** is the compact landing overview. Bookmark Manager and Bitbucket Search remain the two
working tools. The OWL logo opens Home. Home must not be a large app-launcher/dashboard page;
it gives concise access to the two tools and makes the real Bookmark Manager hierarchy clear.

Each application has an app-specific left sidebar:

- **Bookmark Manager:** bookmark finding and organization functions, including
  **All bookmarks**, **Favorites**, **Pinned**, **Recently viewed**, **Frequently viewed**,
  and **Never viewed**; data actions including **Import JSON** and **Export JSON**; and
  **Confluence settings**. Its sidebar must reinforce the tree-first workflow rather than
  add a fourth competing desktop column or reduce the hierarchy to decorative
  navigation.
- **Bitbucket Search:** **Search PDFs**, **Repositories**, and
  **Index & refresh status**, plus Bitbucket-specific configuration or diagnostics.

The Bookmark Manager additionally keeps its Confluence settings gear visible in the page
toolbar because configuration is part of the first-use and authentication-recovery
journey. Moving settings into the app sidebar must not remove or replace this gear.

**Global Search is not shown in top-level navigation while it is unimplemented.** Its
reserved route remains available for development and future compatibility. When Global
Search is implemented, expose it as a shared home/utility capability rather than a third
top-level application. **System Status** is a shared utility reachable from the permanent
footer and, where useful, from an app sidebar; it is not a top-level application.

Keep these public local routes stable through the navigation redesign:

| Purpose | Route |
|---|---|
| Home | `/` |
| Future Global Search | `/search/` |
| Bookmark Manager | `/bookmarks/` |
| Bookmark Manager settings fallback | `/bookmarks/settings/` |
| Bitbucket Search | `/pdfs/` |
| Repositories | `/pdfs/repositories/` |
| Index & refresh status | `/pdfs/status/` |
| System status | `/system-status/` |

Use separately labelled navigation landmarks for Home, the two working tools, and the current
app's sidebar. The active application and active sidebar destination have explicit text
and appropriate `aria-current` state, never color alone. All navigation is keyboard
operable with visible focus. At 200% zoom and supported narrow widths, the app sidebar may
become an accessible drawer, but every destination and core action remains reachable;
opening and closing the drawer preserves context and returns focus to its trigger.

### 7.2 Permanent bottom status bar

The status bar is the primary feedback channel and includes an aria-live region.

Examples:

~~~text
Ready · Bookmarks: 1,044 · PDFs indexed: 20,013
Bookmark saved as #1044
Already saved as #327
Refreshing bookmarks… 287 / 1,044
Indexing PDFs… 4,120 / 20,013
Refresh complete · 12 changed · 3 unavailable · 2 errors
Added to starred documents
Path copied
~~~

Do not show routine success modals for save, refresh, favorite, pin, notes, tags, copy, or search.

Confirmation is reserved for destructive or potentially disruptive actions:

- Delete an OWL bookmark.
- Remove a repository and its local working copy/index.
- Clear local search history.
- Open more than the configured number of files.

### 7.3 Common states

Every major page must intentionally render:

- first-use/empty state;
- loading state;
- active progress state;
- success state;
- partial success;
- recoverable error;
- configuration/authentication error;
- no results;
- stale/unavailable data.

Normal background progress must not freeze navigation or search.

# Part A — Confluence Bookmark Manager

## 8. Bookmark Manager objectives

The Bookmark Manager must comfortably manage 1,000 to 10,000 bookmarks and allow the user to:

- configure and verify the Confluence base URL and PAT through its settings gear;
- save a URL or numeric Page ID;
- prevent Page ID duplicates;
- reconstruct the real ancestor tree;
- search and reveal the result inside that tree;
- inspect authorship, dates, version, status, and hierarchy;
- add notes, tags, favorites, and pins;
- track opens and changed-since-viewed state;
- refresh one, selected, or all bookmarks;
- import a heterogeneous legacy JSON collection;
- export a complete, versioned backup;
- retain inaccessible pages without losing historical organization.

### 8.1 Confluence settings icon and PAT workflow

The Bookmark Manager toolbar must contain a persistent configuration gear icon at the top right in empty, populated, loading, and error states. It is a real button, not decorative imagery, with a visible tooltip and dynamic accessible name such as `aria-label="Confluence settings — Not configured"`. It is reachable by keyboard.

Suggested toolbar:

~~~text
Bookmark Manager                         [Refresh All] [⚙ Confluence settings]
~~~

The gear opens an accessible right-side drawer or dedicated settings panel while preserving tree selection, expansion, search, filters, and scroll state. Focus moves to the panel heading when opened, Escape closes it, and focus returns to the invoking gear. It must not navigate away from unsaved Bookmark Manager work without warning. The panel contains:

- current connection state and configuration source;
- **Confluence base URL**;
- **Personal Access Token**, entered through a masked password field with `spellcheck="false"` and `autocomplete="new-password"`;
- **Show/Hide** for only the value currently being typed;
- **Authentication mode**, under Advanced settings and defaulted to Bearer;
- last successful verification time, when available;
- **Test Connection**, **Save**, and **Cancel**;
- **Replace PAT** when a credential already exists;
- **Remove PAT** for UI-managed credentials, separated visually as a destructive action.

Required states use an icon plus text, never color alone:

- **Not configured**;
- **Stored — not verified**;
- **Connected**;
- **Invalid credential** for a confirmed 401;
- **Access denied** for a 403;
- **Rate limited** for 429, including a safe retry time when known;
- **Unreachable** for DNS, TLS, connection, timeout, or server failure;
- **Unsupported response** for a malformed/incompatible Confluence response;
- **Credential store unavailable** when Keychain/the configured backend cannot be used;
- **Managed externally** when the complete environment profile is active.

Behavior:

1. With no configuration, Bookmark Manager shows a clear first-use card with **Open Confluence settings** while retaining the toolbar gear.
2. Opening the panel or typing makes no network request.
3. **Test Connection** uses the values currently entered, makes one explicit read-only Confluence request with bounded timeout and response size, follows redirects only within the validated canonical origin, and does not save the values.
4. The test result is sanitized and action-oriented. Do not display the upstream response body, authorization header, or token. Do not describe a token as expired unless Confluence explicitly provides that fact.
5. **Save** validates the canonical HTTPS origin, then commits non-secret settings and the Keychain secret atomically. A saved but untested configuration is **Stored — not verified**.
6. A successful test followed by an unchanged save retains **Connected** and its verification time. Changing the URL, PAT, or authentication mode clears the prior verified state.
7. Reopening the panel never returns the saved PAT to the browser. The replacement field is empty and clearly optional unless the origin changes.
8. Replacing a credential is atomic. A validation or secure-storage failure leaves the last working configuration intact and reports that the replacement was not saved.
9. Changing the canonical origin requires a new PAT in the same save. The old origin and credential remain active until the complete replacement succeeds.
10. Removing a UI-managed PAT requires confirmation, deletes the secure-store entry first, then clears connection metadata. Existing local bookmarks, notes, tree, search, and exports remain available offline; save and refresh actions explain that configuration is required.
11. Environment-managed values are never returned to the form. The relevant controls become read-only and explain that changes must be made outside OWL; the UI cannot replace or remove them.
12. If Keychain/credential-store access fails, keep the existing configuration intact and never write a plaintext fallback.

The settings form is available only within the current loopback-only single-user security boundary. Any later LAN, multi-user, or hosted mode requires authentication and HTTPS before exposing credential controls.

## 9. Bookmark identity and data ownership

### 9.1 Canonical identity

Confluence Page ID is unique, indexed, and canonical. A renamed page, moved page, legacy URL, modern URL, and raw Page ID all identify the same bookmark when the Page ID matches.

Saving an existing Page ID must select the local record instead of creating a duplicate or making an unnecessary Confluence request.

Similar titles with different Page IDs are allowed. Show a non-blocking similar-title warning and links to the related bookmarks.

### 9.2 Permanent OWL number

Every actual bookmark receives an immutable OWL number displayed as #1, #2, and so on. Use the immutable database primary key initially. Gaps after deletion are acceptable; never renumber existing bookmarks.

Hierarchy-only ancestors have no OWL number unless independently bookmarked.

### 9.3 Field ownership

Confluence-owned fields:

- title and current URL;
- space name and key;
- version;
- created and updated timestamps;
- creator, author, and last modifier;
- ancestor IDs, titles, and hierarchy.

OWL-owned fields:

- OWL number;
- saved timestamp;
- favorite and pin;
- tags and personal notes;
- usage and viewed-version history;
- saved views;
- import provenance.

Refreshing may update only source-owned fields and refresh/error metadata.

## 10. Bookmark data model

### 10.1 Bookmark

Required conceptual fields:

| Field | Purpose |
|---|---|
| id | BigAutoField and permanent displayed OWL number |
| page_id | Unique, indexed Confluence identity |
| tree_node | Link to the matching ConfluencePageNode |
| title, url | Current Confluence metadata |
| space_name, space_key | Space metadata |
| version | Current Confluence version |
| created_at, updated_at | Confluence timestamps |
| created_by_id/name | Creator |
| modified_by_id/name | Last modifier |
| author_id/name | Normalized primary author for legacy compatibility |
| saved_at | When added to OWL |
| last_refresh_attempt_at | Most recent attempt |
| last_refreshed_at | Most recent successful refresh |
| last_change_detected_at | When OWL observed a version change |
| availability_status | ACTIVE, NOT_FOUND, ACCESS_DENIED, AUTH_ERROR, or REFRESH_ERROR |
| last_error_code/message/at | Sanitized diagnostic state |
| favorite, pinned | Independent OWL flags |
| notes, notes_updated_at | Local plain-text notes |
| open_count | Opens through OWL |
| first_opened_at, last_viewed_at | Usage times |
| last_viewed_version | Version current when last opened |

Index commonly filtered and sorted fields such as page ID, saved/created/updated/refreshed/viewed dates, favorite, pin, availability, space, and author.

### 10.2 ConfluencePageNode

Required conceptual fields:

- internal ID;
- Confluence Page ID when known;
- provisional key for imported breadcrumb-only nodes;
- title, URL, and space key;
- self-referencing parent;
- optional Confluence sibling position;
- metadata update timestamp.

Rules:

- Reuse a node by Page ID.
- A node may exist without an OWL bookmark.
- Parent deletion must never cascade through a populated tree.
- Remove a node only when it has no bookmark and no children.
- Move an existing node atomically when its parent changes.
- Merge provisional imported nodes carefully after real ancestor IDs become available.

### 10.3 Supporting models

- **Tag:** unique normalized name, case-insensitive duplicate prevention, many-to-many with bookmarks.
- **SavedBookmarkView:** name, search text, filter JSON, sort, column settings, dates.
- **BookmarkRefreshRun:** scope, totals, progress, result counts, status, start/end times.
- **BookmarkRefreshItem:** bookmark, outcome, old/new version, error, attempts.
- **BookmarkImportRun:** filename, schema version, totals, progress, outcome.
- **BookmarkImportFailure:** record number, optional Page ID, sanitized reason.
- **ConfluenceConfiguration:** singleton non-secret settings containing canonical base URL/context path, authentication mode, credential source, verification status, configured/tested/verified timestamps, and sanitized last error. It contains no PAT, authorization header, upstream response body, secret-store key/reference, or token fragment.

## 11. Save and duplicate workflow

The main input accepts:

- a numeric Page ID;
- a modern path containing /spaces/{space}/pages/{page_id}/;
- a legacy URL containing viewpage.action?pageId={page_id}.

Workflow:

1. Trim and validate the input.
2. Parse the Page ID.
3. If configuration is missing, retain the entered value in the current page, make no automatic network request, and show **Open Confluence settings**.
4. Reject a URL outside the configured Confluence origin.
5. Search the local database by Page ID.
6. If it exists, expand its ancestors, scroll it into view, select it, highlight it for approximately three to five seconds, populate details, and report its OWL number.
7. If it is new, query Confluence by Page ID, normalize page and ancestor metadata, create or reuse all hierarchy nodes, create the bookmark inside one transaction, reveal it, and report the assigned OWL number.

A normal save uses inline/bottom-bar feedback, not a modal.

## 12. Hierarchy and tree behavior

The central tree is the visual focus.

- Reconstruct the real ancestor chain returned by Confluence.
- Reuse shared ancestors and avoid duplicate branches.
- Show hierarchy-only nodes with different styling from actual bookmarks.
- Update ancestor titles and URLs during refresh.
- Move a page when the Confluence parent changes and remove only genuinely orphaned old nodes.
- Preserve meaningful ancestors while filtering.
- Prefer Confluence sibling order when available; otherwise sort siblings by title.
- Do not flatten the tree to perform a global date sort. Use a separate results/list mode with breadcrumbs for flat global sorting.

Tree interactions:

- individual expand/collapse without reload;
- Expand All and Collapse All;
- multi-select actual bookmarks;
- sticky column headers;
- namespaced localStorage expansion state;
- state preserved after notes, tags, favorite, pin, open, refresh, or selection changes;
- search-driven ancestor expansion, scroll, selection, and temporary highlight;
- clickable breadcrumb segments;
- Copy Breadcrumb;
- Copy Page ID;
- Open Parent in Confluence.

Suggested desktop columns:

~~~text
# | Bookmark / Tree | Status | Created | Updated | Added | Tags
~~~

Only actual bookmarks show OWL number and bookmark metadata columns.

## 13. Dates, status, and changed-since-viewed

### 13.1 Distinct dates

Keep and label these separately:

- Created in Confluence;
- Updated in Confluence;
- Added to OWL;
- Last refresh attempted;
- Last refresh succeeded;
- Last change detected by OWL;
- First opened through OWL;
- Last viewed through OWL.

Show concise relative values such as Today, Yesterday, 8 days ago, 3 months ago, This year, or 2 years ago. Hover and keyboard focus must reveal the exact timezone-aware timestamp.

### 13.2 Independent status dimensions

Availability is stored:

- ACTIVE
- NOT_FOUND for a confirmed 404
- ACCESS_DENIED for a 403
- AUTH_ERROR for authentication/configuration failure
- REFRESH_ERROR for timeout, network, rate limit exhaustion, invalid response, or server failure

These per-bookmark availability values are separate from the global Confluence connection state shown by the settings gear. A global configuration problem does not rewrite every bookmark's last known availability.

Recency is calculated:

- NEW when saved within the configured 30-day window;
- UPDATED when active, not NEW, and updated in Confluence within the configured 30-day window;
- NORMAL otherwise.

Visual priority:

~~~text
Availability problem
NEW
UPDATED
NORMAL
~~~

Use a restrained green indicator for NEW, blue or amber for UPDATED, ordinary dark text for NORMAL, faded grey plus explicit text for NOT_FOUND, a restricted/lock indicator for ACCESS_DENIED, and a warning indicator for REFRESH_ERROR. Never rely on color alone.

### 13.3 Changed since viewed

This is separate from NEW/UPDATED. Show it when a bookmark has been opened before and the current Confluence version differs from last_viewed_version.

Example:

~~~text
Changed since you last opened it
Last viewed version: 17
Current version: 21
~~~

Never-opened bookmarks do not receive this label.

## 14. Details, personal organization, and usage

### 14.1 Details panel

Show:

- title;
- OWL number and Page ID;
- availability, recency, and changed-since-viewed state;
- space and space key;
- author, creator, and modifier;
- version;
- all relevant dates;
- complete breadcrumb;
- current URL;
- tags;
- notes;
- open count.

Actions:

- Open;
- Open Parent;
- Favorite/Unfavorite;
- Pin/Unpin;
- Refresh;
- Copy URL;
- Copy Page ID;
- Copy Breadcrumb;
- Edit Notes/Tags;
- Delete from OWL.

### 14.2 Notes

- Notes are escaped local plain text initially.
- Notes are searchable, importable, and exportable.
- Support quick notes from the tree and full editing in details.
- Notes are never sent to Confluence.
- Refresh preserves notes exactly.
- Saving uses inline/status-bar feedback.

### 14.3 Favorite versus pin

- Favorite means permanently valuable.
- Pin means keep readily accessible now.
- They are independent persistent flags.
- Provide Favorites and Pinned shortcut sections.
- Optional pinned-first sorting applies only inside the current sibling/result context and never changes the actual hierarchy.

### 14.4 Open tracking

Opening through OWL:

1. increments open_count;
2. sets first_opened_at once;
3. updates last_viewed_at;
4. stores the current version in last_viewed_version;
5. opens the validated Confluence URL in a new tab with noopener and noreferrer.

The count represents OWL opens only.

Provide Recently Viewed, Frequently Viewed, Never Viewed, Most Opened, and Recently Opened views/sorts.

## 15. Bookmark search, filters, sorting, and saved views

### 15.1 Local search

Search locally across:

- title;
- Page ID;
- stored URL;
- space name/key;
- author, creator, and modifier;
- tags;
- personal notes;
- breadcrumb/path.

Do not call Confluence for ordinary search.

Debounce general typing by roughly 250 to 400 ms. A pasted URL or exact Page ID may resolve immediately.

A URL/Page ID/title match must reveal the actual tree location by expanding ancestors, scrolling, selecting, highlighting, and loading details.

### 15.2 Filters

Combinable filters:

- favorite;
- pinned;
- one or more tags;
- author, creator, or modifier;
- space;
- availability;
- NEW, UPDATED, or NORMAL;
- changed since viewed;
- Created, Updated, Added, Refreshed, or Viewed date;
- open-count range;
- recently changed in 7 or 30 days;
- broken/inaccessible pages.

Date presets:

~~~text
Any Time
Today
Last 7 Days
Last 30 Days
Last 3 Months
Last 6 Months
This Year
Last Year
Older
Custom Range
~~~

Different filter groups combine with AND. Multiple selected tags require all selected tags by default, and the UI states that rule.

Provide active-filter chips, dynamic counts, Clear Filters, and saved views that restore search, filters, sort, and visible columns. Saved views do not restore transient selection or expansion state by default.

### 15.3 Sorting

Support:

- Added newest/oldest;
- Updated newest/oldest;
- Created newest/oldest;
- Title A–Z/Z–A;
- Author A–Z;
- Favorites first;
- Pinned first;
- Most/least opened;
- Recently/least recently opened;
- Recently refreshed.

Default tree browsing uses Added newest while preserving hierarchy. Flat sorts use a results view that retains breadcrumb context.

### 15.4 Saved bookmark timeline

Keep the Confluence hierarchy as the primary workspace, and also provide a compact saved-bookmark
timeline in the Bookmark Manager left sidebar. Group bookmarks saved during the current calendar
year under month headings, newest month first. Group older bookmarks under year headings, newest
year first. Each entry links to and reveals the real bookmark in the hierarchy. This grouping is a
navigation aid only and never rewrites parent/child relationships. Calculate calendar boundaries
in OWL's configured local timezone, omit empty periods, expose semantic headings and exact saved
dates, and retain the normal hierarchy/search/filter workspace beside the timeline. Search and
filters recalculate the visible timeline groups so unrelated bookmark names do not remain visible.
Bound the timeline's height and paginate or virtualize its entries so a 10,000-bookmark library
does not duplicate the complete tree in the DOM or push filters and saved views out of reach.

## 16. Bookmark refresh

Provide Refresh One, multi-select Refresh Selected, and a visible Refresh All icon with tooltip.

Per bookmark:

1. query the configured Confluence origin by Page ID;
2. record the attempt time;
3. retrieve normalized current page and ancestor metadata;
4. compare versions;
5. update only Confluence-owned fields;
6. rename/move tree nodes safely;
7. set last_change_detected_at for a genuine version change;
8. on success, set last_refreshed_at and ACTIVE;
9. on failure, preserve prior metadata and classify the failure;
10. preserve every OWL-owned field.

Refresh All:

- runs outside the request;
- records durable run/item state;
- reports progress through polling, server-sent events, or equivalent;
- uses configurable, conservative concurrency;
- honors Retry-After and rate limits;
- retries only transient failures with bounded backoff;
- does not retry confirmed 403/404 repeatedly in the same run;
- continues after individual failures;
- keeps search and navigation usable.

If a global authentication failure is detected, stop issuing duplicate requests and show one clear configuration error with **Open Confluence settings**. After a credential is successfully replaced or an environment profile is corrected, allow an explicit retry; a later successful refresh restores the bookmark to ACTIVE without losing history.

Example completion:

~~~text
Refresh complete · 12 changed · 1 unavailable · 2 errors · 1,029 unchanged
~~~

## 17. Bookmark import, export, and delete

### 17.1 Legacy JSON import

- Accept versioned UTF-8 JSON and heterogeneous legacy records.
- Normalize alternative field names and timestamp formats.
- Validate each record independently.
- Deduplicate by normalized Page ID.
- Import metadata, saved time, notes, tags, favorite, pin, usage, availability, and hierarchy when present.
- Avoid one Confluence request per record when usable metadata exists.
- Create provisional hierarchy nodes from breadcrumb strings when ancestor IDs are absent.
- Process in batches with progress.
- Do not abort valid records because one record is malformed.
- Re-importing the same file is idempotent.
- Existing OWL-owned values win by default; imported data may fill blanks but never silently overwrite local notes, tags, favorite, pin, or usage.
- Produce a sanitized failure report.

Assign new OWL numbers deterministically, preferably by ascending legacy saved time. A legacy number may be retained in a separate field but must not become the primary key.

### 17.2 JSON export

Export a versioned document containing:

- export schema version and generation time;
- OWL number and Page ID;
- Confluence metadata and hierarchy;
- status and dates;
- notes, tags, favorite, and pin;
- usage/view metadata;
- sanitized useful error state.

Use ISO-8601 timezone-aware timestamps. A current export must round-trip without duplicates. Never export credentials, cookies, headers, environment values, or tokens.

Treat exports as sensitive because they contain internal URLs and personal notes.

### 17.3 Delete

Delete removes the OWL bookmark only. It never deletes a Confluence page.

Require confirmation. Preserve shared ancestors and siblings. Clean a hierarchy node only when it has no bookmark and no children. Explain that the local OWL data for the bookmark will be removed.

# Part B — Bitbucket PDF Search

## 18. Bitbucket Search objective and normal flow

The second Django application is **bitbucket_search**.

Its primary goal is fast local discovery across approximately 50 GB of PDFs distributed through many Git/Bitbucket repositories.

Searchable sources:

- repository display name;
- normalized relative directory path;
- filename;
- machine-readable text extracted page by page;
- local document and page notes.

Normal flow:

~~~text
Git/Bitbucket repositories
        ↓
safe local clone or fetch
        ↓
discover only PDFs
        ↓
extract text page by page
        ↓
publish local full-text index
        ↓
near-instant local search
~~~

Normal searches never contact Git/Bitbucket and never reopen or scan the raw PDF collection.

## 19. Repository registration, identity, and storage

### 19.1 Repository input

Provide a repository management page with a multiline input accepting one SSH or HTTPS Git URL per line.

Actions:

- Add one or many repositories.
- Enable or disable a repository.
- Sync one, selected, or all repositories.
- Retry failed repositories.
- Upgrade a shallow repository to full history.
- Inspect local path, branch, status, counts, last sync, and error.

Normalize and deduplicate by canonical host/owner/repository identity, not the literal URL string. Reject or redact credential-bearing URLs before persistence.

### 19.2 Repository model

Required conceptual fields:

| Field | Purpose |
|---|---|
| id | Stable OWL repository identity |
| display_name | User-facing name |
| canonical_remote_key | Normalized duplicate key |
| remote_url | Sanitized URL without credentials |
| local_path | Managed path under the configured data root |
| default_branch | Indexed branch |
| enabled | Whether included in Sync All/search |
| history_mode | RECENT or FULL |
| shallow_since, is_shallow | History state |
| sync_state | NOT_CLONED, QUEUED, CLONING, FETCHING, READY, FAILED, DISABLED, or BLOCKED_DIRTY |
| last_sync_started/completed/successful | Audit times |
| last_synced_commit | Commit represented by local working tree/index |
| last_error_code/summary | Sanitized diagnostic state |
| created_at, updated_at | OWL dates |

Default branch only is indexed initially. Store the branch so later branch selection is possible, but do not index every branch in version 1.

### 19.3 Local storage

Use a configurable OWL data root. The logical repository area is:

~~~text
OWL data root/
├── repositories/
├── imports/
├── backups/
├── logs/
├── indexes/
└── tmp/
~~~

A repository-local media/bitbucket or var directory is an acceptable development default if completely ignored by Git. For the real 50 GB collection, document how to place OWL_DATA_ROOT on an appropriate local disk without changing code.

Repository folders use a stable internal ID plus safe slug, preventing collisions between repositories with the same short name.

Never commit clones, PDFs, extracted text, indexes, databases, logs, imports, backups, or machine-specific paths.

### 19.4 Authentication

- Prefer a read-only SSH key through the macOS SSH agent/keychain or an external Git credential manager.
- Never store passwords, PATs, private keys, cookies, or embedded URL credentials.
- Preserve SSH host-key verification.
- Permit only configured hosts and approved SSH/HTTPS transports.
- Git operations are read-only from OWL’s perspective: no commit, push, force update, pull request, or source-file edit.

## 20. Clone, synchronization, and history behavior

### 20.1 Clone once

For a repository without a valid local working copy:

1. validate the remote and destination;
2. clone into a temporary directory;
3. validate the repository, remote, branch, and working tree;
4. atomically move the completed clone into its managed destination;
5. store the local path and commit;
6. continue to discovery.

An incomplete clone never masquerades as ready.

### 20.2 Refresh existing clone

For an existing managed clone:

1. verify path containment, remote identity, clean working tree, and branch;
2. fetch;
3. advance with a safe fast-forward operation;
4. update stored commit and sync metadata;
5. never delete and reclone during normal refresh.

Do not reset, clean, force-checkout, overwrite, or delete unexpected local modifications. Mark a dirty/non-fast-forward clone BLOCKED_DIRTY or failed, preserve its previous index, explain the issue, and allow retry after repair.

### 20.3 History modes

Default history mode is approximately the last three years, using a shallow-since or similarly safe strategy.

Help text must state:

- this limits Git history, not current-file age;
- every PDF present at the selected branch HEAD remains included;
- Git cannot always transfer files only by extension;
- server capability and Git LFS may affect the optimization.

If the preferred recent-history capability is unavailable, use a documented conservative fallback and show a warning. Never silently perform a very large full-history download.

Full History:

- is selected per repository;
- requires a clear disk/time warning and confirmation;
- deepens/unshallows the existing clone;
- does not delete and re-clone.

### 20.4 PDF-only discovery

Index only regular files with a case-insensitive .pdf extension.

Repository synchronization may materialize and inventory regular case-insensitive PDF and VSDX
files so the local checkout does not need the rest of a very large source tree. VSDX extraction
and indexing remain out of scope. Ignore VSDX when building the PDF index, and ignore images,
other Office files, text/source files, .git internals, and symlinks escaping the configured
repository root.

Partial clone or sparse checkout may be used when reliably supported, but the application must
never promise that Git can always transfer only PDF/VSDX blobs. PDF index discovery remains
PDF-only regardless of other files in the checkout.

### 20.5 Git LFS

Detect Git LFS pointer files before extraction.

- Never pass an LFS pointer to the PDF parser.
- Attempt configured read-only LFS retrieval when appropriate.
- If Git LFS is missing or authentication fails, record a specific actionable error.
- Continue processing unaffected PDFs.

### 20.6 Repository removal

Default removal means Disable Repository and hide its active documents from normal results. Preserve clone, index metadata, notes, stars, collections, and usage.

Deleting local repository data is a separate destructive action. Before it occurs, resolve and display the exact local target, estimated size, affected document count, and recoverability. Require explicit confirmation and never broaden the target with an unresolved variable or path.

## 21. Persistent jobs, phases, and concurrency

Repository synchronization and PDF extraction are long-running background jobs with separate configurable limits.

Default flow:

1. validate repositories and disk access;
2. clone/fetch selected repositories concurrently;
3. let all selected syncs reach a terminal state;
4. discover new, changed, removed, and renamed PDFs for successful repositories;
5. queue extraction for new/changed PDFs;
6. extract with a bounded process pool;
7. publish successful index changes atomically through a controlled writer;
8. recalculate summary counts.

This preserves the requested global boundary: repository synchronization completes before extraction begins. Failed repositories retain their last good index while successful repositories continue.

Persist run/job state such as:

- SyncRun and RepositorySyncResult;
- IndexRun and IndexJob;
- QUEUED, RUNNING, CANCELLING, SUCCEEDED, SUCCEEDED_WITH_ERRORS, FAILED, CANCELLED, or INTERRUPTED state;
- phase, total, completed, succeeded, skipped, and failed counts;
- repository/document/page/byte progress;
- start, heartbeat, and completion time;
- retry count and sanitized error;
- cancellation request and worker lease.

Requirements:

- job retries are idempotent;
- an app/worker restart leaves interrupted work visible and retryable;
- only one synchronization per repository runs at a time;
- only controlled writers modify SQLite/FTS state;
- extraction workers return staged results rather than competing as uncontrolled SQLite writers;
- cancellation occurs at safe item boundaries;
- searches remain available against the last published index;
- the UI polls a compact progress endpoint roughly every one to two seconds; WebSockets are not required.

Example progress:

~~~text
Syncing repositories · 18 / 47
Discovering PDFs · 12,847 checked
Extracting PDFs · 31 / 126
Pages indexed · 8,417
~~~

Show current phase, per-repository state, counts, throughput, last successful refresh, errors, and an approximate ETA only when enough measurements exist.

## 22. PDF identity, lifecycle, and Git metadata

### 22.1 Canonical identity

Initial canonical identity is:

~~~text
repository + normalized relative path
~~~

The content hash and Git blob ID identify a revision, not the permanent OWL document record.

### 22.2 PDF document model

Required conceptual fields:

| Field | Purpose |
|---|---|
| id, repository | Stable OWL identity and source |
| filename, relative_path | Search/display identity |
| internal canonical path | Server-only validated path |
| file_size, content_hash, git_blob_id | Change/revision evidence |
| page_count, extracted_character_count | Extraction metadata |
| discovered_at, last_seen_at | Discovery lifecycle |
| first_indexed_at, last_indexed_at | Index dates |
| index_version, extractor_version | Rebuild compatibility |
| lifecycle_state | ACTIVE or REMOVED |
| index_state | PENDING, READY, NO_TEXT, PARTIAL, FAILED, or STALE_ERROR |
| git_added_at, git_modified_at | Available-history dates |
| git_last_commit/author | Last known source change |
| git_change_count | Count visible in available history |
| favorite | Binary star |
| open_count, first_opened_at, last_opened_at | OWL usage |
| extraction_error_code/summary | Sanitized error |
| removed_at | Soft removal time |

Supporting models:

- PDF page metadata and extracted text;
- document-level note;
- page-specific note using one-based page number;
- Collection and document membership;
- search history and saved search;
- sync/index runs and jobs.

### 22.3 Incremental discovery

Initial index calculates content hashes and extracts every readable PDF.

Later synchronization uses Git commit differences, name-status/rename data, and blob IDs to find candidates. Calculate SHA-256 for new/changed or ambiguous files as canonical verification. Do not hash all 50 GB after every no-change sync.

A refresh against the same repository commit schedules zero extractions.

Provide an explicit full reconciliation operation for detecting out-of-band filesystem changes.

### 22.4 Rename/move

When Git reports a rename or an identical content hash gives one unambiguous match, update the existing record’s path and preserve:

- star;
- collections;
- document/page notes;
- open count and dates;
- search usage metadata.

If identity is ambiguous, do not guess. Mark the old record REMOVED and create a new document.

### 22.5 Removed documents

A PDF missing from current HEAD is marked REMOVED, excluded from default search and active counts, and retained with its OWL metadata and available Git history.

Provide a Removed filter. If the same repository/path safely reappears, restore the same record. Removed page text must not participate in default search results.

### 22.6 Git freshness status

Keep lifecycle and index health separate.

Display freshness:

- REMOVED when absent from current HEAD;
- NEW when Git-added within the configured 30 days;
- UPDATED when modified/renamed within 30 days and not NEW;
- NORMAL otherwise.

Priority:

~~~text
REMOVED
NEW
UPDATED
NORMAL
~~~

Calculate expiring NEW/UPDATED state from timestamps. When history is shallow, label counts and dates as based on available history.

### 22.7 Commit, push, and pull-request attribution

OWL must keep source-control roles distinct because they can identify different people:

- Git commit author;
- Git commit committer;
- authenticated Bitbucket push actor, when Bitbucket supplies it;
- pull-request creator;
- pull-request merger for a fulfilled PR;
- pull-request closer for a declined or otherwise closed-without-merge PR.

Never infer or label the Git author as the person who pushed the change. Store stable Bitbucket
account identity when available, plus display name and avatar metadata; retain raw Git name/email
only as a fallback for an unmapped commit identity. Do not merge two people solely because their
display names match.

The indexed branch is the repository's configured/default branch and may be named `master`,
`main`, or something else; never hardcode a branch name. Git author and committer data is available
for commits reachable in the locally available branch history. Label a shallow repository as
**Available history**. The existing **Full History** operation may complete reachable Git commit
history, but it does not backfill missing push evidence or pull-request history. Display separate
coverage states for commit history, push evidence, and PR history. Apply a repository `.mailmap`
when present; otherwise do not guess that different Git names or emails belong to one person. Hide
email addresses by default.

For Bitbucket Data Center, ingest default-branch ref-change activity when the configured account
has the required repository-admin permission, and map each from/to hash range to the reported
actor. Without that permission, show push attribution as unavailable. For Bitbucket Cloud, ingest
commit and pull-request identities from REST; exact push actors are available only for captured
repository push events, so historical push identity must remain unavailable when no event was
stored. Because OWL remains loopback-only, it must not expose a public inbound webhook endpoint.
Cloud push events may be used only through a separately approved, authenticated relay or safe event
import design; until one exists, label Cloud push attribution unavailable. Repository
synchronization remains read-only.

Required conceptual supporting records:

- contributor identity and provider mapping;
- commit hash, author, committer, message, timestamps, and source link;
- observed branch update with actor, from/to hashes, timestamp, trigger, and evidence source;
- pull-request ID/title/link, creator, fulfilled-state merger, non-merge closer, merge commit, branches, and timestamps;
- commit-to-document and commit-to-pull-request relationships.

## 23. PDF extraction and atomic index publication

### 23.1 Page extraction

Extract machine-readable text page by page. Store one-based page number, normalized extracted text, character count, and page extraction state.

Requirements:

- ignore images;
- do not run OCR;
- choose a tested, permissively licensed PDF library;
- isolate parser crashes from the web process;
- bound worker count, time, file size, and memory;
- avoid loading many complete large PDFs simultaneously;
- preserve text meaning needed for safe snippets;
- verify that the content hash still matches after extraction;
- retry or reschedule if the file changed during work.

### 23.2 Failure states

Distinguish:

- encrypted/password-protected;
- invalid/corrupt;
- Git LFS pointer;
- no extractable text;
- partially extractable;
- timeout;
- disappeared during extraction;
- permission denied;
- resource limit;
- unknown parser failure.

One bad PDF never fails its repository or overall run. Its filename/path/repository remain searchable when safe.

### 23.3 Atomic replacement

For a changed PDF:

1. extract into staging records;
2. validate expected document/page relationships;
3. publish the new active page/index records transactionally;
4. preserve all OWL-owned metadata.

If the new extraction fails, retain the last good index as STALE_ERROR and clearly show which prior commit/hash the searchable text represents. Never silently present stale content as current.

### 23.4 FTS architecture

Use SQLite FTS5 behind a search service abstraction.

Conceptually:

~~~text
Canonical Django models
├── repositories and PDFs
├── pages and extraction state
├── notes, stars, collections, usage
├── searches
└── jobs

Derived FTS5 index
├── one metadata representation per PDF
└── one text representation per PDF page
~~~

Do not use wildcard LIKE searches over large page-text tables.

Provide:

- Reindex Document;
- Reindex Failed;
- Reindex Repository;
- Rebuild Search Index.

The derived FTS index is rebuildable. A full rebuild uses a staging index and switches only after validation.

## 24. Search input and keyword semantics

### 24.1 Keyword chips

Pressing Enter converts the current input into one independently removable chip:

~~~text
[ Private Link × ] [ Network × ] [ Edge × ] [ DDoS × ]
~~~

Rules:

- a chip is one concept, even when it contains spaces;
- a multiword chip is an exact normalized phrase by default;
- trim and collapse whitespace;
- match case-insensitively;
- prevent duplicate normalized chips;
- safely escape FTS control characters;
- Backspace on an empty input removes the last chip;
- adding/removing a chip refines results after a short debounce;
- intermediate typing is not search history;
- technical acronyms are not aggressively stemmed or autocorrected.

### 24.2 ALL and ANY

Default is ALL.

ALL means every chip must occur somewhere in the same document across any enabled scope. Chips may occur in different fields or on different pages.

Example valid ALL match:

~~~text
Network       repository
Edge          filename
Private Link  page 17
DDoS          page 42
~~~

ANY means at least one chip occurs.

Implement this as an intersection or union of per-chip document matches so ALL remains correct across separate page rows and metadata fields.

### 24.3 Search scopes

Provide independently selectable scopes:

- PDF content;
- Filename;
- Path;
- Repository;
- My Notes.

The four source scopes are enabled by default; notes may also be enabled by default once note indexing exists. At least one scope remains selected.

Support filename-only, path-only, repository-only, content-only, and notes-only search.

### 24.4 Search service contract

All queries pass through one service boundary similar to:

~~~text
search_documents(
    chips,
    match_mode,
    scopes,
    filters,
    sort,
    page,
    page_size
)
~~~

Views and templates do not build raw FTS expressions.

## 25. PDF filters, sorts, ranking, and results

### 25.1 Filters

Combine filter groups using AND:

- one or more repositories;
- path contains;
- filename contains;
- ACTIVE/REMOVED;
- index state or errors;
- Starred only;
- one or more collections;
- NEW/UPDATED/NORMAL/REMOVED;
- Git modified date;
- recent/frequent/never opened;
- indexed date.

Show active filters, a dynamic count, and Clear Filters.

### 25.2 Sorting and quick views

Sorts:

- Relevance;
- Starred First;
- Most/Least Opened;
- Recently/Least Recently Opened;
- Filename A–Z/Z–A;
- Recently Git Updated;
- Recently Indexed;
- Repository.

Default is Relevance when a query exists. Starred First preserves relevance inside starred and unstarred groups.

Quick views:

- Recently Opened;
- Frequently Used;
- Never Opened;
- Starred PDFs;
- New PDFs;
- Updated PDFs;
- Removed PDFs;
- Saved Searches;
- Index Errors.

### 25.3 Relevance

Use FTS/BM25 plus documented, configurable field boosts.

Required priority behavior:

1. exact filename match;
2. filename contains all chips;
3. strong filename match;
4. strong path match;
5. multiple chips on the same PDF page;
6. repository match;
7. page-text relevance/frequency;
8. personal-note match.

Star, open count, and recent opening may add only a small bounded tie-breaking boost. Usage never allows a weak text match to outrank a clearly relevant document.

### 25.4 Match explanation and snippets

Every result explains each chip:

~~~text
Edge          FILENAME
Network       PATH
Private Link  PAGE 17, 42
DDoS          PAGE 42
~~~

Show a short escaped snippet from the strongest page, preferring pages containing the most chips. Highlight terms safely, limit snippet length, collapse long page lists, and never send complete extracted document text to the browser.

### 25.5 Result layout

Use an information-dense table/list rather than large cards.

Show:

- star and filename;
- repository and relative path;
- Git freshness and index health;
- match explanation;
- best page/snippet;
- open count and last opened;
- actions.

Paginate or virtualize. Initial page size is 50.

### 25.6 People and commits rail

Place a sticky contributor rail on the right of Bitbucket results. Show names with distinct counts
for **Authored commits**, **Committed changes**, **Pushed changes**, **Opened PRs**, and
**Merged PRs**. Count a PR under **Merged PRs** only when Bitbucket reports a fulfilled/merged
state; a person who closes or declines an unmerged PR is not its merger. Missing push attribution
must be labelled **Unavailable**, not guessed.

As the result list scrolls, highlight the people associated with the most visible result. A person
may have several simultaneous role badges. Selecting a name filters or opens that person's activity
view, listing all locally known commits, branch updates, PRs, merges, affected PDF links, and safe
Bitbucket links. Keyboard focus and scroll-driven highlighting must produce the same state, and the
interaction must respect reduced-motion settings.

The active person uses `aria-current` and scroll synchronization never moves keyboard focus.
Selecting a person shows commit hash, subject, date, repository, indexed branch, change role, and
affected PDF links for every matching commit in available local history. On narrow screens the rail
becomes a named drawer or compact horizontal strip without hiding contributor filtering.

Rail counts use the complete current filtered result scope, not only the visible viewport. Count
Authored and Committed as unique `(repository, commit hash)` pairs per role, Pushed as unique
authoritatively observed branch-update events, and Opened/Merged PRs as unique `(repository, PR
ID)` pairs. Resynchronization must not double-count the same provider event; one person may still
receive separate counts when they genuinely performed several roles. The scroll highlight follows
the most visible result but does not change those scoped counts.

## 26. Preview, page navigation, open, and copy

### 26.1 Preview panel

Selecting a result shows:

- filename;
- repository and relative path;
- file size/page count;
- current commit/hash;
- last Git change;
- freshness and index state;
- matched pages and snippets;
- document and page notes;
- collections;
- usage;
- related documents.

### 26.2 Open actions

Provide:

- Open PDF;
- Open Matched Page;
- Open in default macOS PDF application;
- Reveal in Finder.

Prefer an internal browser PDF viewer or streaming route supporting reliable #page=N navigation. If a native viewer cannot jump reliably, open the PDF normally and keep the page number visible/copyable.

The server acts only on a registered document ID and its canonical contained path. Never accept an arbitrary browser-supplied filesystem path.

A successful OWL open atomically increments usage; a missing file or failed invocation does not.

### 26.3 Copy and bulk actions

Per PDF:

- Copy Absolute Path;
- Copy Relative Path;
- Copy Filename;
- Copy Repository.

Bulk:

- Copy result/selection paths, one per line;
- Copy filenames;
- Open All.

State whether bulk copy covers the selection, current page, or full filtered set and display the count before a very large copy.

Open All:

~~~text
1–10 results  open directly
11–50        require confirmation
over 50      ask the user to narrow the result set
~~~

## 27. Stars, collections, notes, and usage

### 27.1 Stars

Every PDF has an independent persistent binary star:

~~~text
☆ normal
★ starred
~~~

Toggle instantly without reload or modal, use brief status feedback, and preserve the star through sync, reindex, content change, removal, restoration, and confident rename.

Do not introduce star ratings.

### 27.2 Collections

A PDF can belong to multiple lightweight collections.

- Create, rename, and delete a collection.
- Add/remove one or many PDFs.
- Filter by one or more collections.
- Enforce case-insensitive unique names.
- Deleting a collection never deletes its PDFs or notes.

### 27.3 Notes

Support one document note plus zero or more page notes with explicit one-based page association.

- Notes are searchable.
- Note matches are explained.
- Refresh/reindex never overwrites them.
- Output is escaped/sanitized.
- Save inline without a modal.

### 27.4 Open tracking

Each PDF stores open_count, first_opened_at, and last_opened_at.

- Count only successful OWL-initiated opens.
- Start at zero.
- Use an atomic increment.
- Set first_opened_at once.
- Reindex never resets usage.
- Confident rename preserves usage.
- Finder/external opens are not monitored.

Display:

~~~text
Opened 47 times · Last opened 2 days ago
~~~

or:

~~~text
Never opened
~~~

## 28. Search history, saved searches, and related PDFs

### 28.1 Search history

Store deliberate completed searches only:

- chips;
- match mode;
- scopes;
- filters;
- sort;
- searched time;
- result count.

Provide Recent Searches, exact rerun, and Clear History. Retain the latest 200 by default. Adjacent identical searches may update the latest entry.

### 28.2 Saved/favorite searches

Save the entire reproducible state with a name, dates, last run, and last result count. One click reruns it.

Saved searches are independent of starred PDFs.

### 28.3 Related documents

Provide explainable non-AI related-document suggestions based on shared filename tokens, repository, parent path, keywords, collections, and notes. Do not add embeddings in the initial implementation.

## 29. Bitbucket Search dashboard

Show:

- repository count;
- active PDF count and total size;
- indexed, pending, no-text, error, stale, and removed counts;
- last refresh;
- current job/phase/progress;
- available disk space;
- recent/frequent/starred/new/updated shortcuts;
- failed repositories/documents with retry actions.

# Part C — Shared search, quality, delivery, and acceptance

## 30. Dashboard and unified OWL search

### 30.1 Dashboard

The OWL dashboard combines useful shortcuts without duplicating full application pages:

- recently viewed Confluence bookmarks;
- changed-since-viewed and recently updated bookmarks;
- favorite and pinned bookmarks;
- recently/frequently opened PDFs;
- starred, new, and updated PDFs;
- saved searches/views;
- repository, bookmark refresh, and index progress;
- inaccessible bookmarks and failed repository/PDF jobs;
- last successful Confluence refresh and PDF index publication.

When Confluence is not configured or has a global authentication/connection problem, show one concise dashboard notice linked to the same Bookmark Manager **Confluence settings** panel. Do not duplicate or reveal configuration values.

### 30.2 Shared query contract

Design shared search interfaces before implementing the combined UI.

A source-neutral query contains:

- terms/chips;
- ALL or ANY match mode;
- enabled scopes;
- source filter;
- status/date/usage filters;
- sort;
- page and page size.

Each application returns a common result shape:

~~~text
source_type
source_id
title
breadcrumb_or_relative_path
repository_or_space
match_reasons
matched_pages
best_snippet
textual_score
favorite
open_count
status
available_actions
~~~

### 30.3 Global Search UI

Provide:

~~~text
All Results | Confluence | PDFs
~~~

Global search:

- searches only local databases/indexes;
- searches bookmark titles, IDs, spaces, people, tags, notes, and breadcrumbs;
- searches PDF repository, path, filename, page text, and notes;
- explains why every result matched;
- escapes FTS syntax, snippets, titles, paths, and notes safely;
- records only deliberate completed searches;
- keeps history local and clearable.

Raw relevance scores from unlike sources are not directly comparable. Initially group All Results by source, show counts, and rank within each source.

Within each source, favor:

1. exact title/filename;
2. strong title/filename;
3. breadcrumb/path;
4. multiple terms together;
5. space/repository/tag;
6. body/note;
7. small capped favorite/usage/recency boosts.

## 31. Error handling and recovery

### 31.1 Confluence errors

Handle:

- empty/invalid input and Page ID parse failure;
- wrong/disallowed host;
- missing base URL/PAT;
- incomplete environment-managed profile;
- credential-store unavailable, denied, read, write, replace, or delete failure;
- 401, 403, 404, 429, timeout, DNS, TLS, connection, and 5xx;
- malformed/incomplete API response;
- database failure;
- partial refresh/import failure.

Only confirmed 404 becomes NOT_FOUND. A 403 is ACCESS_DENIED. A timeout or network/server failure is REFRESH_ERROR. Preserve last known metadata.

### 31.2 Git/repository errors

Handle:

- invalid, duplicate, credential-bearing, disallowed, or unsupported URL;
- SSH host-key/authentication/authorization failure;
- repository/default branch not found;
- timeout or incomplete clone;
- insufficient disk space/permissions;
- dirty working tree or non-fast-forward update;
- folder collision;
- unavailable shallow/partial features;
- Git or Git LFS unavailable;
- LFS object unavailable.

Preserve the prior searchable index after a sync failure.

### 31.3 PDF/index/search/open errors

Handle:

- file removed or changed during work;
- corrupt, encrypted, image-only/no-text, timeout, or parser crash;
- resource limit;
- duplicate/interrupted job;
- database/FTS write failure;
- FTS unavailable/corrupt/version mismatch;
- invalid query characters;
- no enabled scope;
- missing local file;
- failed viewer/Finder/clipboard invocation;
- oversized Open All request.

### 31.4 Recovery rules

- One failed item does not terminate a batch.
- Preserve canonical user data and the last good published index.
- Retry only transient errors with bounded backoff.
- Respect Retry-After.
- Provide Retry Failed, Retry Selected, Reindex Selected, and Rebuild Index actions as appropriate.
- Show a concise action-oriented UI error and a local diagnostic reference.
- Store detailed sanitized diagnostics server-side.
- Never expose a traceback, secret, authorization header, private document content, or credential-bearing URL.

## 32. Privacy and security requirements

Because the GitHub source repository is public while OWL data may be private, these requirements are release blockers.

### 32.1 Public-repository safety

Never commit:

- .env or machine-specific configuration;
- Confluence PATs, Git credentials, private keys, cookies, or headers;
- real internal hosts, repository URLs, bookmark exports, notes, authors, paths, search terms, or screenshots;
- real PDFs, clones, extracted text, search indexes, databases, logs, imports, backups, or temporary extraction files.

Commit only redacted examples and small synthetic fixtures.

Add automated checks for common credential patterns and accidental tracking of databases, media, index, log, private fixture, and repository data.

### 32.2 Network boundary

- Bind to 127.0.0.1 by default.
- Default ALLOWED_HOSTS to localhost/127.0.0.1.
- No analytics, telemetry, cloud search, CDN assets, or external error reporting.
- Normal search makes no external request.
- Network access occurs only for explicit Confluence Test Connection, bookmark save/refresh, or Git sync.
- No OWL login is required for loopback-only single-user mode.
- Binding to 0.0.0.0, LAN access, multi-user use, or deployment requires a separate authentication, HTTPS, and security decision.

### 32.3 Web security

- Keep Django CSRF protection.
- Use state-changing POST/PATCH/DELETE semantics, never mutation by GET.
- Submit, test, replace, and remove credentials only through CSRF-protected same-origin requests. Never place a PAT in a query string, redirect, cookie, response body, or cacheable page.
- Set sensitive settings responses to prevent browser/proxy caching and do not repopulate the PAT after form validation errors.
- Rate-limit connection tests and credential-changing actions locally to reduce accidental request loops.
- Escape untrusted API, Git, import, filename, path, snippet, and note content.
- If Markdown is introduced later, sanitize with an explicit allowlist.
- Use a restrictive content security policy compatible with the local PDF viewer.
- Prefer locally bundled Bootstrap/icons rather than public CDNs.
- Validate import type, structure, size, and record content.
- Use target blank with noopener/noreferrer for external Confluence links.

### 32.4 SSRF, command, and path safety

- Restrict Confluence requests and redirects to the configured origin.
- Reject userinfo, query strings, fragments, unsafe ports/schemes, link-local/metadata targets, and cross-origin redirects before sending an authorization header. Preserve an approved context path such as `/wiki` while comparing origins.
- Restrict Git remotes to allowed hosts/protocols.
- Invoke Git through argument arrays, never shell-concatenated user input.
- Canonicalize every repository/document path.
- Reject traversal and symlink escapes.
- Open, reveal, stream, or index only registered IDs beneath configured roots.
- Treat PDF parsers and source metadata as untrusted inputs.
- Run extraction with no network access where practical and enforce time/resource limits.

### 32.5 Data preservation

- Never automatically delete a clone, PDF, bookmark, note, collection, database, index backup, or export.
- Never reset, clean, force-update, or overwrite a dirty repository.
- Destructive operations show exact target and impact and require confirmation.

## 33. Accessibility and visual UX

### 33.1 Layout

Bookmark Manager desktop proportions:

~~~text
Filters       15–18%
Tree          57–62%
Details       23–25%
~~~

Bitbucket Search uses a compact filter/results/preview layout with the results area dominant.

Desktop is primary. On smaller screens, filters/details may become accessible drawers without losing functionality.

### 33.2 Visual language

- Use compact rows and restrained professional color.
- Tree first, metadata second, controls third.
- Avoid excessive cards, borders, badges, animation, and large buttons.
- Keep headings, indentation, connector lines, and chevrons consistent.
- Do not color entire rows heavily.
- Status always includes text/icon, never color alone.
- Support reduced motion and avoid flashing progress.
- Preserve selection and scroll position after minor updates.

### 33.3 Accessibility

Target WCAG 2.2 AA for core flows.

- Native buttons/links and logical focus order.
- Visible focus.
- Semantic tree/treegrid or an equally accessible disclosure pattern.
- Use aria-level, aria-expanded, aria-selected, meaningful headers, and roving focus where appropriate.
- Status bar uses aria-live polite.
- Every icon-only action has an accessible name and focusable tooltip.
- Exact dates appear in details as well as tooltips.
- Destructive dialogs trap/restore focus correctly.
- Background refresh does not unexpectedly move focus.
- Temporary highlight is not the sole indication of a located item.

Keyboard shortcuts:

- / focuses search;
- Up/Down moves visible rows;
- Left/Right collapses/expands or moves parent/child;
- Enter opens selected item;
- E expands/collapses selected node;
- F toggles favorite/star;
- P toggles pin where available;
- Escape closes a drawer/transient state or clears search focus.

Shortcuts never fire while typing in an input, textarea, select, or editable element.

## 34. Backup, restore, import, and migrations

Distinguish:

- **Import:** safe merge into the current database.
- **Restore:** explicit canonical database replacement after confirmation and automatic backup.
- **Reindex:** rebuild derived search data without altering canonical user metadata.

Use SQLite’s supported online backup mechanism rather than copying a live database unsafely.

Create a backup before:

- a restore;
- a potentially transformative data migration;
- a large JSON import;
- any operation explicitly documented as affecting canonical records.

Backup/export files:

- are timestamped and schema-versioned;
- use ISO-8601 UTC timestamps;
- preserve bookmark numbers, notes, tags, stars, pins, collections, usage, saved searches, and source configuration without credentials;
- may preserve the Confluence base URL and authentication mode, but never a PAT, secure-store key/reference, secret-presence flag, or stale **Connected** state;
- require credential entry and verification again after restore;
- include an integrity checksum and operation summary;
- exclude rebuildable PDF binaries/clones and derived FTS data by default.

Do not silently delete backups without a configured retention policy.

Never edit an already applied Django migration. Require migration-drift checks, fresh-install migrations, upgrade-path tests, and a post-migration smoke test.

## 35. Local observability and system status

Keep observability local and redacted.

Provide size-rotated logs beneath the configured data root. Include internal job/repository/bookmark/document IDs for correlation, but do not log tokens, headers, extracted document content, personal notes, or credential-bearing URLs at normal levels.

Provide a System Status page showing:

- canonical database writable;
- FTS5 available and index health/version;
- worker heartbeat;
- last successful bookmark refresh and PDF index publication;
- queued/running/failed/interrupted jobs;
- repository/data root and available disk space;
- configured external origins without secrets;
- extractor version;
- configuration source, completeness, sanitized connection state, and last verification time without retrieving or printing the PAT.

Provide a diagnostic management command that reports whether required configuration exists without printing secret values.

## 36. Performance and reliability targets

Design and test for:

- 10,000 Confluence bookmarks;
- 20,000 to 25,000 PDFs;
- approximately 50 GB of source PDFs;
- large multi-page PDFs;
- dozens of repositories.

Targets on a documented representative Mac:

| Interaction | Target |
|---|---|
| App shell/dashboard | visible within 2 seconds |
| Warm bookmark search/filter | p95 under 500 ms |
| Warm PDF search, first 50 results | p95 under 500 ms |
| Cold search | within 2 seconds |
| Chip/filter refinement | visible update within 750 ms |
| Expand/collapse | visually immediate, target under 100 ms |
| Favorite/pin/note/open feedback | target under 250 ms |
| Long-job progress | first progress within 2 seconds |

Additional requirements:

- paginate PDF results at 50 by default;
- use lazy/visible-branch rendering for very large trees;
- avoid N+1 queries with select_related/prefetch_related/bulk work;
- importing 1,000 metadata-complete bookmarks does not cause 1,000 Confluence calls;
- search remains available during refresh/index work;
- no raw PDF opens during search;
- a same-commit sync queues zero extraction jobs;
- unchanged documents are not re-extracted;
- extraction memory/concurrency remain bounded;
- run disk-space preflight before clone, full history, or rebuild;
- report measured pages/documents per minute and ETA rather than promise a fixed initial-index duration.

If a target is not met, provide a reproducible benchmark and profile evidence before changing the architecture.

## 37. Test and verification requirements

### 37.1 Unit tests

Cover:

- configuration-source precedence and incomplete environment profiles;
- base-URL canonicalization and HTTPS/origin validation;
- `SecretStore` success, unavailable, denied, and atomic replace/remove behavior;
- origin changes requiring a new PAT and never reusing the old-origin credential;
- sanitization of connection-test outcomes and secret-bearing form errors;
- supported Page ID/URL extraction;
- Confluence origin allowlisting;
- Page ID duplicate prevention;
- permanent OWL number behavior;
- hierarchy reuse, moves, and orphan cleanup;
- status priority and exact 30-day boundaries;
- changed-since-viewed logic;
- refresh ownership preservation;
- import normalization/idempotency and export round-trip;
- Git URL normalization, credential rejection, and host allowlisting;
- clone/recent/full command construction;
- dirty/non-fast-forward repository safety;
- PDF-only discovery and LFS pointer detection;
- new/changed/removed/rename detection;
- hash/blob behavior;
- extraction state and one-based page numbering;
- FTS query escaping and chip normalization;
- ALL across separate fields/pages and ANY behavior;
- exact phrase behavior;
- filters and ranking precedence;
- snippet/match explanation;
- atomic open-count increment;
- preservation of notes/stars/collections/usage;
- path traversal/arbitrary-file rejection.

### 37.2 Integration tests

Use:

- an isolated in-memory/fake `SecretStore`; automated tests never access the real operating-system credential store;
- settings and connection-test requests that assert a submitted fake PAT is absent from responses, database rows, logs, exports, backups, screenshots/traces, and Git-tracked output;
- mocked Confluence responses for 200, 401, 403, 404, 429, timeout, malformed response, and recovery;
- temporary local Git repositories for first clone, fetch, no-change, add/change/remove, rename, shallow/full, dirty tree, and partial failure;
- small synthetic readable, no-text, corrupt, and encrypted PDFs;
- real SQLite FTS5 queries;
- interrupted/retried jobs and staging-index publication;
- backup/restore and clean migration paths.

### 37.3 Browser and accessibility tests

Verify:

- first-use setup through the Bookmark Manager gear, including accessible drawer focus and return;
- masked PAT entry, test-without-save, save, restart, replace, remove, and environment-managed states;
- invalid credential, access denied, unreachable, and secure-store-failure recovery without revealing or losing secrets;
- save new and reveal duplicate;
- tree expansion, navigation, state persistence, search reveal, and sticky headers;
- notes/tags/favorite/pin without full reload;
- refresh/index progress and partial failures;
- chip creation/removal, scopes, ALL/ANY, match explanations, and preview;
- open/copy thresholds;
- status-bar announcements;
- keyboard-only core workflows;
- focus behavior, labels, contrast, and core automated accessibility checks.

### 37.4 Public CI and release checks

Public CI uses synthetic data only. Live Confluence/Bitbucket tests are opt-in local tests behind an explicit environment flag.

Before release:

- formatting and linting;
- full automated tests;
- Django system checks;
- migration-drift check;
- fresh-install migration and smoke test;
- upgrade migration test;
- secret/internal-data scan;
- tracked-file safety check;
- rendered-response, browser-storage, database, log, export, backup, screenshot, and trace checks for PAT non-disclosure;
- manual desktop render and interaction review;
- representative performance smoke test;
- backup/restore validation;
- run-instructions verification on a clean environment.

## 38. Phased delivery plan

All phases are required for the complete product; phases define safe order.

### Phase 1 — Foundation and public-repository safety

- Django scaffold and pinned dependencies;
- local data directory/configuration;
- injectable secure credential-store abstraction and fake test backend;
- core settings, shared shell, status bar, and system status;
- .env.example and comprehensive ignore/safety checks;
- test, lint, CI, and run-documentation baseline.

Definition of done: clean setup starts locally, system checks pass, synthetic test baseline passes, and no runtime/private data can be tracked accidentally.

### Phase 2 — Bookmark Manager core

- Confluence adapter, persistent settings gear, secure PAT setup, connection testing, and configuration validation;
- save URL/Page ID;
- Page ID deduplication;
- metadata ownership;
- permanent OWL number;
- basic list/details/open.

### Phase 3 — Bookmark tree and productivity

- persisted hierarchy and reveal-in-tree search;
- dates/status;
- filters/sorts/saved views;
- notes, tags, favorites, pins, usage;
- JSON import/export.

### Phase 4 — Bookmark refresh and dashboards

- durable jobs;
- refresh one/selected/all;
- progress, retries, rate limits, availability recovery;
- changed-since-viewed and recent/frequent/broken dashboards.

### Phase 5 — Repository synchronization

- repository registration and safe authentication boundary;
- clone once/fetch later;
- recent/full history;
- concurrency, progress, Git metadata, PDF-only discovery, Git LFS states.

### Phase 6 — PDF extraction and search

- page extraction;
- FTS5 and staging publication;
- incremental add/change/remove/rename;
- chip search, ALL/ANY, scopes, filters, ranking, explanations, snippets.

### Phase 7 — PDF productivity

- preview/internal viewer and page navigation;
- open/reveal/copy/bulk safety;
- stars, collections, notes, usage analytics;
- history, saved searches, related documents.

### Phase 8 — Global search and hardening

- shared adapters/result contract and combined UI;
- accessibility completion;
- backup/restore;
- performance tuning;
- recovery/diagnostics;
- operational and release documentation.

Each phase must be usable, tested, documented, and visually checked before the next phase is declared complete. Architecture for later phases may be introduced early, but unfinished placeholders must not be presented as working.

## 39. Definition of done

A feature or phase is done only when:

- its acceptance behavior is implemented without hidden placeholders;
- canonical data ownership is preserved;
- success, empty, loading, cancellation, error, and partial-failure states work;
- database migrations and relevant automated tests pass;
- accessibility requirements are verified;
- no migration drift exists;
- a clean environment can follow the documented setup;
- no secret or private/runtime data is tracked;
- credential flows use the real platform store only in manual local use and the fake isolated `SecretStore` in automated tests;
- submitted fake PAT checks prove non-disclosure across every forbidden surface;
- backup/restore impact is handled where canonical data changes;
- performance is checked proportionally;
- user documentation is current;
- the handoff lists changes, migrations, commands/results, manual checks, known limitations, and next phase;
- nothing is pushed, deployed, exposed beyond loopback, or destructively altered without explicit authorization.

## 40. End-to-end acceptance scenarios

These scenarios are the final product checklist.

### 40.1 Bookmark Manager

1. Saving a valid new Page ID creates exactly one bookmark, reconstructs its ancestors, reveals it in the tree, and reports its permanent OWL number.
2. Saving a different URL for the same Page ID creates no duplicate and reveals the existing row without an unnecessary Confluence request.
3. Modern URL, legacy URL, and raw numeric Page ID forms resolve consistently.
4. A URL outside the configured Confluence origin is rejected safely.
5. Two different Page IDs with similar titles are allowed and produce a non-blocking similarity warning.
6. Shared ancestor Page IDs create one shared branch.
7. Hierarchy-only ancestors appear without OWL bookmark numbers.
8. A renamed page updates title/URL while preserving number, saved date, notes, tags, favorite, pin, and usage.
9. A moved page relocates the existing node without leaving a duplicate stale branch.
10. A confirmed 404 retains and fades the bookmark as NOT_FOUND.
11. A 403 is ACCESS_DENIED, not deleted.
12. Timeout, 429 exhaustion, malformed response, or 5xx preserves metadata and records REFRESH_ERROR.
13. A later successful response restores an unavailable/error bookmark to ACTIVE without losing OWL data.
14. NEW lasts for the configured duration based on saved_at.
15. UPDATED is based on Confluence updated_at, yields visually to NEW, and expires automatically.
16. Every relative date exposes an exact accessible timestamp.
17. Searching a URL, Page ID, or title expands ancestors, scrolls, selects, highlights, and loads details.
18. Filters combine correctly while preserving understandable ancestor context.
19. Flat date/usage sorting does not corrupt the stored tree.
20. Notes are searchable, escaped, import/export capable, and survive every refresh.
21. Favorite and pin are independent, update without a full reload/modal, and survive restart/refresh.
22. Opening through OWL increments usage atomically and records the viewed Confluence version.
23. A later version change displays Changed Since Viewed; a never-opened bookmark does not.
24. Refresh Selected touches only selected bookmarks.
25. Refresh All reports progress, respects rate limits, and continues after individual failures.
26. A global authentication error stops repeated calls and presents one configuration problem.
27. Importing a heterogeneous legacy JSON file continues after malformed records and reports record-level failures.
28. Importing the same file twice does not duplicate bookmarks.
29. Existing non-empty OWL notes and personal state are not silently overwritten by import.
30. Export and re-import preserve supported canonical bookmark data without exporting credentials.
31. Delete removes only the OWL bookmark and does not remove the Confluence page, shared ancestors, or siblings.
32. Core tree, search, details, note, favorite, pin, and open workflows are keyboard and screen-reader usable.

### 40.2 Repository synchronization and PDF indexing

33. Multiple valid repository URLs can be added together, deduplicated, and synchronized concurrently.
34. First sync clones once into the configured root and validates the result before marking it ready.
35. Second sync fetches/fast-forwards the existing clone without deleting/recloning.
36. A dirty or non-fast-forward clone is not overwritten and its prior search index remains usable.
37. Recent history is the default and the UI explains that every current HEAD PDF remains included regardless of age.
38. Full History requires confirmation and deepens the existing clone.
39. Default branch only is indexed.
40. Only PDFs enter the document index; VSDX, images, Office/source files, and escaping symlinks do not.
41. A Git LFS pointer is detected and never sent to the PDF parser.
42. Initial indexing extracts every readable PDF page with correct one-based page numbers.
43. Image-only/no-text, corrupt, encrypted, and parser-failure PDFs produce distinct recoverable states without stopping the run.
44. A repeated sync at the same commit queues zero extraction jobs.
45. Adding, changing, and removing a PDF causes only those incremental index changes.
46. A changed PDF atomically replaces old searchable terms after successful extraction.
47. If changed extraction fails, the last good index remains searchable and is visibly marked stale.
48. A confident Git rename preserves star, collections, notes, and usage.
49. An ambiguous rename is not guessed; the old record is removed and the new path becomes a new record.
50. A removed PDF is excluded by default, visible through the Removed filter, and retains OWL metadata.
51. A safely reappearing document restores the existing record.
52. Interrupted jobs are visible and retryable without duplicate pages/index rows.
53. Search remains usable against the published index throughout sync/extraction.
54. Full index rebuild uses staging and switches only after validation.

### 40.3 PDF search and productivity

55. Pressing Enter creates one phrase-aware chip; Private Link remains one chip.
56. Chips normalize case/space safely, reject duplicates, and escape FTS syntax.
57. In ALL mode, a document matches when different chips occur in repository, path, filename, and separate pages.
58. In ANY mode, one matching chip is sufficient.
59. Repository-only, path-only, filename-only, content-only, and notes-only scopes work.
60. Repository/path/status/star/collection/date/usage filters combine correctly.
61. Exact filename matches rank above path-only and body-only matches.
62. Same-page multi-chip matches receive a boost over equivalent scattered page matches.
63. Usage/favorite boosts never overpower a clearly stronger textual match.
64. Each result states which fields/pages/notes matched each chip.
65. Each result provides a safe limited snippet from the strongest page.
66. Selecting a result shows the complete preview metadata and strongest matched pages.
67. Open Matched Page opens the internal viewer at the correct one-based page or uses the documented fallback.
68. Successful OWL opens increment usage; failed opens and external Finder opens do not.
69. Most Opened, Recently Opened, Never Opened, Starred, New, Updated, Removed, and Index Error views return correct documents.
70. PDF stars update instantly and persist through restart, sync, reindex, content change, and confident rename.
71. A PDF can belong to multiple collections; deleting a collection never deletes a PDF/note.
72. Document/page notes are searchable and survive every repository operation.
73. Search history records deliberate completed searches only.
74. A saved search restores chips, mode, scopes, filters, and sort exactly.
75. Related Documents uses explainable non-AI signals.
76. Copy paths produces one path per line with an explicit selection/page/full-set scope.
77. Open All follows the configured safety thresholds and never accepts arbitrary filesystem paths.

### 40.4 Shared, security, and performance

78. Global Search returns locally sourced Confluence and PDF groups with counts and match explanations.
79. Dashboard counts agree with canonical database/index state.
80. Long-running jobs show phase/progress within two seconds and do not freeze normal navigation/search.
81. Backup/restore round trip preserves canonical OWL data and does not require PDF/index backup.
82. System Status accurately reports database, FTS, worker, job, disk, and index state without secrets.
83. PATs, Git credentials, internal URLs/data, PDFs, clones, databases, indexes, logs, backups, and exports are not tracked by Git.
84. Confluence SSRF, Git command injection, path traversal, symlink escape, arbitrary file serve/open, CSRF, and output-XSS tests pass.
85. Default runtime is reachable only through loopback.
86. Public CI uses only synthetic fixtures and never contacts internal systems.
87. The representative corpus meets or has a documented evidence-based exception to the performance targets.
88. A clean environment can follow README setup, migrate, run checks/tests, start the worker/server, and complete the first-use synthetic workflow.

### 40.5 Confluence configuration

89. A keyboard-focusable gear with the accessible name **Confluence settings** remains visible in the Bookmark Manager toolbar in empty, populated, loading, and error states.
90. With no Confluence profile, first use explains what is missing and opens the same settings panel without blocking local/offline bookmark access.
91. A user can enter a valid canonical HTTPS base URL and masked PAT, test them through one explicit read-only request, save them, and see **Connected** without editing an environment file.
92. Reopening settings after save shows **Stored securely** with an empty replacement field; the saved PAT is never returned to HTML, JavaScript, browser-readable state, or an API response.
93. Connection testing distinguishes success, confirmed 401, 403, and connectivity/server failure with sanitized **Connected**, **Invalid credential**, **Access denied**, or **Unreachable** states.
94. A UI-managed profile survives an OWL restart through non-secret local settings plus the operating-system credential store, while its PAT remains absent from SQLite.
95. Changing the canonical Confluence origin requires a new PAT and never sends the prior origin's credential to the new origin.
96. Cancelling, invalid input, a failed connection test, or secure-store failure does not overwrite the last working profile; a successful replacement is atomic.
97. Removing a UI-managed PAT requires confirmation, removes it from secure storage, disables network-dependent bookmark actions clearly, and preserves all local bookmark data.
98. A complete environment-managed profile takes precedence, is labeled **Managed externally**, and cannot be viewed, replaced, or removed through the UI; an incomplete environment profile is rejected clearly.
99. If Keychain or the configured secure store is unavailable, OWL reports an actionable error and never falls back to plaintext application storage.
100. Submitted PAT values are absent from URLs, redirects, responses, logs, diagnostics, exports, backups, screenshots, traces, test reports, process arguments, and Git-tracked files.

### 40.6 Bookmark timeline and source attribution

101. The bounded bookmark timeline groups current-calendar-year entries by local month and older entries by year using Added to OWL, newest first, with pagination/virtualization suitable for 10,000 bookmarks.
102. Timeline links reveal the canonical hierarchy item, boundaries use configured local time, accessible headings/dates are present, and no stored tree relationship changes.
103. Git author and committer are extracted and displayed separately from the configured/default branch, including a branch named `master`, and neither is presented as the pusher.
104. The right contributor rail shows distinct identities and accurate counts for the current PDF result/change set.
105. Scrolling or keyboard navigation highlights the associated contributor with `aria-current` without stealing focus; selecting a contributor applies the matching activity view.
106. Contributor activity lists every matching available-history commit with hash, subject, date, repository, branch, role, and affected PDF links.
107. Commit-history, push-evidence, and PR-history coverage are labelled separately; Full History expands reachable Git commits without duplicates but never claims to backfill missing push or PR evidence.
108. PR author, fulfilled-state merger, non-merge closer, and push actor appear only from authoritative Bitbucket metadata; unavailable attribution is stated and never inferred.

## 41. Deployment inputs still required from the user

These do not block scaffold, architecture, UI, service contracts, or mocked automated tests. They are required only before live integration:

1. The real Confluence base URL and a valid PAT, supplied through the Bookmark Manager settings gear and operating-system credential store, or through a complete ignored environment-managed profile.
2. Confirmation of the Confluence deployment/API behavior if it differs from the default Bearer REST adapter.
3. A sanitized sample of the legacy JSON shape, or a local uncommitted real file for adapter testing.
4. The permitted Git/Bitbucket hostnames and repository URLs.
5. The local read-only Git authentication mechanism.
6. The preferred physical OWL data root if the default location does not have enough disk space.
7. A representative local PDF corpus for performance measurement; none of its content belongs in public CI or Git.

OWL must provide configuration diagnostics that identify which of these inputs are absent without printing secret values.

## 42. Terminology

- **OWL number:** immutable local number displayed as #1044.
- **Page ID:** stable Confluence page identity.
- **Hierarchy-only node:** Confluence ancestor shown in the tree but not itself saved as an OWL bookmark.
- **Confluence-owned field:** source metadata that refresh may update.
- **OWL-owned field:** local personal data refresh must preserve.
- **Availability:** ACTIVE, NOT_FOUND, ACCESS_DENIED, AUTH_ERROR, or REFRESH_ERROR.
- **Recency:** calculated NEW, UPDATED, or NORMAL state.
- **Changed since viewed:** current Confluence version differs from the version last opened through OWL.
- **SecretStore:** injectable server-side interface for storing/retrieving a PAT in the operating-system credential store; tests use a fake isolated implementation.
- **Environment-managed profile:** complete Confluence base URL and PAT supplied outside OWL; it takes precedence and is read-only in the UI.
- **Repository sync:** safe clone/fetch/fast-forward of a managed read-only working copy.
- **Document identity:** repository plus normalized relative path, with safe rename preservation.
- **Revision:** content hash/Git blob and associated extracted/indexed version.
- **Published index:** last validated FTS state available to searches.
- **STALE_ERROR:** source changed but new extraction failed, so the prior published text is retained and labeled.
- **Chip:** one phrase-aware search concept.
- **ALL:** every chip must match somewhere in the same record/document across enabled scopes.
- **ANY:** at least one chip must match.
