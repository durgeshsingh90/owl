# PDF pipeline operations

This guide is the operator contract for the adaptive PDF pipeline introduced by work
prompt 011. It describes the shipped observe-first behavior, not a promise that automatic
tuning is enabled. The stable product requirements are `PIPE-001` through `PIPE-012` in
`work_prompts/001_OWL_MASTER_REQUIREMENTS.md`; the matching acceptance tests are
`PIPE-T01` through `PIPE-T12` in
`work_prompts/002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md`.

## Release state and safe default

OWL still uses isolated parser processes, durable staging files, one controlled
SQLite/FTS publisher, and searches the last committed index. The new default controller
mode is `observe`. It measures and explains capacity but does not change admission.

The representative adaptive benchmark gate has **not passed**. In particular, the small
calibration workload cannot validate the ETA accuracy, normal semantic concurrency,
large source-byte workload, or adaptive-versus-fixed behavior required by the gate. No
adaptive-enablement manifest is shipped. Keep `PDF_PIPELINE_ADAPTIVE_ENABLED=false` and
use `observe`, `shadow`, or a validated fixed target. See
`PDF_PIPELINE_BENCHMARKS.md` for the recorded method, report IDs, limitations, and
measurements.

## Process and durability model

`python3 start.py` is the supported launcher. It applies database migrations when needed
and starts `run_owl`, which owns the web server, scheduler, repository workers, semantic
workers, PDF controller, isolated extractors, and publisher. Only one supervisor may own
that pool for an OWL data root.

An accepted pipeline run and its repository membership are durable server records.
Closing a browser tab, navigating away, hiding the page, or stopping browser polling does
not stop accepted work. Reopening a page discards client-only state and renders the newest
durable run snapshot.

Stopping `run_owl`, shutting down the laptop, or losing power stops computation. OWL does
not claim otherwise. On the next launch it reconciles interrupted leases, valid staging
files, completed publications, recovery circuits, and queued jobs, then continues from
the last durable boundary. An already published PDF is not repeated; a validated staged
result resumes at publication; an extraction interrupted before staging restarts that
one PDF from the beginning because no page-level parser checkpoint exists.

## Controller configuration

Copy `.env.example` to an ignored local `.env` and restart all OWL processes after a
change. Never put credentials or private paths in the tracked example.

| Purpose | Setting | Safe default and meaning |
|---|---|---|
| Mode | `PDF_PIPELINE_CONTROLLER_MODE` | `observe`; accepts `fixed`, `observe`, `shadow`, or `adaptive`. |
| Explicit adaptive opt-in | `PDF_PIPELINE_ADAPTIVE_ENABLED` | `false`; it is necessary but not sufficient for adaptive control. |
| Immediate disable | `PDF_PIPELINE_CONTROLLER_KILL_SWITCH` | `false`; `true` restores conservative fixed admission. |
| Manual target | `PDF_PIPELINE_MANUAL_FIXED_TARGET` | Empty; when set, it overrides controller recommendations within configured/tested bounds. |
| Configured worker ceiling | `PDF_MAX_EXTRACTION_WORKERS` | `4`; legacy fixed target and hard configured limit, capped at the tested maximum of 8. |
| Minimum target | `PDF_PIPELINE_CONFIGURED_MIN_TARGET` | `1`; safety/recovery may still reduce effective admission to zero. |
| Initial/fixed target | `PDF_PIPELINE_INITIAL_TARGET` | Defaults to `PDF_MAX_EXTRACTION_WORKERS`. |
| Tested hard maximum | `PDF_PIPELINE_TESTED_HARD_MAX` | `8`; values above 8 are unsupported by this release. |
| Shared CPU preference | `PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION` | `0.80`; a ceiling after competing OWL reservations, never a utilization target. |
| Gate manifest | `PDF_PIPELINE_ADAPTIVE_BENCHMARK_GATE_PATH` | Private path under the data root by default; adaptive refuses missing, incompatible, incomplete, or failing evidence. |
| Observation/cooldown | `PDF_PIPELINE_CONTROLLER_OBSERVATION_SECONDS`, `PDF_PIPELINE_CONTROLLER_COOLDOWN_SECONDS` | `60`, `120`; prevent reactions to short samples and oscillation. |
| Hysteresis | `PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES` | `3` consecutive matching proposals. |
| Minimum evidence | `PDF_PIPELINE_CONTROLLER_MIN_DOCUMENTS`, `PDF_PIPELINE_CONTROLLER_MIN_PAGES`, `PDF_PIPELINE_CONTROLLER_MIN_BYTES` | `3`, `10`, `1048576`; insufficient evidence holds the target. |
| Ordinary decrease bound | `PDF_PIPELINE_CONTROLLER_MAX_ORDINARY_DECREASE` | `2`; emergencies and recovery may clamp further. |
| Improvement threshold | `PDF_PIPELINE_CONTROLLER_MIN_THROUGHPUT_IMPROVEMENT` | `0.05`; avoids acting on noise. |
| Guardrails | `PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT`, `PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES`, `PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES`, `PDF_PIPELINE_CONTROLLER_MAX_FOREGROUND_P95_MS` | `85`, 8 GiB, 10 GiB, and 500 ms. Missing required signals fail conservatively. |

