# PDF pipeline benchmark record

This file records reproducible engineering evidence for work prompt 011. Generated
PDFs, databases, and full JSON reports remain under ignored `var/benchmarks`; no
private repository content or canonical OWL database is used.

## Phase 4 calibration experiments

These are small calibration runs, not the representative 20,000-25,000 PDF / roughly
50 GB benchmark gate. Unless stated otherwise, runs used 32 generated PDFs, four
repositories, three pages per PDF, four fixed extractors, a per-repository target of
one, three repetitions, rollback-journal SQLite with `synchronous=FULL`, semantic
search disabled, and metrics sampling disabled. Each trial used a fresh data root and
database.

| Experiment | Report | Result and decision |
|---|---|---|
| Work-conserving locality | `20260903T203907Z-e0863f3941da` | 1,118.52 persisted docs/min median, CV 3.97%. Keep locality-preferred spillover. |
| Strict locality rollback | `20260903T203920Z-55733996df25` | 463.08 docs/min, CV 1.95%, with repository p95 wait near 3.6 s. Do not use strict locality by default. |
| Repeat child pre-hash | `20260903T203937Z-5b6c5765e635` | 1,142.68 docs/min, CV 2.47%. This tiny corpus cannot demonstrate the large-file I/O gain; keep parent-fingerprint handoff because it removes one full read while mutation tests preserve the child post-parse full-fingerprint boundary. The independent rollback remains available. |
| Publication batch 50 | `20260903T203948Z-b57b875e1795` | 1,128.48 docs/min, CV 1.57%. No meaningful gain. |
| Publication batch 250 | `20260903T204003Z-236f408a34a7` | 1,091.35 docs/min, CV 1.35%. No meaningful gain. Keep the existing batch size of 100. |
| WAL calibration | `20260903T204013Z-52b80e1e2ff6` | 1,155.52 docs/min, CV 1.44%. The small trial had no meaningful concurrent-reader or checkpoint load, so it does not justify changing the durable rollback-journal/`FULL` default. |
| Duplicate page index | `20260903T204624Z-77314c54b506` | 921.63 docs/min, CV 1.50%; exact-search p50 1.25-1.30 ms, page-lookup p50 0.083-0.084 ms, 100% exact-search availability. |
| Duplicate page index removed | `20260903T204634Z-aa915c4386d1` | 936.61 docs/min, CV 1.10%; exact-search p50 1.20-1.25 ms, page-lookup p50 0.084-0.088 ms, 100% exact-search availability. SQLite selected the identical unique-constraint auto-index. Keep migration 0020; the observed differences are within small-run variance and show no regression. |

The publisher now performs a read-before-write-reservation check and exponentially
backs off only while idle. Focused tests prove an empty loop does not reserve the
SQLite writer and that new work resets the delay. SQLite timing is stored in bounded,
private, per-process snapshots so parallel controllers cannot clobber the publisher's
samples. A four-document verification run (`20260903T204339Z-38c15ce3e234`) recorded
all four publication transactions, 12 writer-reservation samples, and zero busy
errors.

The benchmark reports the actual busy timeout, journal and synchronous modes,
connection setup, transaction and lock-wait timing, FTS trigger count, page-index
inventory and query plan. Process-boundary tests cover closing inherited Django
connections before parser, resident-controller, repository-sync, and bookmark-refresh
process launches. The isolated parser remains database-free.

## Reproduction

Run a fixed calibration without retaining trial databases:

```bash
PATH="$PWD/.venv/bin:$PATH" python manage.py bitbucket_pdf_pipeline_benchmark \
  --workers 4 --repetitions 3 --documents 32 --repositories 4 \
  --pages-per-document 3 --per-repository-workers 1 \
  --publication-page-batch-size 100
```

Useful one-variable controls are `--strict-repository-locality`,
`--repeat-child-prehash`, `--sqlite-journal-mode wal`,
`--publication-page-batch-size N`, `--metrics-sampling`, and the disposable
`--with-duplicate-page-index` comparison switch. `--full-matrix` selects fixed
targets 1, 2, 4, 6, and 8.

The representative benchmark gate has not passed at this point. Adaptive admission
must remain disabled until the Phase 5 workload, guardrail, foreground-concurrency,
recovery, semantic, and ETA criteria are satisfied.

## Phase 5 fixed-concurrency characterization

Report `20260903T205858Z-ecb34ac67997` ran the complete fixed 1/2/4/6/8 matrix
with three repetitions, 64 generated PDFs, eight repositories, three pages per
PDF, locality-preferred work conservation, at most two parsers per repository,
and concurrent production exact-search/dashboard-payload probes. The host exposed
18 schedulable CPUs and the trials used Python 3.14.5 on arm64 macOS. All 15 trials
passed their terminal-job and persisted-document integrity checks.

| Fixed extractors | Median persisted docs/min | CV | Median duration |
|---:|---:|---:|---:|
| 1 | 474.58 | 1.07% | 8.091 s |
| 2 | 838.32 | 0.60% | 4.581 s |
| 4 | 1,292.26 | 1.42% | 2.972 s |
| 6 | 1,383.96 | 2.50% | 2.775 s |
| 8 | 1,622.02 | 1.71% | 2.367 s |

Eight was about 17.2% faster than six in this small CPU-heavy calibration and its
gain exceeded the observed run variance. This does **not** authorize a target above
eight: the representative 50-GB workload, normal semantic concurrency, cold/warm
cache control, controlled recovery/failure load, and adaptive comparison have not
passed. The practical calibrated fixed range is therefore 6-8, bounded by the
existing tested hard maximum of eight.

Across the matrix, concurrent exact search and dashboard probes had 100% request
availability. Exact-search p95 ranged from about 3.4 to 22.0 ms and dashboard-payload
p95 from about 42.0 to 63.7 ms. SQLite reported zero busy errors; lock-wait p95
remained below 0.30 ms. Maximum staged depth was five jobs. The eight-worker trials
peaked near 47.5% host CPU, 33.4% measured OWL process-tree CPU, and about 1.08 GB
process-tree RSS while reported free memory stayed above 17 GB. Unsupported thermal
state remains explicitly unavailable.

The calibration ETA is not ready for a product gate. Total ETA median absolute
percentage error was roughly 49-56% in the eight-worker trials, mainly because the
sub-three-second workload provides only six or seven post-warm-up checkpoints. Longer
representative runs are required to calibrate the production estimator by workload
class.

Metrics-overhead report `20260903T210026Z-67bc18d51bf2` repeated the four-worker
case with bounded metrics sampling enabled. Its median was 1,272.63 docs/min versus
1,292.26 in the matched disabled matrix, a 1.52% difference within the observed
combined variance; all three trials recorded one metrics sample without errors and
foreground availability stayed 100%.

The harness accepts `--source-padding-bytes-per-document` (up to 10 MB) so an
explicitly scheduled disposable representative run can reach the required source-byte
scale without committing material. `--include-failure-fixtures` adds blank, encrypted,
and malformed cases. Smoke report `20260903T205838Z-a6e35af473f0` proved the mixed
fixture run terminally reconciles expected failures. Timeout/recovery injection and
normal semantic-model concurrency still need a separately controlled representative
run; the harness truthfully lists these limitations.

### Gate decision

The adaptive benchmark gate is **not passed**. No adaptive-enablement manifest is
created, targets above eight are not tested, and adaptive mode remains unable to take
control. Observe/shadow behavior and the conservative fixed default are the releasable
outcome until representative evidence satisfies every gate check.
