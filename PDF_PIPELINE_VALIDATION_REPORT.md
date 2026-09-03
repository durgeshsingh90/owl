# Adaptive PDF pipeline validation report

Date: 2026-09-03
Work prompt: `work_prompts/011_ADAPTIVE_PDF_PIPELINE_IMPLEMENTATION_PROMPT.md`
Active controller mode: `observe`
Release result: **PASS for fixed/observe/shadow; adaptive admission BLOCKED by its gate**

## Delivered scope

Work prompt 011 is implemented through Phase 8:

1. the original isolated-parser, durable-stage, single-publisher, last-published-search
   architecture remains the data-integrity boundary;
2. one supervisor-owned, bounded telemetry series exposes durable run, flow, queue,
   publisher, resource, fairness, recovery, and ETA evidence without per-sample SQLite
   writes;
3. the top bar, repository cards, Home, and Repository Logs consume one versioned
   server-authoritative contract and distinguish queued, confirmed active, terminal,
   stale, unavailable, retry, and pause states;
4. Settings now has addressable Overview, Confluence, Repository sources, and Bookmark
   data sections, with exact-origin trusted hosts kept distinct from credentials and full
   clone URLs;
5. classified component recovery has durable episodes, correlated failures, bounded
   backoff, a 25-attempt default circuit, a separately defined zero threshold, stable
   history, deduplicated notification/popup delivery, and generation-safe Resume;
6. measured low-risk efficiency changes include work-conserving locality, parent-to-child
   fingerprint handoff, an idle publisher backoff, process-boundary connection cleanup,
   and removal of a duplicate page index; unproven batch/WAL/strict-locality changes were
   not enabled;
7. fixed targets 1, 2, 4, 6, and 8 were characterized, and the deterministic shadow
   controller records recommendations, evidence, expected effects, cooldowns, and later
   outcomes without changing admission;
8. bounded adaptive admission is implemented behind an explicit opt-in and a compatible
   passing manifest. Because the representative gate failed, no enablement manifest was
   created and Phase 7 correctly remains inactive.

Migrations 0018–0021 add trusted-host provenance and recovery/tuning evidence and remove
the redundant PDF-page index without weakening existing uniqueness. Git repository
worker concurrency, clone/pull ordering, daily scheduling, read-only transport, and
checkout locks were not changed.

The authoritative PIPE-007 and PIPE-009 wording and the matching CJ-019/PIPE-T09 entries
were aligned with the implemented four-section Settings design and the specified
`fixed`/`observe`/`shadow`/`adaptive` controller modes. Stable IDs were preserved.

## Metrics and control contract

- The private endpoint returns `schemaVersion: 1`, `Cache-Control: no-store`, current-run
  identity, typed samples, series/freshness metadata, null availability reasons, and
  bounded tuning/recovery history. Loopback authorization and redaction tests pass.
- `Extracted/min` increments only after atomic durable staging; `Written/min` increments
  only after publication commit; cache reuse is separate. The common default window is
  60 seconds: under 30 seconds is warming, 30–59 seconds needs three events for a partial
  rate, and a complete window reports even low sample counts with confidence.
- ETA uses durable remaining work, inventory coverage, recent weighted flow, overlapping
  extraction/publication constraints, confidence/range, and freshness. It never divides
  by worker count or sums overlapping repository ETAs. Warming, paused, stale, blocked,
  cancelled, error, and unavailable states remain named rather than becoming false zero.
- Requested, configured, tested, resource-aware, safety, and effective targets remain
  distinct. The 80-percent schedulable-CPU value is an upper shared background budget,
  reduced for semantic/Git/publisher reservations and resource/foreground guardrails.
  It is never a utilization target.
- A kill switch, manual fixed override, failed/missing manifest, stale required signal,
  recovery hazard, or unsupported bound fails back to conservative admission.

The complete operator contract, configuration map, troubleshooting procedure, and
rollback path are in `PDF_PIPELINE_OPERATIONS.md`. Reproducible benchmark method and
evidence are in `PDF_PIPELINE_BENCHMARKS.md`.

## Benchmark evidence and adaptive gate

