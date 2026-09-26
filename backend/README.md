# OWL FastAPI backend

This implements the working Bitbucket/PDF snippets supplied in screenshots. The former Django backend is not used.

## Run

From the OWL project root:

```bash
python3 dev.py start
python3 dev.py status
python3 dev.py restart
python3 dev.py stop
```

Frontend: http://127.0.0.1:8771/home/
API documentation: http://127.0.0.1:8000/docs

The frontend server forwards `/api/` and the existing Bitbucket connection/settings endpoints to FastAPI. Other frontend features still use their existing sample data and simulated pull workflow; this backend does not silently replace those datasets.

## Configure and crawl

1. Open Bitbucket settings in the frontend to test and save the server URL, username and token. Alternatively use `POST /api/settings` in API docs, which also exposes `max_workers` (1–10). The legacy `verify_ssl` field is accepted but always normalized to false.
2. The URL can include a context path (such as `/stash`) or end in `/rest/api/1.0`. No corporate hostname is hardcoded. TLS certificate verification is always disabled for Bitbucket requests.
3. Add a project using `POST /api/project` with `{"project_url":"https://your-server/projects/KEY"}`.
4. Call `POST /api/crawl` with `{}` to crawl all tracked projects, or `{"project_ids":[1]}` for selected projects.
5. Poll `GET /api/jobs/{id}` for new/updated/unchanged/failed counts, repository progress, elapsed time and estimated remaining time. ETA is unavailable until a repository finishes.
6. `POST /api/jobs/{id}/cancel` stops a crawl. Completed records remain saved.

Normal Git pull checks the latest commit of every tracked, non-excluded repository. Repositories at their saved commit checkpoint skip folder discovery and PDF processing. For a changed head, the paginated Bitbucket compare changes API selects only added, modified, or renamed PDFs and removes deleted PDFs from the index. Outstanding PDF failures are retried. Checkpoints advance only after successful processing. New repositories and existing repositories without a checkpoint need one baseline scan; hard retry explicitly requests a full rebuild.

A command-line equivalent is available after saving settings:

```bash
backend/.venv/bin/python backend/pdf_crawler.py https://your-server/projects/KEY
backend/.venv/bin/python backend/search_pdfs.py
```

## API coverage

- `GET /api/health`, `/api/settings`; `POST /api/settings`, `/api/settings/test`, `/api/connection/test`
- `POST /api/project`; `GET /api/projects`, `/api/sidebar`, `/api/projects_summary`
- `POST /api/crawl`; `GET /api/jobs/{id}`; `POST /api/jobs/{id}/cancel`
- `GET /api/search?q=azure`, `/api/repo?project=KEY&repo=all`, `/api/document/{id}`
- `PATCH /api/document/{id}/notes` with `{"notes":"..."}`
- `POST /api/document/{id}/open` increments opens and returns the URL
- `GET /api/stats`, `/api/contributors`, `/api/failed`
- `DELETE /api/repositories/{id}` with `{"confirmation":"delete all"}` removes local records only; a later crawl of its tracked project can discover it again.

Search accepts literal terms (OR), `+azure +aws` (AND), and quoted phrases. Search/repo endpoints support `limit` and `offset`; search also supports project, repo and author filters. Compatibility aliases without `/api` match the screenshot routes.

## Data and lifecycle

SQLite data is in `backend/data/owl.db`, with WAL, foreign keys, and FTS5 triggers. Server/project/repository identity prevents collisions across projects. Upserts preserve document IDs, notes and opens. Unchanged known commits skip download; updated PDFs replace their search entries transactionally. PDFs are held in memory only (50 MB maximum response) and discarded after extraction. No OCR is performed; image-only PDFs may have no searchable text.

Credentials are encrypted in `backend/data/settings.enc`; the local encryption key is `secret.key`. Both files are owner-only, but a user able to read both can decrypt them. Data and credentials are Git-ignored. Use `OWL_DB_PATH` and `OWL_CONFIG_DIR` to isolate environments.

