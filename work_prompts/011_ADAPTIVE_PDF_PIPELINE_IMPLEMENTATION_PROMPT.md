# 011 — Adaptive PDF pipeline implementation prompt

- Work-prompt order: 011
- Prepared: 2026-09-03
- Status: implementation prompt; no feature implementation is recorded here

## Role

Act as the senior engineer responsible for improving OWL's existing Bitbucket PDF
extraction/publication pipeline and the connected dashboard, repository setup, and
Settings experiences.

Work from the implementation that is already present. Do not treat this as a greenfield
queue or worker-system rewrite.

## Goal

Build an observability-first, benchmark-gated pipeline that can eventually adjust PDF
extraction concurrency automatically.

The optimization objective is:

> Maximize successfully persisted end-to-end PDF throughput while preserving data
> integrity, restart recovery, bounded resource use, exact-search availability, semantic
> indexing correctness, and responsive interactive use of OWL.

The objective is not maximum CPU use, the largest possible worker count, or the largest
possible extraction queue.

The user must be able to see, truthfully and at a glance:

1. the estimated wall-clock time until all repositories accepted into the current run
   finish, shown as approximate `HH:MM:SS` when it is supportable and as a truthful
   calculating/waiting/paused/unavailable state otherwise;
2. the current end-to-end activity—queued, cloning, pulling, discovering, extracting,
   writing, extracting and writing concurrently, backpressured, recovering, paused,
   complete, idle, or degraded—without making the user interpret worker internals;
3. the current rolling **Extracted/min** and **Written/min** rates, where written means
   successfully and durably published rather than merely staged;
4. which repositories were added to the run queue, which are active, each active
   repository's remaining-versus-total PDFs and ETA, and which completed successfully;
5. in the detailed diagnostic graph, how many extraction slots are live, working,
   genuinely free, waiting for eligible input, throttled, recovery-paused, or unavailable;
6. whether the PDF publisher is busy, waiting for extraction output, blocked, or idle
   because there is no demand;
7. whether extraction, PDF publication, SQLite contention, CPU, memory, disk, repository
   eligibility, or another OWL workload is constraining progress;
8. whether durable staged output is accumulating, how old and how large it is, and when
   backpressure is active;
9. the measured reason for every automatic tuning recommendation or change;
10. when a worker or publisher stopped, what OWL retried, how long until the next retry,
    and whether recovery is still progressing;
11. when repeated recovery failed enough times that OWL paused the smallest affected
    scope, notified the user with an in-app popup, and offered a safe **Resume** action;
12. that closing the browser tab or portal window does not stop accepted background work,
    while honestly distinguishing that from stopping OWL itself or shutting down the
    laptop;
13. a professional, uncluttered shared **Settings** experience where connection summaries
    are visible before forms, advanced help appears only when requested, and a user can
    explicitly add a trusted repository-host URL without editing `.env` or restarting
    OWL when repository-host policy is not managed externally.

The primary presentation hierarchy is **time, progress, flow, and current activity
first**. Worker counts remain available in Repository Logs and the free/waiting/starved
diagnostic graph, but must not be the main top-bar or repository-card message.

Implement the work incrementally. Observability, classification, fixed-concurrency
benchmarks, and shadow recommendations are hard gates before adaptive mode may control
live extraction admission.

## Authoritative references

Read these files completely before editing:

1. `work_prompts/001_OWL_MASTER_REQUIREMENTS.md`
2. `work_prompts/002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md`
3. `work_prompts/003_CODEX_IMPLEMENTATION_PROMPT.md`

This prompt adds a focused implementation contract. The master requirements remain
authoritative for product, security, performance, and reliability behavior. The
feature-test/customer-journey document remains authoritative for validation execution.
If this work adds product behavior or acceptance criteria not already represented there,
update both authoritative documents together and assign non-conflicting stable test and
journey IDs.

At minimum, inspect the current implementation and its focused tests around:

- `bookmark_manager/management/commands/run_owl.py`
- `bitbucket_search/management/commands/bitbucket_pdf_writer.py`
- `bitbucket_search/services/pdf_indexing.py`
- `bitbucket_search/services/pdf_extractor.py`
- `bitbucket_search/services/pdf_catalog.py`
- `semantic_search/services/jobs.py`
- `semantic_search/services/provider.py`
- `semantic_search/signals.py`
- `bitbucket_search/models.py`
- `bitbucket_search/forms.py`
- `bitbucket_search/views.py`
- `bitbucket_search/urls.py`
- `bitbucket_search/services/repository_urls.py`
- `bitbucket_search/services/https_credentials.py`
- `bookmark_manager/forms.py`
- `bookmark_manager/views.py`
- `bookmark_manager/urls.py`
- `core/middleware.py`
- `owl/settings.py`
- `templates/bookmark_manager/settings.html`
- `templates/bookmark_manager/_settings_panel.html`
- `templates/bookmark_manager/_app_sidebar.html`
- `static/bookmark_manager/bookmarks.css`
- `static/bookmark_manager/bookmarks.js`
- `templates/core/dashboard.html`
- `templates/bitbucket_search/index.html`
- `templates/bitbucket_search/_repository_list.html`
- `templates/bitbucket_search/status.html`
- `static/bitbucket_search/bitbucket_search.js`
- the Bitbucket Search styles and local icon/GIF assets used by those templates;
- PDF indexing, parallel-worker, restart-recovery, cancellation, status, search,
  supervisor, semantic-index, Settings, repository-URL, allowed-host, and HTTPS-credential
  tests under `tests/`

## Verified starting point to recheck

The following is a source and local-runtime snapshot from 2026-09-03. Recheck it before
implementation because code, environment settings, process state, and local data can
change.

- OWL already has parallel isolated PDF extraction controllers.
- Extractor output is written to a private disk-backed staging file, flushed with
  `fsync`, and atomically renamed before becoming publishable.
- A dedicated `bitbucket_pdf_writer` process publishes heavy PDF text and FTS data to
  SQLite one staged PDF at a time.
- Publication is already transactional, and page rows are already inserted with Django
  bulk operations in batches of 100.
- Restart recovery, leases, repository cancellation, checkout locks, and staged-file
  cleanup are deliberate parts of the current safety model.
- New extraction claims already stop at a configured staged-publication high-water mark.
  That mark is admission backpressure, not a strict queue capacity: extractors already
  running can finish and temporarily take backpressure depth above the threshold.
- The source default was four PDF extractors with a supported cap of eight. The ignored
  local environment requested six extractors, a staged-publication threshold of four,
  one active extraction repository, up to six extractors for that repository, two
  semantic workers, and four Git workers. Do not commit or overwrite local environment
  files, and do not assume these values are still current.
- The laptop was described approximately as a 20-CPU, 64-GB machine. The operating
  system inspection reported 18 schedulable CPUs and 64 GB RAM. Detect actual available
  resources at runtime and degrade safely when a signal is unavailable; do not hard-code
  either number.
- The inspected live database had only one successful extraction sample: 876 pages in
  about 3.21 seconds. It is not representative evidence for a 20,000-to-25,000-PDF,
  roughly 50-GB workload and must not be used to select concurrency or claim a speedup.
- The current status snapshot combines multiple job phases and cannot truthfully derive
  live parser utilization, free slots, publisher starvation, or a time series. Its
  repository aggregation can also describe every current `RUNNING` row as extracting
  even though durable job phases include validation, hashing, extraction, and
  publication. Phase-aware telemetry is required before changing the label.
- The Bitbucket Search refresh toolbar currently renders `Workers N running / M total`,
  and its progress display blends Git and indexing percentages using presentation
  weights. Repository activity can render `N PDFs extracting`, while repository cards
  separately render a remaining count. The current status payload does not provide a
  defensible total ETA, per-repository ETA, or rolling extracted/written rates. Replace
  those user-facing summaries under the contract in this prompt; do not relabel the
  existing counts or weighted percentage as an ETA.
- The refresh/activity icon beside **Test connection** is currently untruthful at idle.
  The server template always emits an `indexing.gif` as its default/"idle" visual and a
  second `work-in-progress.gif` for the active state. CSS shows the first animated GIF by
  default and merely swaps GIFs under `bb-refresh-all--active`; the disabled zero-repository
  state only reduces opacity. The browser code does not hide either image, treats the
  existence of the default image as the overall visual, and maps submission/status-check
  pending as well as actual work into the same active class. Current aggregate work flags
  can also conflate queued work, stale sync markers, and staged `PUBLISHING` rows without
  a live writer lease with executing work. Consequently the screenshot state—zero
  repositories, zero PDFs, and zero running workers—still displays an animated indexing
  icon. At this snapshot that idle asset is an approximately 1.6-MB, 168-frame,
  infinitely looping GIF, so it is also needless decoding/work at idle. A failed status
  poll or back/forward-cache restore can temporarily select the running artwork without
  confirmed work. Existing reduced-motion CSS cannot stop animation internal to a GIF.
  Replace this behavior using the explicit top-bar indicator contract below; do not fix
  it with copy or opacity alone.
- Repository cards already contain operation-specific artwork and success-tick state,
  but queued PDF work does not yet have the explicit **Added to queue** lifecycle and
  distinct waiting presentation required here. Reuse appropriate local visual language,
  but verify the completed tick against end-to-end publication state rather than assuming
  its existing meaning is sufficient.
- Interrupted PDF jobs already use durable leases and a small bounded per-document
  automatic retry count (source default: two). The supervisor also relaunches exited
  resident workers. Those are useful foundations, but they are not a persistent
  component-level circuit breaker: repeated resident-process failure can currently
  relaunch indefinitely.
- OWL already mounts a durable shared notification center with periodic polling. Some
  repository-status popover code/templates also exist but were not mounted in the
  production shell at this snapshot; do not claim that surface is active without
  rechecking it. Reuse the mounted notification foundation for recovery notifications.
  The existing retired per-document `resume` route and repository **Stop indexing**
  cancellation are not pipeline pause/resume and must not be presented as such.
- The PDF publisher is not SQLite itself. Repository catalogue publication, semantic
  publication, job claims and heartbeats, web actions, and other short operations can
  also write to SQLite.
- Semantic work is queued after PDF publication and consumes its own CPU, memory, and
  SQLite write budget. It must be included in system-headroom and contention decisions.
- Refresh and indexing requests are intended to enqueue durable work and return while
  helper/supervisor processes continue independently of the page. Re-prove this behavior
  for the supported OWL launcher: closing or navigating away from the browser must not
  cancel accepted work, and reopening the portal must reconstruct current state from
  durable records. The supported launcher currently has an optional/default keep-awake
  path while durable work exists; verify it without promising that a stopped process or
  sleeping/powered-off laptop continues computing. Rely on durable restart recovery and
  say so plainly.
- The dedicated Settings route is currently titled **Confluence Settings** and remains
  inside the Bookmark Manager shell even though it also contains repository credentials
  and bookmark data. It renders the Bookmark Library browse/domain sidebar beside one
  long shared panel containing an always-expanded Confluence form, an always-expanded
  four-field repository-credential form, import/export controls, and then the Confluence
  removal danger action after unrelated bookmark data. The same shared partial puts the
  two connection forms into a 560-pixel full-height drawer, with repeated Cancel actions.
  The live desktop inspection confirmed excessive vertical scanning, weak separation,
  misleading page/status naming, and substantial unused horizontal space. Treat this as
  an information-architecture problem, not a request for smaller fonts or cosmetic CSS.
- A live visual inspection also exposed that opening Settings runs existing
  global browser code which POSTs the Bookmark and repository schedule-tick endpoints.
  In the observed case, the page visit queued a due repository retry and launched its
  worker. Settings navigation must be side-effect-free: it may read status, but it must
  not enqueue, retry, test, clone, pull, or wake workers. Scheduled catch-up belongs to
  the resident server-side scheduler required for browser-independent execution, not to
  a hidden form fired by visiting Settings.
- The existing Settings **Repository host** control is only a credential-origin dropdown;
  it cannot accept a new host. Its choices come from startup-only
  `BITBUCKET_ALLOWED_HOSTS` plus origins of repositories that were already registered.
  New repositories are added elsewhere using full clone URLs, but validation rejects a
  custom hostname until the user edits `.env` and restarts OWL. This creates a circular
  setup problem for a new internal host or custom HTTPS port. Add a distinct durable
  trusted-host workflow under the contract below; do not overload a credential record as
  host authorization.
