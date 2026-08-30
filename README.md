# OWL

OWL (Organised Workspace Locator) is a private, local knowledge workspace with a homepage and
two app areas:

- **Bookmark Manager** is the working app for ordinary web bookmarks and locally stored Confluence
  pages;
- **Bitbucket Search** manages approved Git/Bitbucket repositories with background clone and
  refresh workers, extracts PDF text page by page, and searches a local full-text index.

OWL runs on your own computer and listens only on `127.0.0.1`. The repository is public, but
your credentials, databases, repository checkouts, PDF contents, indexes, and logs must remain
local.

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

The **Bitbucket activity** section ranks Git committers, repositories, and folders for the last
7 days, calendar month, 6 calendar months, or calendar year (rolling back from the current time).
It counts available commits across all file types in each enabled repository's synchronized
branch, not only PDF changes. Counts use the Git committer and commit timestamp, not the person
who pushed. Each `(repository, commit)` counts once; a folder receives one count per commit
touching files directly inside it, including deleted and renamed files. Folder totals can exceed
repository totals because one commit can touch multiple folders. Merge-folder changes are
measured against the first parent, and unavailable shallow-boundary diffs are omitted.
Unusual folder-name bytes and control characters are shown as lossless escapes; ordinary UTF-8
names are unchanged.

Activity history is indexed in the existing background clone/refresh pipeline, before PDF text
extraction is queued. After upgrading, run `python manage.py migrate`, restart OWL and its workers,
and refresh existing repositories once to populate their history. The dashboard reports pending,
stale, and shallow history rather than treating older PDF-only metadata as complete analytics;
it keeps the last successfully published snapshot while a refresh is running or has failed.
The rankings show the top 10 in each category; the summary totals include all matching activity.

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

The notification bell shared by every OWL app shows unread import, export, Confluence, and
Bitbucket refresh cards. A compact, scrollable repository section shows the current status of
every repository, including running work and failed or successful syncs. Expand a repository row
for safe diagnostics and exact timestamps; older notification cards remain separate history and
do not override the latest status. Confluence's weekly schedule and retry details are collapsed
by default. Reading notification status never starts or recovers repository jobs. Notifications
are stored locally and contain sanitized status text rather than credentials or page bodies.

All searching and organization happen against OWL's local SQLite database. Confluence is contacted
only for an explicit connection test, a save/import that retrieves a Confluence page, or a refresh
operation.

### Bitbucket Search — repository synchronization and PDF search working

Bitbucket Search now provides repository registration and durable background synchronization:

- add an approved SSH or HTTPS repository URL from the left repository rail;
- clone it once in a detached background worker, then use fetch plus fast-forward refreshes;
- refresh every enabled repository at 11:00 in `OWL_TIME_ZONE` (Europe/Dublin by default) with a
  bounded parallel repository worker pool. A failed daily attempt retries after two hours, up to
  three retries after the initial attempt during that day's cycle; a success stops that
  repository's remaining retries;
- show queued/downloading/updating progress, a green ready tick, and safe failure states;
- keep managed checkouts under `var/media/bitbucket/repositories/` by default;
- use partial clone and sparse checkout to materialize case-insensitive PDF and VSDX files. If a
  server does not support partial filtering—or the repository has no commits inside the configured
  history window—OWL falls back to a depth-one checkout while keeping the working tree
  document-only;
- keep durable queued work waiting for an available worker and publish regular heartbeats during
  long, otherwise-silent Git and document-discovery steps;
- hydrate PDF/VSDX Git LFS objects with a document-filtered pull when Git LFS is available, and
  refuse to report unresolved pointer files as downloaded documents;
- block refresh instead of resetting, cleaning, or overwriting a dirty, locally-ahead, or diverged
  checkout;
- queue new or changed PDFs for a separate durable background extraction worker only after
  repository synchronization is idle;
- run the permissively licensed `pypdf` parser in bounded child processes, store normalized text
  by one-based page, and isolate encrypted, corrupt, pointer, timeout, and resource-limit failures
  to the affected PDF;
- reuse byte-identical extracted revisions and atomically publish SQLite FTS5 entries. If a changed
  PDF fails, OWL keeps its last published text searchable and labels it stale;
- search locally with removable exact-phrase chips, ALL/ANY matching across repository, path,
  filename, and separate PDF pages, plus repository/index-state filters, relevance sorts, matched
  page explanations, and bounded highlighted snippets;
- keep the existing newest-first Today, Yesterday, week, month, six-month, current-year, last-year,
  and older-year timeline when no search is active.

Repository URLs are canonicalized and deduplicated. Credentials embedded in URLs are rejected;
Git uses the existing SSH agent or external credential manager. Django's `owl/settings.py`
approves `bitbucket.org`, `github.com`, and `scm.mastercard.int` by default. Leave
`BITBUCKET_ALLOWED_HOSTS` unset to use those defaults, or set it in your local `.env` to replace
them with a comma-separated list of exact hostnames. An explicitly blank value disables
repository additions. When overriding, keep all hosts you want to use in the list, for example:

```dotenv
BITBUCKET_ALLOWED_HOSTS=bitbucket.org,github.com,scm.example.invalid
```

Replace `scm.example.invalid` with your internal server's hostname; do not include `https://`,
ports, or repository paths in this setting. Restart OWL and its workers after changing it.
An explicitly configured process environment takes precedence over `.env`.
Bitbucket Server/Data Center clone URLs with context paths are supported, such as
`https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git`; OWL preserves the full
clone path. Internal servers still require network/VPN access and working Git credentials
on the computer running OWL. Keep additional private host overrides in the ignored `.env`.

VSDX extraction and OCR remain out of scope. Image-only PDFs are catalogued and reported as having
no machine-readable text; OWL does not invent text that the parser cannot read.

### Shared utilities

**System Status** provides a redacted local health summary for OWL's database, configuration, and
app foundations. **Global Search** is a planned shared capability rather than a third app; it will
eventually combine Bookmark Manager data with indexed Bitbucket PDFs.

The complete product contract is in [work_prompts](work_prompts/README.md).

## What you need

- macOS, Linux, or Windows 10/11;
- Python 3.13 or 3.14;
- Git;
- Git LFS when a managed repository stores its PDF or VSDX content in LFS;
- an internet connection while cloning OWL and downloading the pinned Python packages.

You do not need a Confluence PAT for setup or automated tests. You need a Confluence Data Center
PAT only when you deliberately connect your own installation through OWL's local settings panel.
Never paste a real PAT into this repository, a test, a screenshot, or a support message.

## First-time setup

The project is already in the requested location on this Mac. Open Terminal and run these
commands exactly:

```bash
cd /Users/durgesh/Projects/owl
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install uv==0.12.5
uv sync --locked --all-extras
cp -n .env.example .env
python manage.py migrate
```

What those commands do:

1. open the existing OWL project;
2. create an isolated Python environment inside `.venv`;
3. install the exact locked application and development dependency versions;
4. create your ignored local settings file without replacing one that already exists;
5. create the local database under `var/`.

On another computer where OWL has not been downloaded, first run:

```bash
git clone git@github.com:durgeshsingh90/owl.git
cd owl
```

### Windows Command Prompt

On Windows with Python 3.13 installed, open Command Prompt and run:

```bat
cd C:\Users\YOUR_USERNAME\code\owl
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python manage.py migrate
python manage.py run_owl
```

`python --version` must report Python 3.13.x or 3.14.x. The activated interpreter shown by
`where python` should be the `.venv\Scripts\python.exe` inside this checkout.

After pulling a newer OWL version on Windows, update the database schema before restarting the
app. This preserves your bookmarks while adding any columns required by the newer code:

```bat
git pull
.venv\Scripts\activate.bat
python manage.py migrate
python manage.py run_owl
```

The first start creates a strong machine-local Django key under `var/secrets/` when
`DJANGO_SECRET_KEY` is blank. The key and the entire `var/` directory are ignored by Git.

## Run OWL

From `/Users/durgesh/Projects/owl`, run:

```bash
source .venv/bin/activate
python manage.py run_owl 127.0.0.1:8000
```

Then open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your browser. Press `Control-C`
in Terminal when you want to stop OWL.

`run_owl` starts the local website, its resident weekly Confluence scheduler, a bounded parallel
Bitbucket repository-worker pool, and a separate bounded PDF-worker pool. The Bitbucket supervisor
queues every enabled repository at 11:00 in `OWL_TIME_ZONE` (Europe/Dublin by default). Each failed
daily attempt waits two hours before retrying, with one initial attempt and at most three retries
during that day's cycle. Repository and PDF worker limits are configured independently with
`BITBUCKET_MAX_REPO_WORKERS` and `PDF_MAX_EXTRACTION_WORKERS`.

Keep `run_owl` running for automatic Bitbucket refreshes to start at their due time. OWL cannot run
Git while the application is stopped, but its schedule and jobs are durable: after a restart,
`run_owl` immediately catches up the latest missed 11:00 local slot and any due retry without
replaying a backlog of older daily slots or duplicating active repository jobs. On startup the
supervisor also retires inherited PDF worker leases, applies bounded PDF retries, and queues active
PDFs from an upgraded database that have not been indexed yet.

The Bookmark Manager's global refresh button and every due Confluence schedule start a separate
local worker process automatically. That worker retrieves saved Confluence pages with up to five
concurrent read-only requests while the web app remains available. Progress and the exact
last-completed timestamp appear beside the refresh icon and in the notification centre.

If you deliberately use Django's plain `runserver` command instead, keep this scheduler running in
a second terminal so weekly work can begin even when no OWL browser page is open:

```bash
python manage.py bookmark_refresh_scheduler
```

Opening any OWL page also performs lightweight Confluence and Bitbucket due-schedule checks once per
minute as a catch-up. The Bitbucket check queues only the latest missed 11:00 local slot or due
retry. These checks only queue durable background work; they do not download Confluence pages or run
Git inside the browser request.

For diagnostics or recovery, a queued run can also be processed directly with:

```bash
python manage.py bookmark_refresh_worker --run-id RUN_ID
```

The durable run and schedule records survive page navigation and app restarts. A stopped worker
remains visible as interrupted and is retried after two hours. Temporary page failures get up to
three immediate attempts within a run. Deleted pages and rejected credentials fail fast; credential
failures still enter the two-hour background retry schedule so a corrected connection recovers.

Bitbucket repository clone and refresh requests use the same non-blocking idea with a dedicated
durable queue. Selecting **Add Repository** or a repository's refresh icon launches a detached
worker automatically; no second terminal is required. The worker stays alive while queued work is
available, then exits after a short idle period. For diagnostics, process one queued job directly:

```bash
python manage.py bitbucket_sync_worker --once
```

Running `python manage.py bitbucket_sync_worker` without `--once` keeps a resident hybrid worker
watching the repository queue and then the PDF queue until it is stopped. `run_owl` instead owns
separate repository-only and PDF controllers so both configured concurrency limits remain explicit.
Automatic daily refresh can be disabled with `BITBUCKET_DAILY_REFRESH_ENABLED=false`; its retry
delay and cap are configured with `BITBUCKET_DAILY_REFRESH_RETRY_SECONDS` (default `7200`) and
`BITBUCKET_DAILY_REFRESH_MAX_RETRIES` (default `3`). Set
`BITBUCKET_DAILY_REFRESH_LOCAL_HOUR` to choose the local hour (`11` by default); OWL interprets it in
`OWL_TIME_ZONE` (`Europe/Dublin` by default).

### Exclude or delete a Bitbucket PDF locally

Each PDF row has **Exclude from refresh** and **Delete PDF** actions. Exclusion keeps a
private snapshot and its existing searchable text unchanged while other repository files
continue refreshing. Use **Include in refresh** to queue a repository refresh and replace
that snapshot with the current repository version. Excluded snapshots live beneath
`var/media/bitbucket/excluded/<repository-id>/` by default; Open, Open folder, and Copy path
use that retained copy.

Deletion requires confirmation. It removes the local working file, retained snapshot (if any),
PDF database record, extraction jobs, and unshared indexed text. A minimal repository/path
exclusion rule remains so later pulls do not recreate the deleted PDF. No remote commits or
files are changed. This is not a secure erase of Git's historical object cache, database
backups, or text still shared by another PDF. Exclusion and deletion wait until repository
synchronization and PDF extraction are idle; local modifications are never overwritten.

### Bitbucket backend logs

Bitbucket web actions and background workers share two local, rotating logs under
`OWL_DATA_ROOT/logs` (by default `var/logs`):

- `bitbucket.log`: detailed events; `BITBUCKET_LOG_LEVEL=DEBUG` is the default.
- `bitbucket-errors.log`: every emitted ERROR and CRITICAL event, independently of
  the diagnostic log's level.

Supported levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. DEBUG records
stage/progress details; INFO records queue, start, and completion events; WARNING
records expected fallbacks or deferred work. Operational failures—including caught
database errors, extraction/page failures, checkout errors, and cleanup failures—use
ERROR. Fatal worker exits use CRITICAL. The console follows `OWL_LOG_LEVEL` (INFO by
default), so full debug detail can stay in the file without filling the terminal.

Events include applicable repository, job, and PDF IDs, process/thread identifiers,
stage, counts, timing, and safe error codes. Error diagnostics can include exception
categories, OS/SQLite codes, and code-frame locations, but never raw exception messages,
source lines, credentials, repository URLs, local PDF paths, search terms, or PDF text.
Normal empty-queue polling is not logged. Rotation uses `OWL_LOG_MAX_BYTES` and
`OWL_LOG_BACKUP_COUNT`; writes and rotation are locked across concurrent workers.
Restart OWL and its workers after changing log levels. Keep the files private: IDs and
operational timing still describe your local workspace. If log storage is unavailable,
OWL reports a content-free message to stderr rather than exposing the failed record.

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

After activating `.venv`, run one command:

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

`.env.example` contains documented defaults only. Your copied `.env` is private and ignored by
Git.

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

The versions are pinned in `pyproject.toml`:

- Django 6.1;
- cryptography 50.0.0;
- python-dotenv 1.2.3;
- keyring 25.7.0;
- pytest 9.1.1;
- pytest-django 4.14.0;
- coverage 7.15.4;
- Ruff 0.16.4.

`uv.lock` also fixes all transitive package versions across supported platforms. OWL uses
uv 0.12.5 to verify and install that universal lock; `./scripts/check.sh` fails if the lock no
longer matches `pyproject.toml`.

The shared shell also bundles Bootstrap 5.3.8 CSS locally under `static/vendor/`; OWL does not
load its UI framework from a CDN. The accompanying MIT license is stored with the asset.

## Repository

- Public source: [github.com/durgeshsingh90/owl](https://github.com/durgeshsingh90/owl)
- SSH address: `git@github.com:durgeshsingh90/owl.git`

No real credentials, private repository URLs, internal document data, local databases, indexed
files, logs, backups, or exports belong in this public repository.