Mode transitions always occur at job-admission boundaries. They do not terminate a PDF
already parsing:

- `fixed` uses the manual target when present, otherwise the initial/configured target.
- `observe` records the same metrics but never recommends or changes a target.
- `shadow` records bounded hypothetical decisions, evidence, expected effect, cooldown,
  and later outcome without applying them.
- `adaptive` may apply a target only when the mode, explicit opt-in, compatible passing
  manifest, resource signals, safety constraints, and recovery state all allow it.
- The kill switch, a manual fixed target, a stale/unavailable required signal, a failed
  gate, or a recovery hazard returns admission to the conservative fixed/safety result.

`requested`, configured maximum, tested maximum, resource-aware ceiling, safety ceiling,
and `effective` target are intentionally separate values. The UI includes the limiting
reason whenever the effective target is lower. OWL never starts idle workers merely to
reach the 80-percent CPU preference.

## Metrics, rates, and state meanings

The local metrics endpoint is versioned, loopback-authorized, redacted, and sent with
`Cache-Control: no-store`. The owner samples every 5 seconds by default, retains 30
minutes in a bounded memory ring, and may publish a restrictive atomic snapshot below
the data root. Browser pages poll around every 5 seconds while active, around every 30
seconds while idle, and stop polling while hidden.

These controls are available in `.env.example`:

- `PDF_PIPELINE_METRICS_ENABLED`, `PDF_PIPELINE_METRICS_SAMPLE_SECONDS`,
  `PDF_PIPELINE_METRICS_RETENTION_SECONDS`, and
  `PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED` control sampling and private snapshots.
- `PDF_PIPELINE_METRICS_STALE_SECONDS=15` is the freshness boundary. A stale snapshot
  cannot prove running work or support a live ETA.
- `PDF_PIPELINE_RATE_WINDOW_SECONDS=60`,
  `PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS=30`, and
  `PDF_PIPELINE_RATE_MIN_EVENTS=3` define the common rate window.
- `PDF_PIPELINE_SQLITE_LOCK_BLOCKED_MS=250` classifies meaningful publisher lock
  contention.

`Extracted/min` counts PDFs only after the validated staging file has been atomically
handed off and the durable job transition commits. `Written/min` counts PDFs only after
the publication transaction commits. Cache reuse is separate and can explain why total
completion exceeds publication rate. Pages, bytes, characters, queue depth, staged bytes,
oldest staged age, transaction timing, lock wait, failures, retries, and resource gauges
retain their own units and definitions.

The first 30 seconds of a 60-second window is `warming`. From 30 through 59 seconds, a
rate is shown only with at least three events and is marked as a partial window. Once the
window is complete it reports even zero, one, or two events, with low confidence where
appropriate. `0` is therefore different from unavailable.

State labels are evidence classifications:

- `idle` means fresh evidence shows no durable demand.
- `waiting` or `starved` means capacity is available but no eligible extractor or
  publisher input is currently available.
- `backpressured` or `publication limited` means durable staged work is constraining
  extraction or publication is the current bottleneck.