- Preserve the existing repository safety boundary while removing that setup friction.
  Repository clone URLs are currently credential-free, canonicalized and deduplicated;
  only SSH/HTTPS are accepted; SSH uses the `git` user; host matching is exact rather than
  suffix/wildcard based; and unsafe paths, userinfo, queries, fragments, and unapproved
  hosts are rejected without echoing secrets. HTTPS credentials are scoped to an exact
  scheme/hostname/effective-port origin, stored encrypted or in the OS credential store,
  and never rendered after save or after an invalid POST. Confluence's externally managed
  read-only behavior, Settings no-JavaScript fallback, focus handling, and Bookmark
  **Export JSON**/**Import bookmarks** workflows must also survive the redesign.
- Git clone/pull scheduling is accepted as-is for this scope. At this snapshot the source
  default permits four repository controllers, so Git is not globally serial even though
  each repository admits only one active Git job. Recheck effective runtime configuration
  and report the truth, but do not optimize, parallelize, or serialize Git synchronization
  merely to satisfy this PDF/dashboard work. ETA logic must model the observed Git
  scheduling rather than assume an order.
- Exact PDF search already uses SQLite FTS5. Search must remain available while indexing,
  and a new search technology is not part of this work.

## Architectural decision

Keep the existing durable architecture:

```text
PDF jobs
   |
   v
parallel isolated extraction controllers
   |
   v
durable disk-backed staged results + admission backpressure
   |
   v
one coordinated PDF publisher
   |
   v
SQLite models + FTS5
```

Add metrics, truthful state classification, an ETA-first top bar and repository
lifecycle, graphs, benchmark tooling, a durable retry/circuit-breaker control plane,
and—only after the gates in this prompt pass—a conservative admission controller around
that design.

Do not replace durable staging with an in-memory queue. Do not route every claim,
heartbeat, or small status write through the PDF publisher. Do not allow several heavy
PDF publishers to compete for SQLite. Do not terminate an in-flight parser merely to
reduce the target worker count.

Prefer a fixed, bounded resident controller pool with a dynamically controlled claim or
admission target. A different design is acceptable only if it preserves in-flight work,
lease recovery, cancellation, restart safety, and the measured low-overhead requirement,
and is justified with evidence.

Automatic recovery and automatic concurrency tuning are separate concerns. Recovery may
restart a failed component or reclaim a stale lease while the admission target stays
unchanged. Adaptive tuning must never interpret a recovery pause as healthy idle capacity
or try to compensate for a crashing component by starting more workers.

Treat 80 percent of detected schedulable CPU capacity as the user's desired upper
background-work budget, not as a promise to keep CPU at 80 percent, a minimum worker
count, or permission to ignore the pipeline bottleneck. The controller may approach that
budget only when representative benchmarks and live guardrails show that extra admission
improves durable end-to-end throughput. It must reduce below it for publication, SQLite,
memory, disk, thermal, foreground-latency, error, or recovery constraints.

## Before editing

1. Read the authoritative references and the relevant implementation/tests completely.
2. Inspect `AGENTS.md` instructions, the current branch, and the dirty worktree. Preserve
   all user-owned and unrelated changes.
3. Trace job creation, eligibility, claim, extraction, staging, writer claim, publication,
   semantic enqueue, completion, retry, interruption, cancellation, and recovery.
4. Trace every SQLite-writing path that can overlap PDF publication. Do not infer total
   database activity from the PDF publisher alone.
5. Record the effective settings and process topology without printing secrets or
   modifying ignored environment profiles.
6. Trace the full Settings page and drawer, Confluence/credential/import/export actions,
   repository-host derivation, clone-URL validation, all outbound Git entrypoints, and
   environment-versus-UI policy provenance before changing their shared contract.
7. Establish a reproducible fixed-concurrency baseline before changing scheduling,
   SQLite pragmas, indexes, batch sizes, hashing, polling, or controller behavior.
8. Define exact metric numerators, denominators, clocks, sampling windows, stale cutoffs,
   and unavailable states before building charts or automatic decisions.
9. Produce a phased plan with a verification and rollback checkpoint for every phase.
10. Ask only when a missing choice would cause incompatible work, unsafe external access,
   destructive data handling, or an unapproved durability trade-off.

## Required terminology and invariants

Use these concepts consistently in code, API payloads, dashboard text, logs, tests, and
documentation. If internal field names differ, keep the public meaning identical.

| Term | Required meaning |
|---|---|
| Input queued | A PDF indexing job is queued, whether or not it is currently eligible to be claimed. |
| Repository added to queue | The repository was durably accepted into the current refresh/index run. Its Git work or PDF inventory may not have started, and its final PDF total may still be unknown. This state is not an active worker. |
| Repository active | The repository is currently cloning, pulling, discovering/cataloguing PDFs, extracting, writing, retrying, or completing another named phase. Once its current PDF inventory is known, show remaining and total end-to-end PDF counts. |
| Repository indexing complete | For the current repository revision/run, inventory is final, every accepted PDF succeeded through durable publication/cache attachment, and no cancelled, permanently failed, unresolved, queued, running, staged, or publishing work remains. This is the only state that receives the unqualified green completion tick. |
| Trusted repository host | One exact canonical hostname explicitly authorized by built-in policy, external configuration, or a durable local Settings action. Trust permits later validation of supported SSH/HTTPS clone URLs; it is not a credential and does not prove connectivity. |
| Repository host URL | The Settings input used to create a UI-managed trusted-host record. It is a credential-free HTTPS origin such as `https://scm.company.example:8443`, with no repository path, userinfo, query, or fragment. |
| Repository clone URL | A full credential-free SSH or HTTPS Git address containing the owner/project and repository path. It remains separate from the host URL and is what **Add repository** accepts. |
| HTTPS credential origin | The exact canonical `https://hostname:effective-port` scope to which one saved credential may be attached. It contains no repository path and must already belong to a trusted host. |
| Eligible input | A queued job can be claimed now after repository state, synchronization, checkout, cancellation, retry, and locality/admission rules are applied. |
| Active extractor | A live controller owns a fresh-heartbeat job in validation, hashing, or parsing/extraction work. A staged or publishing job is not an active extractor. |
| Free / idle with no demand | A live admitted extraction slot has no eligible work. This is healthy idle capacity, not starvation. |
| Waiting for eligible input | A healthy slot cannot claim queued work because repository synchronization, checkout, locality, retry timing, or another source rule makes it ineligible. |
| Backpressured | Eligible input exists, but new extraction claims are withheld because the durable-staging high-water rule is active. |
| Paused by controller | A live slot is above the controller's current admission target and will not claim another job. |
| Paused by recovery circuit | A configured slot or component is intentionally not relaunched/admitted because its recovery circuit reached the failure threshold. It is neither free nor unexpectedly unavailable. |
| Unavailable worker | An expected controller process is absent or its heartbeat is stale. Do not count it as free. |
| Recovery attempt | One deliberate restart/reclaim/relaunch operation followed by a stability check. Supervisor poll iterations and individual PDF parse failures are not component recovery attempts. |
| Recovery episode | A durable sequence of related recovery attempts for one failure scope and reason family, ending only after a defined healthy stability window or an explicit superseding event. |
| Retry wait | A transient failure was classified as retryable and the affected component is waiting until its persisted `nextRetryAt` time. |
| Recovery paused | The circuit is open after the configured consecutive failed-recovery threshold, or an immediate safety condition required a pause. Automatic relaunch/claim activity for the affected scope stops until safe resume. |
| Resume | A loopback-authorized, idempotent request to move the same paused episode into a new half-open probe generation from the last durable job/staging boundary. Resume is not cancellation, does not erase history, and does not grant another automatic 25-attempt budget. |
| Permanent item failure | One PDF is unsupported, corrupt, encrypted, unavailable, or otherwise non-retryable under existing policy. Isolate that item; do not spend the component-level recovery budget or pause healthy unrelated work. |
| Staged waiting | Extraction output is durable and publishable but is not currently owned by the PDF publisher. |
| Publication in flight | A staged result is owned by a fresh, live PDF publisher and its publication has not completed. |
| Publisher busy | The PDF publisher is loading, validating, performing ordinary sub-threshold lock acquisition, or transactionally publishing a staged result. Break those subphases out where measurable. |
| Publisher starved / awaiting input | The publisher is live and has no staged result while meaningful eligible or active upstream extraction demand exists for a sustained observation window. |
| Publisher idle with no demand | The publisher is live, there is no staged work, and no meaningful eligible or active upstream demand exists. This is not starvation. |
| Publisher blocked | A staged result exists but cannot be published because of repository synchronization, database-lock wait beyond the named threshold, retry backoff, or another explicit dependency. Use a reason such as `blocked_sqlite`; do not count the same instant as busy. |
| Publisher recovery paused | The publisher relaunch circuit is open and OWL is deliberately preserving staged output until resume. This is not idle, starved, or unexpectedly unavailable. |
| Publication limited | Backpressure depth, staged bytes, or oldest staged age rises while the publisher is continuously busy and extraction output exceeds successful publication. |
| SQLite contended | Measured database-lock wait or busy/locked errors cross a defined threshold. Publisher duty cycle alone does not prove this state. |
| Source blocked | Work is queued but repository/synchronization/checkout/locality conditions prevent enough jobs from becoming eligible. |
| Stalled / degraded | Backlog exists but expected progress or heartbeats are stale, or failures make the measurement unreliable. |
| Cache reuse completion | A revision attaches to reusable extracted content without passing through the normal staged-new-text publication path. Report this separately from publisher throughput. |
| Extracted/min | Successful normal extractor outputs that crossed the atomic durable-stage handoff during the named rolling window, normalized to one minute. It does not include failed parses, merely started jobs, cache reuse, or writer publication. |
| Written/min | Successful durable PDF publications committed during the named rolling window, normalized to one minute. It does not mean a staging file was written and does not include failed or rolled-back transactions. |
| Repository ETA | The forecast wall-clock duration until that repository's current accepted PDF work reaches explicit end-to-end terminal outcomes, based on known remaining work, pipeline overlap, observed rates, and current constraints. Success terminates at durable publication/cache attachment; permanent failure and cancellation are distinct terminal outcomes. It is unknown until evidence is sufficient. |
| Total ETA | The forecast wall-clock makespan until all repositories accepted into the current run reach explicit end-to-end terminal outcomes. It is not the sum of per-repository ETAs when work overlaps, and a terminal run with errors must not be presented as successful completion. |

There are two distinct queues:

1. database jobs waiting for extraction; and
2. durable extracted results waiting for publication.

Never display one as the other.

`PDF_MAX_STAGED_PUBLICATIONS` or its successor is a **backpressure threshold**, not a
hard queue capacity, unless strict reservations are deliberately implemented. With a
threshold `S` and `W` already-running extractors, backpressure depth can transiently approach
`S + W - 1`. Test and document the actual bound. Track staged bytes, oldest staged age,
and available disk space because the queue is disk-backed.

Define the exact admission numerator as:

```text
backpressureDepthJobs = stagedWaitingJobs + publicationInFlightJobs
```

This matches the current gate's inclusion of every `PUBLISHING` job, including the
writer-owned job. Use `backpressureDepthJobs` for graphs, classification, and controller
decisions. `stagedBytes` includes files represented by both categories for as long as
their staging files remain on disk; never invent bytes for an in-flight file already
removed by the safe publication lifecycle.

Worker states must be mutually exclusive at each sample. Enforce and test:

```text
live = active + idleNoDemand + waitingForEligibleInput
       + backpressured + pausedByController
admittedLive = active + idleNoDemand + waitingForEligibleInput + backpressured
expectedResident = live + pausedByRecovery + unavailable
```

A controller that is starting but has not produced a fresh valid heartbeat is not yet
live. Classify it as unavailable/warming according to the documented freshness rule; do
not count it as free. A slot deliberately suppressed by an open recovery circuit is
`pausedByRecovery`, not unavailable. While ordinary in-flight work drains, keep its slot
in its real active state; move it to `pausedByRecovery` only at the safe job boundary.

Publisher `busy`, `starved`, `idle_no_demand`, and `blocked` are mutually exclusive for
any live-time sample. Ordinary lock acquisition below the named lock-wait threshold is
busy; a wait beyond that threshold or retry backoff is blocked with the appropriate
reason. `paused_by_recovery` and `unavailable` are separately measured non-live states.
Test that each sample has one state and that time percentages partition their documented
denominator exactly.

## Pipeline state classification

Expose one primary state plus zero or more secondary constraints. This avoids pretending
that CPU pressure, SQLite contention, and repository blocking are mutually exclusive.

The stable primary state codes must cover at least:

- `idle`
- `warming_up`
- `recovering`
- `recovery_paused`
- `balanced`
- `extraction_limited`
- `publication_limited`
- `sqlite_contended`
- `backpressure`
- `cpu_limited`
- `memory_limited`
- `disk_limited`
- `source_blocked`
- `degraded`

Every state response must include:

- a stable machine-readable code;
- a short human label;
- a stable reason code;
- a concise evidence-based reason containing the relevant measurements and window;
- confidence such as `warming`, `low`, `medium`, or `high`;
- secondary constraint codes when more than one condition applies;
- the observation-window start/end or duration;
- `unknown`/`unavailable` rather than a fabricated zero when a required signal is absent.

The primary pipeline classifier and the recovery state machine are related but distinct.
Map recovery `retry_wait`, `recovering`, `resume_requested`, and
`recovering_half_open` to the primary `recovering` presentation with the exact recovery
substate preserved in the payload. Map recovery `paused` to primary `recovery_paused`.

State precedence must be explicit and tested. Safety and truthfulness take priority: for
example, an unavailable publisher with backlog is `degraded`, not `extraction_limited`;
queued but ineligible work is `source_blocked`, not publisher starvation; a growing
staged queue is publication-limited or blocked, not starved; and an open recovery circuit
is `recovery_paused`, not idle or source-blocked.

Do not classify from a single instantaneous sample. Use rolling measurements and minimum
evidence requirements. Classifiers must remain deterministic for the same sample series.

## User-facing run, activity, and completion contract

Model the visible refresh/index operation as a durable current run, not as a collection
of browser-local counters. Give the run a stable ID and record the repositories accepted
into it before expensive Git or PDF work starts. A successful **Refresh all** response
must immediately acknowledge every included repository as **Added to queue**, while each
card then renders its newest authoritative queued or active generation. If only part of
the batch was accepted, show the accepted and rejected repositories explicitly; never
imply that all were queued.

Expose a machine-readable global activity code and a user-facing label separately from
the primary health/constraint classifier. The activity contract must cover at least:

- `idle`;
- `queueing` while the submission itself is not yet durably accepted;
- `queued` after durable acceptance but before any repository is active;
- `checking_connection`, `cloning`, `pulling`, and `discovering` for Git/catalogue
  preparation;
- `validating` and `hashing` before ordinary extraction;
- `extracting`, `writing`, and `extracting_and_writing`;
- `reusing_cached` when cache attachment is the only current PDF progress;
- `backpressured` and `source_blocked`;
- `retry_wait`, `recovering`, and `paused`;
- `completing` for durable finalization after ordinary extraction has ended;
- `complete`, `completed_with_errors`, and `cancelled`.

Keep top-bar icon presentation separate from this global activity code. Expose a small,
explicit presentation state (`hidden`, `idle_actionable`, `idle_unavailable`,
`submitting`, `queued`, `running`, `retry_wait`, `recovering`, `paused`, `terminal`, or
`unknown`) plus the fresh evidence that justified it. A transport event such as starting
a status request is not a pipeline activity transition. The dashboard section below
defines the exact visual and accessibility behavior for every presentation state.

`degraded` and `unavailable` are health/availability overrides, not ordinary activity
codes. Preserve the underlying activity when exposing either override so diagnostics can
still say what work was in progress.

Extraction and publication normally overlap. When both have current measured activity,
the top bar must say **Extracting + writing** rather than selecting one arbitrarily. When
Git and PDF work overlap across repositories, preserve a primary activity plus typed
secondary activities, for example **Extracting + writing · 1 repository pulling**. A
run-blocking recovery pause, integrity hazard, or unavailable required component takes
presentation precedence without deleting the underlying activities from the detail
payload. A pause or failure limited to one repository/slot while healthy work continues
elsewhere remains a secondary status such as **1 repository paused**.

Make aggregation deterministic. A safety pause, active recovery, or degraded/unavailable
required run-wide component overrides the healthy activity label. Otherwise prefer the combined
`extracting_and_writing` state when fresh phase/heartbeat evidence shows both, then the
single writing, extracting, cache-reuse, hashing, validating, Git/discovery, queued,
completing, terminal, or idle state actually supported by phase telemetry. When the run
is terminal, select `cancelled`, then `completed_with_errors`, then clean `complete` using
the same precedence as ETA. Present
backpressure/source blocking as the primary activity only
when it is actually withholding progress; otherwise retain it as a secondary constraint.
If several Git phases are active across repositories, choose a documented stable summary
and expose each typed secondary activity/count rather than inventing one global phase.

For every repository in the current run, expose a durable lifecycle state, current
phase, run/revision identity, accepted time, activation time, last-progress time, known
PDF total, end-to-end completed count, remaining count, unresolved-failure count, and
terminal outcome. Counts must be scoped to the current repository revision/run, not mixed
with stale attempts from earlier runs.

Repository lifecycle buckets are mutually exclusive. Degraded/unavailable are orthogonal
health overlays and are not additional lifecycle buckets. Enforce:

```text
acceptedRepositories = queuedRepositories + activeRepositories + pausedRepositories
                       + completedRepositories + completedWithErrorsRepositories
                       + cancelledRepositories
```

A repository remains in the `active` lifecycle bucket while any safe in-flight
extraction or healthy publisher drain is still making forward progress, with the scoped
pause shown as a secondary health/status constraint. Move it to `paused` only when
unfinished current-run work is being deliberately withheld and no allowed drain remains
active. If the drain completes every item, classify the terminal outcome normally instead
of briefly forcing `paused`.

Define repository remaining as every accepted current-run PDF that has not yet reached a
successful end-to-end publication/cache-reuse outcome or another explicit terminal
outcome. A staged or publication-in-flight PDF remains outstanding; do not show zero
remaining merely because parsing ended. Once inventory is known, enforce and test:

```text
total = successful + permanentFailed + cancelled + remaining
remaining = queued + runningExtraction + stagedWaiting + publicationInFlight
            + retryWait + otherNonterminal
```

These are unique current-revision PDFs, not attempt-row totals. Select one authoritative
latest state per PDF so an interrupted/failed attempt followed by a retry cannot inflate
the total, remaining count, rates, progress, or ETA.

If discovery can add current-run PDFs after the first inventory, publish an explicit
`inventoryFinal` flag. Label totals as provisional until it is true and allow ETA to move
when the inventory changes. Do not force an apparently monotonic percentage by hiding
newly discovered work.

The primary repository and total ETA ends when all accepted work is terminal. A successful
PDF becomes terminal at durable publication/cache attachment, when exact text search can
use the result; permanent failure and cancellation remain explicitly different terminal
outcomes. Semantic indexing remains a separately visible downstream status/backlog and
must not be silently included in or excluded from an ETA; if a semantic-ready ETA is
later offered, label it separately.

The unqualified green completion tick means the repository's current revision/run is
fully PDF-indexed and durably exact-searchable. Require all of the following:

```text
inventoryFinal = true
successful = total
permanentFailed = cancelled = remaining = unresolvedFailures = 0
stagedWaiting = publicationInFlight = 0
```

Any `permanentFailed > 0` is unresolved for green-completion purposes unless a later,
separately specified user-resolution policy creates a new run/outcome; merely dismissing
an error cannot turn it green. Semantic backlog may be shown as a separate non-blocking
badge. Use a distinct amber
**Completed with errors** state when terminal failures remain, and distinct paused,
cancelled, stale, and failed states. Never award the green tick after extraction alone,
for an old revision while a new run is queued, or only because Git synchronization
succeeded.

## Metrics contract

Create a lightweight, versioned metrics contract. Exact implementation names may adapt
to existing conventions, but the endpoint and tests must expose equivalent information.

At minimum, provide:

### Current run and activity

- durable current run ID, accepted time, and last-progress time;
- accepted, queued, active, paused, completed, completed-with-errors, and cancelled
  mutually exclusive repository lifecycle counts satisfying the partition above;
- degraded and unavailable repository health-overlay counts, which may overlap lifecycle
  buckets and are never added into the accepted partition;
- global activity code, user-facing label, typed secondary activities, and evidence;
- known/final inventory coverage across the accepted repositories;
- current-run total, successful, unresolved-failure, cancelled, and remaining PDF counts;
- a clear distinction between no current run, a completed run, and a run whose telemetry
  owner is unavailable.

### Controller and workers

- controller mode: `fixed`, `observe`, `shadow`, or `adaptive`;
- configured minimum, `configuredPdfHardMax`, `testedPdfHardMax`, requested target,
  resource-aware ceiling, safety ceiling, effective admission target, stable limiting
  reason, and expected-resident/live process counts;
- live extraction controllers;
- admitted slots;
- active extractors;
- extractor occupancy percentage, defined as active divided by admitted capacity and
  shown alongside the underlying counts;
- idle-with-no-demand slots;
- waiting-for-eligible-input slots;
- backpressured slots;
- controller-paused slots;
- recovery-paused slots;
- unavailable or stale controllers;
- worker heartbeat freshness and aggregate reason without exposing process arguments or
  sensitive paths.

### PDF publisher and SQLite

- publisher expected/live/heartbeat state;
- publisher state: `busy`, `starved`, `idle_no_demand`, `blocked`,
  `paused_by_recovery`, or `unavailable`;
- publisher busy, awaiting-input, no-demand, and blocked percentages over the same named
  rolling window;
- time loading/validating staged data separately from actual SQLite transaction time
  where practical;
- SQLite transaction wall time;
- database lock-wait p50 and p95;
- database busy/locked error count and rate;
- successful and failed publication counts;
- other known OWL write workloads, including semantic work, represented in contention
  context where measurable.

Do not label PDF-publisher duty cycle as `SQLite utilization`. A truthful UI may display
`PDF publisher busy` and `SQLite lock wait` separately.

### Recovery circuit

- one exact persisted/API recovery-state enum: `healthy`, `retry_wait`, `recovering`,
  `paused`, `resume_requested`, or `recovering_half_open`;
- durable recovery episode ID, monotonic record generation, and pause generation;
- smallest affected scope: supervisor/controller, extraction pool, worker slot, PDF
  publisher, or repository where supported;
- sanitized reason family and stable reason code;
- consecutive failed recovery attempts and pause threshold;
- first/last failure time, last attempt time, and next retry time;
- current backoff and stability-window progress;
- whether the pause was threshold-triggered or safety-triggered;
- whether resume is applicable and its safety state (`safe`, `blocked`, or
  `not_applicable`), plus a stable reason when blocked;
- a typed local resume action containing method, loopback-local URL, episode ID, expected
  record generation, pause generation, and a server-produced idempotency key only while
  the action is applicable; never expose a state-changing GET link;
- pause time, acknowledgement time, resume-request time, and recovery time;
- previous episode/outcome history without exposing exception text, paths, PDF names, or
  repository URLs;
- separate per-document retry/exhaustion totals so poison PDFs cannot be confused with a
  failing pipeline component.

### Queues and fairness

- input queued jobs;
- eligible input jobs;
- active extraction jobs;
- staged waiting jobs;
- publication-in-flight jobs;
- backpressure-depth jobs, exactly `staged waiting + publication in flight`;
- backpressure threshold jobs;
- staged bytes;
- backpressure-depth and staged-byte growth rates over the named observation window;
- oldest eligible-input age;
- oldest staged-result age;
- queued, eligible, running, and oldest-wait age by repository in the detailed view;
- each current-run repository's lifecycle/phase, inventory-final flag, total, completed,
  remaining, unresolved-failure, and terminal-outcome values;
- repository admission/locality reason;
- available disk bytes and configured disk-safety threshold.

### Throughput and latency

- extractor outputs per second/minute;
- successful writer publications per second/minute;
- cache-reuse completions per second/minute;
- end-to-end completed documents per second/minute;
- pages persisted per second/minute;
- source bytes and extracted characters processed per second/minute;
- failures, timeouts, interruptions, and retries per window;
- extraction, staged-wait, publication, semantic-readiness, and end-to-end mean plus
  p50/p95 latency where sample volume is sufficient;
- totals since the current supervisor telemetry series started;
- ETA only when enough recent stable throughput exists, with confidence or a range rather
  than false precision.

The top-bar **Extracted/min** and **Written/min** values use one documented recent rolling
window, initially 60 seconds, and must come from their own monotonic success counters.
Evaluate readiness independently: extraction can be available while publication is still
warming, and vice versa. For each rate, normalize a shorter complete interval to one
minute only after its minimum evidence rule passes. During a partial 30-to-59-second
window, require at least three successful events at that boundary before extrapolating;
one very fast PDF must not create a misleading per-minute rate. Once the full 60-second
window exists, show its directly measured 0, 1, 2, or greater event rate. Mark one/two
events as low-sample confidence instead of leaving a legitimately slow pipeline warming
forever. A window shorter than 30 seconds stays **Warming**. Display an em dash or named
unavailable state when its telemetry is not known. Keep the common window plus each
metric's state, confidence, elapsed time, event count, nullable value, unavailable reason,
and `asOf` time in the payload even if the compact UI hides them.

Compare normal extractor output only with normal writer publication. Cache reuse can
bypass durable new-text publication and must not make the writer appear faster than it is.
PDFs vary greatly in page count and bytes, so never tune or explain performance from
documents/second alone.

Instrument extraction success at the atomic durable-stage handoff, not at parser start or
before rename/validation. Instrument written success only after the publication
transaction commits. Give each counted boundary a stable job/transition identity so
supervisor restart, snapshot rebuild, lease reclaim, retry, or UI polling cannot count it
twice. Cache reuse bypasses one or both normal boundaries and remains a separate rate.
When cache reuse is the only current progress, show a **Reusing cached results** activity
or secondary label so truthful `0/min` normal extraction/publication rates do not look
like a stall.

### ETA and progress forecasting

Provide one total-run forecast and one forecast for each active repository. Every ETA
object must expose:

- state: `waiting_for_inventory`, `warming`, `available`, `paused`, `blocked`, `stale`,
  `complete`, `completed_with_errors`, `cancelled`, `unavailable`, or `not_applicable`;
- nullable `etaSeconds` using a monotonic-duration calculation;
- display text generated from the numeric duration, not separately calculated;
- confidence (`low`, `medium`, or `high`) and, where supportable, lower/upper duration;
- calculation time, observation window, completed sample/work volume, and current-series
  ID;
- inventory coverage and whether the total is final;
- stable reason/basis codes for the chosen estimate or unavailable state;
- the effective extraction target and limiting phase assumed by the forecast.

Render an available estimate as `ETA ~HH:MM:SS`. Hours are total hours and do not wrap at
24. Round only for display while retaining seconds in the payload. A one-second tick-down
animation may be used between samples, but each new sample must reconcile to the server
forecast. Prefixing with `~` and exposing confidence/details are required because
`HH:MM:SS` formatting must not imply exactness. Give assistive technology an equivalent
long form such as "approximately 12 hours, 34 minutes, 56 seconds."

Only a server-confirmed clean terminal run uses `state = complete`, `etaSeconds = 0`, and
display **Complete**. This includes a clean final zero-PDF inventory. A terminal run with
one or more permanent failures uses `completed_with_errors`, `etaSeconds = 0`, and
**Completed with errors** only when no work was cancelled; it never shows a green tick.
A target with any explicit cancellation uses `cancelled`, `etaSeconds = null`, and
**Cancelled** because the intended completion was aborted, even if an earlier item also
failed permanently. Preserve the error count and expose **Cancelled with errors** as
secondary detail. Thus terminal precedence for both a repository target and the total-run
target is `cancelled`, then `completed_with_errors`, then clean `complete`. A client-side
tick-down must clamp above zero and switch to **Completing…** when its estimate expires
until a newer server snapshot confirms the actual terminal state; it must never
manufacture `ETA ~00:00:00` for nonterminal work.

Estimate remaining wall-clock time from the end-to-end critical path: remaining
extraction work, durable staged/publication work, current parallelism, observed
extractor/writer rates, backpressure, retry/recovery state, and repository eligibility.
Prefer page/byte/character-weighted history when inventory provides those predictors;
use PDF count only as a low-confidence fallback. Account for extraction/publication
overlap rather than simply adding both phase durations.

The total ETA is the forecast makespan for every repository accepted into the current
run. Do not sum per-repository ETAs when repositories or pipeline phases can overlap.
Model observed Git clone/pull concurrency/order and unknown inventory. If queued
repositories have not been inventoried and no calibrated historical estimate exists,
show **Calculating total ETA** with coverage such as `3 of 8 repositories estimated`
instead of omitting them or fabricating a duration.

Do not publish a numeric ETA when there is insufficient stable work, the relevant series
is stale, progress is paused, a required component is unavailable, the queue inventory is
materially unknown, or a zero/near-zero rate makes the forecast unbounded. Use the named
`warming`, `stale`, `paused`, `unavailable`, or other applicable state and reason.
Recalculate after inventory changes, controller-target changes,
backpressure, retry, resume, or a material throughput change. Apply bounded smoothing and
hysteresis to reduce visual jumping, but never force ETA to decrease: it may rise when
new work is discovered or conditions slow, and the reason must be explainable.

Calibrate the estimator with replayable traces and measure error on completed runs at
several progress checkpoints. Report median absolute percentage error and over/under
bias by workload class. ETA is an observability feature, not an adaptive-controller input
until its accuracy is independently proven.

### Resources and foreground guardrails

- schedulable CPU count detected by the running process;
- normalized host CPU percentage;
- normalized OWL process-tree CPU percentage, with the normalization documented;
- total, used percentage, and available memory; never rely on percent used alone;
- OWL process-tree resident memory;
- memory-pressure signal when the platform exposes one;
- free disk and, where safely available, read/write throughput or pressure;
- semantic worker target/live/active counts and backlog;
- Git/repository worker activity relevant to the resource budget;
- thermal or power/battery constraint when available through a reliable low-overhead
  interface; otherwise report it as unavailable and do not guess;
- p50/p95 dashboard, exact-search, and representative request latency during indexing;
- search availability and SQLite error/timeout guardrails.

Resource detection must respect schedulable/affinity or container limits where available,
fall back safely on platforms without those interfaces, and reserve headroom for the web
process, Git synchronization, semantic workers, the operating system, and other laptop
workloads. Never default to using every reported CPU.

Define and expose the initial CPU-derived background slot budget as:

```text
backgroundCpuSlotBudget = max(0, floor(schedulableCpuCount * 0.80))
effectivePdfHardMax = min(configuredPdfHardMax, testedPdfHardMax)
resourceAwarePdfCeiling = min(
    effectivePdfHardMax,
    max(0, floor(backgroundCpuSlotBudget - otherActiveOrReservedCpuHeavyOwlSlots))
)
```

The configured hard maximum can be lower than the tested maximum and always wins; the
tested maximum prevents configuration from selecting unvalidated concurrency. Neither
value is inferred from detected CPU count.

Document how active/reserved semantic, Git, publisher, and other CPU-heavy OWL work maps
to the shared budget. Express each reservation in normalized core-equivalent slots,
derive conservative values from benchmarked recent peak/EWMA use, round the final PDF
ceiling down, and attach a freshness time. Do not double-count one process across pools
or count idle processes as fully active. If measurements are stale/unavailable, use a
documented conservative configured reservation rather than zero. The remaining
approximately 20 percent is headroom for OWL's web/foreground path, the operating system,
and interactive laptop use. For illustration only, 20 schedulable CPUs produce a raw
budget of 16 CPU-equivalent slots and 18 produce 14. Actual detection, the tested hard
maximum, other OWL work, and live guardrails determine the lower effective PDF ceiling.
This is an admission budget, not a claim that every process consumes exactly one core.
A transient ceiling of zero suppresses new claims while competing or safety-critical work
owns the budget; resident processes may remain alive and in-flight durable work follows
the existing safe-drain rules.

On a genuinely one-CPU limit, the raw 80-percent budget rounds to zero. Permit one
explicitly labelled **minimum progress exception** only while eligible work exists, no
competing CPU-heavy OWL task owns the slot, and foreground/safety guardrails pass. Report
that the 20-percent reserve cannot be represented at this granularity; do not pretend the
exception satisfies the preference. While the exception is active, expose an effective
resource-aware ceiling of one with reason `minimum_progress_exception`; an independent
safety ceiling of zero still wins.

### Example versioned payload

The final schema may add fields, but it must be versioned, documented, and tested. A
representative shape is:

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-09-03T14:31:10Z",
  "seriesId": "01-example-supervisor-series",
  "seriesStartedAt": "2026-09-03T14:00:00Z",
  "historyStartedAt": "2026-09-03T14:01:10Z",
  "historyComplete": false,
  "sampleIntervalSeconds": 5,
  "windowSeconds": 60,
  "state": {
    "code": "publication_limited",
    "label": "PDF publication limited",
    "reasonCode": "staged_age_and_depth_rising",
    "reason": "Extractor output exceeded successful publication for 60 seconds; backpressure depth and oldest staged age increased.",
    "confidence": "high",
    "constraints": ["sqlite_contended"]
  },
  "activity": {
    "code": "extracting_and_writing",
    "label": "Extracting + writing",
    "secondary": [
      {"code": "pulling", "repositoryCount": 1}
    ],
    "reasonCode": "extractor_and_publisher_progress",
    "evidence": {
      "activeExtractionJobs": 4,
      "publisherState": "busy",
      "repositoriesPulling": 1
    }
  },
  "topBarActivityIndicator": {
    "state": "running",
    "hasFreshRunningWork": true,
    "evidenceCodes": ["extractor_heartbeat", "publisher_publication_in_flight"],
    "evidenceAt": "2026-09-03T14:31:09Z",
    "freshForSeconds": 15
  },
  "run": {
    "id": "01-example-current-run",
    "acceptedAt": "2026-09-03T14:00:00Z",
    "lastProgressAt": "2026-09-03T14:31:09Z",
    "repositories": {
      "accepted": 8,
      "queued": 3,
      "active": 2,
      "completed": 3,
      "completedWithErrors": 0,
      "paused": 0,
      "cancelled": 0,
      "health": {
        "degraded": 0,
        "unavailable": 0
      }
    },
    "pdfs": {
      "total": 5000,
      "successful": 800,
      "permanentFailed": 0,
      "cancelled": 0,
      "remaining": 4200,
      "unresolvedFailures": 0,
      "inventoryFinal": false,
      "inventoryRepositoriesKnown": 5,
      "inventoryRepositoriesAccepted": 8
    },
    "totalEta": {
      "state": "warming",
      "etaSeconds": null,
      "display": "Calculating total ETA",
      "confidence": "low",
      "lowerSeconds": null,
      "upperSeconds": null,
      "asOf": "2026-09-03T14:31:10Z",
      "windowSeconds": 60,
      "completedSamples": 12,
      "completedWork": {
        "documents": 78,
        "pages": 2520,
        "sourceBytes": 480000000
      },
      "seriesId": "01-example-supervisor-series",
      "inventoryFinal": false,
      "inventoryCoverage": 0.625,
      "reasonCode": "repository_inventory_incomplete",
      "limitingPhase": "publication",
      "effectiveExtractionTarget": 6
    },
    "repositoryProgress": [
      {
        "repositoryId": 42,
        "runId": "01-example-current-run",
        "revisionId": 4201,
        "lifecycleState": "active",
        "phase": "extracting_and_writing",
        "acceptedAt": "2026-09-03T14:00:00Z",
        "activatedAt": "2026-09-03T14:08:00Z",
        "lastProgressAt": "2026-09-03T14:31:09Z",
        "inventoryFinal": true,
        "totalPdfs": 900,
        "successfulPdfs": 540,
        "permanentFailedPdfs": 0,
        "cancelledPdfs": 0,
        "remainingPdfs": 360,
        "unresolvedFailures": 0,
        "terminalOutcome": null,
        "eta": {
          "state": "available",
          "etaSeconds": 754,
          "display": "ETA ~00:12:34",
          "confidence": "medium",
          "lowerSeconds": 610,
          "upperSeconds": 930,
          "asOf": "2026-09-03T14:31:10Z",
          "windowSeconds": 60,
          "completedSamples": 12,
          "completedWork": {
            "documents": 64,
            "pages": 2100,
            "sourceBytes": 390000000
          },
          "seriesId": "01-example-supervisor-series",
          "inventoryFinal": true,
          "inventoryCoverage": 1.0,
          "reasonCode": "weighted_recent_pipeline_rate",
          "limitingPhase": "publication",
          "effectiveExtractionTarget": 6
        }
      }
    ]
  },
  "controller": {
    "mode": "observe",
    "configuredMin": 1,
    "configuredPdfHardMax": 8,
    "testedPdfHardMax": 8,
    "requestedTarget": 6,
    "backgroundCpuBudgetFraction": 0.8,
    "backgroundCpuSlotBudget": 14,
    "otherActiveOrReservedCpuHeavyOwlSlots": 2.0,
    "resourceReservationFreshAt": "2026-09-03T14:31:10Z",
    "resourceAwarePdfCeiling": 8,
    "safetyCeiling": 8,
    "effectiveAdmissionTarget": 6,
    "limitingReason": null,
    "cooldownUntil": null
  },
  "workers": {
    "expectedResident": 8,
    "live": 8,
    "admitted": 6,
    "active": 4,
    "occupancyPct": 66.7,
    "idleNoDemand": 0,
    "waitingForEligibleInput": 0,
    "backpressured": 2,
    "pausedByController": 2,
    "pausedByRecovery": 0,
    "unavailable": 0
  },
  "publisher": {
    "live": true,
    "state": "busy",
    "busyPct": 92.0,
    "starvedPct": 0.0,
    "noDemandPct": 0.0,
    "blockedPct": 8.0,
    "sqliteTransactionPct": 71.0,
    "sqliteLockWaitP95Ms": 12.0,
    "sqliteBusyErrors": 0
  },
  "recovery": {
    "state": "healthy",
    "halfOpen": false,
    "episodeId": null,
    "generation": 0,
    "pauseGeneration": 0,
    "scope": null,
    "reasonCode": null,
    "consecutiveFailedAttempts": 0,
    "lifetimeAttempts": 0,
    "pauseAfterAttempts": 25,
    "lastAttemptAt": null,
    "nextRetryAt": null,
    "pausedAt": null,
    "resumable": false,
    "resumeSafety": "not_applicable",
    "resumeBlockedReason": null,
    "resumeAction": null
  },
  "queues": {
    "inputQueuedJobs": 120,
    "eligibleInputJobs": 118,
    "activeExtractionJobs": 4,
    "stagedWaitingJobs": 5,
    "publicationInFlightJobs": 1,
    "backpressureDepthJobs": 6,
    "backpressureThresholdJobs": 4,
    "stagedBytes": 12345678,
    "backpressureDepthGrowthPerSecond": 0.08,
    "stagedBytesGrowthPerSecond": 120000,
    "oldestEligibleWaitSeconds": 31,
    "oldestStagedWaitSeconds": 18
  },
  "throughput": {
    "rateWindowSeconds": 60,
    "extractedRate": {
      "state": "available",
      "confidence": "high",
      "elapsedSeconds": 60,
      "eventCount": 72,
      "perSecond": 1.2,
      "perMinute": 72.0,
      "unavailableReason": null,
      "asOf": "2026-09-03T14:31:10Z"
    },
    "writtenRate": {
      "state": "available",
      "confidence": "high",
      "elapsedSeconds": 60,
      "eventCount": 66,
      "perSecond": 1.1,
      "perMinute": 66.0,
      "unavailableReason": null,
      "asOf": "2026-09-03T14:31:10Z"
    },
    "cacheReuseCompletionsPerSecond": 0.2,
    "documentsCompletedPerSecond": 1.3,
    "pagesPersistedPerSecond": 42.0,
    "sourceBytesProcessedPerSecond": 8000000,
    "failedPerSecond": 0.0,
    "extractionLatencyMeanMs": 3100,
    "extractionLatencyP50Ms": 2400,
    "extractionLatencyP95Ms": 8100,
    "publicationLatencyMeanMs": 240,
    "publicationLatencyP50Ms": 180,
    "publicationLatencyP95Ms": 620,
    "endToEndLatencyP95Ms": 9200
  },
  "resources": {
    "schedulableCpuCount": 18,
    "hostCpuPct": 54.0,
    "owlProcessTreeCpuPct": 37.0,
    "hostMemoryUsedPct": 48.0,
    "hostMemoryAvailableBytes": 33000000000,
    "owlProcessTreeRssBytes": 5100000000,
    "diskAvailableBytes": 400000000000,
    "semanticWorkersActive": 2,
    "thermalState": "unavailable"
  },
  "foreground": {
    "exactSearchAvailable": true,
    "exactSearchP95Ms": 310,
    "dashboardP95Ms": 420
  },
  "samples": [
    {
      "at": "2026-09-03T14:31:10Z",
      "intervalSeconds": 5,
      "workers": {
        "requestedTarget": 6,
        "effectiveAdmissionTarget": 6,
        "active": 4,
        "idleNoDemand": 0,
        "waitingForEligibleInput": 0,
        "backpressured": 2,
        "pausedByController": 2,
        "pausedByRecovery": 0,
        "unavailable": 0
      },
      "publisherState": "busy",
      "backpressureDepthJobs": 6,
      "backpressureThresholdJobs": 4,
      "extractorOutputs": 6,
      "writerPublications": 5,
      "extractorOutputsPerSecond": 1.2,
      "writerPublicationsPerSecond": 1.0,
      "hostCpuPct": 54.0,
      "hostMemoryAvailableBytes": 33000000000,
      "availability": {}
    }
  ],
  "tuningEvents": []
}
```

All example values are illustrative, not defaults or benchmark evidence.

`seriesId` changes whenever the supervisor/telemetry owner restarts or counters reset.
`seriesStartedAt` identifies that series, `historyStartedAt` identifies the earliest
retained sample, and `historyComplete` says whether the bounded response still contains
the beginning of the series. Samples contain a wall-clock timestamp, measured interval,
worker and queue gauges, interval deltas and derived rates, publisher state, resource
gauges, and an availability-reason map. Use `null` plus a stable reason in `availability`
when a value is missing; never substitute zero. Clients must break graph lines rather
than connect values across a `seriesId` change.

A tuning event must have a similarly explicit contract. At minimum include a sequence or
event ID, timestamp, controller mode, action (`recommend`, `apply`, `rollback`, or safety
override), previous target, proposed/new target, reason code, redacted human reason,
observation window, compact numeric evidence, confidence, cooldown deadline, and later
outcome. Shadow recommendations must be visually distinguishable from applied changes.

## Metrics storage and collection

- Do not write one telemetry row per second into the same SQLite database being measured.
- Keep high-frequency samples in a bounded in-memory ring owned by one authoritative
  process, preferably the supervisor/controller.
- Retain enough short history for useful graphs, initially about 15 to 30 minutes, and
  make retention configurable and bounded.
- If cross-process or restart visibility requires a local snapshot, use a small redacted
  file beneath the configured data root, restrictive permissions, atomic replacement,
  a schema version, owner/run identity, and a freshness timestamp. Never expose its path
  in the public payload.
- Choose a low-overhead worker-heartbeat/phase transport that cannot corrupt state when
  processes exit or restart. Detect stale heartbeats explicitly.
- Persist only natural job-boundary timestamps, downsampled run summaries, sparse tuning
  events, and sparse recovery/pause/resume transitions. Add database fields only when
  they are required for durable product truth and update them in existing state-transition
  writes where possible.
- Use a monotonic clock for durations/rates and a wall clock only for display timestamps.
- Bound label/cardinality. Metrics and tuning events must not contain PDF text, document
  titles, repository URLs, credentials, command arguments, checkout paths, usernames, or
  other unnecessary sensitive data.
- Measure and report telemetry overhead. Collection must not materially change pipeline
  throughput, lock behavior, or foreground latency.

## Dashboard contract

Provide one coordinated presentation hierarchy across the Bitbucket Search top bar,
repository cards, Home summary, and detailed Repository Logs. All surfaces must use the
same current-run identity, metric definitions, timestamps, and server-authoritative
state; browser-local estimates must not diverge from the API.

### Bitbucket Search top bar: ETA and flow first

Replace the current primary `Workers N running / M total` / `Calculating workers…`
summary with these compact values, in this order:

1. **Total ETA ~HH:MM:SS** for all repositories accepted into the current run;
2. **Extracted X/min**;
3. **Written Y/min**;
4. **Status: <current activity>**.

Use the truthful named ETA state in place of a duration, for example **Calculating total
ETA**, **Waiting for inventory**, **ETA paused**, or **ETA unavailable**. Do not show a
zero duration until the run is actually complete. Do not show worker counts in this
top bar, including its expandable Details content. Keep target/live/active/free/waiting
worker information in Repository Logs and its diagnostic graph.

Replace the separate ambiguous `PDF indexing in progress · N active` results-summary
copy as well; it must not reintroduce `N PDFs extracting` or suggest queued and running
jobs are the same state. It may reuse the global status/remaining summary or link to the
detailed run view without duplicating the full top bar.

The four values form one labelled accessible status group and wrap cleanly at narrow
widths. Do not depend on separators or color alone. Rate details expose their common
window and freshness. The status must preserve concurrency, for example **Extracting +
writing**, and may append a concise secondary phase such as **1 repository pulling**.

#### Top-bar refresh/activity icon: animate only for confirmed work

Treat the refresh action, background-activity indicator, and ETA/rate/status group as
separate concerns even if they remain visually adjacent. The animated icon beside
**Test connection** must be present and moving only when fresh authoritative state says
that work is actually executing. Use this deterministic presentation contract:

- `hidden`: with no saved repository, no accepted current run, and no confirmed work,
  render no refresh/activity icon control at all. It must not be focusable or announced,
  and neither animated GIF may have a live `src`. Keep **Test connection**, the
  notification control, the empty-inventory explanation, and **Add repository** available.
- `idle_actionable`: when at least one included repository can be refreshed but no work
  is active, retain a clearly labelled **Refresh all repositories** action using a static
  reload SVG only. Do not display an indexing/running GIF and do not set `aria-busy`.
- `idle_unavailable`: when repositories exist but none is refresh-eligible, or the refresh
  endpoint is unavailable, show a static disabled refresh affordance with the visible and
  programmatically associated reason. Do not use a GIF, `aria-busy`, a focusable wrapper,
  or a tooltip as the only explanation.
- `submitting`: while the refresh request is being accepted durably, show a submission
  spinner and **Adding to queue…**. This browser request state must not select the
  background-running GIF.
- `queued`: after acceptance but before any eligible phase is confirmed executing, show
  the distinct waiting icon and **Added to queue**/waiting text. Queue depth alone is not
  running evidence.
