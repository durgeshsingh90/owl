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

Before each Bitbucket clone or refresh, OWL tests access using Git with your configured
authentication. The check times out after 20 seconds by default
(`BITBUCKET_CONNECTION_TIMEOUT_SECONDS`, capped at 120). A failed check stops the download and
shows a notification: network failures suggest checking VPN/connectivity; authentication and
certificate failures have separate guidance. After connecting to VPN, select **Refresh** again.
Existing daily retry limits remain unchanged.

Open **Background status** to see the latest two Git output lines directly below each repository,
updating automatically during clone/refresh. Expand a repository and **Git log** for more output.
This shows operations run by OWL; pushes or pulls run separately in a terminal are not captured.
The local database retains a credential-redacted tail of up to 200 lines / 32K
characters per job. Logs update while that view is open; old jobs from before this feature have
no captured output. Run `python manage.py migrate` and restart OWL/workers after this upgrade.

The adaptive PDF pipeline's configuration, metric meanings, recovery procedure, benchmark
evidence, and rollback steps are documented in
[PDF pipeline operations](PDF_PIPELINE_OPERATIONS.md). The current benchmark evidence is in
[PDF pipeline benchmark record](PDF_PIPELINE_BENCHMARKS.md), and the final Phase 8 evidence and
release decision are in [PDF pipeline validation report](PDF_PIPELINE_VALIDATION_REPORT.md).
The extraction-to-JSONL-to-SQLite delivery has its own
[JSONL pipeline validation report](PDF_JSONL_PIPELINE_VALIDATION_REPORT.md).

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

The **Bitbucket activity** section ranks Git committers, repositories, and folders for **Today**,
**This week**, **Last week**, **This month**, **Last 6 months**, and **This year**. It defaults to
This week. Calendar periods use OWL's configured local time zone: today starts at midnight,
weeks start Monday, this month starts on the first, and this year starts January 1. Last week
covers the previous Monday through Sunday, excluding this Monday. Last 6 months remains a
rolling calendar-month window. Current periods end at the current time, excluding future commits.
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
Bitbucket refresh cards. A separate **Background status** icon beside the bell opens a compact,
scrollable panel for the current status of every repository, including running work and failed
or successful syncs. Expand a repository row for safe diagnostics and exact timestamps; older
notification cards remain in the bell and do not override the latest status. Confluence's weekly
schedule and retry details are collapsed by default in Background status. The status indicator
shows active work, attention needed, or all repositories up to date; the bell badge counts unread
notifications only. Both panels share one read-only status poll, without duplicating scheduler
checks. Reading status never starts or recovers repository jobs. Notifications are stored locally
and contain sanitized status text rather than credentials or page bodies.

All searching and organization happen against OWL's local SQLite database. Confluence is contacted
only for an explicit connection test, a save/import that retrieves a Confluence page, or a refresh
operation.

### Bitbucket Search — repository synchronization and PDF search working

Bitbucket Search now provides repository registration and durable background synchronization:

- add an approved HTTPS repository URL from the left repository rail;
- clone it once in a detached background worker, then use fetch plus fast-forward refreshes;
- run `git ls-remote --symref -- <https-url> HEAD` before every clone or pull; a failed HTTPS
  preflight opens a firewall/VPN/credential prompt with **Retry** and **Cancel**;
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
- queue new or changed PDFs for a separate durable background extraction pool only after
  that repository's synchronization is idle. Multiple PDFs from one repository can be read
  concurrently; Git updates and local deletion take an exclusive checkout lock;
- run the permissively licensed `pypdf` parser in bounded child processes, store normalized text
  by one-based page, and isolate encrypted, corrupt, pointer, timeout, and resource-limit failures
  to the affected PDF;
- reuse byte-identical extracted revisions and atomically publish SQLite FTS5 entries. If a changed
  PDF fails, OWL keeps its last published text searchable and labels it stale;
- search locally with removable exact-phrase chips, ALL/ANY matching across repository, path,
  filename, and separate PDF pages, plus repository/index-state filters, relevance sorts, matched
  page explanations, and bounded highlighted snippets;
- keep a newest-first Today, Yesterday, Day Before Yesterday, This Week, Last Week, This Month,
  Last Month, Last 3 Months, Last 6 Months, This Year, Last Year, Last 2 Years, Last 3 Years, and
  older-year timeline when no search is active, using the original Git addition's commit date, not
  the date OWL discovered the PDF. The column is labelled "Date added to repo";
  PDFs whose original addition is outside available history show their OWL discovery date with
  the visible source **First seen by OWL**, while confirmed dates show **Git addition**;
- display 500 PDFs per inventory page by default (also capped at 500 for larger configured values).
  Use the page-navigation links to browse older results; scrolling does not append more rows;
