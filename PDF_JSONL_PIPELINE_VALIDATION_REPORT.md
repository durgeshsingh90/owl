# PDF JSONL staging pipeline validation report

Date: 2026-09-04
Source prompt: attached extraction to staging to SQLite implementation request
Release result: **PASS**

## Delivered architecture

The PDF data path is now:

`PDFs -> 16 configurable extractor processes -> one JSONL stager -> size-only chunks -> one SQLite writer`

- Extractors write validated text to private, atomic per-PDF spool files. They do not
  publish extracted content, page rows, revisions, or FTS rows to SQLite. The existing
  SQLite job claims, heartbeats, counters, and durable failure records remain the small
  compatibility control plane.
- A process lock enforces one JSONL stager. It writes one complete UTF-8 object per PDF to
  `current.jsonl`, including `file_path`, `file_name`, `content`, and the existing
  authenticated extraction manifest.
- Rotation happens only when the configured byte target is reached. The default is
  52,428,800 bytes (50 MiB). There is no record-count or time-based rotation.
- Rotation fsyncs the open file, atomically renames it to `chunk_NNNNNN.jsonl`, fsyncs
  the directory, immediately creates and fsyncs a new `current.jsonl`, and atomically
  writes the chunk's lifecycle sidecar.
- The stager seals a non-empty tail when extraction has ended. Extraction admission is
  independent of sealed-chunk depth and SQLite state.
- A separate process lock enforces exactly one SQLite chunk writer. It reads each JSONL
  file incrementally, retaining the existing per-PDF atomic transaction, bulk page
  inserts, revision reuse, signals, and FTS behavior. This keeps SQLite lock duration
  bounded while preserving existing indexing semantics.

No new runtime dependency or database migration was needed.

## Durability, replay, and retention

Every extractor spool is fsynced before its job is handed to the stager. Every JSONL line
is fsynced before that spool is removed. On restart, `current.jsonl` keeps all complete
lines and truncates only a provably incomplete final line. A malformed complete line is
preserved and rejected instead of being silently discarded.

Sealed chunks have explicit `SEALED`, `IMPORTING`, `IMPORTED`, or `FAILED` state. A
missing sidecar after a rotation crash is reconstructed by incrementally validating the
sealed file. Invalid data or metadata is retained as `FAILED`.

A chunk changes to `IMPORTED` only after every record has completed its SQLite commit.
If a process dies after some commits but before that state update, replay verifies and
skips the already committed revisions, then continues unfinished records. Existing
database uniqueness plus full revision/page verification prevents duplicate publication.

`imported_at` is written only with `IMPORTED`. Startup cleanup removes the chunk data
first and its sidecar second only when `imported_at + PDF_JSONL_RETENTION_DAYS` has passed.
It never removes `current.jsonl`, `SEALED`, `IMPORTING`, `FAILED`, uncommitted, or
unverifiable chunks. The default retention is seven days.

## Configuration and supervision

| Setting | Default | Purpose |
|---|---:|---|
| `PDF_MAX_EXTRACTION_WORKERS` | `16` | Configurable extractor process count; defensively capped at 32. |
| `PDF_JSONL_CHUNK_SIZE_BYTES` | `52428800` | Size-only JSONL rotation target; capped at 1 GiB. |
| `PDF_JSONL_STAGING_DIRECTORY` | empty | Uses the private `BITBUCKET_TEMP_ROOT/pdf-publication` directory. |
| `PDF_JSONL_RETENTION_DAYS` | `7` | Time from successful import until cleanup eligibility. |

`run_owl` now supervises one `pdf-stager-1` and one `pdf-writer-1` alongside the 16
extractors. Detached reindex/sync paths launch both roles as well, and process locks make
duplicate launches harmless. Stager recovery has its own durable recovery scope.

The pre-existing adaptive controller remains disabled by default. SQLite busy errors and
JSONL backlog now produce a hold decision and never reduce extraction concurrency.

## Dashboard contract

The existing metrics endpoint and Repository Logs dashboard now expose:

- discovered, pending, currently extracting, successfully extracted, failed, and
  successfully indexed PDF counts;
- active, configured, and maximum extraction workers;
- PDF, extracted-page, persisted-page, and SQLite publication throughput;
- `current.jsonl` bytes, incoming records, sealed/total queued chunks, queued bytes,
  writer state, and current chunk;
- retained/failed chunks, retained bytes, and oldest/next cleanup eligibility; and
- explicit `SQLite backlog`, `Extraction starving SQLite`, `Balanced`, and `Idle` flow
  diagnoses.

Legacy staged-job gauges remain in the historical graph for API compatibility, but are
labelled as legacy and do not control extraction.

## Validation evidence

The repository's canonical database was not used. Automated tests use isolated Django
test databases, temporary staging directories, and generated PDFs.

| Check | Result |
|---|---|
| Locked dependency resolution/sync | PASS - 56 packages resolved, 50 checked |
| Public repository safety | PASS - 390 indexed or untracked candidates |
| Focused JSONL/indexing/controller/dashboard Python group | PASS - 94 tests |
| Complete Python suite | PASS - 2,719 passed, 1 native-Windows-only skip |
| Branch-aware coverage | PASS - 83.5% across 24,039 statements |
| Dashboard JavaScript renderer | PASS - 13 tests |
| Django system check | PASS - no issues |
| Migration drift | PASS - no changes detected |
| Ruff formatting and repository-wide lint | PASS - 285 files formatted |
| Git whitespace check | PASS |

The focused tests cover UTF-8 line integrity, size-only rotation, tail sealing, incomplete
tail recovery, duplicate append recovery, missing/corrupt metadata, import state,
seven-day cleanup, current/queued retention, failed-chunk retention, commit-before-status
replay without duplicate publication, FTS-compatible publication, single process roles,
and non-throttling SQLite backlog behavior.

An isolated synthetic smoke report, `20260903T230920Z-980331a7a6e6`, ran the complete new
topology with 16 extractors, one stager, one SQLite writer, 32 generated PDFs, two
repositories, and two generated pages per PDF. All 32 jobs succeeded; persisted-document
integrity passed; SQLite recorded zero busy errors; runtime was 1.422 seconds, about
1,350 indexed PDFs/minute. This verifies functional 16-process execution, not production
capacity for the full 21 GB corpus.