- `running`: attach/show the active artwork only with fresh server evidence of at least
  one executing phase: a Git job in `RUNNING` with its live lease/controller and fresh
  heartbeat; a non-publication PDF `RUNNING` phase with extractor PID/lease and fresh
  heartbeat; a cache attachment actually executing with equivalent fresh ownership; or
  a `PUBLISHING` job with a live writer PID/lease and fresh writer heartbeat. A staged
  `PUBLISHING` row with no current writer ownership is waiting, not running. A
  configured/resident process, total worker capacity, stale repository sync marker, job
  status without fresh ownership/heartbeat, `hasActiveWork`, or a queued job is
  insufficient by itself. Permit a short, explicitly bounded first-heartbeat grace only
  when an authoritative start transition and concrete current owner/PID exist; expiry
  without a heartbeat becomes static `unknown`/stalled, not indefinitely running.
- `retry_wait`, `paused`, and `terminal`: use a static waiting, pause, attention, or
  completion mark with truthful text. None may retain the running GIF. `recovering` may
  animate only during a confirmed recovery attempt; a backoff countdown remains static.
- `unknown`: while authoritative state is missing or stale, use a static unknown/warning
  presentation. A status fetch beginning, failing, timing out, or being restored from the
  browser back/forward cache must never manufacture `running`.