- keep the current page and saved theme stable during background work. Status updates live,
  and one reload shows the final changes after all repository/PDF jobs settle. An open status
  or notification panel defers that reload until it is closed.

The main PDF search checks **saved extracted text, filenames, and repo-relative paths** by
default (repository names are also included). Filename/path matches are available as soon as
the PDF is catalogued, even before text extraction finishes. Content matches use the published
text in OWL's database; searching does not reopen PDF files or contact Git. Add separate chips
to match terms across different fields or pages, or use **Search in** filters to narrow the scope.

Repository URLs are canonicalized and deduplicated. Credentials embedded in URLs are rejected.
New repository registration is HTTPS-only. HTTPS repositories can use an exact-host credential
saved in OWL Settings, with an external credential manager still available when OWL has no saved
credential for that origin. Django's `owl/settings.py` approves `bitbucket.org` and `github.com`
by default. Leave `BITBUCKET_ALLOWED_HOSTS` unset to use those defaults, or set it in your local
`.env` to replace them with a comma-separated list of exact hostnames. An explicitly blank value
disables repository additions. When overriding, keep all hosts you want to use in the list, for
example:

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

### Bitbucket app

OWL also contains a separate Django app named `bitbucket`, mounted at `/bitbucket/` alongside
Bitbucket Search. It has independent database tables, migrations, repository checkouts, routes,
templates, static assets, and background command names. Its local Add Repository form accepts
credential-free HTTPS clone URLs and automatically approves the exact submitted HTTPS origin
before `git ls-remote`, clone, or pull can run. This app does not use or require
`BITBUCKET_ALLOWED_HOSTS`. The app clones a repository once, schedules at most one successful pull per day,
catalogues PDF and VSDX totals, shows PDF metadata in 500-row pages and timeline groups, records
PDF opens, supports multi-select open/path-copy actions, reveals containing folders, and derives
the People rail from Git commit evidence.

VSDX extraction and OCR remain out of scope. Image-only PDFs are catalogued and reported as having
no machine-readable text; OWL does not invent text that the parser cannot read.

### Shared local semantic search

OWL adds semantic search as a second, local retrieval layer for both apps. Existing exact search
still runs first; only when it returns no matches does OWL show related-content results. Bitbucket
Search embeds the text from published PDF revisions, retaining page numbers for result snippets.
Bookmark Manager embeds each bookmark's stored title, stored page text, notes, and tags. This
includes saved Confluence text and any metadata already stored for an ordinary web bookmark, but
OWL never fetches an ordinary bookmark URL merely to create an embedding.

Semantic queries keep one compact centroid per source in memory, select a bounded candidate set,
then stream candidate chunks from SQLite for page-level reranking. This keeps search memory tied to
the number of PDFs and bookmarks rather than the total number of extracted pages; the rerank bound
is configurable when a larger recall window is worth more query work.

Embedding follows the normal source lifecycle:

- a newly published PDF text revision or saved bookmark queues durable semantic work;
- changed PDF text, bookmark text, notes, or tags queues a replacement, and the new chunks are
  published atomically only if they still match the current source content;
- deleting a bookmark or an unreferenced PDF revision cascades to its semantic jobs, index, chunks,
  and vectors. Removing a repository therefore removes semantic data unique to that repository;
  a byte-identical PDF revision stays indexed while another managed PDF still uses it. None of
  these actions deletes or changes a remote Git, Confluence, or web source.

`run_owl` supervises a separate pool of semantic workers, so PDF revisions and bookmarks can be
embedded in parallel without blocking Git, PDF extraction, bookmark refresh, or web requests.
Each worker owns one local model session. `SEMANTIC_MAX_WORKERS=2` is the default; use two or three
on a machine with enough memory, and no more than four. Inspect durable progress without changing
it with the command below. Startup reconciliation also queues existing stored sources that do not
yet have a current index, so an upgrade backfills them without re-cloning or re-fetching content.

```bash
python manage.py semantic_status
```

The first semantic worker downloads the pinned retrieval model (about 67 MB) into
`OWL_DATA_ROOT/models/semantic/` unless `SEMANTIC_MODEL_PATH` points to a provisioned local model.
Only model files are downloaded: PDF text, bookmark text, queries, embeddings, and indexes remain
on this computer and are never uploaded by semantic search. After the model is cached, set
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