All trials used generated PDFs and fresh disposable data roots/databases. Raw JSON lives
under ignored `var/benchmarks/reports` and is not tracked.

Fixed-matrix report `20260903T205858Z-ecb34ac67997` ran three repetitions per target with
64 PDFs, eight repositories, three pages per PDF, at most two parsers per repository,
and concurrent exact-search/dashboard probes:

| Fixed target | Median persisted PDFs/min | CV |
|---:|---:|---:|
| 1 | 474.58 | 1.07% |
| 2 | 838.32 | 0.60% |
| 4 | 1,292.26 | 1.42% |
| 6 | 1,383.96 | 2.50% |
| 8 | 1,622.02 | 1.71% |

All 15 trials passed terminal and persisted-document integrity. Foreground probe
availability was 100 percent, exact-search p95 was approximately 3.4–22.0 ms,
dashboard-payload p95 was approximately 42.0–63.7 ms, SQLite recorded no busy errors,
and lock-wait p95 stayed below 0.30 ms. The matched metrics-on run differed by 1.52
percent, within combined observed variance.

The adaptive gate is **BLOCKED** because this calibration is not the required roughly
20,000–25,000 PDF / 50 GB representative workload, normal semantic-model concurrency and
controlled timeout/recovery load were not included, thermal state was unavailable, and
eight-worker ETA MAPE was approximately 49–56 percent. These omissions are recorded in
the reports; they are not replaced by inference. The smallest safe next action is a
scheduled disposable representative run using the existing harness, followed by shadow
replay and a new gate evaluation.

## Visible UI validation

The supported launcher ran against a synthetic isolated data root. No live external
host was contacted and the canonical OWL database was not opened.

| Visible checkpoint | Result |
|---|---|
| Home with no work | PASS — compact Pipeline health rendered truthful idle state. |
| Bitbucket Search with zero repositories/current run | PASS — activity control was absent, non-focusable, and had no active resource; Test connection and Add repository remained available. |
| Queued repository | PASS — top bar and card showed Added to queue / Waiting in queue, not running. |
| Confirmed live work | PASS — a current owned sync process with fresh evidence showed Checking connection and Worker responding. |
| Clean completion | PASS — card showed the green PDF indexing complete state. |
| Partial failure | PASS — card and top bar showed Completed with errors using a non-green state. |
| Browser close/reopen | PASS — the queued durable run reconstructed in a new tab without client state. |
| Supported OWL stop/restart | PASS — durable recovery/pause state reconstructed; no claim was made that work ran while the process was stopped. |
| Repository Logs | PASS — current cards, capacity/state and flow/backlog panels, units, titles, legends, ETA diagnostics, and recovery/tuning empty states rendered. |
| Settings information architecture | PASS — exact four-section navigation, compact Confluence and Bookmark data tasks, no Bookmark Library sidebar, and no implicit scheduling POST. |
| UI-managed custom host | PASS — `https://SCM.UI-VALIDATION.invalid:8443/` normalized to its exact origin and showed Approved — not yet verified; repository/run counts stayed zero. |
| Desktop and narrow layout | PASS — Settings and recovery dialog had no horizontal overflow at 420 by 900. |
| Recovery popup accessibility | PASS — one `alertdialog`, initial focus on Resume, inert redacted facts, visible heading, scope/reason/details/dismiss, and generation-bound action. |

The visible pass found two real CSS defects and fixed them: Chromium treated the sticky
header's backdrop filter as the containing block for the fixed recovery overlay, and the
dialog heading inherited a low-contrast shell color. The final 1280 by 720 measurement
placed the full dialog between 119.8 and 600.2 pixels, initial focus remained on Resume,
and the heading rendered light ink on the dark dialog. A regression test now protects
both rules and the cache-busted stylesheet version.

The exhaustive state truth tables for actionable/unavailable idle, submission,
staged-without-writer, retry wait, recovering, failed/out-of-order polls, back/forward
cache restoration, reduced motion, ETA variants, source blocking, backpressure,
publisher starvation/limitation, and tuning/recovery events are covered by deterministic
Python and real-DOM JavaScript tests. They were not each saved as separate screen-level
captures in this pass; no screenshots were added to the public repository.

