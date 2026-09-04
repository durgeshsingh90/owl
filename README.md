# OWL

OWL (Organised Workspace Locator) is a private, local knowledge workspace with a homepage and
two app areas:

- **Bookmark Manager** manages ordinary web bookmarks and locally stored Confluence pages;
- **Bitbucket** is a clone-free Bitbucket Data Center document catalogue for PDF metadata, saved
  PDF text, and VSDX counts read through the REST API with an HTTPS access token.

OWL runs on your own computer and listens only on `127.0.0.1`. The repository is public, but
your credentials, databases, saved PDF contents, indexes, and logs must remain local.

The shared React + TypeScript client lives in `frontend/` and the Django application lives in
`backend/`. Home, Bookmark Manager, Bookmark Manager Settings, and the independent Bitbucket
document desk are React screens built by Vite. Django owns data, validation, CSRF, scheduled work, and remote metadata access; its
templates are minimal mount documents. Run every Python
and Django command in this guide from `backend/` unless a command says otherwise.

## What the OWL apps do

### Home

Home is the launcher and local analytics dashboard for the workspace. Alongside the two app cards
and light/dark theme control, it shows bookmark totals, indexed searchable-text size, OWL open
counts, refresh issues, a Top 10 most-viewed table, and useful recent/unopened page lists. Its
GitHub-style yearly calendar can be filtered by pages added, opened, refreshed, and note edits.
Historical saved dates are complete; detailed daily opens, refreshes, and note edits are counted
from the analytics migration onward because older versions stored only aggregate open counts.
Home also shows a cached approximation of the local database size, table count, total stored table
entries, and the exact date and time of that measurement. Home reads OWL's local database and does
not contact an external service by itself.

**Bookmark Manager activity** ranks the people with the most pages written and latest page
updates, with Today, last 7 days, last month, and last year filters. Today starts at local midnight;
month and year are rolling calendar periods. These filters stay independent of the Bitbucket
period and bookmark activity calendar. Rankings use saved Confluence pages only, based on source
creation/update timestamps—not when OWL saved or refreshed a bookmark. Writers include the saved
creator/author, deduplicated per person and page; editor rankings use each page's latest stored
modifier. A page with multiple writers can credit multiple people while counting once in the
page total. Display names are normalized for grouping, matching the name-based People panel.

These are page counts, not a complete historical edit log: a later refresh can replace a page's
latest editor. Initial creation alone does not count as an update; OWL requires a later source
update date or a version greater than one. Missing people/date metadata is reported and excluded
from attribution. Ordinary web bookmarks are excluded because OWL has no reliable writer/editor
metadata for them. This section needs no new migration or source requests; existing metadata is
available immediately and is updated by the normal bookmark refresh.

### Bookmark Manager — working

Bookmark Manager is the main implemented app. It can:

- save any complete HTTP or HTTPS URL once and group ordinary web bookmarks automatically by
  domain; domain categories can be renamed without changing their stable identity;
- connect read-only to one trusted Confluence Data Center origin using a Personal Access Token;
- identify Confluence pages by their stable numeric Page ID across modern, legacy, renamed, and
  fragment-bearing URLs;
- save exactly the selected Confluence page, its root-to-page hierarchy, its title and people
  metadata, and the selected page's searchable text. Ancestor nodes build the tree but their body
  text is not stored;
- display the hierarchy as a numbered outline such as `1`, `1.1`, and `1.1.1`, while every saved
  bookmark also keeps a permanent OWL database ID;
- search locally as you type across saved titles, Page IDs, URLs, notes, people, breadcrumbs,
  tags, and stored Confluence page text. Pasting a URL first finds existing Page-ID or canonical
  URL matches; pressing Enter adds it only when no saved match exists;
- browse All, Favorites, Pinned, Recently viewed, Frequently viewed, Never viewed, and automatic
  domain categories from the left sidebar;
- keep quick notes and tags locally, track opens and viewed versions, show Confluence writers and
  editors, and filter the People column by name;
- select a tree branch, include its children, and delete selected bookmarks from OWL without
  deleting anything in Confluence;