The exact screenshot state—zero repositories, zero PDFs, and zero running work—must map
to `hidden`, not to a dimmed animated button with an unavailable tooltip. When repositories
exist but are idle, the static refresh action remains available; this does not count as
the background activity icon. The ETA/rate/status group may independently show its
truthful idle/empty state, so hiding the icon must not erase useful status text.

Use one reducer/state renderer for server-rendered HTML, hydration, polling, request
submission, and back/forward-cache restoration. Do not map the current
`repositoryStatusPending`/fetch-in-flight flag into active work. Accept only a response
for the current run and newest sequence/generation; an older poll cannot resurrect an
animation after a newer terminal state. On a poll failure, retain last-known running
presentation only while its explicit server freshness window remains valid and mark it
stale in text; after that, render static `unknown`. Conversely, a partial/stale idle
response cannot hide activity that a newer authoritative sample still confirms.

Do not keep an animated GIF permanently loaded and rely only on `display: none`, class
swaps, reduced opacity, or the HTML `hidden` property. Conditionally emit or attach the
animated resource only for the allowed active state, remove its `src`/resource when that
state ends, and start with safe server-rendered markup so there is no idle-animation
flash before JavaScript runs. Under `prefers-reduced-motion`, show a static frame/SVG
and the same status text even during confirmed work; CSS animation-duration rules do not
pause pixels encoded inside a GIF.

Set `aria-busy="true"` only on the relevant status/control while submission or confirmed
execution/recovery is active, never merely because a poll is pending or failed. Queued,
retry-wait, paused, terminal, unknown, hidden, and actionable-idle states need distinct
non-color text. Announce real state transitions once through the existing status region;
ordinary polls must not repeat announcements.

The existing blended Git/indexing percentage must not be treated as measured progress.
If a progress bar remains, make it determinate only from current-run terminal outcomes
over a known inventory and label provisional inventory explicitly; otherwise use an
indeterminate progress indicator with the current activity text. ETA and progress must
come from the versioned metrics contract, not client-side weighting.

### Home dashboard: compact pipeline health

Place a compact **Pipeline health** section after **Your apps** and before the existing
historical Bitbucket activity area in `templates/core/dashboard.html`.

Show:

- total-run ETA or its truthful named state;
- current activity plus the primary health state, confidence, and measured reason;
- rolling **Extracted/min** and **Written/min** rates;
- current-run accepted/queued/active/completed repository counts and remaining/total PDFs
  when the inventory is known;
- recovery state, failed-attempt count/threshold, next retry time, and **Resume** when an
  actionable recovery pause exists;
- backpressure depth, its threshold, staged bytes, and oldest staged age;
- CPU, available memory, and disk-safety summary;
- a clear link to detailed Repository Logs / Index & refresh status.

Keep Home compact. It is a current-health surface, not a full operations console. Do not
make raw worker counts its main message; free/waiting/starvation details belong in the
linked diagnostic view.

### Repository cards: queued, active, remaining, ETA, and complete

The accepted response must visibly confirm every repository durably added by **Refresh
all**, without waiting for that repository's Git worker or PDF inventory. A repository
card whose newest authoritative lifecycle state is still queued renders:

- a dedicated waiting/queue icon that is visually distinct from clone, pull, active
  extraction, publication, attention, and completion icons;
- the literal primary label **Added to queue** and optional secondary **Waiting in
  queue** reason;
- no per-repository numeric ETA and no remaining/total PDF line until the repository is
  active and its current inventory is known.

Use a lightweight local SVG or CSS animation for the waiting state. Reuse a local asset
only if its meaning is unambiguously waiting; do not use the active extraction GIF for a
queued repository. Any animation must stop under `prefers-reduced-motion`, while the icon
and text remain understandable. The acceptance acknowledgement proves the queue action;
a fast card transition may move directly to a newer authoritative active state. Never
delay actual work, preserve a stale queue generation, or fabricate waiting time merely to
flash the queued icon.

Only repositories actually accepted into the run receive **Added to queue**. Preserve
truthful alternatives for excluded, skipped/up-to-date, already active, cancelled, and
enqueue-failed repositories. Do not show a queue position unless scheduling guarantees a
stable meaningful order.

When a repository becomes active:

- show its actual phase: checking connection, cloning, pulling, discovering/cataloguing,
  validating, hashing, extracting, writing/publishing, extracting and writing,
  backpressured, source-blocked, retrying/recovering, paused, or completing;
- once inventory is known, show **Remaining R of T PDFs** where both values use the
  current-run end-to-end invariant;
- show `ETA ~HH:MM:SS` on a separate line only when that repository's estimate is
  available, otherwise show **Calculating ETA**, **ETA paused**, or the appropriate named
  state;
- never use `N PDFs extracting` as the card's primary summary and never label a
  `PUBLISHING` job as extracting.

When the current repository revision/run satisfies the completion invariant, replace
the waiting/working symbol with a clearly green check and accessible text **PDF indexing
complete**. A successful zero-PDF inventory is complete and receives the same green
check. A new accepted run removes the old current-run completion presentation until that
run finishes. Partial failure, pause, cancellation, stale revision, and Git-only success
must use distinct non-green states. If preserving the existing two-stage Git/PDF ticks,
make the final PDF completion meaning unambiguous and ensure the combined result follows
this contract.

### Repository Logs: detailed performance view

Place the detailed pipeline view after the existing overview cards and before **Choose a
repository** in `templates/bitbucket_search/status.html`.

Use two aligned, coordinated panels rather than four unrelated charts:

1. **Capacity and state timeline**
   - stacked counts for active, idle with no demand, waiting for eligible input,
     backpressured, controller-paused, recovery-paused, and unavailable extraction
     capacity;
   - target, configured hard maximum, and live process count;
   - a publisher-state ribbon for busy, awaiting input/starved, idle with no demand,
     blocked, and unavailable;
   - OWL CPU and resource warnings as adjacent cards or sparklines, not as a misleading
     overlay with differently defined worker utilization.
2. **Flow balance and durable backlog**
   - extractor outputs versus successful writer publications versus total completions;
   - pages/minute and optionally bytes/characters/minute;
   - backpressure depth, its threshold, staged bytes, oldest staged age, and disk
     headroom on the same time axis;
   - clear indication when cache reuse explains a difference between publication and
     total completion rates.

These two panels must answer all four questions in the original proposal: utilization,
throughput balance, queue/backpressure behavior, and active/free worker capacity.

Also include:

- exact current-value cards;
- total and per-repository ETA diagnostics, basis/confidence/range, inventory coverage,
  estimator freshness, and completed-run forecast error;
- rolling extracted/written rates with their window and durable stage/publication
  definitions;
- overall state and evidence-based explanation;
- repository fairness/oldest-wait diagnostics;
- errors, timeouts, retries, and stale/unavailable process warnings;
- current recovery episode with attempt/threshold, retry countdown, pause scope, sanitized
  reason, stability progress, and **Resume** action;
- a bounded recovery-event history distinct from performance-tuning history;
- an expandable tuning history containing time, previous target, proposed/new target,
  mode, reason code, human reason, observation window, evidence, result, rollback, and
  cooldown;
- truthful empty, warming, unavailable, stale, idle, partial-failure, and active states.
  Include retry-wait, recovering, and recovery-paused states without describing them as
  idle or ordinary controller throttling.

Use existing OWL/Django templates and local JavaScript/CSS. Prefer a small accessible SVG
or canvas implementation with a semantic table/text fallback. Do not add a heavy chart
library or SPA framework unless measured requirements cannot be met without it and the
dependency is explicitly justified.

Graphs must have visible titles, units, legends, time range, last-updated/freshness state,
and keyboard/screen-reader access to equivalent values. Do not use color as the only
signal. Limit `aria-live` announcements to meaningful state changes; do not announce
every sample.

Poll a dedicated lightweight metrics endpoint approximately every five seconds while
pipeline work is active, around every 30 seconds while idle, and pause when the document
is hidden. Reuse the existing request/error/retry conventions. Do not expand an expensive
per-repository status query merely to serve high-frequency charts.

Pausing browser polling is only a presentation optimization. It must never pause,
cancel, slow, or own the server-side run. Enqueue work through the durable server-side
path, then prove that it continues after navigation and after the last OWL browser tab or
portal window closes. On reopen, discard stale client state and render the durable
current run. If the supported OWL launcher exits or the laptop stops/sleeps despite its
configured keep-awake behavior, do not claim computation continued; recover durable work
on the next OWL launch and show the interruption/recovery truthfully.

The endpoint must be loopback/local-action protected consistently with OWL, return
`Cache-Control: no-store`, expose `schemaVersion`, avoid secret/path/content leakage, and
degrade visibly when the supervisor or telemetry snapshot is unavailable.

## Professional Settings and repository-host configuration

Redesign the existing full Settings page as a shared OWL configuration surface. This is
an information-architecture and workflow change, not a request to squeeze the current
long form into a smaller card. Keep Django server rendering, named routes, CSRF, the
no-JavaScript fallback, and the current light/dark design language; do not introduce a
SPA or a second settings implementation.

A GET, client initialization, section navigation, status refresh, or notification poll
on Settings must be observation-only. Do not mount or invoke hidden schedule-tick forms
there. The resident supervisor/schedulers own due background work independently of all
browser pages; only an explicit labelled user action may enqueue or test from Settings.

### Settings information architecture

Keep the existing Settings URL and named-route compatibility, but use the truthful page
title **Settings** or **OWL Settings**, not **Confluence Settings**. The full Settings page
must not render the Bookmark Library browse/domain sidebar. Give it a compact dedicated
section navigation with these destinations:

1. **Overview** — secret-free connection summaries and the next useful action only;
2. **Confluence** — connection status, test/edit form, and its own removal action;
3. **Repository sources** — trusted repository hosts, per-host access/credential status,
   and links/actions for adding repositories;
4. **Bookmark data** — Export JSON, Import bookmarks, progress, and the latest import
   result.

Do not add a generic top-level **Advanced** or **Danger** dumping ground. Put genuinely
advanced help in disclosure beside the relevant setting, and keep resource-specific
destructive actions beside the resource they affect.

On desktop, use a restrained two-column settings shell: a narrow sticky section nav and
one comfortably sized content column. On narrow screens, collapse that navigation to an
accessible section menu or horizontal list and keep a single content column without
horizontal scrolling. Render one selected section at a time, with server-addressable
links/query state so refresh, history, validation errors, and no-JavaScript navigation
return to the same section. Put focus on the section heading or first invalid field after
navigation or a failed submission.

Use progressive disclosure throughout:

- show a compact status row/card before an edit form;
- use **Connect**, **Edit**, **Add host**, **Add credential**, or **Replace credential**
  to reveal the one form relevant to the current task;
- keep only one main form expanded at a time;
- move long explanations about credential types, permissions, storage, and examples into
  accessible **Learn more** disclosures while retaining concise inline field help;
- give each view one obvious primary action and consistent secondary/cancel placement;
- show test results and validation errors beside the field/action that produced them;
- use a sticky action bar only while a form has unsaved changes, and never duplicate
  Cancel buttons within one task;
- avoid nested bordered boxes, repeated headings/status panels, tiny text, and relying on
  color alone. Use spacing, type hierarchy, short labels, state icons, and subdued
  dividers to create structure.

Confluence, repository access, and bookmark data must remain independent. Move **Remove
Confluence connection** into a collapsed danger subsection within **Confluence**. Keep
Export and Import together under **Bookmark data**. Do not move secrets, imports, or
destructive operations into the Overview.

The Bookmark Manager gear must still have an accessible Settings path. Do not continue
to duplicate every connection form in the current 560-pixel drawer. If the drawer is
retained, reduce it to a compact secret-free connection summary with **Open full
Settings** and preserve close/Escape/focus-return behavior; all editing occurs on the
canonical full page.

### Trusted repository hosts

Under **Repository sources**, add a compact **Repository hosts** list and a clearly named
**Repository host URL** field. Its help text must say plainly:

> Approves this Git server for later repository connections. It does not add or clone a
> repository.

Accept a credential-free HTTPS origin such as `https://bitbucket.org` or
`https://scm.company.example:8443`, with an optional trailing slash only. Normalize it to
one canonical origin and hostname using NFKC/IDNA, lowercase/trailing-dot rules, and an
explicit effective port. Preserve a non-default HTTPS port. Reject HTTP and every other
scheme, username/password or other userinfo, repository/application paths, query,
fragment, wildcard, control character, malformed/ambiguous hostname or IDN, invalid
port, and overlength input. Never echo the raw rejected value; users may accidentally
paste credentials.

Saving a host is an explicit local trust/configuration action only. It must not perform
DNS, HTTP, Git, clone, refresh, or queue work, and it must not label the host **Connected**.
Use truthful states such as **Approved — not yet verified**, **In use**, **Managed
externally**, and **Unavailable under current policy**. Only a successful bounded,
read-only `git ls-remote` against a concrete repository clone URL can establish
**Repository access verified**. Fetching a server root page cannot prove repository
access.

For each host, show only compact non-secret facts: canonical origin/hostname, source
(`built_in`, `environment`, or `ui`), dependent repository count, HTTPS credential state
(**Not configured**, **Stored — not verified**, **Connected**, **Invalid**, or
**Unavailable**), and a genuine last repository-access verification time when one exists.
Never show a stored token or username. Keep credential editing collapsed beneath the
selected host, and populate provider-appropriate choices:

- Bitbucket Cloud token choices only for `bitbucket.org`;
- a truthful generic **Account name + HTTPS access token** label for supported Data
  Center/generic hosts;