- `blocked` means a concrete constraint such as SQLite lock contention prevents progress.
- `retry wait`, `recovering`, and `paused` are recovery states, not ordinary idle states.
- `degraded` means some required capacity or evidence is unavailable while work remains.
- `unknown` or `unavailable` means the authoritative signal is missing, invalid, or stale;
  OWL does not substitute zero or infer that work is running.

High-frequency samples stay outside SQLite. Durable lifecycle, tuning, recovery, and ETA
accuracy records use bounded, low-frequency database writes.

## ETA behavior and accuracy

ETA uses the end-to-end overlapping extraction/publication critical path, current durable
inventory, remaining weighted work, recent rates, constraints, and coverage. It does not
divide by worker count or sum independent repository estimates. It is displayed as total
hours `HH:MM:SS`, may rise when inventory or conditions change, and includes confidence,
range, basis, freshness, and inventory coverage.

`PDF_PIPELINE_ETA_MIN_COMPLETIONS=3` and
`PDF_PIPELINE_ETA_STALE_SECONDS=30` bound when recent throughput may support an estimate.
Before enough evidence, the UI says it is calculating/warming; on pause it says ETA is
paused; stale or missing evidence makes ETA unavailable. Terminal success has zero
remaining time, while cancellation has no completion estimate.

Completed-run forecast checkpoints are retained for calibration. The current small
calibration produced roughly 49-56 percent median absolute percentage error at eight
workers, so ETA is useful as a qualified operational estimate but does not pass the
adaptive release gate. A longer representative workload is required before enabling
automatic tuning.

## Queue, repository-card, and page semantics

The acceptance response acknowledges each accepted repository as queued. A fast worker
may move it directly to a newer active state before the next render. Cards always prefer
the newest authoritative state; client acknowledgement cannot hide newer server evidence.

- `queued` means accepted durably but no eligible repository phase is confirmed active.
- Active labels name the confirmed phase: checking connection, cloning/pulling,
  discovering/cataloguing, extracting, publishing, or semantic preparation.
- Completion is green only when the current accepted generation meets its terminal
  contract. Git-only success, stale revision, partial failure, pause, cancellation, and
  historical failure use distinct non-green states.
- A repository excluded from a run is never shown as queued for that run.
- The top bar, repository cards, Home, and Repository Logs all consume the same run and
  snapshot identity. Animated activity artwork is loaded and animated only for fresh,
  confirmed active evidence and respects reduced-motion preferences.

Repository Logs is the detailed operations surface: current values, capacity/state and
flow/backlog timelines, ETA basis and forecast accuracy, fairness, resource warnings,
recovery state/history, and tuning history. Home intentionally stays compact.

## Recovery and Resume

Recovery is enabled by `PDF_PIPELINE_RECOVERY_ENABLED=true`. The default circuit pauses
the smallest safe component scope after exactly 25 consecutive failed recovery probes.
`PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS` normally belongs in the 20-30 range. Smaller
values are useful only for controlled testing. A value of `0` means **no automatic
component relaunch**: pause and notify on the first detected retryable unexpected failure,
exit, or stall. It never means unlimited retries or disabled reporting.

The component threshold is independent of
`PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES`, which remains the small per-document attempt
budget. Recovery backoff defaults to 1 second, caps at 300 seconds, and adds bounded
20-percent jitter. Equivalent extraction-slot failures within 10 seconds are correlated;
two affected slots escalate to one extraction-pool incident. A resumed circuit admits
one half-open probe and closes only after 60 seconds of stable heartbeat/progress.

A pause preserves queued jobs, committed index data, and valid staging. It publishes a
durable unread notification and one accessible popup per pause generation. `Resume` is a
CSRF-protected, generation-bound, idempotent local action available from the popup,
Background status, and pipeline details. It refuses unresolved hazards and never resets
the audit history or grants a fresh per-document retry budget.

## Settings and repository trust

The canonical page is **Settings**, reached from the Bookmark Manager gear or
`/bookmarks/settings/`. Its server-addressable sections are Overview, Confluence,
Repository sources, and Bookmark data. A GET, section change, client initialization, or
notification/status poll is observation-only. Only an explicitly labelled action may
test a connection, change trust, save a credential, import, or enqueue work.

Repository trust deliberately separates three concepts:

1. A repository host is a credential-free exact HTTPS origin such as
   `https://scm.company.example:8443`. Saving it approves local policy only; it does not
   contact the network, prove access, or add a repository.
2. An HTTPS credential is scoped to the exact scheme, host, and effective port. Its
   secret stays in the operating-system credential store or encrypted local database and
   never returns to the page after save. SSH continues to use the OS SSH agent.
3. A clone URL is a concrete SSH or HTTPS repository URL entered in Bitbucket Search.
   Only a bounded read-only `git ls-remote` for that URL can verify repository access;
   successful validation may then queue the repository.

When `BITBUCKET_ALLOWED_HOSTS` is unset, built-in defaults and durable UI approvals form
the effective policy. When it is explicitly populated, the environment list is
authoritative and Settings becomes read-only for host policy. An explicitly blank value
is deny-all. UI requests never rewrite the environment or `.env`. Host removal is blocked
while repositories, active jobs, or credentials depend on it and never cascades deletion
of repository/index data.

## Git scheduling remains unchanged

This scope does not alter repository-controller concurrency, clone/pull ordering, daily
scheduling, retry policy, checkout locks, or the rule that OWL performs read-only Git
operations. The configured repository worker pool remains independent of PDF extraction.
Git can feed the durable PDF queue while parser admission is measured or constrained; a
PDF controller target never changes `BITBUCKET_MAX_REPO_WORKERS`.

## Troubleshooting

1. Open **Repository Logs** and check the current state explanation, snapshot freshness,
   limiting reason, queue depths, SQLite timing, recovery episode, and redacted logs.
2. If status is unknown/degraded, confirm one supported `run_owl` supervisor owns this
   data root and restart it with `python3 start.py`. Do not start a second worker pool.
3. If ETA is warming, allow at least 30 seconds and three completions. If stale, confirm
   the supervisor metrics owner is alive; do not treat the last number as current.
4. If the publisher is blocked, close duplicate OWL instances and inspect SQLite lock
   timing and disk headroom. Valid staged files should remain in place.
5. If recovery is paused, correct the displayed hazard, then use the generation-bound
   **Resume** action. Repeated Resume clicks are safe; an obsolete generation is rejected.
6. If a host cannot be added, check whether environment policy is authoritative. Approve
   an origin in Settings first, save an exact-origin credential if HTTPS needs one, then
   add the full clone URL in Bitbucket Search.
7. Use `python manage.py check`, `python manage.py showmigrations`, and the isolated
   benchmark command from `PDF_PIPELINE_BENCHMARKS.md` for local diagnosis. Benchmark
   data belongs only under the ignored data root.

## Rollback

For an immediate operational rollback, set
`PDF_PIPELINE_CONTROLLER_KILL_SWITCH=true`, clear
`PDF_PIPELINE_MANUAL_FIXED_TARGET`, keep adaptive opt-in false, and restart OWL. This
restores the conservative fixed target while retaining metrics and durable work. To
observe without any recommendations, use `PDF_PIPELINE_CONTROLLER_MODE=observe`; to stop
pipeline telemetry sampling and private snapshots as well, set
`PDF_PIPELINE_METRICS_ENABLED=false` after collecting diagnostic evidence.

Do not delete the database, pipeline state directory, staging files, or migration rows to
roll back concurrency. Those contain the durable boundary needed for safe recovery.
Removing the adaptive gate file merely makes adaptive enablement fail closed. Restore a
prior application version only with its schema-compatible migration plan and a verified
database backup; then restart all resident processes together.

## Traceability and release recommendation

Implementation and regression coverage map to `PIPE-001`–`PIPE-012` and
`PIPE-T01`–`PIPE-T12`. The reproducible benchmark record maps the throughput, SQLite,
foreground, resource, failure-fixture, and telemetry-overhead evidence to `PIPE-T11`.

Release the durable observability, dashboard, trusted-host Settings workflow, recovery,
low-risk efficiency changes, fixed-target characterization, and shadow controller in
observe-first mode. Treat adaptive admission as **BLOCKED**, not passed, until a scheduled
representative workload satisfies every documented gate, including ETA calibration and
adaptive-versus-fixed comparison.