- import OWL or legacy JSON backups, extract URLs from UTF-8 text files, continue after individual
  failures, and export a credential-free JSON backup;
- refresh all saved Confluence pages in a separate background worker so titles, hierarchy,
  metadata, timestamps, availability, and searchable text can update while you continue working.
  The header shows progress and the exact last-completed date and time. OWL schedules this weekly;
  temporary or credential failures retry after two hours until a later success, while permanently
  deleted pages remain visible as references without causing an endless retry loop.

The notification bell shows unread import, export, and Confluence refresh cards. Notifications are
stored locally and contain sanitized status text rather than credentials or page bodies.

All searching and organization happen against OWL's local SQLite database. Confluence is contacted
only for an explicit connection test, a save/import that retrieves a Confluence page, or a refresh
operation.

### Bitbucket document desk

The separate `bitbucket` Django app is mounted at `/bitbucket/`. Open its gear icon and enter a
Bitbucket Data Center HTTPS clone URL plus an HTTP access token with repository-read permission.
The token is encrypted in the local database, bound to the exact HTTPS origin, sent only in a
Bearer header or username/token Basic authentication, and never returned to the browser. One saved
origin credential can be reused by leaving the token field blank when adding another repository on
that server.

The document desk does not clone or pull repositories. It uses the files, commits, and raw-content
REST endpoints for its first crawl and at most one scheduled refresh per local calendar day. It
downloads each changed PDF in memory, records file size, page count, SHA-256, latest commit details,
earliest available addition author/date, and extracted text in Django's database. Unchanged PDFs
are skipped by commit ID, failed files retain a visible error, and VSDX files are stored only as an
aggregate count. The frontend searches this saved metadata and PDF text through a synchronized
SQLite FTS5 index. A PDF name opens its Bitbucket browse URL in a new tab, its path copies the full
repo-relative path, and **Show in folder** opens the containing Bitbucket directory. These browser
links contain no access token and may require the normal Bitbucket web sign-in/SSO session.

Crawler tuning is defined in `backend/owl/settings.py` and mirrored in `backend/.env.example`.
`BITBUCKET_APP_MAX_WORKERS` defaults to `1`; PDF size, page, text, retry, timeout, SSL verification,
API pagination, and UI search limits are configurable there as well.

Its user interface shares the React + TypeScript application in `frontend/` with Home and Bookmark
Manager. Django keeps the token, REST API, scheduling, CSRF, and open-count APIs in `backend/`; each migrated
template is only a React mount shell. `python backend/start.py` serves the committed production
bundle from `frontend/dist/` and does not require Node.js at runtime. After changing frontend
source, rebuild and test it with:

```console
cd frontend
npm ci
npm run check
```

For Vite hot reload, run `python dev.py` from the repository root and open the Vite URL shown in
the terminal. The root launcher starts both applications. Vite shows Home at `/static/`, Bookmark
Manager at `/static/bookmarks/`, Settings at `/static/bookmarks/settings/`, and Bitbucket at
`/static/bitbucket/`; it proxies their Django data and action endpoints to the backend.

VSDX extraction and OCR remain out of scope for the document desk.

### Shared local semantic search

OWL adds semantic search as a second, local retrieval layer for Bookmark Manager. Existing exact
search still runs first; only when it returns no matches does OWL show related-content results.
Each bookmark's stored title, page text, notes, and tags are embedded locally. This includes saved
Confluence text and metadata already stored for an ordinary web bookmark, but OWL never fetches an
ordinary bookmark URL merely to create an embedding.

Semantic queries keep one compact centroid per source in memory, select a bounded candidate set,
then stream candidate chunks from SQLite for final reranking. This keeps search memory tied to
the number of bookmarks; the rerank bound is configurable when a larger recall window is worth
more query work.

Embedding follows the normal bookmark lifecycle:

- a new bookmark queues durable semantic work;
- changed bookmark text, notes, or tags queues an atomic replacement;
- deleting a bookmark cascades to its semantic jobs, index, chunks, and vectors.