- an SSH explanation that uses the existing OS SSH agent and never asks OWL to store an
  SSH private key;
- for a provider/credential shape OWL has not proven, show **Use SSH or an external Git
  credential manager** rather than a misleading Bitbucket-specific option.

The existing `BitbucketHTTPSCredential` row is a secret envelope scoped to an exact HTTPS
origin; it is not host authorization. Add a separate non-secret durable trusted-host
record/model with canonical uniqueness and provenance. An SSH-only or externally
credentialed host must remain representable, and removing a credential must not silently
de-authorize its host.

### Effective-host policy and runtime enforcement

Replace duplicate direct reads of `BITBUCKET_ALLOWED_HOSTS` with one effective-host
service used by all repository URL normalization, credential-origin validation/choices,
Settings summaries, connection tests, queue admission, scheduled refresh, and the final
outbound worker/Git boundary. The policy is:

- when `BITBUCKET_ALLOWED_HOSTS` is unset, preserve the built-in defaults and union them
  with enabled UI-managed hosts;
- when `BITBUCKET_ALLOWED_HOSTS` is explicitly supplied, including an explicitly blank
  value, treat it as externally managed policy. Show that state in Settings and do not
  let the UI broaden, edit, or remove it; explicit blank remains deny-all;
- if previously stored UI hosts fall outside a newly applied external policy, retain
  their records but mark them inactive/unavailable. Do not contact them or silently
  delete dependent state;
- a database/migration/read failure while resolving UI-managed trust fails closed for
  hosts not independently approved by the active built-in/external policy.

UI host changes must become effective across web, supervisor, scheduler, and worker
processes without rewriting `.env` or requiring an OWL restart. Revalidate effective
trust immediately before every outbound Git operation rather than only when a repository
is first registered; this closes the existing time-of-check/time-of-use gap. A revoked or
newly unavailable host blocks new network work with a safe reason while preserving local
repositories, checkouts, indexes, queued records, and history.

Adding an existing canonical host is idempotent, and concurrent duplicate submissions
produce one record. Add migrations and a safe compatibility/backfill strategy for
built-in/environment hosts and hosts referenced by existing repositories/credentials;
do not lock out working repositories on upgrade or turn an existing connected origin
into UI-owned configuration accidentally.

Allow removal only for an unused UI-managed host through an explicit confirmed action.
If a saved repository, queued/running job, or credential depends on it, return a safe
conflict response and show the dependency counts and next actions. Never cascade-delete
repositories, checkouts, extracted/indexed data, jobs, or credentials. Built-in and
environment-managed hosts are read-only in the UI.

Host add/remove endpoints must be POST-only, CSRF-protected, `never_cache`, rate-limited,
and strict-loopback even when broader read access is configured. If the desktop
`Origin: null` compatibility path is required, add only the exact named mutations to its
narrow allowlist. Audit a safe event/code and canonical non-secret identity only; never
place raw input, credentials, clone URLs, local paths, or exception text into logs,
messages, sessions, redirects, metrics, or responses.

### Repository clone URLs remain a separate task

After a host is approved, offer **Add repository** and navigate to the existing repository
workflow, where the user pastes one or more full SSH/HTTPS clone URLs. Make the host-vs-
clone distinction visible next to both inputs. A newly approved custom host and custom
HTTPS port must be immediately valid for the existing clone validator and immediately
available as the exact HTTPS credential origin, without a restart.

Do not create a second repository-add validator or queue path inside Settings. If product
requirements call for an inline/collapsed **Add repository** form there, reuse the exact
same form component, endpoint, normalization service, whole-batch validation, canonical
deduplication, idempotent registration, and background queue behavior used by Bitbucket
Search. Preserve the existing limit, SSH/HTTPS-only policy, credential-free URLs, exact
hostname matching, safe path rules, `git` SSH user, `--` argument boundary, TLS/SSH
verification, timeout, and sanitized error behavior.

## Automatic recovery, circuit breaker, pause, notification, and resume

Automatic recovery is required for the PDF pipeline supervisor/controller, extraction
pool or individual supervised extraction slots, and the PDF publisher. Design the state
machine so it can be reused later, but do not silently replace the independent Git,
Confluence, or semantic retry policies as part of this scope.

Detect loss of forward progress as well as process exit. A resident worker can remain
alive while repeatedly catching database errors, sleeping, and retrying its command loop.
Use fresh phase/progress heartbeats, successful claims/completions, and classified
error-loop evidence so an alive-but-stuck component cannot evade the recovery circuit.
Do not turn an ordinary idle worker with no eligible demand into a false stall.

### Keep retry layers separate

There are two different failure budgets:

1. **Per-document attempts.** A parser can fail because one PDF is corrupt, encrypted,
   unsupported, too large, changing during extraction, or otherwise unsuitable. Preserve
   the existing small bounded per-document retry policy and its error classification. Do
   not increase `PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES` to 20–30 and do not let one poison
   PDF pause the healthy pipeline. Terminalize/quarantine that current revision according
   to existing policy, keep previously published searchable text where OWL already does
   so, and continue unrelated work.
2. **Component recovery attempts.** A supervised process or controller can exit, stop
   heartbeating, fail to relaunch, or repeatedly fail its stability check. This is the
   circuit-breaker budget requested here. Default the pause threshold to **25 consecutive
   failed recovery attempts**, the midpoint of the requested 20–30 range, and make it a
   strictly validated setting. Show the configured value in diagnostics and the UI.

A supervisor poll, a dashboard refresh, several stack frames from one failure, and several
PDFs that fail for unrelated permanent input reasons must not increment the component
counter. Deduplicate one detected incident into one recovery attempt.

A planned OWL shutdown, normal app quit, or laptop suspend/wake is not a component
failure. Recover leases on the next startup/wake without incrementing the failure streak.
If one stop/wake makes several leases stale together, record one correlated incident—not
one recovery attempt per lease or worker.

Classify failures before retrying:

- retry transient process exit, stale heartbeat, temporary launch failure, recoverable
  SQLite busy/locked conditions, and temporary local I/O/resource conditions when the
  existing safety contract permits;
- do not retry user cancellation as a failure;
- do not repeatedly retry permanent input, invalid configuration, missing credentials,
  unsupported format, deterministic validation, or integrity failures without a
  meaningful state/configuration change;
- pause immediately for critical disk, data-integrity, migration/schema, unsafe
  configuration, or repeated corruption signals instead of deliberately burning through
  25 attempts;
- keep the reason family stable and redacted so equivalent crashes contribute to the
  correct circuit without storing raw private exception data.

### Durable recovery state machine

Use a durable, sparse control record or equivalent canonical state that survives worker,
supervisor-thread, web-process, and normal OWL restarts. High-frequency telemetry stays
outside SQLite, but a write on failure/retry/pause/resume boundaries is appropriate.

The recovery record and alert path must not depend solely on the component that failed.
If SQLite itself is temporarily unavailable, use a minimal redacted atomically replaced
fallback state beneath the configured data root, then reconcile it into canonical state
and publish the pending notification when SQLite recovers. Bound and test this fallback;
never store raw exception text or private identifiers in it.

Define one authority order, globally monotonic per-scope generation/compare-and-swap rule,
and reconciliation direction for database and fallback state. The highest valid monotonic
generation wins regardless of whether it is healthy or paused; therefore an older paused
fallback cannot resurrect after a newer successful recovery. Resolve equal-generation,
corrupt, or otherwise unorderable disagreement fail-closed to paused, and never let two
stores each launch a controller. Archive or atomically clear reconciled fallback state
only after canonical persistence commits.

If neither SQLite nor the same-disk fallback can be written—for example because the disk
is exhausted—fail closed in memory: stop affected claims/relaunches, emit a bounded
redacted emergency log, keep retrying the control-state write with bounded backoff, and
delay the durable notification until a store recovers. On the next startup, run the
safety preflight before spawning affected roles so the same unresolved condition pauses
again. Never claim that an unwritable pause was durably recorded.

Required states and transitions include:

```text
healthy
  -> retry_wait
  -> recovering
  -> healthy                 after the stability gate passes
  -> retry_wait              when recovery fails below the threshold
  -> paused                  at threshold or for an immediate safety condition

paused
  -> resume_requested        after a valid user action
  -> recovering_half_open    one controlled recovery probe
  -> healthy                 only after the stability gate passes
  -> paused                  immediately if the half-open probe fails
```

Persist at minimum the episode ID, record and pause generations, scope, reason
code/family, consecutive and lifetime attempt counts, threshold, first/last failure,
last/next attempt, current
backoff, paused reason/time, acknowledgement, resume request, recovery time, and sanitized
last outcome. Enforce one active recovery episode per scope and protect transitions with
the same single-owner/atomicity principles used elsewhere in OWL.

Use `generation` as the monotonic compare-and-swap/control-store generation and increment
it for every persisted transition. Use a separate `pauseGeneration` that increments only
when the circuit opens or reopens and keys popup delivery/acknowledgement. Keep the
episode ID stable across half-open resume probes until recovery reaches the stability
gate and closes the episode.

Give every actual recovery probe a unique durable attempt ID. Increment lifetime attempts
when the probe begins, whether it later succeeds or fails. Increment
`consecutiveFailedAttempts` only when a normal recovery probe fails; open the circuit when
that failure reaches the threshold. Once open, saturate the displayed consecutive count
at the threshold. A failed user-authorized half-open probe finalizes the already-counted
attempt as failed and appends one failure-history event; it does not increment
`lifetimeAttempts` again, turn 25 into 26, or grant/reset a budget. It immediately reopens
at 25 of 25.

When correlated slot failures escalate to an extraction-pool or pipeline scope, transfer
the correlation/attempt IDs into one owning episode, mark narrower episodes superseded,
and count each distinct failed probe once. Scope escalation must neither reset the streak
nor add child counters together and double-count the same incident.

Use exponential backoff with bounded jitter and a configurable maximum delay. Persist
`nextRetryAt` so a supervisor restart does not turn a delayed retry into a tight loop.
Never count a process spawn as successful recovery by itself. Reset the consecutive
failure streak only after the component remains healthy for a named stability window and:

- produces fresh heartbeats/progress while demand exists; or
- remains healthy for the full no-demand stability window when no eligible work exists.

Record every recovery attempt and transition in the redacted operation/recovery history,
but coalesce repeated identical log lines and notifications.

### Pause the smallest safe scope

Open the circuit at the smallest scope that protects progress:

- a permanent PDF failure affects that PDF revision only;
- a repository eligibility/checkout failure pauses or blocks that repository while other
  repositories continue when safe;
- one repeatedly crashing extraction slot may be recovery-paused while healthy slots
  continue above the configured minimum;
- a common extraction failure may pause new extraction claims for the extraction pool;
- a repeatedly failing publisher must stop new extraction admission before durable stage
  growth becomes unsafe and preserve every valid staged result;
- a controller/supervisor or integrity failure may pause the complete PDF pipeline.

At startup and after watchdog recovery, load/reconcile the durable circuit state before
spawning an affected resident role, releasing its leases, or admitting new work. A paused
scope must not briefly relaunch or claim a job during startup and then be stopped again.

Re-evaluate scope when multiple workers fail with the same reason. Do not respond to a
common failure by cycling all slots independently 25 times.

An automatic pause is not cancellation:

- do not mark healthy queued work cancelled;
- do not delete staging files;
- do not discard completed publication or semantic work;
- do not reset retry/audit history;
- let unrelated and already-safe work drain according to the affected scope;
- preserve restart/lease/cancellation invariants.

If extraction is paused while the publisher is healthy, allow the publisher to drain
already durable staged output. If the publisher is paused, stop new extraction claims and
retain staged output for later publication. If only one repository is paused, continue
other eligible repositories subject to the normal fairness and resource rules.

The exact resume guarantee is **from the last durable boundary**, not arbitrary
instruction-level or page-level continuation. Already published PDFs stay complete and
must not be repeated. A valid staged result resumes at publication without re-extraction.
Queued jobs remain queued. A parser that stopped before durable staging may restart that
one PDF from the beginning because the current extractor has no page-level checkpoint.
Say this truthfully in operating documentation and UI help.

Define that boundary precisely. In the current implementation, the staging file is
atomically renamed before the job row is moved to `PUBLISHING`, and a writer can commit
publication before unlinking the staging file. Cover both filesystem/database crash gaps:

- treat a matching stage file plus the durable `PUBLISHING` transition as the ordinary
  authoritative publication boundary;
- on startup/resume, detect an orphan stage left after atomic rename but before the phase
  transition; validate its job ID, target revision/blob/path/size, extractor version,
  manifest/schema, content bounds/checksum, cancellation state, and current document
  identity under the normal locks before promoting it to `PUBLISHING`;
- if safe orphan promotion cannot be proven, quarantine/clean it according to a bounded
  redacted policy and truthfully re-extract only that unfinished PDF;
- detect a terminal succeeded job whose stage file remains after database commit, verify
  the published revision, and remove the stale file idempotently without publishing or
  embedding it twice;
- never promote a stage for cancelled, superseded, changed, unavailable, or mismatched
  content.

Add deterministic crash injection tests after stage `fsync`/rename but before phase
commit, after the phase commit but before writer claim, after publication commit but
before stage unlink, and during resume reconciliation.

### Notification and popup contract

Reuse OWL's durable `Notification` / `publish_notification` service, notification center,
and existing adaptive polling rather than building a disconnected alert store. Add a
specific redacted PDF-pipeline recovery notification kind or equivalent; do not overload
a successful repository-refresh notification with a different meaning.

Extend notification/API payloads with a typed, server-produced action contract only as
needed. Accept only same-origin local paths, render all text as text rather than HTML, and
send **Resume** as CSRF-protected `POST` with the episode ID, expected record generation,
pause generation, and idempotency key. Never persist or execute arbitrary action URLs
supplied by metrics, logs, exception text, or repositories.

When a recovery circuit pauses:

1. create or update one durable unread notification for that recovery episode and current
   pause generation;
2. show one non-blocking but attention-grabbing in-app popup/alert dialog when an OWL page
   is visible, or on the next page load when the UI was closed;
3. include the affected scope, last safe redacted reason, pause time, durable-work summary,
   and recommended next step. A threshold pause says `Paused after 25 failed recovery
   attempts` using the actual count; an immediate disk/integrity/configuration safety
   pause says that it paused immediately and must not falsely claim the threshold was
   exhausted;
4. provide **Resume**, **View pipeline details/logs**, and **Dismiss** actions;
5. keep the notification in history after dismissal and update the same episode when
   resume is requested, succeeds, fails again, or re-pauses.

Each newly opened `pauseGeneration` must explicitly mark the durable notification unread,
even when its generic warning/error state value did not change. Repeated publication of
the same generation must preserve the user's read and popup-acknowledgement choices.

The popup must appear at most once per **pause generation** across repeated polling. A
failed half-open resume that reopens the circuit increments the pause generation and must
produce one new popup, while retaining the same underlying recovery episode/history. Use
a durable generation acknowledgement or an equivalent cross-tab-safe mechanism; do not
rely only on a JavaScript variable that resets on navigation. Keep notification `read_at`
separate from popup acknowledgement: dismissing or marking a card read must not
accidentally acknowledge a later pause generation. Dismiss acknowledges the current
popup but does not resume work, delete the notification, clear the failure count, or hide
the persistent paused state on Home/Repository Logs.

Treat an active recovery or unacknowledged pause generation as active work for polling.
While an OWL page is visible, surface the pause within one normal active polling interval
rather than waiting for the 30-second idle cadence. Pause network polling while hidden as
elsewhere, then fetch immediately on visibility return.

Use OWL's accessible dialog patterns: focus the heading or primary action, trap focus only
while modal, restore focus on close, support Escape for dismissal when safe, provide
visible text and an `aria-live` announcement once, and never rely on color or repeated
screen-reader polling announcements. Avoid `window.alert()` and notification spam.

This requirement is for a local in-app popup and durable notification. If the browser is
closed, the popup can appear only when an OWL page is next opened. If the entire OWL
process or machine is stopped, it cannot emit a live in-app alert; detect the unfinished
recovery episode at the next startup and notify then. Do not claim OS-level push
notifications or an out-of-process watchdog unless separately approved and implemented.

### Resume contract

Provide the same **Resume** action from the popup, durable notification card, Home pipeline
health card, and detailed Repository Logs view.

The action must:

- use a strict loopback-only, CSRF-protected `POST` endpoint;
- include the recovery episode ID, expected record generation, pause generation, and
  server-produced idempotency key so a stale popup cannot resume a newer pause;
- be idempotent and single-flight across repeated clicks, tabs, and process races. The
  first valid request atomically records the key and increments the generation; an exact
  duplicate key/predecessor-generation request returns the already-started half-open
  result without launching another probe, while a different genuinely stale action is
  rejected;
- recheck current disk, memory, database, configuration, checkout, and integrity safety
  conditions before changing state;
- return a clear `409`-style blocked response when the root safety condition remains,
  leaving the circuit paused and explaining the redacted next action;
- increment the same episode's generation and begin one controlled half-open recovery
  probe without erasing prior failure history or granting a fresh 25-attempt budget;
- reclaim stale leases and use existing durable staging/queue recovery before launching
  replacement work;
- restore only the affected scope and start conservatively; do not jump straight to a
  previously high adaptive target;
- keep adaptive tuning frozen until the recovery stability gate passes;
- update the notification and all dashboard surfaces immediately to
  `resume_requested`/`recovering_half_open`, then to healthy or paused;
- never convert the existing destructive **Stop indexing** action into resume or silently
  requeue work explicitly cancelled by the user.

After successful stability, close the circuit, mark the episode recovered, reset only the
consecutive component-failure streak, retain lifetime/history counters, and let adaptive
control warm up again. A later unrelated unexpected component failure, exit, or stall
begins a new episode. If the half-open probe fails before stability, finalize its
already-counted attempt as failed, append one failure-history event, reopen the same
episode immediately, preserve the saturated failure streak, update the same notification,
and require another explicit **Resume** after the user addresses the cause. Do not
increment `lifetimeAttempts` a second time. Automatic operation may reset a consecutive
streak only after the stability gate passes.

## Adaptive controller modes

Support these explicit modes:

| Mode | Behavior |
|---|---|
| `fixed` | Use the operator-configured target. Keep health telemetry, classification, and dashboards available, but make no recommendation or automatic change. |
| `observe` | Collect metrics and classify state, but do not recommend or change the target. This is the initial safe rollout mode. |
| `shadow` | Calculate and record the action the controller would take, but do not change admission. Use this to validate decisions against fixed-concurrency evidence. |
| `adaptive` | Apply bounded admission-target changes only after every enablement gate below passes. |