One API process owns one background crawl at a time. Do not run multiple Uvicorn workers against this local job runner. Normal shutdown cancels work; interrupted jobs are marked interrupted on startup. Discovery paginates repositories and every folder, retries transient failures, rejects redirects, and preserves existing documents if a file fails. Remotely deleted files are retained until explicitly removed locally. Sync reads the Bitbucket REST API; it does not clone repositories or execute `git pull`.

The screenshot bookmark files were empty placeholders. Confluence crawling, bookmark persistence, and complete frontend data/pull integration are not part of the reconstructed PDF snippets.

## Tests

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases, test credentials and mocked Bitbucket HTTP responses with real PDF extraction. They cover pagination, incremental indexing, FTS replacement/deletion, metadata preservation, failure recovery, cancellation, duplicate-job rejection, and settings validation. Corporate Bitbucket connectivity needs your saved credentials and network access.


### Backend diagnostics

Logs are written to `backend/data/logs/backend.log` (JSON lines, 5 MB rotation,
three backups). Override the folder with `OWL_LOG_DIR`. Request IDs connect HTTP
requests to Bitbucket attempts, response statuses, timings, retries, network
exception types and OS error codes. SSL verification remains disabled. Logs do
not record tokens, authorization headers, request/response bodies, or raw
exception messages. Proxy environment variable names are logged, not values.

After copying updates to Windows, run `python dev.py restart`, reproduce the
settings failure, then inspect:

```powershell
Get-Content backend/data/logs/backend.log -Tail 80
```

The settings UI uses OWL's encrypted saved credentials, not the standalone
crawler's `config.ini`. Both a server URL ending in `/stash` and its full
`/stash/rest/api/1.0` URL are accepted. Use the same credentials as the working
standalone script. Local tests do not establish corporate VPN connectivity.


### Import from Bitbucket New

New accepts one project, repository, or PDF browse/raw URL per line, on the
configured Bitbucket server. Project URLs enumerate repositories and recursively
scan PDFs; individual PDF URLs index only those files. URLs with branch query
parameters are rejected rather than silently importing a different revision.
The existing commit check, SHA-256 hash, PyMuPDF extraction, SQLite upsert and
FTS search remain shared by both paths. Temporary PDFs are removed in `finally`.
This is an adaptation of the supplied script to the existing API and database,
not a verbatim copy: the HTTP client remains HTTPX, and stable upserts/FTS triggers
avoid duplicate search rows.

POST `/api/imports` starts a managed job. The screen polls `/api/jobs/{id}` for
actual counts and can stop the job or resume monitoring after a reload. Pull
now starts a real project crawl as well. No simulated PDF counts are generated.

Background scanning is owned by the running backend, not the browser. PDF text
extraction uses a separate worker process so progress requests remain responsive.
The UI discovers the latest saved job on load/focus, including in a new tab.
Closing the page does not cancel a scan. Stopping/restarting the backend stops
the job; it does not automatically resume interrupted work.

The progress bar reports successful repositories / total repositories, processed
PDFs / discovered PDFs, and estimated remaining time across the entire job.
Repository/file discovery runs before PDF processing so the denominator is stable.
A repository with a PDF failure does not count as successful. PDF processed counts
include failed attempts; failures remain separately visible. ETA is unavailable
until discovery finishes and at least one PDF has been processed. If discovery
fails, the UI labels the PDF count as known files.

### Hard retry

The Bitbucket page's **Hard retry** button previews all tracked projects on the configured server and requires `HARD RETRY` confirmation. It saves a SQLite backup under `backend/data/backups/` (or next to `OWL_DB_PATH`), then clears those projects' PDF records, PDF notes/view counts, FTS entries and failed-PDF records. Project/repository registrations, settings, bookmarks and other servers' data remain. The background job rediscovers every repository and PDF, downloads and extracts unchanged PDFs too, and automatically retries failed PDFs once. Progress uses the normal job/sidebar statuses. A failed or cancelled rebuild leaves a partial index; the pre-retry snapshot remains available. Hard retry is rejected while another crawl is active.

