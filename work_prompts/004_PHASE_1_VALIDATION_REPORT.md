# 004 — Phase 1 validation report

Date: 2026-08-25
Work prompt: `001_OWL_MASTER_REQUIREMENTS.md`
Delivery phase: Phase 1 — Foundation and public-repository safety
Result: **PASS for the defined Phase 1 scope**

## 1. Delivered scope

Phase 1 now provides:

- a Django 6.1 project on Python 3.14 with `core`, `bookmark_manager`, and
  `bitbucket_search` applications;
- a shared responsive OWL shell, primary navigation, permanent accessible status bar,
  dashboard, Bookmark Manager foundation, PDF Search foundation, repository/index
  placeholders, and System Status;
- a persistent, keyboard-focusable **Confluence settings** gear on Bookmark Manager;
- an honest Phase 2 settings foundation that does not display a PAT field, store a
  credential, or contact Confluence before the secure workflow exists;
- validated local data, database, log, media, backup, import, repository, index, and
  secret locations beneath the ignored `var/` runtime root;
- an injectable secret-store boundary with operating-system keyring and synthetic
  in-memory test implementations;
- non-secret Confluence configuration storage and a first migration;
- sanitized exception handling, logging, status reporting, and diagnostics;
- a staged-index-aware public-repository scanner that rejects likely credentials,
  private endpoints, runtime data, PDFs, exports, screenshots, databases, keys, and
  temporary extraction artifacts without printing suspected values;
- exact direct and transitive dependency locking through `pyproject.toml` and the
  universal `uv.lock`;
- synthetic CI, formatting, linting, system checks, migration checks, test coverage,
  setup instructions, and a one-command local quality gate.

## 2. Automated validation evidence

Command executed:

```text
./scripts/check.sh
```

Final result:

| Check | Result |
|---|---|
| Universal dependency lock | PASS — 27 packages resolved from the unchanged lock |
| Locked environment synchronization | PASS |
| Public-repository safety scan | PASS — 70 indexed or untracked, non-ignored candidates |
| Formatting | PASS — 50 files already formatted |
| Ruff code quality | PASS |
| Django system checks | PASS — no issues |
| Migration drift | PASS — no changes detected |
| Synthetic automated tests | PASS — 85 passed |
| Branch-aware coverage | PASS — 83.0% total |
| Existing local migration application | PASS — no migrations pending |
| Git whitespace validation | PASS |

The tests ran with the dedicated test settings, an isolated temporary data root,
blank external connection settings, and the in-memory secret store. They did not make
a live Confluence or Bitbucket request.

## 3. Visible customer-interface validation

The running local site was reviewed in the in-app browser at a normal desktop size and
at a 390 by 844 phone-sized viewport.

| Checkpoint | Result |
|---|---|
| Dashboard and primary navigation render | PASS |
| Bookmark Manager first-use foundation is clear | PASS |
| **Confluence settings** gear is visible, enabled, named, and keyboard-focusable | PASS |
| Gear opens the dedicated secure-settings foundation | PASS |
| Settings foundation makes Phase 2 status explicit and contains no PAT field | PASS |
| Permanent status region is visible | PASS |
| System Status reports database, FTS5, data-root, credential-store, Confluence, and worker states | PASS |
| Local Bootstrap stylesheet loads without a CDN | PASS |
| Desktop and phone-sized pages avoid horizontal overflow | PASS |
| PDF Search correctly identifies itself as a later Phase 7 feature | PASS |
| Browser console warning/error check | PASS — none observed |

No screenshot was saved into the public repository. The safety policy intentionally
rejects screenshot artifacts from public source.

## 4. Phase 1 journey and feature-ID traceability

`PASS` below means the complete Phase 1 portion was exercised. `PARTIAL` means the
foundation was exercised but the stable ID also contains behavior assigned to a later
phase. `NOT RUN` means the later-phase journey was intentionally not simulated.

| Journey or test ID | Status | Phase 1 evidence and remaining work |
|---|---|---|
| CJ-001 | PARTIAL | First-use explanation and settings entry point pass. Actual PAT entry, connection test, save, replace, remove, and restart belong to Phase 2 and were not run. |
| CJ-016 | PARTIAL | Locked setup, checks, migration, local start, and loopback smoke pass. Full cross-feature clean-machine release journey belongs to the completed product and was not run. |
| CFG-001 | PARTIAL | Gear visibility, exact accessible name, enabled state, and keyboard focusability pass. Phase 2 drawer Escape/focus-return behavior was not run. |
| CFG-002 | PASS for foundation | No-configuration first-use state is clear and local pages remain available without a connection. |
| CFG-012 | PARTIAL | Complete/incomplete environment-profile selection and precedence are covered by unit tests. The completed browser configuration workflow was not run. |
| CFG-013 | PARTIAL | Secure-store unavailable/failure behavior and the no-plaintext-fallback boundary are covered with fakes. The Phase 2 recovery UI was not run. |
| CFG-014 | PARTIAL | Targeted redaction, log, debug-response, scanner, and configuration-boundary tests pass. The full per-run credential canary across every later export, backup, screenshot, trace, database, and browser surface was not run. |
| OPS-003 | PASS for foundation | Automated and visible checks confirm accurate sanitized Phase 1 System Status output. Later worker/repository/index health behavior remains for its implementation phase. |
| SEC-001 | PASS for foundation | Ignore rules plus staged-index and untracked-file scanning cover credentials and runtime/private artifacts, including temporary extraction files. |
| SEC-003 | PASS | Loopback-only defaults and non-loopback rejection are covered by tests and local start verification. |
| SEC-004 | PARTIAL | The public CI definition uses only synthetic/blank external configuration and a temporary data root. Hosted GitHub Actions was not run because this work was not pushed. |
| OPS-004 | PARTIAL | Documented locked setup, migration, quality gate, and local first-use start pass on this Mac. A separate pristine-machine run was not performed. |

All CFG IDs not listed above, all Bookmark Manager behavior IDs, and all repository,
indexing, PDF, global-search, long-job, backup/restore, performance, and complete-product
release journeys are **NOT RUN by design** because their product behavior is assigned to
Phases 2 through 8.

## 5. Security and privacy statement

- No real PAT, internal Confluence URL, Bitbucket credential, private repository URL,
  PDF, bookmark export, screenshot, database, index, or customer document was added.
- No live external service was contacted by the automated or visible validation.
- Secret-store tests used invented synthetic values and never returned a stored secret
  to a template or response.
- The command-line status output reports component health without printing the local
  data-root path or any configuration value.
- No commit or push was made as part of this implementation.

## 6. Deliberate Phase 1 limitations

The configuration gear is present now, but the real PAT form, HTTPS-origin validation,
explicit read-only connection test, secure save/replace/remove workflow, and detailed
connection states are Phase 2 work. The current settings page states this clearly and
does not pretend that a connection exists.

Bookmark creation, the Confluence tree, repository synchronization, PDF extraction and
search, durable jobs, backups, global search, and full release/performance acceptance are
also not part of Phase 1.

## 7. Next implementation phase

Proceed with **Phase 2 — Bookmark Manager core** from work prompt 001. Begin with the
Confluence adapter and the complete secure settings workflow behind the existing gear,
using only the fake secret store and synthetic Confluence responses until the user
deliberately supplies a real PAT through the finished local interface.
