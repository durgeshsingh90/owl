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