## Automated validation

The repository-native gate ran from the unchanged `scripts/check.sh`. A disposable Git
index was scoped only to its index-authoritative safety scan so the unstaged delivery
could be checked without altering the user's real staging area; the real index remained
clean and synthetic Git integration tests used their own normal indexes.

| Check | Result |
|---|---|
| Locked dependency resolution/sync | PASS — 56 packages resolved, 50 checked |
| Public repository safety | PASS — 385 tracked/untracked public candidates |
| Ruff formatting | PASS — 280 files |
| Ruff lint | PASS |
| Django system check | PASS — no issues |
| Migration drift | PASS — no changes detected |
| Python suite | PASS — 2,712 passed, 1 native-Windows-only skip |
| Branch-aware coverage | PASS — 83.8% across 23,477 statements |
| JavaScript suite | PASS — 144 passed |
| Git whitespace | PASS |

The focused Phase 8 UI/recovery/settings/configuration group passed 42 tests, and the
focused metrics group passed 32 tests plus its 13 JavaScript renderer tests before the
complete gate. No generated database, PDF, screenshot, secret, or benchmark corpus is
tracked. The 1.3 MB synthetic UI root was moved to Trash after validation and is
recoverable there; ignored benchmark JSON was intentionally retained.

## Stable traceability

| Journey/test ID | Status | Evidence |
|---|---|---|
| PIPE-T01 | PASS | Isolated parser, atomic staging, controlled publisher, search continuity, process-boundary connections, and crash tests pass. |
| PIPE-T02 | PASS | Durable acceptance/membership/lifecycle/terminal precedence and browser/process restart reconstruction pass. |
| PIPE-T03 | PASS | Once-only stage, commit, cache-reuse, retry, and reconstructed counters pass. |
| PIPE-T04 | PASS | Deterministic fresh/stale/unavailable demand, worker, publisher, queue, constraint, and recovery classification passes. |
| PIPE-T05 | PASS | Rate/ETA boundary math, critical-path replay, named states, and completed-run error recording pass; accuracy is insufficient only for adaptive enablement. |
| PIPE-T06 | PASS | Shared payload, top bar/cards/Home/Logs, accessibility, responsive layout, active-resource detachment, race handling, and reduced-motion tests pass; core states also passed visible review. |
| PIPE-T07 | PASS | Four Settings sections, observation-only GET/navigation, host provenance/normalization/precedence, credential scope, outbound revalidation, conflict-safe removal, and synthetic browser journey pass. |
| PIPE-T08 | PASS | Recovery classifications/scopes, bounded persisted backoff, exact threshold, zero semantics, correlation, fallback, popup/history deduplication, blocked/idempotent resume, half-open stability, and restart tests pass. |
| PIPE-T09 | PASS | One-owner fixed/observe/shadow/adaptive contracts, separate bounds, manifest compatibility, hysteresis/cooldown, replay determinism, kill switch, and fallback pass. |
| PIPE-T10 | PASS | Shared 80-percent CPU ceiling and CPU/memory/disk/SQLite/error/foreground/missing-signal guardrails pass deterministic tests. |
| PIPE-T11 | BLOCKED | Repeated isolated fixed matrix and one-variable experiments pass, but the representative scale, semantic/recovery load, thermal evidence, adaptive comparison, and ETA accuracy gate do not. |
| PIPE-T12 | PASS | Browser close/reopen and OWL stop/restart truth, loopback/redaction, unchanged Git policy, and safe observe/shadow fallback pass. |
| CJ-019 | BLOCKED | Steps 1–7 and shadow/gating behavior pass; the overall journey remains blocked at its representative benchmark/adaptive-enablement checkpoint. |

## Release recommendation

Release the durable pipeline/run model, telemetry, trusted-host Settings workflow,
recovery, dashboard, measured efficiency changes, fixed-target support, and shadow
controller with the default `observe` mode. Keep
`PDF_PIPELINE_ADAPTIVE_ENABLED=false`. Do not describe this build as automatically
adaptive until every PIPE-T11 representative gate passes and a compatible enablement
manifest is deliberately installed.