The project can live in any folder. Open Terminal in your OWL checkout and run:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install uv==0.12.5
uv sync --locked --all-extras
cp -n .env.example .env
python manage.py migrate
```

What those commands do:

1. create an isolated Python environment inside `.venv`;
2. install the exact locked application and development dependency versions;
3. create your ignored local settings file without replacing one that already exists;
4. create the local database under `var/`.

On another computer where OWL has not been downloaded, first run:

```bash
git clone git@github.com:durgeshsingh90/owl.git
cd owl
```

### Windows Command Prompt

On Windows with Python 3.13 installed, open Command Prompt in your OWL checkout and run:

```bat
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python start.py
```

`python --version` must report Python 3.13.x or 3.14.x. The activated interpreter shown by
`where python` should be the `.venv\Scripts\python.exe` inside this checkout.

After pulling a newer OWL version on Windows, update the database schema before restarting the
app. This preserves your bookmarks while adding any columns required by the newer code:

```bat
git pull
python start.py
```

The first start creates a strong machine-local Django key under `var/secrets/` when
`DJANGO_SECRET_KEY` is blank. The key and the entire `var/` directory are ignored by Git.

## Run OWL

Stop any earlier OWL server and workers before starting another instance. From your OWL folder:

```bash
python3 start.py
```

On Windows, use `python start.py` (or `py start.py`). No environment activation is needed:
the launcher finds the checkout from its own location and prefers its `.venv` interpreter
(`.venv/bin/python` on macOS/Linux, `.venv\Scripts\python.exe` on Windows). If no local `.venv`
exists, it uses the Python interpreter that launched it; install OWL's dependencies first.
You can also invoke the script by its path from another folder, including paths with spaces.

The launcher checks the app, backs up an existing SQLite database **only when updates are pending**,
applies those updates, and starts `run_owl` with the website and its background workers.
Backups go to the configured data folder's `backups` directory (normally `var/backups`), with unique
names; it never overwrites earlier backups. A failed backup or database update stops startup.
If a database lock blocks the backup for 30 seconds, startup stops and asks you to close other
OWL instances; large backups can keep running while they make progress.
It does not install packages, pull Git changes, replace `.env`, or change worker settings.

Optional commands:

```bash
python3 start.py 9000       # Use a different port
python3 start.py --check   # Check setup without applying updates or starting workers
python3 start.py --help
```

The address and port default come from `run_owl`, not the launcher. Normally open
[OWL](http://127.0.0.1:8000/) in your browser. Keep the terminal open; press `Control-C` to stop.
Direct `python manage.py run_owl` remains available when you manage database updates yourself.

`run_owl` starts the local website, its resident weekly Confluence scheduler, a bounded parallel
Bitbucket repository-worker pool, 16 isolated PDF extraction workers, one JSONL staging writer,
and one dedicated SQLite writer. The Bitbucket supervisor queues every enabled repository at 11:00 in `OWL_TIME_ZONE`
(Europe/Dublin by default). Each failed
daily attempt waits two hours before retrying, with one initial attempt and at most three retries
during that day's cycle. Worker limits are configured in `owl/settings.py`.
PDF extraction is configured for one repository and up to 16 PDFs at a time. Each extractor hands
its validated result to a durable per-PDF spool. The sole staging writer appends complete UTF-8
records to `current.jsonl`, seals size-only 50 MB chunks, and lets extraction continue while the
dedicated SQLite writer imports pages and search-index updates. Other repositories remain queued until
the active repository's PDF run completes. Git downloads can continue independently.
The Repository logs page shows the configured limit, active workers, every durable PDF
attempt, and the retained redacted Git clone/refresh output. Select a repository there and use
**Stop indexing now** to cancel its queued attempts and revoke active parser leases; an active
isolated parser is terminated when its next one-second heartbeat observes the revoked lease.
The same repository-scoped action is available from the sidebar selection toolbar.
The sidebar and top status panel show the number of PDFs remaining in the current run. Extraction
results waiting for either writer survive an OWL restart in the spool, `current.jsonl`, or sealed
chunks. Imported chunks are retained for seven days from successful import, while current, queued,
importing, failed, and uncommitted chunks are never automatically deleted. OWL also keeps a durable
recovery circuit for the supervisor, controller, JSONL stager, publisher, extraction
pool, and individual extraction slots. Transient component failures retry with bounded backoff and
pause after 25 consecutive failed probes by default; permanent problems with one PDF remain within
that PDF's existing retry policy. Equivalent slot failures observed within 10 seconds are moved to
one extraction-pool episode so one incident is not counted once per worker. Change those operational
defaults only with `PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS`,
`PDF_PIPELINE_RECOVERY_CORRELATION_WINDOW_SECONDS`, and
`PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT`.

A recovery pause preserves queued jobs, published text, and valid staging files. Resume means
**continue from the last durable boundary**: an already published PDF is not repeated, a valid
staged result continues at publication, and an unstaged parser attempt restarts that PDF from the
beginning because page-level parser checkpoints do not exist. The Background status, PDF pipeline
details, and recovery notification expose the same generation-bound **Resume** action. OWL runs one
half-open stability probe only after its safety preflight passes. If SQLite is unavailable, a small
redacted same-disk checkpoint blocks affected launches until canonical recovery state can be
reconciled; if neither store is writable, the current process fails closed and reports that the
pause was not durably recorded.

Restart OWL and all background workers after upgrading so every process uses the new
shared-reader/exclusive-writer checkout locking.

Keep `run_owl` running for automatic Bitbucket refreshes to start at their due time. OWL cannot run
Git while the application is stopped, but its schedule and jobs are durable: after a restart,
`run_owl` immediately catches up the latest missed 11:00 local slot and any due retry without
replaying a backlog of older daily slots or duplicating active repository jobs. On startup the
supervisor also retires inherited PDF worker leases, applies bounded PDF retries, and queues active
PDFs from an upgraded database that have not been indexed yet.
Resident workers exit when their owning supervisor disappears, and a replacement supervisor stops
and relaunches any owned controller whose durable heartbeat is silent for 90 seconds. Only one
`run_owl` process owns the worker pool for a given OWL data directory, preventing duplicate
background pools. A stopped Git controller's job is fenced and retried once as a distinct attempt;
PDF and semantic jobs use their own bounded retry counts. An individual parser that remains alive
but never finishes is stopped by its separate ten-minute extraction timeout so the queue can
continue.

On macOS and Windows, OWL also keeps the display and computer awake while a Git, PDF extraction,
or semantic indexing job is queued or running, then releases that temporary assertion automatically
when every queue becomes idle or `run_owl` exits. Set
`OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK=false` to disable this behavior.

The Bookmark Manager's global refresh button and every due Confluence schedule start a separate
local worker process automatically. That worker retrieves saved Confluence pages with up to five
concurrent read-only requests while the web app remains available. Progress and the exact
last-completed timestamp appear beside the refresh icon and in Background status.

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

### Exclude or remove a Bitbucket repository

Select repositories using their sidebar checkboxes, then use the action icons beside **New**.
**Refresh selected** queues those repositories in the background. **Exclude from refresh** skips
the selected repositories during **Refresh all**, daily refreshes, and automatic retries. Their existing
PDFs, searchable text, and People information remain available. A refresh already queued or
running finishes normally; the exclusion applies to future work. You can still select an excluded
repository and use **Refresh selected**, or select only excluded repositories and use
**Include in refresh** to restore bulk and scheduled refreshes.

Deletion uses the same two-click control as Bookmark Manager. Select the repositories, then click
the **🔒** button once to unlock it (**🔓**). Clicking that same button again within 10 seconds
deletes the selected repositories immediately, without another confirmation page. This removes
their managed local checkouts, retained PDF copies, repository records, commit history, document
records, jobs, and indexed text that no other PDF uses. The remote repository is never changed.
The button relocks after 10 seconds, when the selection changes, after submission/page restore,
or when status cannot be verified. Starting or finishing repository work does not turn this
accidental-delete guard into a worker lock. Removal is not UI-locked by PDF indexing: OWL first
cancels that repository's queued and running PDF attempts, waits briefly for active parsers to
release the checkout, and then removes it. Active Git clone/refresh work is not killed; the
removal request returns a retryable conflict for only that repository. If local cleanup fails, OWL
keeps a recovery record and offers **Retry removal** instead of claiming that deletion finished.
This is ordinary local deletion, not a secure erase of backups, application logs, or disk history.

The top-bar **Refresh all repositories** control is icon-only, with its name on hover. While Git
or PDF workers are active, it becomes a loading indicator and cannot queue another global refresh.
The separate **Repository logs** control replaces its idle terminal icon with distinct Clone, Pull,
and Indexing chips while those operations run; each chip shows honest progress (or a queued/running
state when no percentage exists) and a live elapsed timer from the real worker start. Affected
repository cards use the matching operation icon, progress bar, and timer. Running repositories
continue to show their elapsed worker timers in the sidebar.

Run migrations and restart OWL/workers after upgrading. Existing per-PDF exclusions migrate
to their parent repository. Their frozen copies stay readable until a successful explicit
refresh (or a refresh after re-including the repository) replaces them with the current Git
version. Previously deleted PDFs remain deleted. New per-file refresh exclusions are no longer
available.

PDF controls are read-only: open a file, reveal its folder, copy its path, or search its text.
There is no individual **Delete PDF** action; requests from old deletion forms are rejected
without changing files or database records. Previously deleted PDFs are not restored by this
UI change. Repository-level **Remove repository** remains a separate confirmed action that
removes its entire local checkout and indexed data, without changing the remote repository.

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

PDF extraction errors include a safe stage/reason and available numeric OS/Windows codes.
`pdf_dependency_unavailable` means the worker could not load its PDF parser; install the
project dependencies in the Python environment that starts OWL, restart OWL/workers, then
Refresh the affected repository. Automatic retries are disabled for this installation failure.
Other parser failures are not assumed to mean the PDF is corrupt: their safe diagnostics
distinguish parser startup, file access, and page extraction without logging document content.

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