`run_owl` supervises a separate pool of semantic workers, so bookmarks can be embedded without
blocking bookmark refresh, Bitbucket metadata refresh, or web requests.
Each worker owns one local model session. `SEMANTIC_MAX_WORKERS=2` is the default; use two or three
on a machine with enough memory, and no more than four. Inspect durable progress without changing
it with the command below. Startup reconciliation also queues existing stored sources that do not
yet have a current index, so an upgrade backfills them without external requests.

```bash
python manage.py semantic_status
```

The first semantic worker downloads the pinned retrieval model (about 67 MB) into
`OWL_DATA_ROOT/models/semantic/` unless `SEMANTIC_MODEL_PATH` points to a provisioned local model.
Only model files are downloaded: bookmark text, queries, embeddings, and indexes remain on this
computer and are never uploaded by semantic search. After the model is cached, set
`SEMANTIC_MODEL_OFFLINE=true` to prohibit further model downloads. Set
`SEMANTIC_SEARCH_ENABLED=false` to disable semantic indexing and fallback search completely.

The main tuning settings in `.env` are `SEMANTIC_MAX_WORKERS`,
`SEMANTIC_EMBEDDING_BATCH_SIZE`, `SEMANTIC_CHUNK_MAX_CHARACTERS`,
`SEMANTIC_CHUNK_OVERLAP_CHARACTERS`, `SEMANTIC_RECONCILE_SECONDS`,
`SEMANTIC_SEARCH_TOP_K`,
`SEMANTIC_RERANK_SOURCE_CANDIDATES`, and
`SEMANTIC_SEARCH_MIN_SCORE`. Model identity is pinned by `SEMANTIC_MODEL_ID`,
`SEMANTIC_MODEL_REPOSITORY`, and `SEMANTIC_MODEL_REVISION`; change those together only when you
intend OWL to rebuild the local corpus. Restart OWL and its workers after changing semantic
settings.

### Shared utilities

**System Status** provides a redacted local health summary for OWL's database, configuration, and
app foundations. **Global Search** is a planned shared capability rather than a third app; it will
eventually combine Bookmark Manager data with saved Bitbucket document metadata.

The complete product contract is in [work_prompts](work_prompts/README.md).

## What you need

- macOS, Linux, or Windows 10/11;
- Python 3.13 or 3.14;
- Node.js with npm for the Vite development server;
- Git;
- an internet connection while cloning OWL and downloading the pinned Python packages.

You do not need a Confluence PAT for setup or automated tests. You need a Confluence Data Center
PAT only when you deliberately connect your own installation through OWL's local settings panel.
Never paste a real PAT into this repository, a test, a screenshot, or a support message.

## First-time setup

The project can live in any folder. The repository keeps the React client in `frontend/` and the
Django application in `backend/`. Open Terminal in your OWL checkout and run:

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install uv==0.12.5
uv sync --locked --all-extras
cp -n .env.example .env
python manage.py migrate
cd ../frontend
npm ci
cd ..
```

What those commands do:

1. create an isolated Python environment inside `backend/.venv`;
2. install the exact locked application and development dependency versions;
3. create your ignored local settings file without replacing one that already exists;
4. create the local database under `backend/var/`;
5. install the pinned frontend packages.

On another computer where OWL has not been downloaded, first run:

```bash
git clone git@github.com:durgeshsingh90/owl.git
cd owl/backend
```

### Windows Command Prompt

On Windows with Python 3.13 installed, open Command Prompt in your OWL checkout's `backend`
folder and run:

```bat
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
cd ..\frontend
npm ci
cd ..
python dev.py
```

`python --version` must report Python 3.13.x or 3.14.x. The activated interpreter shown by
`where python` should be the `.venv\Scripts\python.exe` inside this checkout.

After pulling a newer OWL version on Windows, restart with the root launcher:

```bat
git pull
python dev.py
```

`dev.py` runs Django checks first, backs up the existing SQLite database when updates are pending,
and applies all migrations before it starts either Django or Vite. This preserves existing data
while adding columns required by the newer code. If preparation fails, neither service starts.

If a directly launched development server reports
`no such table: bitbucket_repository`, stop it with `Ctrl+C` and run `python start.py` from the
`backend` folder. The launcher backs up the SQLite database, repairs the earlier Bitbucket draft's
migration history, applies the current schema, and then starts the website and workers.

The first start creates a strong machine-local Django key under `var/secrets/` when
`DJANGO_SECRET_KEY` is blank. The key and the entire `var/` directory are ignored by Git.

## Run OWL

Stop any earlier OWL server and workers before starting another instance. From the repository
root, the `start` command launches Django and Vite together in one terminal:

```bash
python3 dev.py start
```

Plain `python3 dev.py` is an equivalent shortcut.

On Windows, use `python dev.py` (or `py dev.py`). The launcher runs Django on
`http://127.0.0.1:8000/` and Vite on `http://127.0.0.1:5173/static/`; open the Vite address for
frontend hot reload. If either port is occupied, it selects the next available port, points Vite at
the selected Django port, and prints the actual frontend URL. Both services share the terminal, and
`Control-C` stops both of them. Before starting either service, it completes the safe database
preparation described below. The launcher prefers `backend/.venv`, accepts the former root `.venv`,
and reports missing Python or npm setup without installing packages automatically.