Provide an immediate configuration kill switch and a manual fixed override. Validate
settings strictly and fail closed to a conservative fixed target on invalid values.
Existing installations must not silently enter adaptive mode merely because code was
upgraded.

The recovery circuit operates in every controller mode. `fixed` means fixed concurrency,
not disabled crash recovery. While recovery is in `retry_wait`, `recovering`, `paused`,
`resume_requested`, or `recovering_half_open` for a scope, adaptive decisions for that
scope are frozen and must not consume those samples as performance evidence.

Use these distinct values and expose their limiting reasons:

1. `requestedTarget`: operator-fixed target or the adaptive controller's proposal;
2. `configuredPdfHardMax`: the operator's absolute configured cap;
3. `testedPdfHardMax`: the highest value supported by completed
   correctness/benchmark evidence;
4. `resourceAwarePdfCeiling`: the current 80-percent/shared-work candidate ceiling;
5. `safetyCeiling`: an immediate live integrity/pressure/recovery cap, including zero;
6. `effectiveAdmissionTarget`: the final result after mode and ceiling precedence.

In `adaptive` mode, ordinary effective admission is the requested target capped by both
the effective configured/tested hard maximum and resource-aware ceiling. In `fixed`,
`observe`, and `shadow`, the 80-percent resource-aware value remains advisory so an
upgrade does not silently tune the operator's target, but the effective hard maximum and
any active safety/recovery ceiling still apply. A manual fixed override follows the same
rule. The active safety ceiling
always wins in every mode and may suppress all new claims while in-flight work drains
safely.

Use equivalent explicit math, with an inactive safety ceiling represented by the
effective hard maximum:

```text
effectivePdfHardMax = min(configuredPdfHardMax, testedPdfHardMax)
ordinaryModeCeiling = (
    resourceAwarePdfCeiling if mode == adaptive else effectivePdfHardMax
)
effectiveAdmissionTarget = max(0, min(
    requestedTarget,
    effectivePdfHardMax,
    ordinaryModeCeiling,
    safetyCeiling,
))
```

The configured minimum is a growth floor only when every active ceiling can support it.
If the applicable resource-aware or safety ceiling is below the minimum, the ceiling
wins, including an effective target of zero. Record the requested target, every ceiling,
the final effective admission target, mode, and exact limiting reason; do not overload one
field called `target` with these distinct meanings.

The controller must have one owner under the existing supervisor lock. Multiple web or
worker processes must not independently tune the same pool.

## Adaptive controller rules

### Bounds and headroom

- Keep a configured minimum and hard maximum.
- Default the desired background CPU budget fraction to `0.80` and derive the raw slot
  budget with the formula above. Treat it as an upper resource target, not a mandatory
  extractor count or host-CPU set point.
- Treat the existing supported PDF maximum of eight as the initial tested ceiling. Do
  not silently configure 14 or 16 extractors solely from CPU count. If fixed-concurrency
  evidence still improves at eight, extend the benchmark and correctness matrix in small
  steps toward the current CPU-derived budget; raise the supported/configured hard
  maximum only through that evidence-backed change.
- When the tested maximum prevents approaching the CPU-derived budget, expose
  `limited_by_tested_maximum` rather than pretending the 80-percent preference was met.
- Derive a resource-aware ceiling from schedulable CPUs, available memory, process-tree
  RSS, semantic/Git workload, and reserved foreground/OS headroom. It may lower, never
  exceed, the configured hard maximum.
- Aim toward the resource-aware ceiling only while meaningful eligible demand exists and
  each increase improves successful durable end-to-end throughput. Never start idle
  workers merely to hit 80 percent, and never interpret unused RAM or low instantaneous
  CPU as proof that more extractors will help.
- Missing/stale CPU, memory, pressure, or competing-workload signals force the documented
  conservative fixed fallback. They must never cause an 80-percent guess.

### Observation stability

- Sample at a low fixed cadence such as five seconds while active.
- Use a rolling/EWMA observation window initially in the 60-to-120-second range.
- Require a configurable minimum amount of completed work, using pages/bytes as well as
  documents, before acting.
- Use warm-up, hysteresis, and a 60-to-120-second cooldown after ordinary changes.
- Keep all thresholds named, documented, configurable where operationally useful, and
  unit-tested. Do not bury unexplained magic numbers in controller code.
- Make no ordinary tuning decision on a tiny or exhausted queue, stale data, low
  confidence, a changing benchmark workload, or unavailable critical metrics.

### Increase admission gradually

Increase by one slot only when all of the following remain true for the required window:

- meaningful eligible input exists and is expected to continue;
- admitted extractors are actually occupied often enough that another slot could help;
- staged output is usually empty or low;
- the live publisher is genuinely awaiting input for a material fraction of the window,
  rather than idle because demand is absent or work is source-blocked;
- successful publication/completion is not already constrained by SQLite or another
  writer;
- CPU, available memory, process-tree RSS, disk, failure, timeout, thermal/power when
  available, and foreground-latency guardrails all have measured headroom;
- no recent change is still warming up or in cooldown.

### Reduce or pause admission

Reduce gradually for sustained ordinary constraints such as:

- backpressure depth, staged bytes, or oldest staged age continually increasing;
- extraction output materially exceeding successful publication while the publisher is
  continuously busy;
- sustained SQLite lock wait or busy/locked errors;
- sustained CPU, memory, disk I/O, thermal, error-rate, or foreground-latency pressure;
- controller evidence that the last increase did not improve successful end-to-end
  throughput.

Safety reductions may be immediate and larger for critically low disk, acute memory
pressure, repeated database errors, severe foreground unavailability, or another
well-defined integrity risk. Ordinary scaling down must stop new claims at job boundaries
and allow in-flight parsers to finish and publish safely.

Admission must reserve conservative memory and disk headroom for every admitted
extractor to finish its current worst-case supported job and stage its result. If a hard
safety threshold is crossed despite that reserve, first stop all new claims. A cooperative
interruption may then use the existing proven-safe interruption/lease-recovery path only
when tests show it cannot lose completed staged data or corrupt job state. Never
force-kill merely to hit a target. If safe interruption is not available, let in-flight
work drain and report the hard-pressure condition as degraded.

Do not reduce merely because the publisher is busy when backlog is small and stable and
all guardrails pass. A busy component is not automatically a bottleneck.

### Evaluate and roll back

- Record the baseline window, decision, expected outcome, and post-change window.
- Compare successful persisted documents, pages, bytes/characters, p95 latency, failures,
  staged age/depth, SQLite lock wait, and foreground latency.
- Roll back an ordinary increase when enough evidence shows no meaningful benefit. A
  provisional 3-to-5-percent improvement threshold may be evaluated, but calibrate it to
  benchmark variance and keep it explicit rather than treating it as universal truth.
- Record whether each shadow/applied decision helped, hurt, was inconclusive, or was
  superseded by a safety condition.
- After restart, begin conservatively and warm up. Do not blindly restore a learned target
  from a different workload or resource condition.
- If the controller crashes, loses ownership, sees inconsistent metrics, or cannot read
  critical signals, stop adapting and fall back to the documented configured fixed
  target. Never fail into unbounded concurrency.

## Scheduling and fairness review

The current locality policy can concentrate extraction on one active repository. That
improves checkout locality but may leave slots unused when the chosen repository cannot
fill the pool, and a large repository can make later repositories wait a long time.

Instrument repository eligibility and oldest wait before changing policy. Then benchmark
a work-conserving policy that:

- prefers current-repository locality;
- admits another repository when the current repository cannot fill the effective target;
- respects synchronization and checkout locks;
- enforces a documented fairness/starvation bound or quantum when sustained multi-repo
  demand exists;
- gives `PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES` or its successor real, tested semantics;
- does not reduce total throughput or weaken cancellation/restart correctness.

Update the existing test that requires one repository to finish before another only if
the new requirement and fairness evidence justify the behavior change. Add multi-repo
tests proving both locality preference and work conservation.

This review concerns PDF extraction admission after repositories are eligible. It does
not authorize a Git scheduling rewrite. Preserve the effective repository-controller
count and the one-active-Git-job-per-repository invariant unless a separate approved
requirement changes them. Record whether the tested run performs clone/pull work serially
or concurrently; do not repeat the user's tentative serial assumption as a verified fact.

## Low-risk efficiency work to measure

Treat each item as a separate experiment with before/after evidence. Do not bundle them
into one opaque performance change.

1. **Idle publisher reservation and polling**
   - The current empty-queue path may reserve the SQLite writer before discovering there
     is no staged work.
   - Prefer a cheap read-before-writer-reservation check, followed by race-safe claim
     logic.
   - Add exponential idle backoff or a low-cost wakeup mechanism while preserving prompt
     active processing and restart safety.
   - Prove an empty writer loop no longer creates avoidable writer-lock pressure or busy
     polling.
2. **Repository work conservation**
   - Measure unused slots and oldest repository wait under the current locality policy.
   - Test locality-preferred spillover as described above.
3. **Repeated PDF file reads**
   - Profile controller hashing, child pre/post fingerprints, parser reads, cache reuse,
     and disk throughput. New PDFs may currently incur at least three full hash reads plus
     parsing reads.
   - Remove or combine a pass only if mutation detection, cache identity, and post-parse
     integrity guarantees remain equivalent and tests cover concurrent file change.
4. **Publication batching and FTS cost**
   - Measure the existing per-PDF transaction, page `bulk_create(batch_size=100)`, FTS5
     triggers, model hooks, and semantic enqueue before altering them.
   - Benchmark batch sizes and transaction boundaries one variable at a time. Do not
     assume `executemany` is an improvement over the existing bulk path.
5. **SQLite journal and synchronization settings**
   - The inspected local database used rollback journal mode and `synchronous=FULL`.
   - Benchmark WAL for reader/writer coexistence under real concurrent search, dashboard,
     PDF publication, repository catalogue publication, and semantic publication.
   - WAL does not create multiple SQLite writers. Account for checkpoint behavior,
     network/removable-filesystem restrictions, backup/restore, process crashes, and
     migration/test behavior.
   - Keep durable synchronous behavior as the default. Do not select `NORMAL`, `OFF`, or
     another weaker durability mode without an explicit documented product decision and
     accepted power-loss trade-off.
6. **SQLite connection lifecycle**
   - Verify that each supervisor, web, Git, PDF controller, PDF publisher, semantic
     worker, and parser subprocess has the intended connection lifecycle.
   - Close inherited/stale Django connections before spawning or forking, keep the parser
     subprocess database-free, and never share one SQLite connection across processes.
   - Confirm that each database-using process creates and closes/refreshes its own
     connection safely, the configured busy timeout is actually applied, and no
     transaction remains open during long extraction, staging-file I/O, polling sleep, or
     unrelated computation.
   - Measure connection setup, transaction duration, and lock wait separately. Add
     process-boundary/fork-safety tests and failure cleanup tests before changing
     connection reuse.
7. **Indexes**
   - Inspect query plans and write cost for `PDFTextPage`, including the unique
     `(revision, page_number)` constraint, equivalent explicit indexes, automatic foreign
     key indexes, and FTS triggers.
   - Remove an index only through a tested migration after both ingestion and search
     benchmarks prove it is redundant and safe.

## Benchmark gate

Create a repeatable benchmark harness that uses an isolated temporary OWL data root and
database. Never benchmark destructively against the user's canonical database. Do not
commit real PDFs, extracted text, repository URLs, private metadata, databases, or
generated indexes.

The representative workload must cover:

- the documented target of 20,000 to 25,000 PDFs and roughly 50 GB, using generated or
  explicitly approved untracked local material;
- a smaller reproducible calibration corpus suitable for CI/developer iteration;
- tiny, medium, large, and very large multi-page PDFs;
- varied source bytes and extracted-character counts;
- scanned/empty/malformed/encrypted/timeout/failure cases already supported by OWL;
- duplicate content and cache-reuse paths;
- several repositories, including one large and many small repositories;
- simultaneous repository sync eligibility changes;
- semantic indexing active at its normal configured concurrency;
- concurrent exact searches, dashboard polling, and representative interactive requests;
- cold and warm filesystem/cache conditions where reproducible.

Run fixed targets at 1, 2, 4, 6, and 8 extractors, or document why a point is unsafe or
inapplicable. Start with 4/6/8 for quick calibration, then run the complete matrix. If
eight still yields a statistically meaningful durable-throughput improvement and all
guardrails pass, extend in steps such as 10, 12, 14, and 16 only up to the current
resource-aware/80-percent candidate ceiling. Do not run an inapplicable point merely
because it appears in this example. Use the same workload and settings, restore the same
clean snapshot between trials, repeat trials enough to estimate variance, and record
machine state and background workload.

Measure at minimum:

- successful persisted documents, pages, source bytes, and extracted characters per
  minute;
- extraction, staged wait, publication, semantic readiness, and end-to-end p50/p95;
- backpressure depth, staged bytes/oldest age, and repository oldest wait;
- transaction time, SQLite lock wait/errors, and WAL checkpoint cost if applicable;
- host and process-tree CPU/RSS, available memory, disk headroom/I/O, and thermal state
  where reliable;
- search/dashboard/request p50/p95 and availability;
- failures, retries, timeouts, interruptions, recovery, and data-integrity checks;
- telemetry overhead with metrics disabled versus enabled;
- total and per-repository ETA forecast error at fixed progress checkpoints, including
  warm-up duration, inventory coverage, median absolute percentage error, and systematic
  over/under-estimation.

SQLite journal mode, batch size, index changes, hashing changes, scheduling policy, and
worker target are separate independent variables. Change one at a time before testing
interactions.

Do not enable adaptive mode until:

1. metric definitions and state classifications are verified against deterministic
   traces and real process behavior;
2. fixed-target variance and bottlenecks are understood;
3. shadow decisions agree with the evidence and do not oscillate;
4. the controller respects every resource, integrity, and foreground-latency guardrail;
5. restart, cancellation, worker loss, publisher loss, stale metrics, low disk, and
   database contention tests pass;
6. adaptive performance is no worse than the best practical fixed configuration beyond
   documented benchmark variance on stable work, and it provides a measurable benefit or
   safer resource response on changing work;
7. the user-visible dashboard describes free, waiting, starved, blocked, and unavailable
   states truthfully;
8. higher-than-eight operation, if proposed, passes the same concurrency, restart,
   cancellation, recovery, SQLite, search, and foreground checks rather than only a
   throughput microbenchmark;
9. the 80-percent budget is observable as a ceiling/preference with an explicit limiting
   reason whenever the effective target is lower.

If evidence does not support automatic tuning, ship the observe/dashboard improvements
and keep the controller in `observe` or `shadow`. Report the gate as not passed; do not
manufacture a success claim.

## Phased delivery

Each phase must be independently releasable, preserve existing behavior, pass its focused
checks, and include a rollback path.

### Phase 0 — Baseline and contracts

- Reconfirm the architecture/settings/process topology.
- Update authoritative requirements and stable test IDs for this scope.
- Define current-run/repository lifecycle, activity, ETA, rate, completion, resource-budget,
  recovery, schema, clocks, windows, thresholds, and acceptance criteria.
- Define the canonical Settings information architecture and the separate trusted-host,
  credential-origin, and repository-clone URL contracts, including external-policy
  precedence and migration compatibility.
- Build the isolated benchmark harness and record the untouched fixed baseline.

### Phase 1 — Observe-only instrumentation

- Add exact job-phase telemetry and once-only timestamps/counters at durable stage handoff
  and committed publication.
- Add durable current-run membership plus per-repository inventory, remaining/total, and
  lifecycle data without tying execution to a browser session.
- Add the bounded in-memory sample ring and redacted snapshot/history mechanism.
- Add the versioned lightweight endpoint, rolling extracted/written rates, and shadow ETA
  calculation/accuracy capture.
- Keep admission fixed; do not recommend or change concurrency.
- Measure telemetry overhead against Phase 0.

### Phase 2 — Dashboard and truthful classification

- Replace top-bar worker counts with total ETA, extracted/min, written/min, and current
  activity; remove the duplicate ambiguous active-PDF summary.
- Decouple the refresh action from its background-activity artwork. Hide the icon control
  entirely for the zero-repository/no-run state, use a static reload action when idle and
  actionable, and load/animate running artwork only from fresh confirmed phase evidence.
  Make idle-unavailable, submission, queued, retry-wait, paused, terminal, stale/unknown,
  back/forward-cache, and reduced-motion behavior follow the explicit indicator state
  machine above.
- Add durable **Added to queue** repository cards with a distinct waiting icon, then
  phase-aware remaining-of-total/ETA while active and the green completion tick only at
  the end-to-end completion boundary.
- Add Home pipeline health and detailed Repository Logs panels, retaining worker
  capacity/free/starved information in the diagnostic view.
- Replace the long Bookmark-shell Settings form with the dedicated, sectioned shared
  Settings surface and compact quick-settings summary. Preserve Confluence, credential,
  import/export, no-JavaScript, focus, and secret-handling behavior, and remove implicit
  schedule ticks/worker wake-ups from Settings page initialization.
- Add the durable UI-managed repository-host registry, single effective-host service,
  progressive per-host credential flow, dependency-safe removal, and **Add repository**
  handoff without changing the existing clone/queue implementation.
- Add state explanations, confidence, freshness, accessibility, adaptive polling, and
  tuning-history empty state. Prove browser polling is observation-only and work survives
  closing/reopening the portal.
- Validate active, idle-no-demand, source-blocked, backpressure, publisher-starved,
  publication-limited, SQLite-contended, stale, unavailable, recovery, ETA warming,
  queued, phase-overlap, completion, and completed-with-errors displays.

### Phase 3 — Bounded automatic recovery and resumable pause

- Add durable component recovery episodes, retry classification, bounded exponential
  backoff, the default 25-attempt circuit threshold, and smallest-safe-scope pausing.
- Keep per-document retries separate and bounded under existing policy.
- Add the durable notification, one-popup-per-pause-generation behavior, recovery history, and
  secure idempotent **Resume** action on every required surface.
- Prove last-durable-boundary resume across extractor, staging, publisher, supervisor, and
  normal OWL restarts before enabling the feature by default.

### Phase 4 — Evidence-backed low-risk efficiency fixes

- Address idle publisher reservation/polling first.
- Benchmark repository work conservation, repeated reads, batches/FTS, WAL, connection
  lifecycle, and indexes as separate experiments.
- Keep or revert each change based on measured end-to-end and foreground results.

### Phase 5 — Fixed-concurrency characterization