Crawls enumerate project/repository API pages and sort projects and repository slugs alphabetically (case-insensitive). PDF discovery runs sequentially first to determine the total, followed by sequential repository processing in the same order. PDF paths are alphabetical too. Initial imports, normal crawls and hard retries all use this order; `max_workers` remains readable for configuration compatibility but does not enable repository concurrency.

PDF extraction uses a single shared background thread, with no multiprocessing or child extraction processes. Repository and PDF requests remain sequential. Worker settings are normalized to one, including older configurations with larger values.

### Flowing console logs

Run `python dev.py logs` from the project root (or `python3 dev.py logs` on macOS/Linux). It prints the last 20 lines per source, then follows backend, crawler diagnostics, frontend and supervisor logs. Use `--service backend` for backend/crawler only, `--lines 0` for new output only, or `--no-follow` for a snapshot. Ctrl+C exits the log viewer without stopping OWL. Missing files are watched until created; rotation and truncation are handled on Windows, macOS and Linux.


## Bookmark Manager and Confluence
The existing Bookmark Manager stores bookmarks, full extracted page text, ancestor
metadata, notes, flags, and open counts in SQLite's bookmark_workspace record.
Use its settings gear to save a Confluence Data Center base URL and bearer PAT.
The PAT is encrypted in confluence.enc with a separate local confluence.key under
OWL_CONFIG_DIR (backend/data by default); it is never returned to the browser.
SSL verification is selectable. Saving settings validates the connection.

Adding a URL on the configured Confluence origin resolves pageId, /pages/ID,
/display/SPACE/title, and /x/ short links, checks the page, and fetches its
rendered text (storage text fallback), space, authorship, version, dates and
ancestor breadcrumbs. The tree displays the saved page under those ancestors.
This does not recursively import unrelated pages or download attachments.
Other HTTP(S) links are saved and grouped by hostname without sending the PAT
or fetching their content. Update refreshes saved metadata while preserving
notes and open counts; failures retain previous content and display the error.
HTML imports attempt the same metadata lookup and retain failed entries for retry.

Validation uses mocked Confluence responses and an isolated SQLite database;
corporate connectivity must be tested through the settings gear on your network.

### Automatic Confluence updates

While the OWL backend is running, saved bookmarks and downloaded page copies on
its configured Confluence server refresh automatically. The first attempt runs
on startup after installing this feature. After a fully successful run, the next
run is scheduled seven days later. Connection failures and incomplete updates
retry every two hours without a retry limit. The schedule persists in SQLite;
overdue work resumes on startup. This does not wake a sleeping computer or start
OWL when it is closed.

The bookmark page displays update progress and the next attempt. Use **Reload
updated pages** when background updates are available in an already-open tab.
Workspace revision checks prevent stale tabs from overwriting newer updates;
notes, stars, groups and other local bookmark fields are preserved. Pages outside
the configured Confluence server and ordinary web bookmarks are not refreshed by
this schedule. Existing downloaded pages are refreshed, without discovering new
pages or adding them to the saved bookmarks.

### Confluence Tracker

Open **Confluence Tracker** on the OWL home screen, or `/confluence-tracker/`.
It shares Bookmark Manager's Confluence connection. Add a root page URL or numeric
page ID. Each scan discovers all descendant IDs first (including pagination and
the direct-child fallback), then downloads page content and metadata sequentially.
The initial scan establishes the baseline. Subsequent scans flag new, changed,
returned, or no-longer-listed pages, retaining cached content and change history.
An absent page may have moved or become inaccessible; it is not treated as deleted.