To run only Django and its background workers, change to `backend/` and invoke its launcher:

```bash
cd backend
python3 start.py
```

The backend launcher checks the app, backs up an existing SQLite database **only when updates are
pending**, applies those updates, and starts `run_owl` with the website and its background workers.
Backups go to the configured data folder's `backups` directory (normally `var/backups`), with unique
names; it never overwrites earlier backups. A failed backup or database update stops startup.
If a database lock blocks the backup for 30 seconds, startup stops and asks you to close other
OWL instances; large backups can keep running while they make progress.
It does not install packages, pull Git changes, replace `.env`, or change worker settings.

Backend-only optional commands, run from `backend/`:

```bash
python3 start.py 9000       # Use a different port
python3 start.py --check   # Check setup without applying updates or starting workers
python3 start.py --help
```

The address and port default come from `run_owl`, not the launcher. Normally open
[OWL](http://127.0.0.1:8000/) in your browser. Keep the terminal open; press `Control-C` to stop.
Direct `python manage.py run_owl` remains available when you manage database updates yourself.

`run_owl` starts the local website, the weekly Confluence scheduler, the standalone
Bitbucket daily metadata scheduler, and the configured bookmark semantic worker pool. Bitbucket
repository adds and refreshes use the clone-free REST API document worker. Keep `run_owl` running
for scheduled work; its durable queues are resumed after restart.

The Bookmark Manager's global refresh button and every due Confluence schedule start a separate
local worker process automatically. That worker retrieves saved Confluence pages with up to five
concurrent read-only requests while the web app remains available. Progress and the exact
last-completed timestamp appear beside the refresh icon and in Background status.

If you deliberately use Django's plain `runserver` command instead, keep this scheduler running in
a second terminal so weekly work can begin even when no OWL browser page is open:

```bash
python manage.py bookmark_refresh_scheduler
```

Opening an OWL page can also perform lightweight due-schedule checks as a catch-up. These checks
only queue durable background work; they do not download Confluence pages or Bitbucket documents
inside the browser request.

For diagnostics or recovery, a queued run can also be processed directly with:

```bash
python manage.py bookmark_refresh_worker --run-id RUN_ID
```

The durable run and schedule records survive page navigation and app restarts. A stopped worker
remains visible as interrupted and is retried after two hours. Temporary page failures get up to
three immediate attempts within a run. Deleted pages and rejected credentials fail fast; credential
failures still enter the two-hour background retry schedule so a corrected connection recovers.

Bitbucket document refreshes use a dedicated durable queue. Adding a repository or requesting a
refresh launches a short-lived worker automatically; no second terminal is required. For
diagnostics, process one queued job directly:

```bash
python manage.py bitbucket_document_worker --once
```

Automatic daily refresh can be disabled with `BITBUCKET_APP_DAILY_REFRESH_ENABLED=false`; set
`BITBUCKET_APP_DAILY_REFRESH_LOCAL_HOUR` to choose the local hour (`9` by default).

### Bitbucket backend logs

Bitbucket web actions and REST API document workers write two rotating local logs under
`OWL_DATA_ROOT/logs` (by default `var/logs`):

- `bitbucket.log`: detailed events, controlled by `BITBUCKET_LOG_LEVEL`;
- `bitbucket-errors.log`: emitted ERROR and CRITICAL events.

Logs contain operational identifiers and sanitized diagnostics, never access tokens or document
text. Rotation uses `OWL_LOG_MAX_BYTES` and `OWL_LOG_BACKUP_COUNT`.

### Bookmark Manager backend logs

Bookmark actions, local search, imports/exports, Confluence requests, configuration,
and background refresh workers write to `OWL_DATA_ROOT/logs` (normally `var/logs`):

- `bookmarks.log`: detailed events, controlled by `BOOKMARK_LOG_LEVEL` (default `DEBUG`).
- `bookmarks-errors.log`: ERROR and CRITICAL failures, regardless of the detailed
  file's threshold.

All five levels are supported: DEBUG for stages, request timings, and counts; INFO
for work starting and completing; WARNING for validation, retries, and expected
fallbacks; ERROR for failed operations, failed import/refresh items, caught database
or credential-store errors, and notification failures; CRITICAL for fatal worker or
scheduler exits. Routine idle/status polling is quiet. The console follows
`OWL_LOG_LEVEL` (default `INFO`).

Diagnostics include applicable refresh/import run, bookmark, and page IDs, safe
error categories/codes, stages, HTTP status, counts, and elapsed time. They never
record credentials, request headers, raw exception messages, URLs, imported
documents, page contents, search terms, titles, or person names. Both applications
share the safe formatter and process-locked rotating file handler. Rotation uses
`OWL_LOG_MAX_BYTES` and `OWL_LOG_BACKUP_COUNT`.

Restart OWL and its workers after changing levels. No database migration is needed.
Treat these files as private workspace diagnostics even though content is omitted.

## Connect Confluence and save a bookmark

1. Open **Bookmark Manager**.
2. Select the **Confluence settings** gear in the top-right corner.
3. Enter the exact HTTPS base URL for your Confluence Data Center application, including its
   context path when it has one.
4. Enter a PAT, select **Test connection**, and review the sanitized result.
5. Select **Save settings**. OWL encrypts the PAT locally, using the operating-system credential
   store when available and its encrypted SQLite field as the fallback.
6. Paste a modern or legacy Confluence page URL into the shared search/add field. OWL first checks
   the local database for the same Page ID; if there is no match, press Enter or select
   **Add bookmark**. A numeric Page ID can also be added directly.
7. OWL saves exactly that page as one bookmark. Its root-to-page ancestors become hierarchy-only
   tree nodes, and searchable body text is stored only for the selected page.

The terminal running `python manage.py run_owl` reports the save stages: extracted Page ID,
selected-page fetch, ancestor count, page-text character count, and final bookmark ID. OWL does not
log the PAT or page body. Django's development-server request line can contain the search query, so
never paste a URL containing credentials, tokens, or other secrets.

Reopening settings never returns the stored PAT. Changing to a different canonical origin requires
a new PAT. Removing the connection deletes the secure credential while keeping local bookmarks.
OWL's adapter sends bounded, read-only `GET` requests using the standard Confluence Data Center
Bearer-PAT flow; it does not create, edit, or delete Confluence content.

## Organize and reuse bookmarks

The Bookmark Manager keeps its hierarchy and every productivity field locally:

- type `/` to focus search, or search by title, Page ID, URL, person, breadcrumb, tag, quick note,
  or stored Confluence page text;
- enter multiple words separated by spaces to require every word, even when the words match
  different fields;
- paste a URL to match its canonical URL and any embedded Confluence Page ID before deciding
  whether Enter should select an existing bookmark or add a new one;
- add quick notes and comma-separated tags, and mark favorites and pins independently;
- use recent, frequent, never-viewed, favorite, pinned, or domain views without changing the
  stored tree;
- use the arrow keys to move through the tree, `E` to expand/collapse, `F` for favorite, and `P`
  for pin; selecting a bookmark checkbox shows that page's details;
- copy a Page ID, breadcrumb, or validated source URL, and open a page through OWL to update local
  usage and viewed-version history;
- use the People search icon to narrow the locally summarized Confluence writers and editors,
  then select a person to show their written or updated pages.

**Export JSON** downloads a versioned, SHA-256-protected local backup that excludes credentials and
connection configuration. **Import bookmarks** accepts current OWL exports, heterogeneous legacy
JSON collections, and UTF-8 text files containing URLs. It continues after malformed or incomplete
records, reports sanitized record-level failures, and keeps successfully processed URLs. Re-importing
is idempotent, and existing OWL-owned notes, tags, favorites, pins, and usage win over incoming
values. Deleting a bookmark requires confirmation and affects OWL only, never Confluence.

Tree expansion, selection, scroll position, and multi-selection are retained in the browser for
the local workspace. The global refresh action uses a durable run record and a separate worker;
its progress survives navigation, and stale or interrupted work is reported instead of silently
appearing successful.

## Run all local checks

After changing to `backend/` and activating its `.venv`, run one command:

```bash
./scripts/check.sh
```

It checks formatting and code quality, validates Django and migrations, runs the synthetic test
suite with coverage, and confirms that Git's index and untracked, non-ignored files contain no
likely secrets or runtime data. It clears Confluence and Bitbucket connection settings for the
check process, so this normal quality gate cannot use a real integration accidentally.

To run only the read-only public-repository safety scan:

```bash
python scripts/check_tracked_files.py
```

That scan changes nothing. It reads tracked content directly from Git's staged index and reads
untracked, non-ignored files from the working tree. Therefore, replacing a staged private value
with a placeholder only in the working copy cannot hide it from the check. It reports file names,
line numbers, and problem types without printing suspected values. Runtime directories, secret
key files, PDFs, screenshots, and bookmark export artifacts are also rejected from public source.
This is a strong guardrail, not proof that arbitrary content can never contain private data;
review the staged diff before every public commit as well.

## Local configuration and PAT safety

`backend/.env.example` contains documented defaults only. Your copied `backend/.env` is private
and ignored by Git.

For normal interactive use, the Bookmark Manager configuration gear encrypts the Confluence PAT
with OWL's machine-local secret key. OWL prefers the operating-system credential store (macOS
Keychain or Windows Credential Manager) and automatically stores the encrypted payload in its
local SQLite database when that store is unavailable. A complete `CONFLUENCE_BASE_URL` plus
`CONFLUENCE_PAT` environment profile remains available for deliberate development or headless use.
An environment-managed profile takes precedence, remains blank in the browser, and cannot be
replaced or removed through the UI.

Public CI uses only blank or clearly synthetic connection values, a temporary database, an
isolated in-memory credential store, and tests marked to exclude every live external integration.

## Dependency versions

The versions are pinned in `backend/pyproject.toml`:

- Django 6.1;
- cryptography 50.0.0;
- python-dotenv 1.2.3;
- keyring 25.7.0;
- pytest 9.1.1;
- pytest-django 4.14.0;
- coverage 7.15.4;
- Ruff 0.16.4.

`backend/uv.lock` also fixes all transitive package versions across supported platforms. OWL uses
uv 0.12.5 to verify and install that universal lock; `./scripts/check.sh` fails if the lock no
longer matches `backend/pyproject.toml`.

The shared shell also bundles Bootstrap 5.3.8 CSS locally under `backend/static/vendor/`; OWL does not
load its UI framework from a CDN. The accompanying MIT license is stored with the asset.

## Repository

- Public source: [github.com/durgeshsingh90/owl](https://github.com/durgeshsingh90/owl)
- SSH address: `git@github.com:durgeshsingh90/owl.git`

No real credentials, private repository URLs, internal document data, local databases, indexed
files, logs, backups, or exports belong in this public repository.