- Run and document the fixed 1/2/4/6/8 matrix and, only if eight still helps safely, the
  evidence-gated steps toward the current 80-percent resource candidate.
- Establish resource ceilings, guardrails, variance, and the best practical fixed range.
- Calibrate total/per-repository ETA and publish estimator error by workload class.
- Do not infer a target or ETA from the single existing live sample or CPU count.

### Phase 6 — Shadow controller

- Implement deterministic recommendations without changing admission.
- Record evidence, expected effect, cooldown, hypothetical target, and later outcome.
- Replay captured traces to test stability, hysteresis, and classification.

### Phase 7 — Bounded adaptive admission

- Enable only after the benchmark gate passes.
- Apply job-boundary target changes with manual override, kill switch, safe fallback, and
  rollback evaluation. Use the 80-percent CPU-derived budget only as an upper preference,
  shared with other CPU-heavy OWL work and bounded by the tested hard maximum.
- Begin as an explicitly enabled local mode; do not silently opt in existing users.

### Phase 8 — Full validation and operating documentation

- Run automated, concurrency, recovery, benchmark, migration, security, and visible UI
  checks.
- Document settings, mode transitions, metric meanings, unknown/degraded behavior,
  ETA behavior/accuracy, queue/card semantics, browser-versus-process lifecycle, Git
  scheduling left unchanged, Settings navigation, repository-host policy/provenance,
  host-versus-clone URL semantics, benchmark method/results, troubleshooting, rollback,
  and safe defaults.
- Record traceability and the final release recommendation.

## Configuration expectations

Use names consistent with current OWL settings. At minimum, provide or clearly map:

- controller mode;
- minimum admission target;
- configured hard maximum / existing `PDF_MAX_EXTRACTION_WORKERS`;
- initial/fixed target;
- background CPU budget fraction, defaulting to `0.80`, with validation strictly above
  zero and at most one;
- conservative CPU-equivalent reservations/accounting for active semantic, Git,
  publisher, and other CPU-heavy OWL work;
- sample cadence and observation window;
- extracted/written display-rate window and minimum complete sample/work requirement,
  initially 60 seconds with at least 30 elapsed seconds and three events to extrapolate a
  partial window, while the complete window reports every observed count with low-sample
  confidence where appropriate;
- ETA minimum evidence, smoothing/hysteresis, staleness cutoff, confidence/range, and
  calibration settings;
- minimum evidence threshold;
- cooldown and hysteresis;
- CPU, memory, disk, SQLite-lock, error-rate, and foreground-latency guardrails;
- metrics retention;
- snapshot/tuning-history enablement;
- component recovery enabled state;
- component recovery pause threshold, defaulting to 25 consecutive failed attempts;
- recovery backoff base, maximum, and bounded jitter;
- recovery stability window and heartbeat/progress criteria;
- recovery history retention and popup acknowledgement behavior;
- manual fixed override and immediate disable switch.

Validate types and ranges at startup. Log sanitized effective values and validation errors,
never secrets or sensitive paths. Unsafe, missing, or unsupported values must choose a
documented conservative behavior rather than expanding concurrency.

Keep the component pause threshold separate from
`PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES`. Validate the default at 25 and document 20–30 as
the intended operator range, while allowing deliberately smaller values in isolated
tests. Define zero as **no automatic component relaunch: pause and notify on the first
detected retryable unexpected failure, exit, or stall**. Never interpret zero as unlimited
retries or disabled failure reporting.

Expose whether `BITBUCKET_ALLOWED_HOSTS` was unset, explicitly populated, or explicitly
blank; the parsed tuple alone cannot preserve that policy provenance. Do not require a
secret setting for UI-managed hosts. Keep the durable host registry non-secret and make
its effective-policy resolution deterministic and observable with safe source labels.
An external setting remains the higher-authority policy and must not be rewritten by a
web request.

Maintain backward-compatible defaults. When no new pipeline-controller settings are
present, `PDF_MAX_EXTRACTION_WORKERS` must continue to determine both the expected
resident pool and normal-operation requested/effective target in `fixed`, `observe`, and
`shadow` modes, subject only to the documented tested-hard-max and active safety/recovery
ceilings. Merely
upgrading OWL must not change extraction concurrency. Adaptive growth beyond that legacy
value requires an explicitly configured higher hard maximum, successful validation of
the supported range, and all benchmark gates in this prompt; never reinterpret an
existing value as permission to spawn more processes.

The `0.80` preference becomes actionable only in enabled adaptive mode or an explicitly
selected fixed target that has passed the expanded benchmark/correctness gate. In other
modes, still calculate and expose the raw budget, tested maximum, effective ceiling, and
limiting reason so the user can see why the current target is lower without silently
changing it.

Do not alter the user's ignored `.env` as part of implementation. Document example
configuration using non-secret placeholders.

## Data integrity, compatibility, and security boundaries

- Preserve extracted text, page numbering, document/revision identity, metadata, FTS5
  behavior, exact-first search behavior, semantic enqueue semantics, and cache reuse.
- Preserve one atomic publication outcome per revision and the existing uniqueness and
  canonical-content rules.
- Preserve staged-file durability, `fsync`/atomic rename, lease recovery, retries,
  cancellation ordering, checkout locks, and cleanup-after-commit behavior.
- Preserve search availability during refresh/index work and the performance targets in
  the master requirements.
- Keep long-running work outside normal HTTP requests.
- Keep repository-host approval, repository registration, and credential storage as
  separate state transitions. A host save must never contact the network or queue work.
- Preserve exact-host and exact-HTTPS-origin authorization at every outbound Git
  boundary. Never infer wildcard/subdomain trust or reuse a credential across a host or
  port boundary.
- Never write `.env` from Settings, broaden explicitly managed external host policy, or
  cascade-delete repository/index data when a host or credential is removed.
- Keep monitoring local, bounded, redacted, and low-cardinality.
- Never emit credentials, authorization headers, credential-bearing URLs, PDF text,
  document titles, repository URLs, local checkout paths, personal data, or process
  arguments into metrics, tuning reasons, API responses, charts, fixtures, screenshots,
  benchmark reports, or logs.
- Use synthetic fixtures and isolated fake stores. Do not access real remote repositories
  or credential stores without explicit approval.
- Use migrations for every schema change. Never edit an applied migration.
- Preserve cross-platform startup. Platform-specific resource signals must have tested
  unavailable/fallback behavior.
- Preserve user-owned data and unrelated working-tree changes.

## Required tests

Add focused automated coverage for at least:

### Metric math and retention

- monotonic rates and reset/restart handling;
- extracted success counted exactly once only after atomic durable staging, written
  success counted exactly once only after committed publication, and cache reuse excluded
  from both normal rates;
- common-window extracted/min and written/min normalization, including true zero versus
  warming/unavailable, the initial partial-window 30-second/three-event threshold, and a
  full 60-second window reporting zero/one/two events without perpetual warming;
  extraction/writing readiness and null reasons remain independent when only one rate has
  enough evidence;
- rolling and EWMA windows;
- p50/p95 calculation;
- queue growth and age;
- mutually exclusive worker partitions and their count invariants;
- exact backpressure-depth numerator, including the publication-in-flight job;
- mutually exclusive publisher duty-cycle partitions summing correctly;
- cache reuse separated from normal publication;
- missing, stale, partial, and counter-wrap/reset data;
- current-run and per-repository lifecycle/count invariants, provisional/final inventory,
  stale-attempt exclusion, active safe-drain versus paused lifecycle, zero-PDF
  completion, mixed cancellation/error precedence, and completed-with-errors outcomes;
- total/per-repository ETA state transitions, unbounded-hour `HH:MM:SS` formatting,
  nonnegative rounding, range/confidence, staleness, zero-rate, pause, unavailable,
  inventory change, and series reset behavior;
- server-confirmed clean/zero-PDF **Complete**, **Completed with errors**, and
  **Cancelled** terminal rendering, plus client countdown expiry switching to
  **Completing…** rather than false `00:00:00`;
- ETA critical-path behavior under overlapping extraction/publication, multiple active
  repositories, configured Git concurrency, backpressure, changing targets, retry, and
  recovery; total ETA is not a naive sum of repository ETAs;
- deterministic ETA smoothing/reconciliation and replayed completed-run forecast-error
  calculation;
- ring-buffer retention, bounded memory, atomic snapshot reads/writes, schema versioning,
  series resets/history completeness, and stale owner/run detection;
- telemetry-disabled behavior and measured overhead.

### Classification

- every global activity code and repository lifecycle/phase transition, including
  queued, connection check, clone, pull, discovery, validation, hashing, extraction,
  publication-only, simultaneous extraction/writing, cache reuse, source blocking,
  backpressure, completing, complete, completed-with-errors, and cancelled;
- a `PUBLISHING` job is never reported as extracting, and queued work is never reported
  as a running worker;
- idle with no demand;
- free capacity versus unavailable processes;
- publisher awaiting input with real eligible demand;
- source/sync/locality blocked;
- publication-limited rising backlog;
- backpressure at and above the soft threshold;
- SQLite contention based on lock evidence;
- CPU, memory, disk, and optional thermal constraints;
- warming/low-confidence and multi-constraint states;
- degraded/stalled processes and recovery;
- deterministic reason codes/text inputs and no sensitive content.

### Controller

- all four modes;
- ownership and duplicate-controller prevention;
- hard/resource-aware bounds;
- `floor(schedulable CPUs * 0.80)` budget math, including the illustrative 20-to-16 and
  18-to-14 cases, the guarded one-CPU minimum-progress exception, competing OWL-work
  core-equivalent reservation rounding/freshness, a transient zero-admission ceiling,
  and no double counting of idle processes;
- requested target, tested maximum, resource-aware ceiling, safety ceiling, configured
  minimum, and effective-admission precedence in every mode; resource/safety zero wins
  where applicable and in-flight work drains safely;
- tested-hard-maximum limiting reason, evidence-gated steps above eight, and reduction
  below the 80-percent candidate under every resource/foreground/pipeline guardrail;
- no attempt to hit 80 percent without demand or throughput benefit, and conservative
  fallback when resource signals are missing or stale;
- minimum sample/work requirements;
- +1 increase conditions;
- gradual and safety decrease conditions;
- hysteresis, warm-up, cooldown, and anti-oscillation;
- no action on tiny queues, source blocking, stale metrics, or low confidence;
- post-change evaluation, inconclusive result, and rollback;
- restart conservatism;
- manual override/kill switch;
- metrics/controller failure fallback;
- backward-compatible legacy-setting mapping and explicit higher-ceiling opt-in;
- hard-pressure finish reserves and safe cooperative-interruption fallback;
- no termination or loss of in-flight work.

### Recovery, pause, notification, and resume

- exact persisted/API recovery enum and primary-state mapping;
- transient process exit, stale heartbeat, launch failure, publisher failure, and
  supervisor-loop failure detection;
- alive-but-no-forward-progress and caught-error-loop detection without false positives
  for healthy no-demand idle;
- permanent PDF/input failure isolation without component-counter increments;
- user cancellation never being retried;
- one incident producing one attempt despite repeated supervisor/UI polls;
- planned shutdown and suspend/wake recovery producing no failure attempts, with several
  resulting stale leases coalesced into one incident;
- independent per-document and component-level retry budgets;
- exponential backoff, bounded deterministic jitter under test, persisted `nextRetryAt`,
  and no restart tight loop;
- threshold boundary tests immediately below, at, and above the default 25 attempts;
- unique attempt IDs, lifetime increment-at-start, consecutive increment-on-failure,
  saturation at 25 while open, and no reset/double-count during scope escalation;
- consecutive streak resetting only after the configured stability gate;
- same-cause multi-worker collapse into the correct common scope;
- smallest-safe-scope pause for document, repository, slot, extraction pool, publisher,
  and whole PDF pipeline conditions;
- immediate safety pause without consuming 25 artificial attempts;
- `pausedByRecovery` versus controller-paused, unavailable, idle, and source-blocked
  metrics/classification;
- queued-job, in-flight drain, staged-file, already-published, semantic-enqueue, and audit
  preservation across pause;
- extractor-pause publisher drain and publisher-pause extraction admission stop;
- pause state surviving worker, supervisor, web, and normal OWL restart;
- highest-generation database/fallback reconciliation, equal/unorderable fail-closed
  behavior, no resurrection of an older pause, no split-brain role launch, cleanup only
  after canonical commit, and neither-store-writable in-memory safety fallback;
- startup reading paused state before any affected spawn, lease release, or claim;
- last-durable-boundary resume, including staged publication without re-extraction and
  honest restart-from-beginning behavior for an unstaged PDF;
- orphan-stage validation/promotion or safe cleanup across rename/phase-commit and
  publication-commit/unlink crash windows, with no duplicate publication or embedding;
- strict-loopback/CSRF protection, stale episode/generation rejection, idempotent
  single-flight resume, and unresolved-safety `409` behavior;
- exact duplicate idempotency-key/predecessor-generation response versus genuinely stale
  action rejection;
- one half-open probe per valid resume, immediate reopen on probe failure, no fresh
  25-attempt budget, and close/reset only after the stability gate;
- conservative post-resume target and adaptive warm-up freeze;
- durable deduplicated notification transitions, explicit unread reset only for a new
  pause generation, and one popup per pause generation across poll, navigation, and
  multiple tabs;
- popup **Resume**, details, dismiss/acknowledgement, focus management, Escape behavior,
  focus restoration, non-color cues, and single `aria-live` announcement;
- threshold-pause versus immediate-safety-pause popup wording using the truthful attempt
  count;
- UI-closed pause appearing on next load and process-stopped pause appearing after next
  startup, without claiming impossible live notification while OWL is not running;
- redaction of exception details, paths, repository/PDF identity, and credentials from
  state, notifications, popups, logs, API payloads, and tests.

### Queue, scheduling, and writer

- one durable current-run membership entry per accepted Refresh-all repository before
  expensive work, explicit partial acceptance, and no false queued state for excluded,
  skipped, already-active, or failed submissions;
- acceptance response/redirect visibly acknowledges every accepted repository as
  **Added to queue**, while a newer active generation wins card rendering without a
  forced queued delay;
- fast queue-to-active races selecting the newest authoritative state without delaying
  work or reverting an active repository to queued;
- active repository remaining/total values across extraction, staging, publication,
  retry, terminal failure, cancellation, and newly discovered inventory;
- green completion only when `successful = total` and permanent-failed, cancelled,
  remaining, unresolved, staged, and publishing counts are all zero for final inventory,
  including a successful zero-PDF run; no green completion for Git-only, stale-revision,
  queued-new-run, paused, cancelled, or partial-failure states;
- simultaneous extractor completion at the staged high-water mark and the documented soft
  upper bound;
- staged bytes/age and disk-safety behavior;
- no unbounded in-memory payload queue;
- empty publisher loop does not reserve the SQLite writer unnecessarily;
- race-safe read-before-claim and wakeup/backoff behavior;
- prompt publication after idle backoff;
- multiple repositories, locality preference, work-conserving spillover, oldest-wait
  fairness, synchronization deferral, and checkout locks;
- effective Git controller concurrency/order remains unchanged by this scope, is measured
  rather than assumed serial, and is represented correctly in ETA traces;
- cache-reuse, normal staged publication, retries, timeouts, malformed output,
  cancellation, restart recovery, stale leases, worker death, and publisher death.

### SQLite, search, and semantic interaction

- real file-backed SQLite contention tests, not only mocks;
- publication transaction integrity and FTS consistency;
- exact search remains available and correct during publication;
- semantic jobs enqueue only after valid publication and retain their lifecycle behavior;
- shared PDF/semantic resource guardrails;
- per-process SQLite connection ownership, pre-spawn/fork closure, parser isolation, busy
  timeout, transaction duration, and failure cleanup;
- WAL/checkpoint/backup/restart behavior if WAL is adopted;
- fresh-install, upgrade-path, and migration-drift tests for schema/index changes.

### Settings and repository hosts

- full page has truthful **Settings** title/status, dedicated Settings navigation and
  `aria-current`, and no Bookmark Library browse/domain controls;
- GET, hydration, section changes, status/notification polling, and back/forward restore
  on Settings create no refresh/import/repository/PDF jobs and make no schedule-tick,
  connection-test, or outbound network mutation; scheduled work remains supervisor-owned;
- Overview, Confluence, Repository sources, and Bookmark data are
  server-addressable and preserve the selected section across reload/back/forward,
  no-JavaScript navigation, success, and validation failure;
- invalid submission returns to the correct expanded task and focuses the first invalid
  field; successful submission returns a concise status without reopening unrelated forms;
- compact summaries are secret-free, one main form is expanded at a time, each task has
  one primary action, and destructive controls remain with their affected resource;
- quick-settings drawer, if retained, contains summaries and **Open full Settings** rather
  than duplicated forms, while close, Escape, focus return, and fallback-link behavior
  remain correct;
- current Confluence test/save/replace/remove and externally managed read-only behavior,
  blank stored-PAT rendering, and origin-change-with-new-PAT rule all regress cleanly;
- Bookmark Export JSON, Import bookmarks, import progress/result/failure disclosure, and
  credentials-excluded guarantees remain intact;
- desktop and narrow widths in light and dark themes have readable type/line length,
  coherent spacing, no horizontal overflow, keyboard order, visible focus, non-color
  status cues, and no clipped sticky navigation/action bar;
- **Repository host URL** help and examples clearly distinguish server approval from a
  full **Repository clone URL**, and adding a host never contacts DNS/HTTP/Git, queues a
  job, registers a repository, or claims verified access;
- canonical host tests cover NFKC/IDNA, hostname case, trailing dot/slash, default port,
  preserved custom port, and idempotent plus concurrent duplicate submissions;
- validation rejects HTTP/file/other schemes, userinfo/credentials, query, fragment,
  wildcard/suffix/subdomain confusion, repository/application path, invalid port,
  malformed/ambiguous IDN, control characters, and overlength input;
- a credential-like marker in rejected host input is absent from response HTML/JSON,
  template context, session, database, messages, redirects, audit events, metrics, and logs;
- effective-host policy covers environment unset with built-ins plus UI additions,
  explicit environment override/read-only behavior, explicit blank deny-all, previously
  stored UI hosts outside new policy, database/migration unavailable fail-closed behavior,
  restart persistence, and visibility across separately running web/supervisor/workers;
- all repository validators, credential-origin choices, Settings summaries, connection
  tests, interactive and scheduled queue admission, and the final outbound Git boundary
  use the same effective-host service; an unapproved/revoked host cannot be contacted;