Each root syncs automatically once every 24 hours after a successful run. A failed
or incomplete run retries after two hours, repeatedly until successful. The next
daily run is then scheduled 24 hours after that success. SQLite stores the schedule
and a worker lease prevents overlapping runs. **Check now** starts an explicit
manual check. OWL's backend must be running; overdue work resumes on startup.

The sidebar filters by page subtree. The list supports search, unreviewed changes,
updated/created/detected date selection, relative ranges, month/year archives, and
inclusive custom date ranges in the browser's local timezone. Page titles open
Confluence in a new tab; open counts record clicks through this app, not global
Confluence analytics. **Details** shows returned metadata, cached page text, the
text before the latest change, and the latest 50 change records. Review clears
only changes already received by the browser. Notifications are in-app badges.

Tracker validation uses isolated databases and mocked Confluence responses. A live
corporate-server sync requires your network/VPN and configured connection.

### Bitbucket sync controls and loading

The sidebar shows PDF counts and indexed-PDF commit dates with icon tooltips.
Repository completion ticks are transient and displayed only during an active
sync. The shared last-pull timestamp advances after all repositories finish,
including a completed run with reported errors; stopping does not advance it.

Pause holds the next upstream request or PDF operation after the current operation
finishes. Resume continues the same job. Stop saves its existing recovery checkpoint.
Paused time is excluded from timing measurements. A restart converts a paused job
into an interrupted, resumable job.

`repository_sync_timings` persists the sum and count of successful normal sync
durations per repository, including discovery. Hard retries and partial-file
recovery runs do not skew these averages. The total ETA combines those averages
with observed progress and completed repository durations in the current run.
It shows “calculating” until enough information is available and is an estimate,
especially when the number of changed PDFs or network speed varies.

The library and home overview initially load at most 200 PDFs committed during
the current UTC month, retaining complete repository totals. The remaining PDF
metadata loads in background batches of 1,000 with ID deduplication; older records
remain available after loading. A visible message identifies a partial library.
The hard-retry entry uses a lock icon and still requires typing HARD RETRY in its
confirmation dialog. The separate retry-failed toolbar shortcut is removed.

### NAAS Update

Open `/naas/` from OWL Home to track `.yaml`, `.yml`, and README files (`README`, `.md`, `.markdown`, `.rst`, `.txt`, `.adoc`, case insensitive). Add Bitbucket repository or individual file URLs. NAAS copies the PDF explorer interface and uses the same incremental sync, search, notes, metadata, commit/version downloads, filters, pause/resume, ETA and retry controls. YAML is indexed as text, never executed. UTF-8 and UTF-16 text are supported.

NAAS has its own database (`owl-naas.db` beside `owl.db`, overridable with `OWL_NAAS_DB_PATH`), jobs and browser preferences. It initially uses the saved Bitbucket connection; saving NAAS settings creates an independent encrypted connection under the config directory's `naas/` folder. All-repository sync and hard retry stay within the repositories added to NAAS. Empty repositories remain tracked for future additions. No local Git checkout is created.

### Automatic Bitbucket pulls

The PDF explorer checks for a scheduled pull every minute while OWL is running. The next regular run is three Monday–Friday days after a completed scheduled pull (weekdays counted in UTC). On first setup it uses the last completed pull date, or waits three weekdays if no pull exists. Each automatic attempt tests the configured connection before starting. Failed connection checks, partial failures, interrupted jobs and cancellations retry after two hours until a complete success. The schedule and current job are stored in SQLite, survive restart, and never overlap an active manual pull. There is no scheduler for NAAS in this change.

Scheduled pulls update progress quietly and do not reload the document table or change the current selection. Refresh after completion to see newly indexed records. Both manual and scheduled Bitbucket pulls now test the connection first. NAAS and Bitbucket imports accept HTTPS clone URLs such as `/stash/scm/KEY/repository.git`, converting them to the same-server browser repository address without creating a local checkout.