- a newly approved custom host accepts the existing supported SSH/HTTPS clone forms and a
  custom HTTPS port becomes an exact credential-origin choice immediately without restart;
  unapproved hosts, deceptive suffixes, and other ports remain outside credential scope;
- Bitbucket Cloud credential types appear only for `bitbucket.org`; generic/Data Center
  copy is truthful, unsupported combinations are unavailable, and SSH never asks for or
  stores a private key;
- token inputs remain blank on GET and invalid POST, encrypted/keyring storage behavior
  is unchanged, and no token/username appears in HTML, session, logs, or model plaintext;
- **Approved — not yet verified** changes to repository-access verified only after a
  successful bounded `git ls-remote` for a concrete repository; root-page HTTP success
  cannot mark it verified;
- unused UI host removal succeeds only after explicit confirmation; repository,
  queued/running job, or credential dependencies return conflict with safe counts and no
  cascade; built-in/environment hosts remain non-removable;
- migrations/backfill preserve existing repository and credential access with correct
  provenance and do not make working environment hosts UI-owned;
- host mutation routes reject GET, missing/invalid CSRF, non-loopback requests, invalid
  narrow opaque-origin use, and rate-limit abuse; successful responses are `no-store` and
  audit only sanitized canonical identity/codes;
- existing batch repository-add limit, whole-batch atomic validation, canonical SSH/HTTPS
  deduplication, idempotent reuse, background queueing, and sanitized errors regress
  unchanged whether launched from Bitbucket Search or a reused Settings component.

### API and dashboard

- endpoint schema, `no-store`, loopback authorization, low query cost, and unavailable
  supervisor behavior;
- durable current-run/activity, total/per-repository ETA, rates, resource-budget,
  inventory-coverage, remaining/total, and repository-lifecycle fields;
- series ID/history metadata, typed samples, null availability reasons, and graph-line
  breaks across series resets;
- no secrets, content, repository URLs, or local paths in payloads/reasons;
- active/idle adaptive polling and hidden-document pause;
- current cards, both graph panels, state explanation, freshness, table fallback, keyboard
  access, screen-reader labels, and non-color cues;
- top bar shows total ETA, extracted/min, written/min, and concurrent-aware status in the
  required order; it does not show `Workers N running`, `Calculating workers`,
  `N PDFs extracting`, or the ambiguous queued-plus-running `N active` summary;
- top-bar indicator coverage includes the exact empty-inventory state from the supplied
  screenshot: zero repositories and no current run produce no icon, no focus target,
  no `aria-busy`, no active class, and no loaded/decoded animated asset; **Test
  connection** and **Add repository** remain usable;
- idle with an enabled repository shows only the static refresh action; idle with no
  refresh-eligible repository or endpoint shows the static unavailable state and reason;
  submission shows its spinner; queued-only work and staged `PUBLISHING` without a live
  writer show waiting; fresh owned Git, extraction, cache-attachment, and writer-only
  publication each show confirmed running; retry-wait, paused, terminal, and
  stale/unknown states do not show the running GIF;
- a poll start/failure/timeout and a back/forward-cache restore cannot select running,
  while the explicit freshness window may preserve a last-known active state only until
  it expires; newest run/sequence/generation wins in out-of-order active-to-terminal and
  terminal-to-stale response races;
- server-rendered markup and JavaScript hydration agree before first paint, leaving no
  idle-animation flash; exiting active state detaches the animated resource, and
  `prefers-reduced-motion` uses a static alternative rather than attempting to slow a GIF;
- JavaScript DOM fixtures include the static mark, submission spinner, waiting mark, and
  both formerly unconditional animated-image nodes/resource hooks so a simplified fixture
  cannot let the real regression pass; assert both state, resource attachment, accessible
  name, focusability, and one-time live-region announcements;
- revise legacy assertions that require an animated image on the empty page, treat queued
  Git/PDF work as an active animation, or only check that the active CSS selector exists;
  replace them with the full truth table rather than preserving the false-running behavior;
- queued repository uses the distinct waiting icon and **Added to queue** text without a
  premature remaining/ETA value; active cards show actual phase, **Remaining R of T
  PDFs**, and ETA state; completed cards show the green check and accessible completion
  text;
- waiting animation honors `prefers-reduced-motion`, and every queue/phase/completion
  state is understandable without motion or color;
- ETA calculating, waiting-for-inventory, available, paused, blocked, stale, complete,
  and unavailable states at desktop and narrow widths;
- enqueue work, close/navigate away from every OWL browser page, verify server-side
  extraction/publication continues, reopen, and verify the durable run is reconstructed;
- stopping OWL or simulating restart does not falsely claim background computation and
  resumes durable work on the next supported launch;
- narrow and desktop layouts;
- tuning history for recommended, applied, rolled-back, inconclusive, and safety events;
- empty, warming, idle, partial failure, stale, unavailable, active, source-blocked,
  backpressure, starved, and publication-limited visible states.

## Verification requirements

For each phase:

1. Map work to the stable feature-test and customer-journey IDs.
2. Run the smallest focused unit/integration/concurrency tests first.
3. Run Django checks, migration-drift checks, formatting, linting, JavaScript tests, and
   public-repository/safety checks.
4. Run the complete project gate using:

       PATH="$PWD/.venv/bin:$PATH" ./scripts/check.sh

5. Use an isolated data root and synthetic corpus for destructive/repeatable benchmark
   trials, and clean it up safely afterward.
6. Exercise Home and Repository Logs in the visible browser at desktop and narrow widths.
   Also exercise the Bitbucket top bar and repository cards. Independently observe added
   to queue, clone/pull/discovery, extraction, publication, simultaneous extracting and
   writing, remaining-of-total, ETA warming/available/paused/unavailable, green complete,
   completed-with-errors, active, idle, backpressure, source-blocked, publisher-starved,
   publication-limited, stale/unavailable, retry-wait, recovering, recovery-paused,
   popup, blocked-resume, successful-resume, tuning-event, and recovery journeys. Recreate
   the supplied zero-repository screenshot state and visually prove that no animation or
   dead icon appears beside **Test connection**; then prove idle-actionable,
   idle-unavailable, queued, running, staged-awaiting-writer, stopped, failed-poll,
   back/forward-cache, and reduced-motion transitions. API or test success does not
   replace screen-level proof. In the same visible pass, exercise the full Settings and
   compact gear/drawer journey in light and dark themes at desktop and narrow widths:
   section navigation, no-JavaScript fallback, Confluence connect/edit, host add and
   conflict removal, immediate custom-port credential selection, Add repository handoff,
   import/export, invalid-field focus, long safe host text, and externally managed/blank
   host-policy states. Record screenshots that contain synthetic values only.
7. Verify graph, rate, remaining/total, and ETA values against the underlying
   deterministic trace or process/job state. Complete replay runs to compare forecast
   with actual remaining duration.
8. Verify exact search and normal dashboard interaction remain within master-requirement
   targets while indexing. Record p50/p95 and the workload, not only a subjective result.
9. Run crash/restart/cancellation checks around extraction, staging, writer claim,
   transaction, semantic enqueue, recovery-threshold pause, notification, and resume
   boundaries. Use fake clocks/counters for threshold coverage; do not make a test sleep
   through 25 real backoff cycles.
10. Compare every performance change with the untouched baseline and state benchmark
    variance. Do not claim improvement from an unrepeated anecdotal run.
11. In a real browser journey, enqueue multiple repositories, close all portal tabs or
    windows, observe process/job progress independently, reopen, and verify state
    reconstruction. Separately stop/restart OWL and verify durable recovery without
    claiming that work ran while the process was stopped.
12. Record `PASS`, `FAIL`, `BLOCKED`, or `NOT RUN` for every selected ID. A missing real
    large corpus may mark full-scale performance evidence `BLOCKED`; it must not be
    silently replaced by the one-PDF database sample.
13. Stop immediately for suspected secret exposure, unapproved external access,
    destructive behavior, path escape, canonical data corruption, or data loss.

## Acceptance criteria

This scope is complete only when:

- the existing durable extraction/staging/publication model and all safety semantics are
  preserved;
- the Bitbucket Search top bar replaces worker counts with total `ETA ~HH:MM:SS` or a
  truthful named ETA state, **Extracted/min**, **Written/min**, and a concurrent-aware
  current status;
- the animated top-bar activity artwork appears only from fresh authoritative evidence
  of executing work: the zero-repository/no-run state has no dead or animated icon,
  actionable idle has a static refresh control, unavailable idle has a static reason,
  queued/staged-waiting work has its waiting icon, and submission, retry-wait, paused,
  terminal, stale/unknown, poll failure, and browser-cache restoration cannot masquerade
  as background execution; active artwork is detached when execution ends and has a
  static reduced-motion equivalent;
- no top-bar content, results summary, or repository-card summary says `Workers N running`,
  `Calculating workers`, `N PDFs extracting`, or combines queued/running work as merely
  `N active`;
- Home gives a compact truthful answer about ETA, flow, current activity, and pipeline
  health;
- the canonical full Settings page is titled truthfully, uses dedicated section
  navigation instead of the Bookmark Library sidebar, shows compact summaries before
  forms, exposes one focused task/primary action at a time, and keeps Confluence,
  Repository sources, Bookmark data, and resource-specific danger actions clearly
  separated at desktop and narrow widths; merely opening or navigating Settings is
  observation-only and cannot tick a schedule or start work;
- Settings accepts and durably approves a credential-free **Repository host URL** without
  `.env` editing or restart when policy is UI-manageable, clearly distinguishes it from a
  full clone URL, immediately offers the exact origin for supported credentials, and
  never claims host save alone verified repository access;
- one fail-closed effective-host service preserves built-in/external precedence, exact
  host/origin scoping, final outbound Git revalidation, provider-appropriate credential
  choices, secret blank-on-reload behavior, and dependency-safe non-cascading removal;
- every repository accepted into Refresh all is immediately acknowledged as **Added to
  queue**; each card still waiting uses a distinct queue icon, while a card already active
  advances to its newer state and, after inventory, shows the actual phase, **Remaining R
  of T PDFs**, and a truthful ETA state;
- the unqualified green completion tick appears only for the current final inventory with
  `successful = total` and zero permanent-failed, cancelled, remaining, unresolved,
  staged, and publishing counts, including a clean zero-PDF run; partial, paused,
  cancelled, stale, and Git-only outcomes cannot appear complete;
- Repository Logs shows coordinated capacity/state and flow/backlog history;
- a live worker is distinguishable from a free slot, source-blocked slot, throttled slot,
  and unavailable process;
- publisher starvation is distinguishable from no demand, publication saturation,
  repository blocking, and SQLite contention;
- the dashboard never calls a soft backpressure threshold a hard queue capacity;
- metrics include pages/bytes/characters and latency distributions, not only PDFs/sec;
- extracted/min counts only once at atomic durable staging, written/min only once after
  committed publication, and cache reuse is separate;
- total ETA covers all accepted current-run repositories through explicit terminal
  outcomes—durable publication/cache attachment for success and separately labelled
  permanent-failure/cancellation outcomes—accounts for phase/repository overlap and
  observed Git scheduling, never invents a number without sufficient evidence, and
  exposes confidence, coverage, basis, and freshness in details;
- ETA uses total-hour `HH:MM:SS`, may increase when inventory or conditions change, and
  has completed-run accuracy evidence rather than being derived from the old weighted
  progress display;
- high-frequency telemetry does not add material SQLite write load and has measured low
  overhead;
- every state and tuning event has stable evidence, confidence, and freshness;
- transient unexpected component failures, exits, or stalls retry automatically with
  bounded persisted backoff and no tight relaunch loop;
- the default component circuit pauses after exactly 25 consecutive failed recovery
  attempts while per-document failures retain a separate small retry budget;
- the smallest safe scope pauses without cancelling queued jobs, deleting valid staged
  output, repeating completed publication, or stopping healthy unrelated work;
- a durable unread notification and exactly one accessible in-app popup per pause generation
  explain the redacted reason and offer **Resume**, details, and dismiss;
- resume is secure, idempotent, generation-safe, blocked when hazards remain, and
  continues from the last durable boundary with honest handling of an unstaged in-flight
  PDF; it permits one half-open probe, reopens immediately on failure, and closes only
  after the stability gate;
- retry/pause/resume survives normal OWL restart, and the UI does not promise a live popup
  while the entire OWL process or machine is stopped;
- fixed-concurrency and relevant one-variable optimization benchmarks are reproducible;
- the 80-percent schedulable-CPU preference is implemented as a shared upper background
  slot budget, with roughly 20 percent foreground/OS headroom, a tested hard maximum,
  competing OWL work accounted for, and a visible reason whenever the effective PDF
  ceiling is lower;
- the controller never starts useless workers merely to hit 80 percent and any supported
  ceiling above eight has full correctness and foreground validation;
- shadow mode demonstrates stable, sensible, non-oscillating recommendations;
- adaptive mode either passes every gate and behaves within hard safety/foreground
  constraints, or remains disabled with the failed/blocked evidence stated clearly;
- manual fixed mode, immediate disable, conservative fallback, and rollback all work;
- exact and semantic search correctness, UI responsiveness, recovery, cancellation, and
  data integrity pass regression coverage;
- accepted work continues when every browser portal tab/window is closed and reconstructs
  correctly on reopen; documentation plainly says that stopping OWL or the laptop stops
  computation and durable work resumes on the next launch;
- Git repository-controller concurrency and clone/pull ordering remain unchanged by this
  scope and are measured rather than described as globally serial;
- authoritative requirements, customer journeys, configuration docs, and traceability are
  updated consistently.

## Non-goals

- Do not replace SQLite, add PostgreSQL, Redis, Celery, or an external queue as part of
  this scope.
- Do not replace durable staging with an in-memory queue.
- Do not add a SPA/React rewrite or a heavy chart dependency by default.
- Do not treat the Settings redesign as permission to remove Confluence configuration,
  Bookmark Export/Import, secure credential storage, the no-JavaScript path, or existing
  repository-add behavior.
- Do not combine repository-host approval, a full repository clone URL, and a credential
  into one ambiguous field or model. Do not write `.env`, auto-trust a host found in a
  pasted clone URL, contact the network merely when saving a host, accept wildcards, or
  store SSH private keys in OWL.
- Do not add a remote GIF, analytics service, or external ETA/monitoring dependency for
  the queue/card presentation.
- Do not change PDF extraction semantics, OCR/VSDX support, search ranking, exact-first
  behavior, or semantic content policy.
- Do not remove durability, locking, lease, cancellation, retry, or recovery safeguards.
- Do not retry one corrupt/permanent-failure PDF 25 times; the 25-attempt default belongs
  to component recovery episodes.
- Do not implement automatic pause by cancelling jobs or deleting valid staged output.
- Do not claim native OS push, email, SMS, or an out-of-process watchdog; this scope uses
  OWL's durable in-app notification and popup on the next available page/startup.
- Do not claim that browser JavaScript owns or sustains background execution. Closing the
  portal must be harmless, but stopping OWL or powering off the laptop necessarily stops
  computation until restart. Do not use a Settings page visit as an implicit scheduler
  tick or worker wake-up.
- Do not treat a status request, failed poll, back/forward-cache revalidation, configured
  or resident worker process, worker-capacity count, `hasActiveWork`, or queued-job count
  as proof that work is running. Do not solve the icon bug by renaming the idle GIF,
  dimming it, or swapping one always-loaded animated asset for another.
- Do not display a numeric ETA by dividing remaining PDFs by worker count, summing
  overlapping repository ETAs, or relabelling the existing blended progress percentage.
- Do not force ETA to decrease or show `00:00:00` for warming, paused, stale, blocked, or
  unknown-inventory work.
- Do not weaken SQLite synchronous durability automatically.
- Do not treat all reported CPUs as available to PDF extraction.
- Do not force host CPU or PDF workers to 80 percent when demand is absent, the publisher
  is limiting, the tested hard maximum is lower, or any safety/foreground guardrail says
  to use less.
- Do not change Git clone/pull concurrency or ordering as part of the PDF adaptive work.
- Do not store per-second time-series rows in the main SQLite database.
- Do not claim multi-repository fairness, adaptive tuning, hard queue capacity, or
  performance improvement without direct validation.
- Do not expose a public monitoring endpoint or external telemetry service.

## Completion response

Lead with what is working and the active controller mode. Then report:

- completed phases and major files/migrations;
- the preserved architecture and any deliberately changed scheduling behavior;
- Settings information architecture, section/deep-link behavior, compact-drawer decision,
  responsive/accessibility proof, and preserved Confluence/import/export journeys;
- repository-host model/migration, canonicalization, effective-policy precedence,
  final-outbound revalidation, host-versus-clone workflow, credential scoping, dependency-
  safe removal, and synthetic visible/security evidence;
- exact metric definitions, retention, overhead, endpoint schema, and unavailable-state
  handling;
- top-bar total ETA/rates/activity, repository queue/active/complete lifecycle, Home, and
  Repository Logs behavior, with visible-validation evidence;
- top-bar icon-state evidence for empty, actionable-idle, unavailable-idle, submitting,
  queued/staged-waiting, confirmed running, retry-wait, recovering, paused, terminal,
  stale/unknown, failed-poll, back/forward-cache, and reduced-motion states, including
  proof that inactive GIF resources are not attached;
- ETA algorithm, inventory coverage, confidence/range, warm-up/unavailable rules,
  completed-run accuracy/bias, and exact extracted-versus-written counter boundaries;
- background browser-close/reopen and OWL stop/restart evidence, with the lifecycle
  limitation stated plainly;
- fixed-concurrency and one-variable benchmark method, results, variance, and raw report
  location outside Git;
- classifier trace results and shadow/adaptive decision quality;
- recovery scopes, retry classifications, per-document versus component budgets,
  threshold/backoff/stability defaults, and pause/resume durability evidence;
- popup/notification deduplication, acknowledgement, blocked-resume, successful-resume,
  and accessibility evidence;
- every tuning default, 80-percent CPU-derived budget input, tested hard bound, competing
  OWL-work accounting, effective limiting reason, resource reserve, kill switch,
  fallback, and rollback;
- confirmation that Git concurrency/order was measured and left unchanged;
- selected journey/test IDs with `PASS`, `FAIL`, `BLOCKED`, or `NOT RUN`;
- commands run, cleanup performed, and confirmation that no private data or generated
  benchmark artifacts are tracked;
- requirements not completed, failed adaptive gates, known limitations, and the smallest
  safe next action.

Do not call the work adaptive or complete merely because graphs render or a worker target
changes. Completion requires truthful measurement, representative evidence, preserved
correctness, bounded behavior, and visible proof.
